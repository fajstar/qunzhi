# -*- coding: utf-8 -*-
"""
群智 V15 · 引用与参考文献模块
============================
职责（四项，均为纯本地计算，不调用任何模型）：
  1. normalize_pool   —— 给信息池每条分配编号 P1..Pn，并保证结构化字段齐备
  2. render_injection —— 渲染带编号的注入片段（玩家知道自己引的是哪一篇）
  3. render_reference_list —— 由代码渲染参考文献表（**绝不让 LLM 生成，防编造文献**）
  4. check_citations  —— 三类标记：有出处 / 无出处断言 / 引用了不存在的编号（幻觉）

设计原则：凡涉及“事实性清单”的输出一律由代码生成；模型只负责在提示词里标注编号。
"""
import re
import sys

# 引用标记：[P3]
CITE_RE = re.compile(r"\[(P\d+)\]")

# ---------- 来源分类（V15 修复 2026-10-09）----------
# 事故复盘：博查网页检索会把自媒体/新闻混进信息池，而参考文献表**一视同仁全列**，
# 结果"今日头条｜大学生利用ai写论文"被当成参考文献展示——这是硬伤。
# 下面按"域名优先 → 结构信号 → 标题特征"三级判定，把学术文献与网络资料分开。
ACADEMIC_DOMAINS = (
    "kns.cnki.net", "cnki.net", "doi.org", "arxiv.org", "openalex.org", "crossref.org",
    "doaj.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "ncpssd.cn", "ncpssd.org",
    "chinaxiv.org", "nstl.gov.cn", "semanticscholar.org", "ssrn.com", "sciencedirect.com",
    "link.springer.com", "springer.com", "onlinelibrary.wiley.com", "ieeexplore.ieee.org",
    "dl.acm.org", "tandfonline.com", "journals.sagepub.com", "jstor.org", "journals.plos.org",
    "mdpi.com", "frontiersin.org", "nature.com", "science.org", "cell.com", "europepmc.org",
    "bioone.org", "academic.oup.com", "cambridge.org", "iopscience.iop.org", "aps.org",
)
WEB_DOMAINS = (
    "sohu.com", "toutiao.com", "163.com", "sina.com.cn", "sina.cn", "qq.com", "baidu.com",
    "zhihu.com", "weixin.qq.com", "mp.weixin.qq.com", "360doc.com", "jianshu.com", "csdn.net",
    "baijiahao.baidu.com", "ifeng.com", "thepaper.cn", "xinhuanet.com", "people.com.cn",
    "chinanews.com", "cnbeta.com.tw", "ithome.com", "douban.com", "bilibili.com", "weibo.com",
    "doc88.com", "docin.com", "book118.com", "wenku.baidu.com", "xiaohongshu.com", "zhuanlan",
    "yidianzixun", "kanzhun", "so.com", "sm.cn", "quark", "dy.163.com", "jianshu",
    # 高校新闻网/校报（.edu.cn 下但不是期刊）——P2 事故：xsb.jsnu.edu.cn 曾被判"来源未明"
    "xsb.", "xiaobao", "news.", "newsletter", "webplus", "page.htm",
)


def _domain_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def classify_source(rec):
    """返回 academic / web / unknown（三级判定：域名白名单 → 域名黑名单 → 结构+标题信号）"""
    url = (rec.get("url") or "").lower()
    dom = _domain_of(url)
    if rec.get("source_type") == "cnki_rag" or rec.get("doi"):
        return "academic"
    if any(d in dom for d in ACADEMIC_DOMAINS):
        return "academic"
    if any(d in dom for d in WEB_DOMAINS):
        return "web"
    # 结构信号：有作者或有被引数 → 倾向学术
    if (rec.get("authors") or "").strip() or (rec.get("cite") or "").strip():
        return "academic"
    title = rec.get("title") or ""
    # 标题特征：自媒体标题习惯带 "_栏目_栏目" 后缀、或"今日头条"等站点名
    if re.search(u"_[^_]{1,8}_[^_]{1,8}$", title) or any(
            k in title for k in (u"今日头条", u"搜狐", u"网易", u"百家号", u"知乎", u"公众号")):
        return "web"
    if (rec.get("source") or "").strip():
        return "academic"
    return "unknown"


def kind_label(kind):
    return {"academic": u"学术文献", "web": u"网络资料", "unknown": u"来源未明"}.get(kind, u"来源未明")


def tier_label(tier):
    """期刊分级标签（**首选** item 里的 journal_level 字段，含国际具体分区）。

    1 = CSSCI（2025-2026 名录）    2 = 北大核心（2023 年版）
    11 = CSSCI 扩展版              3 = 国际 Q1/Q2    4 = 国际 Q3/Q4
    5 = 国际期刊（SJR 未收录）      0 = 未识别
    """
    try:
        t = int(tier or 0)
    except Exception:
        t = 0
    return {1: u"CSSCI", 2: u"北大核心", 11: u"CSSCI扩展", 3: u"国际 Q1/Q2",
            4: u"国际 Q3/Q4", 5: u"国际期刊", 0: u"—"}.get(t, u"—")


# 表里不能出现裸竖线，否则破坏 Markdown 表格
def _cell(v, dash=u"—"):
    s = str(v or "").strip()
    return s.replace("|", u"／") if s else dash


def _pick(item, *keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", [], {}):
            return v
    return ""


def _parse_from_content(content):
    """旧数据把作者/来源/年份拍平进了 content 字符串；这里回捞，保证兼容。"""
    out = {}
    c = content or ""
    m = re.search(u"作者：([^；;|]{1,80})", c)
    if m:
        out["authors"] = m.group(1).strip()
    m = re.search(u"来源：([^；;|]{1,80})", c)
    if m:
        out["source"] = m.group(1).strip()
    m = re.search(u"发表时间：((?:19|20)\\d{2})", c) or re.search(u"((?:19|20)\\d{2})\\s*年", c)
    if m:
        out["year"] = m.group(1)
    m = re.search(u"被引[：: ]*(\\d+)", c)
    if m:
        out["cite"] = m.group(1)
    # 兜底：形如「李四；来源：教育研究；发表时间：2023…」——首段短于 20 字则视为作者
    if not out.get("authors"):
        head = re.split(u"[；;]", c)[0].strip()
        if 0 < len(head) <= 20 and u"：" not in head:
            out["authors"] = head
    return out


def _fallback_title(content):
    """无标题时，用元数据段之前的首段当标题（避免把「作者：/来源：」塞进标题）。"""
    first = re.split(u"[；;]", content or "")[0].strip()
    if u"：" in first:
        first = re.split(u"：", first)[0].strip() or (content or "")
    return (first or u"（无标题）")[:60]


# ---------- 期刊级别与归一（V15 2026-10-10，B1 单一收口）----------
# 动机（实证）：2026-10-10 真实会话里 8/8 条 DOI 为空、期刊名靠解析正文得到——
# 根因是净化环节只保留 title/url/content/provider，把提供商给出的结构化元数据丢了。
# 于是"级别"变成了对自由文本做正则解析的产物，换源就静默降级。
# 现在：① 结构化字段优先；② 级别查询统一走 rag/evidence_pool.lookup_journal；
#       ③ 无论哪条建池路径，落库前都经过 ensure_journal_fields。
_PROVIDER_NAMES = {"cnki", "crossref", "doaj", "pubmed", "semanticscholar", "openalex",
                   "arxiv", "ncpssd", "chinaxiv", "nstl", "rag", "multi", "scholarly",
                   "bocha", "web", "crawler", "openalex·"}
_JOURNAL_RE = re.compile(u"(?:来源|期刊|期刊/会议)[：:]\\s*([^；;]{2,80})")
_EVIDENCE_MOD = None


def _evidence():
    """懒加载 rag/evidence_pool（避免导入期耦合；两种部署路径都试）。"""
    global _EVIDENCE_MOD
    if _EVIDENCE_MOD is not None:
        return _EVIDENCE_MOD
    try:
        import evidence_pool as _e
    except Exception:
        import os as _os
        _rag = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "rag")
        if _rag not in sys.path:
            sys.path.insert(0, _rag)
        import evidence_pool as _e
    _EVIDENCE_MOD = _e
    return _EVIDENCE_MOD


def _norm_doi(doi):
    try:
        return _evidence().normalize_doi(doi)
    except Exception:
        return str(doi or "").strip().lower()


def _norm_title(title):
    try:
        return _evidence().norm_title(title)
    except Exception:
        return re.sub(r"[\s\W_]+", "", str(title or "").lower(), flags=re.UNICODE)


def resolve_journal(rec):
    """取这条记录的期刊名：**结构化字段优先**，最后才退回解析正文。"""
    for k in ("journal", "venue", "container_title", "publication"):
        v = str(rec.get(k) or "").strip()
        if v:
            return v
    s = str(rec.get("source") or "").strip()
    if s and s.lower() not in _PROVIDER_NAMES:
        return s
    m = _JOURNAL_RE.search(str(rec.get("content") or rec.get("summary") or ""))
    return m.group(1).strip() if m else ""


# ---- 文献类型判定（V16 2026-10-11，修 2）----
# 实证背景：2026-10-10 真实会话注入池 8 条里，**3 条的 source 是授予单位**
# （东北师范大学 / 内蒙古师范大学 / 上海师范大学）——它们是**学位论文**，
# 但被当成期刊名去查核心名录 → 查不到 → 一律标「非核心期刊」。
# 后果：用户看到"全是非核心期刊"，而其中 3/8 根本不是期刊，是标签错误。
# 判别依据：知网的加密 URL 不含库类型，所以用**来源名启发式**（授予单位以院校/研究院结尾）。
# 边界如实说明：会议论文集名、报纸名与期刊名在字面上无法可靠区分，本判定只管学位论文。
_DEGREE_SOURCE_RE = re.compile(
    u"(大学|学院|学校|高等专科学校|职业技术|研究院|研究所|科学院|党校|师范大学|师范学院)$")
_COLLEGE_HINT_RE = re.compile(u"(大学|学院|学校|研究院|研究所|科学院)")


def classify_doc_type(rec):
    """判定文献类型：thesis（学位论文）/ journal（期刊）/ conference / newspaper / unknown。

    **只做有把握的那一类**：来源名以院校/研究院结尾 → 学位论文。
    其余一律按期刊处理，但若显式带了 `type` 字段则尊重之。
    """
    t = str(rec.get("type") or rec.get("source_type") or "").strip()
    if t:
        tl = t.lower()
        if tl in ("thesis", "dissertation") or u"学位" in t or u"硕士" in t or u"博士" in t:
            return "thesis"
        if tl in ("journal", "article"):
            return "journal"
        if u"会议" in t or tl == "conference":
            return "conference"
        if u"报纸" in t or tl == "newspaper":
            return "newspaper"
    j = resolve_journal(rec)
    if not j:
        return "unknown"
    # 授予单位特征：以院校/研究院结尾，且**不是**期刊常见的结尾（学报/评论/研究/教育…）
    if _DEGREE_SOURCE_RE.search(j) and not re.search(u"(学报|评论|研究|教育|科学|杂志|期刊|导报|通报)$", j):
        return "thesis"
    if _COLLEGE_HINT_RE.search(j) and re.search(u"(学报|评论|研究|教育|科学|杂志|期刊)$", j):
        return "journal"
    return "journal"


def ensure_journal_fields(rec):
    """保证 type / journal / journal_tier / journal_level 四件套齐备（落库前必做，原地修改）。

    V16（修 2）：**先判类型再查名录**。学位论文的"来源"是授予单位，不是期刊，
    因此不进核心期刊名录，级别显示为「学位论文（非期刊）」——而不是误导性的「非核心期刊」。
    """
    dtype = classify_doc_type(rec)
    rec["type"] = dtype
    j = resolve_journal(rec)
    rec["journal"] = j
    if dtype == "thesis":
        rec["journal_tier"] = -1                      # -1 = 不适用（非期刊）
        rec["journal_level"] = u"学位论文（非期刊）"
        return rec
    if dtype == "conference":
        rec["journal_tier"] = -2
        rec["journal_level"] = u"会议论文"
        return rec
    if not j:
        rec["journal_tier"] = 0
        rec["journal_level"] = u"—"
        return rec
    try:
        tier, level = _evidence().lookup_journal(j)
    except Exception:
        tier, level = 0, u"—"
    try:
        rec["journal_tier"] = int(tier or 0)
    except Exception:
        rec["journal_tier"] = 0
    rec["journal_level"] = level or u"—"
    return rec


def normalize_pool(pool):
    """给信息池逐条分配 ref_no，并补齐结构化字段。输入可为任意 dict 列表。"""
    out = []
    for i, item in enumerate(pool or [], 1):
        if not isinstance(item, dict):
            item = {"content": str(item)}
        content = str(item.get("content") or "")
        rec = {
            "ref_no": "P%d" % i,
            "title": str(_pick(item, "title", "name") or "").strip(),
            "authors": str(_pick(item, "authors", "author") or "").strip(),
            "source": str(_pick(item, "source", "venue", "journal", "source_name") or "").strip(),
            "year": str(_pick(item, "year", "date", "published", "publication_date") or "").strip()[:4],
            "cite": str(_pick(item, "cite", "citations", "citationCount", "cited_by_count", "cited") or "").strip(),
            "doi": str(_pick(item, "doi") or "").strip(),
            "url": str(_pick(item, "url", "link", "html_url") or "").strip(),
            "abstract": str(_pick(item, "abstract", "summary") or "").strip(),
            "content": content,
            "source_type": str(_pick(item, "source_type", "source_kind") or "").strip(),
            # V15（2026-10-10）：期刊分级（1=CSSCI 2=北大核心 11=CSSCI扩展 3/4=国际 Q1-Q4 5=国际未收录）
            "journal": str(_pick(item, "journal") or "").strip(),
            "venue": str(_pick(item, "venue") or "").strip(),
            "journal_tier": int(_pick(item, "journal_tier") or 0),
            "journal_level": str(_pick(item, "journal_level") or "").strip(),
            # V15（2026-10-10，B1）：可审计字段——快照日期、归一键、版本合并留痕、撤稿标记。
            # 这些字段必须进白名单，否则重建 rec 时会被静默丢掉（本函数是白名单式重建）。
            "doi_norm": str(_pick(item, "doi_norm") or "").strip(),
            "doi_raw": str(_pick(item, "doi_raw") or "").strip(),
            "title_norm": str(_pick(item, "title_norm") or "").strip(),
            "snapshot_date": str(_pick(item, "snapshot_date") or "").strip(),
            "versions": int(_pick(item, "versions") or 1),
            "merged_from": list(_pick(item, "merged_from") or []),
            "retracted": bool(_pick(item, "retracted") or False),
            "type": str(_pick(item, "type", "cr_type", "publication_type") or "").strip(),
        }
        fb = _parse_from_content(content)
        for k in ("authors", "source", "year", "cite"):
            if not rec[k] and fb.get(k):
                rec[k] = fb[k]
        if not rec["title"]:
            rec["title"] = _fallback_title(rec["abstract"] or content)
        ensure_journal_fields(rec)                  # V15(B1)：级别单一收口，落库前必做
        if not rec["doi_norm"]:
            rec["doi_norm"] = _norm_doi(rec["doi"])
        if not rec["title_norm"]:
            rec["title_norm"] = _norm_title(rec["title"])
        rec["kind"] = classify_source(rec)          # academic / web / unknown
        out.append(rec)
    return out


def _meta_line(rec):
    parts = [x for x in [rec.get("authors"), rec.get("year"), rec.get("source")] if x]
    if rec.get("cite"):
        parts.append(u"被引 %s" % rec["cite"])
    return u"，".join(parts)


def render_injection(pool, index, mode="asymmetric", own_span=150, shared_span=80):
    """渲染给第 index 位玩家的注入片段（含编号，玩家引用时写 [P1]）。"""
    if not pool:
        return ""
    own = pool[index % len(pool)]
    shared = pool[(index + 2) % len(pool)]
    if len(pool) > 1 and shared.get("ref_no") == own.get("ref_no"):
        for cand in pool:
            if cand.get("ref_no") != own.get("ref_no"):
                shared = cand
                break

    def line(rec, span):
        head = u"[%s]（%s）%s" % (rec.get("ref_no", "P?"), kind_label(rec.get("kind")),
                                rec.get("title", ""))
        meta = _meta_line(rec)
        if meta:
            head += u"（%s）" % meta
        body = (rec.get("abstract") or rec.get("content") or "")[:span]
        return u"- %s：%s" % (head, body)

    if mode == "symmetric":
        head = u"主教练提供的共享背景资料（引用时请标注编号，如 [P1]）："
        lines = [head] + [line(r, own_span) for r in pool[:4]]
    else:
        head = (u"主教练提供的背景资料（引用时请标注编号，如 [P1]；"
                u"没有编号依据的话写成「待验证」）：")
        lines = [head, line(own, own_span),
                 u"- 补充背景：" + line(shared, shared_span)[2:]]
    return "\n".join(lines)


def render_reference_list(pool, header=u"### 参考文献", limit=20, kinds=("academic",)):
    """由代码渲染参考文献表。**只收录学术来源**（网络资料另见 render_web_sources）。
    **不接受模型生成的文献文本。**"""
    rows_items = [r for r in (pool or []) if r.get("kind") in kinds][:limit]
    excluded = len([r for r in (pool or []) if r.get("kind") not in kinds])
    if not rows_items:
        return ""
    rows = [header, "",
            u"> 下表由系统按主教练本轮实际注入的信息池自动生成，未经过语言模型改写，"
            u"**仅收录可溯源到期刊/会议/出版机构或 DOI 的学术来源**；"
            u"「级别」依据公开的 CSSCI（2023-2024）与《中文核心期刊要目总览》（2023 年版）名录判定。"
            u"报告正文中的 [P1] 等编号即指向本表。", "",
            u"| 编号 | 标题 | 作者 | 来源 | 级别 | 年份 | 被引 | 链接 |",
            u"|---|---|---|---|---|---|---|---|"]
    for r in rows_items:
        url = r.get("url") or ""
        link = u"[链接](%s)" % url if url.startswith("http") else u"—"
        rows.append(u"| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r.get("ref_no", ""), _cell(r.get("title")), _cell(r.get("authors")),
            _cell(r.get("journal") or r.get("source")),
            _cell(r.get("journal_level") or tier_label(r.get("journal_tier"))),
            _cell(r.get("year")), _cell(r.get("cite")), link))
    if excluded:
        rows += ["", u"> 另有 %d 条非学术来源（网页/新闻/自媒体）未计入本表，"
                     u"见下方「网络资料」一节。" % excluded]
    return "\n".join(rows)


def render_web_sources(pool, header=u"### 网络资料（非学术来源）", limit=20):
    """网络/新闻/自媒体来源单列，明确其性质，避免被当成学术文献引用。

    B9（2026-10-10）：开了白名单网后，网络资料是**过了规则过滤器**才进来的，
    因此表里补一列「可信度」把放行理由写清楚（未开网时该列为空，表结构与旧版一致）。
    """
    items = [r for r in (pool or []) if r.get("kind") in ("web", "unknown")][:limit]
    if not items:
        return ""
    has_tier = any(str(r.get("source_tier_label") or "").strip() for r in items)
    cols = [u"编号", u"标题", u"来源站点"] + ([u"可信度"] if has_tier else []) + [u"链接"]
    lines = [header, "",
             u"> 以下为检索到的**网页/新闻/自媒体**内容，仅作背景参考，"
             u"**不作为文献依据**；正文若引用请写明「据网络报道」。", "",
             u"| " + u" | ".join(cols) + u" |", u"|" + u"---|" * len(cols)]
    for r in items:
        url = r.get("url") or ""
        link = u"[链接](%s)" % url if url.startswith("http") else u"—"
        cells = [r.get("ref_no", ""), _cell(r.get("title")), _cell(_domain_of(url) or "—")]
        if has_tier:
            cells.append(_cell(r.get("source_tier_label") or u"—"))
        cells.append(link)
        lines.append(u"| " + u" | ".join(cells) + u" |")
    if has_tier:
        lines += ["", u"> 「可信度」由**规则白名单**判定：政府/国际组织/高校/权威媒体为"
                      u"「可信来源」，有 DOI 或学术域名为「学术来源」；"
                      u"**白名单外的网络源已在注入前剔除**（剔除明细见检索漏斗）。"]
    return "\n".join(lines)



def check_citations(report, pool):
    """三类标记：有出处的引用 / 无出处的断言 / 引用了不存在的编号（幻觉）。

    全部为本地正则与集合运算，不调用模型——避免“用模型检查模型”的同源问题。
    """
    valid = set(r.get("ref_no") for r in (pool or []))
    academic = set(r.get("ref_no") for r in (pool or []) if r.get("kind") == "academic")
    text = report or ""
    cited = CITE_RE.findall(text)
    cited_set = set(cited)
    phantom = sorted(cited_set - valid, key=lambda x: int(x[1:]))
    used = sorted(cited_set & valid, key=lambda x: int(x[1:]))
    web_cited = sorted((cited_set & valid) - academic, key=lambda x: int(x[1:]))  # 引用了网络/未明来源（只算池内真实存在的）

    unsupported = []
    for s in re.split(u"[。！？\\n]", text):
        s = s.strip()
        if len(s) < 8:          # 太短的片段（如“2024年”）不作断言处理
            continue
        if u"[P" in s:
            continue
        if u"待验证" in s:
            continue
        if re.search(u"\\d+%|\\d{4}\\s*年|研究表明|数据显示|据统计|文献", s):
            unsupported.append(s[:80])

    # ---- V15 反幻觉（2026-10-09）：数字级核验 ----
    # V16 修正（2026-10-11，修 5）：**删掉"必须带依据词"这条前置条件**。
    #   实证背景：扫描 40 局有报告的会话，含数字的句子 242 句、其中 **226 句无编号（93%）**，
    #   而本核验报的是 0 —— 因为旧判据要求句子同时含「据/研究/数据显示/调查/统计/报告/文献」
    #   才算，于是"两周内两种方式差异不显著""一组用记录工具"这类**编出来的具体量一个都不报**。
    #   这是典型的"漏报伪装成通过"。
    #   新判据按**数字类型**分级，不再依赖依据词：
    #     · Tier1 带量纲的具体数字（%／倍／个百分点／万／亿／篇／项／人／分／次／小时／周／月）
    #       → 这是对世界的**断言**，池里查无实据即「疑似幻觉数字」；
    #     · Tier2 仅年份（2023 年）→ 多为政策/文献的时间标注，单独列为**提示**，不与 Tier1 混计。
    pool_text = u"".join(
        (r.get("title") or "") + (r.get("abstract") or "") + (r.get("content") or "")
        + (r.get("authors") or "") + (r.get("source") or "")
        for r in (pool or [])
    )
    pool_nums = set(re.findall(u"\\d+(?:\\.\\d+)?", pool_text))
    NUM_RE = re.compile(u"\\d+(?:\\.\\d+)?\\s*(?:%|％|倍|个百分点|万|亿|篇|项|人|分|年|个|次|小时|周|月)")
    QUANT_RE = re.compile(u"\\d+(?:\\.\\d+)?\\s*(?:%|％|倍|个百分点|万|亿|篇|项|人|分|次|小时|周|月)")
    unsourced_numbers, unsourced_years = [], []
    # 中文数字量词（"两周""三个月""五成"）——只认阿拉伯数字会漏掉这类编造的具体量。
    # 但它们也大量出现在合法的口语表述里，故**一律归入 Tier2 提示级**，不计入幻觉数字。
    CN_NUM_RE = re.compile(u"[一二两三四五六七八九十]+\\s*(?:周|个月|月|天|次|倍|成|人|篇|项|分)")
    # V16 判据（第二次修正）：**按"数字是不是结果型断言"分级**，而不是按"有没有依据词"。
    #   为什么改：第一次修正直接删掉依据词要求，结果把"建议分 3 个阶段推进，每阶段 2 周"
    #   这种**计划**也判成幻觉数字——计划不是对世界的断言（自检 _selftest_hallucination 抓出）。
    #   正确的区分：
    #     · **结果型**（%/倍/个百分点/万/亿）: 数量级的**发现**，池里无据即疑似幻觉 →
    #       这类正是用户实际看到的"无依据幻觉数字"，而旧判据因缺少「据/研究」字样而全部漏报；
    #     · **计划型**（个/周/月/天/次/阶段）: 多为方案设计，**不判幻觉**，降为提示级；
    #     · 带依据词（据/研究/数据显示/实测/调查/统计/达到）的数字 → 一律提升为 Tier1。
    RESULT_RE = re.compile(u"\\d+(?:\\.\\d+)?\\s*(?:%|％|倍|个百分点|万|亿)")
    # V16 判据（第三次修正，2026-10-11，依据真实会话的 3 条误报）：
    #   真实误报（用户能看到的）：
    #     - **第 3 周**：中期数据整理，检查边缘学生参与停滞情况…
    #     - **第 4 周**：后测（交流恐惧量表）、数据汇总、指标裁决
    #     - **第 5 周**：撰写试点报告，决定是否扩展或调整方案
    #   这是**实验方案的时间表**，不是对世界的断言。两处根因：
    #     ① 旧 BASIS_RE 的**裸「据」**会匹配到「**数**据」「**根**据」「**证**据」「**依**据」里的那个字
    #        → "中期数据整理"被误判为"有外部依据"；
    #     ② 旧 PLAN_RE 只认「第[一二三四五]**步**」，**不认阿拉伯数字的「第 3 周」** → 计划没被豁免。
    #   现在：依据词改为**断言形态**（据/研究显示/数据显示/报告指出…），计划词覆盖阿拉伯数字。
    BASIS_RE = re.compile(
        u"(?<![数根证依])据|研究(?:显示|表明|发现)|数据显示|实测|"
        u"调查(?:显示|表明|发现)|统计(?:显示|表明)|报告(?:显示|指出|表明)|"
        u"文献(?:显示|表明)|达到")
    PLAN_RE = re.compile(
        u"建议|可以|计划|拟|设想|方案|阶段|步骤|"
        u"第\\s*[0-9一二三四五六七八九十]+\\s*(?:周|个月|月|天|次|步|阶段)|"
        u"每\\s*\\d+\\s*(?:周|月|天|次)")
    # V16 判据（第四次修正，2026-10-11）：Tier2 **收窄为"真正的年份引用"**。
    #   依据：上一版 Tier2 是"有数字但不是 Tier1"的兜底桶，实测在一局报告里塞进 11–22 条，
    #   内容几乎全是**实验方案本身**（"总计 5 周""约 80 人""三项指标""第 0 周前测"）。
    #   它既不误导（标了"非幻觉"），但也没有用——把方案复述一遍当"提示核对"是噪音。
    #   现在：只有句子含**四位年份**（19xx/20xx 年）才进 Tier2，其余一律不报。
    #   另：**中文数量词不再单列**（"两周""三项"绝大多数出现在方案/计划语境），
    #   它们的语义由 PLAN_RE 的整句豁免覆盖。
    YEAR_RE = re.compile(u"(?:19|20)\\d{2}\\s*年")
    for s in re.split(u"[。！？\\n]", text):
        s = s.strip()
        if len(s) < 8 or u"待验证" in s or u"[P" in s:
            continue
        nums = [m.group(0) for m in NUM_RE.finditer(s)]
        if not nums:
            continue                      # 无阿拉伯数字（含中文量词）→ 不报
        bare = [re.findall(u"\\d+(?:\\.\\d+)?", n)[0] for n in nums]
        if any(b in pool_nums for b in bare):
            continue                      # 该句的数字在池里有据 → 不算
        _basis, _result, _plan = BASIS_RE.search(s), RESULT_RE.search(s), PLAN_RE.search(s)
        if _plan and not _basis:
            continue                      # 计划型整句豁免
        if _basis or _result:
            unsourced_numbers.append(s[:90])
        elif YEAR_RE.search(s):
            unsourced_years.append(s[:90])
        # 其余（计划数量、指标口径等）不报——它们不是"对世界的断言"

    if not pool:
        verdict = u"无外部信息源，报告不含引用（属正常降级）"
    elif phantom:
        verdict = u"⚠️ 出现引用了不存在的编号（疑似编造文献）：%s" % u"、".join(phantom)
    elif unsourced_numbers:
        verdict = u"⚠️ 有 %d 处具体数字在信息池中查无实据（疑似幻觉数字）" % len(unsourced_numbers)
        if unsourced_years:
            verdict += u"；另有 %d 处年份无出处（提示级）" % len(unsourced_years)
    elif unsourced_years:
        verdict = u"有 %d 处年份未标注出处（提示级，非幻觉数字）" % len(unsourced_years)
    elif unsupported:
        verdict = u"仍有 %d 处含数字/年份的断言未标注出处" % len(unsupported)
    else:
        verdict = u"引用可追溯"
    return {
        "pool_size": len(pool or []),
        "academic_size": len(academic),
        "web_size": len(valid - academic),
        "cited": used,
        "cited_count": len(used),
        "cited_academic": sorted(cited_set & academic, key=lambda x: int(x[1:])),
        "cited_web": web_cited,
        "phantom": phantom,
        "phantom_count": len(phantom),
        "unsupported": unsupported,
        "unsupported_count": len(unsupported),
        "unsourced_numbers": unsourced_numbers,
        "unsourced_numbers_count": len(unsourced_numbers),
        # V16（修 5）：Tier2 年份单独计数，不与 Tier1 混计——避免把政策/文献时间标注当幻觉数字
        "unsourced_years": unsourced_years,
        "unsourced_years_count": len(unsourced_years),
        "verdict": verdict,
    }


def to_meta_list(pool):
    """给接口与存档用的结构化清单（不含长正文）。"""
    keys = ("ref_no", "title", "authors", "source", "journal", "journal_tier", "journal_level", "year",
            "cite", "doi", "doi_norm", "url", "source_type", "kind", "type",
            "snapshot_date", "versions", "merged_from", "retracted",
            # B9（2026-10-10）：网络来源可信度三件套——前端「网络资料」表要显示它，
            # 也是"这条为什么被放行"的可解释依据（未开白名单网时为空，前端自动隐藏该列）。
            "source_tier", "source_tier_label", "source_tier_reason")
    return [{k: r.get(k, "") for k in keys} for r in (pool or [])]


def citation_rules_text(has_pool=True):
    """报告生成提示词里追加的引用要求（供引擎拼进 prompt）。"""
    if not has_pool:
        return (u"\n【引用要求】本次没有外部信息源，请不要编造文献；"
                u"凡是需要外部依据的论断，一律写成「待验证」。")
    return (u"\n【引用要求】\n"
            u"1. 凡是依据主教练注入资料得出的结论，句末必须标注编号，如 [P1]；\n"
            u"2. 没有编号依据的论断必须写成「（待验证）」，禁止编造文献或编号；\n"
            u"3. 注入资料分两类：【学术文献】可作为文献依据引用；"
            u"【网络资料】只能作为背景，引用时必须写明「据网络报道」，不得当作学术文献；\n"
            u"4. 不要自己写参考文献列表，系统会在报告末尾自动附上。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    demo = [
        {"title": u"生成式AI在高校作业中的伦理规制", "authors": u"张三", "source": u"高教研究",
         "date": u"2024-03-01", "cite": "12", "url": "https://example.org/1",
         "abstract": u"提出过程性评价与AI辅助声明制度相结合的治理路径。"},
        {"content": u"李四；来源：教育研究；发表时间：2023-05-01；被引 7；摘要：课堂参与度的实证分析"},
    ]
    pool = normalize_pool(demo)
    print(render_injection(pool, 0))
    print()
    print(render_reference_list(pool))
    print()
    fake_report = (u"研究表明过程性评价有效 [P1]。某项数据显示参与度提升 30%%。"
                   u"另据文献 [P9] 结论相反。")
    print(check_citations(fake_report, pool))
