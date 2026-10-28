# -*- coding: utf-8 -*-
"""
群智 V15.5 · 多源学术检索（零 Key 可用）
==========================================
设计目标：在不增加任何付费 Key、不装任何额外依赖的前提下，把学术来源从
「知网 + OpenAlex + arXiv + 博查」扩展到覆盖国际期刊/开放获取/医学文献。

已实测可用的源（2026-09-12 实测 HTTP 200）：
  - Crossref     : 1.5 亿+ DOI 元数据，覆盖全学科期刊/会议，含被引数
  - DOAJ         : 开放获取期刊索引，多语种，含摘要
  - PubMed       : 医学/生命科学/部分心理与教育研究（E-utilities，免 Key）
  - OpenAlex     : 2.5 亿文献（已有模块，此处复用）
  - Semantic Scholar : 覆盖好但免费额度极易触发 429，仅作"尽力而为"补充

不可用的（已实测）：
  - Unpaywall    : 422，要求填入真实邮箱，不接受示例邮箱 → 不接入
  - arXiv        : 本机网络被重置（云端有独立模块 arxiv_search.py，保持原状）

设计要点：
  - 只用 requests，硬超时，任何失败都返回 [] 并打印一行日志（绝不影响主流程）
  - 统一输出结构，交给 coach_search 走同一套"净化 → 低质过滤 → 时效过滤"管线
  - 默认只取近 MAX_AGE 年文献，与系统"学术类不过 3 年"的口径一致
"""
import time
import re
from datetime import datetime

import requests

UA = "QunZhi/1.0 (research decision rehearsal system; academic metadata lookup)"
TIMEOUT = 20
MAX_AGE_YEARS = 3
# V15（2026-10-09）：各源最近一次失败的**具体原因**。
# 这些源都是间歇性失败（被挡/改版/限速/超时），此前一律静默返回 0 条，
# 导致无法判断"是这个源没数据，还是被限速了"。各源在自己的失败分支写入这里，
# multi_source_search 会把它记进逐源台账（source_status）。
LAST_STATUS = {}


def _from_year(max_age: int = MAX_AGE_YEARS) -> int:
    return datetime.now().year - max_age


def _clean(text, limit=600):
    """去 HTML 标签 / 去多余空白（Crossref 的 abstract 常带 JATS 标签）"""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(text))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _year_of(*cands):
    for c in cands:
        try:
            y = int(c)
            if 1900 <= y <= 2100:
                return y
        except Exception:
            continue
    m = re.search(r"(19|20)\d{2}", str(cands[0] or ""))
    return int(m.group(0)) if m else None


# ---------------- Crossref（免 Key，全学科） ----------------

def search_crossref(query: str, rows: int = 6, max_age: int = MAX_AGE_YEARS) -> list:
    """Crossref 检索：返回近 max_age 年的期刊/会议文献元数据"""
    try:
        r = requests.get(
            "https://api.crossref.org/works",
            params={
                "query": query,
                "rows": max(rows * 2, 10),
                "filter": f"from-pub-date:{_from_year(max_age)}-01-01,type:journal-article",
                "select": "title,author,issued,DOI,abstract,container-title,is-referenced-by-count,URL",
                "sort": "relevance",
            },
            headers={"User-Agent": UA}, timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[Crossref] HTTP {r.status_code}", flush=True)
            LAST_STATUS["crossref"] = f"HTTP {r.status_code}"
            return []
        items = (r.json().get("message") or {}).get("items") or []
        out = []
        for it in items:
            title = (it.get("title") or [""])[0]
            if not title:
                continue
            year = None
            try:
                year = (it.get("issued", {}).get("date-parts") or [[None]])[0][0]
            except Exception:
                pass
            authors = "、".join(
                (a.get("family") or a.get("name") or "") for a in (it.get("author") or [])[:3]
            )
            venue = (it.get("container-title") or [""])[0]
            doi = it.get("DOI") or ""
            parts = [title]
            if authors:
                parts.append("作者：" + authors)
            if venue:
                parts.append("期刊：" + venue)
            if year:
                parts.append(f"发表时间：{year}")
            if it.get("is-referenced-by-count") is not None:
                parts.append(f"被引：{it['is-referenced-by-count']}")
            abs_ = _clean(it.get("abstract"))
            if abs_:
                parts.append("摘要：" + abs_[:400])
            out.append({
                "title": _clean(title, 200),
                "url": it.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
                "summary": "；".join(parts),
                "source": "crossref",
                "year": year,
                "doi": doi,
                "citations": it.get("is-referenced-by-count"),
                # V16（修 1 补漏）：abstract 必须**作为字段**返回，否则 _keep_meta 带不走、
                # 下游只剩"题录"（实证：真实会话 4 条里 3 条 abstract=0 字、无法做蕴含核验）。
                "abstract": abs_ or "",
                "abstract_missing": (not abs_),
            })
            if len(out) >= rows:
                break
        print(f"[Crossref] 命中 {len(out)} 篇（{query[:24]}）", flush=True)
        return out
    except Exception as e:
        print(f"[Crossref] 失败: {str(e)[:90]}", flush=True)
        LAST_STATUS["crossref"] = "异常: " + str(e)[:60]
        return []


# ---------------- DOAJ（免 Key，开放获取期刊） ----------------

def search_doaj(query: str, rows: int = 6, max_age: int = MAX_AGE_YEARS) -> list:
    """DOAJ 检索：开放获取期刊论文（多语种，含摘要）"""
    try:
        import urllib.parse
        url = "https://doaj.org/api/search/articles/" + urllib.parse.quote(query) + "?pageSize=" + str(max(rows * 2, 10))
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"[DOAJ] HTTP {r.status_code}", flush=True)
            LAST_STATUS["doaj"] = f"HTTP {r.status_code}"
            return []
        results = r.json().get("results") or []
        out = []
        min_year = _from_year(max_age)
        for it in results:
            bj = it.get("bibjson") or {}
            title = bj.get("title") or ""
            if not title:
                continue
            year = _year_of(bj.get("year"))
            if year and year < min_year:      # DOAJ 不支持服务端年份过滤，本地过滤
                continue
            authors = "、".join((a.get("name") or "") for a in (bj.get("author") or [])[:3])
            journal = (bj.get("journal") or {}).get("title") or ""
            links = bj.get("link") or []
            url_ = links[0].get("url") if links else ""
            doi = ""
            for ident in (bj.get("identifier") or []):
                if ident.get("type") == "doi":
                    doi = ident.get("id") or ""
            parts = [title]
            if authors:
                parts.append("作者：" + authors)
            if journal:
                parts.append("期刊：" + journal)
            if year:
                parts.append(f"发表时间：{year}")
            abs_ = _clean(bj.get("abstract"))
            if abs_:
                parts.append("摘要：" + abs_[:400])
            out.append({
                "title": _clean(title, 200),
                "url": url_ or (f"https://doi.org/{doi}" if doi else ""),
                "summary": "；".join(parts),
                "source": "doaj",
                "year": year,
                "doi": doi,
                # V16（修 1 补漏）：abstract 作为字段返回（同 crossref）
                "abstract": abs_ or "",
                "abstract_missing": (not abs_),
            })
            if len(out) >= rows:
                break
        print(f"[DOAJ] 命中 {len(out)} 篇（{query[:24]}）", flush=True)
        return out
    except Exception as e:
        print(f"[DOAJ] 失败: {str(e)[:90]}", flush=True)
        LAST_STATUS["doaj"] = "异常: " + str(e)[:60]
        return []


# ---------------- PubMed（免 Key，医学/健康/心理） ----------------

def search_pubmed(query: str, rows: int = 6, max_age: int = MAX_AGE_YEARS) -> list:
    """PubMed E-utilities：esearch 拿 ID → esummary 拿题录（免 Key，限 3 次/秒）"""
    try:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        yr = _from_year(max_age)
        r = requests.get(f"{base}/esearch.fcgi", params={
            "db": "pubmed", "term": query, "retmode": "json", "retmax": rows,
            "datetype": "pdat", "mindate": str(yr), "maxdate": str(datetime.now().year),
            "sort": "relevance",
        }, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"[PubMed] esearch HTTP {r.status_code}", flush=True)
            return []
        ids = ((r.json().get("esearchresult") or {}).get("idlist") or [])
        if not ids:
            print("[PubMed] 无命中", flush=True)
            return []
        time.sleep(0.35)      # 遵守 3 请求/秒限制
        r2 = requests.get(f"{base}/esummary.fcgi", params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "json",
        }, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r2.status_code != 200:
            print(f"[PubMed] esummary HTTP {r2.status_code}", flush=True)
            return []
        res = (r2.json().get("result") or {})
        out = []
        for pid in ids:
            it = res.get(pid)
            if not isinstance(it, dict):
                continue
            title = it.get("title") or ""
            if not title:
                continue
            year = _year_of((it.get("pubdate") or "")[:4])
            authors = "、".join(a.get("name", "") for a in (it.get("authors") or [])[:3])
            journal = it.get("fulljournalname") or it.get("source") or ""
            parts = [title]
            if authors:
                parts.append("作者：" + authors)
            if journal:
                parts.append("期刊：" + journal)
            if year:
                parts.append(f"发表时间：{year}")
            out.append({
                "title": _clean(title, 200),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                "summary": "；".join(parts),
                "source": "pubmed",
                "year": year,
            })
        print(f"[PubMed] 命中 {len(out)} 篇（{query[:24]}）", flush=True)
        return out
    except Exception as e:
        print(f"[PubMed] 失败: {str(e)[:90]}", flush=True)
        LAST_STATUS["pubmed"] = "异常: " + str(e)[:60]
        return []


# ---------------- Semantic Scholar（尽力而为，易 429） ----------------

def search_semanticscholar(query: str, rows: int = 5, max_age: int = MAX_AGE_YEARS) -> list:
    """Semantic Scholar 图 API。

    V15（2026-10-10）：支持 API Key。免 Key 走共享池极易 429（实测间歇性失败），
    在 .env 里配 `S2_API_KEY=...` 后走官方配额，基本不再 429。申请免费：
    https://www.semanticscholar.org/product/api （选 Academic/Research 用途，免费）
    """
    try:
        _hdr = {"User-Agent": UA}
        _s2_key = os.getenv("S2_API_KEY", "").strip()
        if _s2_key:
            _hdr["x-api-key"] = _s2_key
        r = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": query, "limit": rows, "year": f"{_from_year(max_age)}-",
                    "fields": "title,year,abstract,citationCount,externalIds,venue,authors,openAccessPdf"},
            headers=_hdr, timeout=TIMEOUT,
        )
        if r.status_code != 200:
            _why = "免费额度受限（未配 S2_API_KEY）" if r.status_code == 429 else f"HTTP {r.status_code}"
            print(f"[S2] HTTP {r.status_code}（{_why}，跳过）", flush=True)
            LAST_STATUS["semanticscholar"] = f"HTTP {r.status_code}（{_why}）"
            return []
        data = (r.json().get("data") or [])
        out = []
        for it in data:
            title = it.get("title") or ""
            if not title:
                continue
            authors = "、".join((a.get("name") or "") for a in (it.get("authors") or [])[:3])
            doi = (it.get("externalIds") or {}).get("DOI") or ""
            parts = [title]
            if authors:
                parts.append("作者：" + authors)
            if it.get("venue"):
                parts.append("期刊/会议：" + str(it["venue"]))
            if it.get("year"):
                parts.append(f"发表时间：{it['year']}")
            if it.get("citationCount") is not None:
                parts.append(f"被引：{it['citationCount']}")
            abs_ = _clean(it.get("abstract"))
            if abs_:
                parts.append("摘要：" + abs_[:400])
            out.append({
                "title": _clean(title, 200),
                "url": (it.get("openAccessPdf") or {}).get("url") or (f"https://doi.org/{doi}" if doi else ""),
                "summary": "；".join(parts),
                "source": "semanticscholar",
                "year": it.get("year"),
                "doi": doi,
                # V16（修 1 补漏）：abstract 作为字段返回（同 crossref）
                "abstract": abs_ or "",
                "abstract_missing": (not abs_),
            })
        print(f"[S2] 命中 {len(out)} 篇（{query[:24]}）", flush=True)
        return out
    except Exception as e:
        print(f"[S2] 失败: {str(e)[:90]}", flush=True)
        LAST_STATUS["semanticscholar"] = "异常: " + str(e)[:60]
        return []


# ---------------- 相关性过滤（关键：国际源的模糊匹配噪声很大） ----------------
# 实测问题：中文课题直接喂 Crossref，会命中"液压电梯在特殊环境下的适应性研究"
# "5G网络环境下的通信协议安全性分析与改进" 这类只在"环境/改进"上撞词的无关论文。
# 因此对所有国际源结果做一次"主题词重合度"过滤。

_STOP = {"如何", "怎么", "什么", "哪些", "以及", "进行", "研究", "分析", "探讨", "问题",
         "方法", "方案", "体系", "构建", "提升", "改进", "优化", "的", "与", "和", "及"}


def _topic_terms(topic: str) -> set:
    """从课题里抽取主题词：英文词 + 中文 2/3 字词组（去掉通用词）"""
    t = topic or ""
    terms = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", t):
        terms.add(w.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", t):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in _STOP:
                terms.add(bg)
        for i in range(len(run) - 2):
            tg = run[i:i + 3]
            if tg not in _STOP:
                terms.add(tg)
    return terms


def _relevance_score(topic_terms: set, item: dict) -> int:
    text = ((item.get("title") or "") + " " + (item.get("summary") or "")[:400]).lower()
    if not text.strip():
        return 0
    return sum(1 for term in topic_terms if term in text)


def filter_relevant(topic: str, items: list, min_score: int = 1) -> list:
    """词面兜底过滤：只剔除与课题**零重合**的条目。
       注意：靠词面重合度做精细判定不可靠——实测"液压电梯在特殊环境下的适应性研究"
       包含"低温环境"、重合度高达 7，会被误判为相关。因此精细判定交给
       调用方的 LLM 相关性裁判（coach_search._judge_relevance），这里只做兜底。"""
    terms = _topic_terms(topic)
    if not terms:
        return list(items or [])
    kept, dropped = [], []
    for it in (items or []):
        sc = _relevance_score(terms, it)
        it["_rel"] = sc
        (kept if sc >= min_score else dropped).append(it)
    if dropped:
        print(f"[相关性过滤] 国际源剔除 {len(dropped)} 条零重合结果", flush=True)
    kept.sort(key=lambda x: x.get("_rel", 0), reverse=True)
    return kept


# ---------------- 中文平台（V15.5 新增，2026-09-12 实测均免 Key 可用） ----------------
# 说明：以下三个接口均为"网页前端自己在用"的 JSON 接口，无鉴权、无验证码，
#       已实测 HTTP 200；我们只读取题录（标题/作者/摘要/年份/DOI），不做批量爬取。

UA_BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}


def _cn_key_query(terms: list, fields=("IKTE", "IKST")) -> str:
    """NCPSSD 的 Solr 查询式。
    实测两个坑：① 裸词（"教育"）一律返回 0 条，必须带字段前缀；
    ② 多词短语在同一字段内做精确匹配也会 0 条 —— 必须拆词后用 OR 连接。
    IKTE=题名、IKST=主题词、IKPYTE=原篇题名、IKET=英文题名、IKSE=英文主题。"""
    parts = []
    for t in terms[:4]:
        t = (t or "").strip().strip('"')
        if len(t) < 2:
            continue
        for f in fields:
            parts.append(f'{f}="{t}"')
    return "(" + " OR ".join(parts) + ")" if parts else ""


def search_ncpssd(query: str, rows: int = 6, max_age: int = MAX_AGE_YEARS,
                  terms: list = None) -> list:
    """国家哲学社会科学文献中心（中文社科文献量最大，教育学/心理学/社会学覆盖好）
    POST https://www.ncpssd.org/searchHandler/search （form-urlencoded，零鉴权）"""
    try:
        q = _cn_key_query(terms or [query])
        if not q:
            return []
        r = requests.post(
            "https://www.ncpssd.org/searchHandler/search",
            data={"search": q, "pageNum": "1", "pageSize": str(max(rows * 2, 10)),
                  "sort": "synUpdateType|DESC,date|DESC", "sType": "0", "ajaxKeys": ""},
            headers={**UA_BROWSER, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest", "Referer": "https://www.ncpssd.org/"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[NCPSSD] HTTP {r.status_code}", flush=True)
            LAST_STATUS["ncpssd"] = f"HTTP {r.status_code}"
            return []
        # V15（2026-10-09）：该接口会间歇性返回 SPA 的 HTML（被挡/改版），
        # 此时 json() 会抛错并被吞掉，表现为"永远 0 条"。这里显式记下原因。
        _ct = r.headers.get("Content-Type", "")
        if "json" not in _ct.lower():
            LAST_STATUS["ncpssd"] = f"返回非 JSON（{_ct[:30]}，疑似改版/被挡）"
            print(f"[NCPSSD] 返回非 JSON：{_ct[:40]}", flush=True)
            return []
        d = (r.json().get("data") or {})
        out = []
        for x in (d.get("rows") or []):
            title = x.get("title") or ""
            if not title:
                continue
            year = _year_of(x.get("years"))
            authors = (x.get("creator") or "").replace(";", "、")
            journal = x.get("cbw_name") or ""
            parts = [title]
            if authors:
                parts.append("作者：" + authors[:60])
            if journal:
                parts.append("期刊：" + str(journal))
            parts.append(f"发表时间：{year or '未知'}")
            abs_ = _clean(x.get("remark"))
            if abs_:
                parts.append("摘要：" + abs_[:400])
            doi = x.get("doi") or ""
            out.append({
                "title": _clean(title, 200),
                "url": x.get("pdfurl") or (f"https://doi.org/{doi}" if doi else "https://www.ncpssd.org/"),
                "summary": "；".join(parts),
                "source": "ncpssd",
                "year": year,
                "doi": doi,
            })
            if len(out) >= rows:
                break
        print(f"[NCPSSD] 命中 {len(out)} 篇（total={d.get('total')}）", flush=True)
        return out
    except Exception as e:
        print(f"[NCPSSD] 失败: {str(e)[:90]}", flush=True)
        LAST_STATUS["ncpssd"] = "异常: " + str(e)[:60]
        return []


def search_chinaxiv(query: str, rows: int = 6, max_age: int = MAX_AGE_YEARS) -> list:
    """ChinaXiv 中科院预印本（全库 4.7 万条，仅 POST）

    实测两个静默陷阱（都不报错，只会悄悄返回错数据，必须防御）：
      · 分页参数名是 `pageSize` 而不是 `size` —— 传 `size` 会被忽略并固定返回 10 条；
      · 检索参数名是 `searchText` 而不是 `q` —— 传 `q` 会**静默返回全库 47465 条**。
    因此这里除了用正确参数名，还对 total 做合理性校验：若返回的 total 等于全库规模，
    说明参数被忽略了，直接放弃本次结果，避免把随机全库条目注入讨论。
    """
    try:
        q = (query or "").strip()
        if len(q) < 2:
            return []
        r = requests.post(
            "https://chinaxiv.org/api/search",
            json={"searchText": q, "page": 1, "pageSize": max(rows, 5)},
            # V15 修复（2026-10-09）：缺 Referer/Origin 时该接口直接 Connection reset（被挡），
            # 表现为"这个源永远 0 条"。补上后实测 HTTP 200 + 真实命中数。
            headers={**UA_BROWSER, "Content-Type": "application/json",
                     "Accept": "application/json, text/plain, */*",
                     "Referer": "https://chinaxiv.org/",
                     "Origin": "https://chinaxiv.org"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[ChinaXiv] HTTP {r.status_code}", flush=True)
            LAST_STATUS["chinaxiv"] = f"HTTP {r.status_code}"
            return []
        data = (r.json().get("data") or {})
        total = data.get("total")
        if isinstance(total, int) and total >= 40000:
            print(f"[ChinaXiv] total={total} 疑似参数被忽略（返回全库），放弃本次结果", flush=True)
            return []
        content = (data.get("content") or [])
        out = []
        for it in content:
            title = it.get("title") or it.get("entitle") or ""
            if not title:
                continue
            year = _year_of(it.get("year"), str(it.get("month") or "")[:4])
            authors = "、".join((it.get("authors") or [])[:3])
            parts = [title]
            if authors:
                parts.append("作者：" + authors)
            if it.get("journalref"):
                parts.append("期刊：" + str(it["journalref"]))
            parts.append(f"发表时间：{year or '未知'}")
            abs_ = _clean(it.get("abstracts"))
            if abs_:
                parts.append("摘要：" + abs_[:400])
            doi = it.get("doi") or ""
            csoaid = it.get("csoaid")
            out.append({
                "title": _clean(title, 200),
                "url": f"https://chinaxiv.org/abs/{csoaid}" if csoaid else (f"https://doi.org/{doi}" if doi else ""),
                "summary": "；".join(parts),
                "source": "chinaxiv",
                "year": year,
                "doi": doi,
            })
            if len(out) >= rows:
                break
        print(f"[ChinaXiv] 命中 {len(out)} 篇", flush=True)
        return out
    except Exception as e:
        print(f"[ChinaXiv] 失败: {str(e)[:90]}", flush=True)
        LAST_STATUS["chinaxiv"] = "异常: " + str(e)[:60]
        return []


def search_nstl(query: str, rows: int = 6, max_age: int = MAX_AGE_YEARS) -> list:
    """NSTL 国家科技图书文献中心（科技类文献量最大；只取题录）"""
    try:
        import json as _json
        q = {"c": max(rows, 5), "st": "0", "f": [], "p": "",
             "q": [{"k": "t", "v": query, "a": 0, "o": "AND"}],
             "op": "AND", "s": ["crti:desc"], "t": ["JournalPaper"]}
        r = requests.post(
            "https://www.nstl.gov.cn/api/service/nstl/web/execute"
            "?target=nstl4.search4&function=paper/pc/list/pl",
            data={"query": _json.dumps(q, ensure_ascii=False), "webDisplayId": "11", "sl": 1,
                  "facetRelation": "[]", "pageSize": max(rows, 5), "pageNumber": 1},
            headers={**UA_BROWSER, "Referer": "https://www.nstl.gov.cn/search.html?t=JournalPaper"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"[NSTL] HTTP {r.status_code}", flush=True)
            LAST_STATUS["nstl"] = f"HTTP {r.status_code}"
            return []
        out = []
        for rec in (r.json().get("data") or []):
            d = {}
            for item in rec:
                v = item.get("v")
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    v = list(v[0].values())[0]
                elif isinstance(v, list) and len(v) == 1:
                    v = v[0]
                d[item.get("f")] = v
            title = d.get("tit")
            title = " ".join(title) if isinstance(title, list) else (title or "")
            if not title:
                continue
            year = _year_of(d.get("yea"))
            aut = d.get("hasAut") or []
            authors = ""
            if aut and isinstance(aut[0], list):
                # V15 修复（2026-10-09）：某些记录的作者值是 list，直接 join 会抛
                # TypeError: sequence item 0: expected str instance, list found
                # ——该异常此前吞掉了整个 NSTL 源（实测 0 条）。
                _names = []
                for x in aut[0]:
                    if x.get("f") != "nam":
                        continue
                    v = x.get("v")
                    if isinstance(v, list):
                        v = " ".join(str(y) for y in v)
                    if v:
                        _names.append(str(v))
                authors = "、".join(_names)
            ab = d.get("abs")
            ab = " ".join(ab) if isinstance(ab, list) else (ab or "")
            parts = [_clean(title)]
            if authors:
                parts.append("作者：" + authors)
            parts.append(f"发表时间：{year or '未知'}")
            if ab:
                parts.append("摘要：" + _clean(ab)[:400])
            out.append({
                "title": _clean(title, 200),
                "url": ("https://www.nstl.gov.cn/api/service/nstl/web/execute"
                        f"?target=nstl4.search4&function=paper/pc/detail&id={d.get('id')}"),
                "summary": "；".join(parts),
                "source": "nstl",
                "year": year,
                "doi": d.get("doi") or "",
            })
            if len(out) >= rows:
                break
        print(f"[NSTL] 命中 {len(out)} 篇", flush=True)
        return out
    except Exception as e:
        print(f"[NSTL] 失败: {str(e)[:90]}", flush=True)
        LAST_STATUS["nstl"] = "异常: " + str(e)[:60]
        return []


# ---------------- 统一入口：按课题类型选源 + 合并去重 ----------------

MED_KEYWORDS = ["医", "健康", "心理", "临床", "患者", "病", "药", "护理", "公共卫生", "疫情", "睡眠", "抑郁", "焦虑"]


def _has_ascii_words(text: str) -> bool:
    """判断查询里是否含英文词（PubMed/Crossref 对纯中文查询的匹配质量很差，
    实测纯中文课题会让 PubMed 返回化学晶体结构论文等完全无关结果）"""
    return bool(re.search(r"[A-Za-z]{3,}", text or ""))


def route_sources(topic: str, query_en: str = None, cn_terms: list = None,
                  is_tech: bool = False) -> list:
    """按课题类型返回要检索的来源（顺序即优先级）

    query_en：课题的英文检索式（由调用方用 LLM 生成）。纯中文课题必须提供，
    否则跳过 PubMed——实测中文直接喂 PubMed 会命中大量无关化学/晶体学论文。
    cn_terms：课题的中文关键词列表（由调用方用 LLM 生成），供 NCPSSD 使用
    （NCPSSD 的裸词查询一律返回 0，必须拆词带字段前缀）。
    """
    t = topic or ""
    med = any(k in t for k in MED_KEYWORDS)
    effective = query_en if (query_en and _has_ascii_words(query_en)) else (topic if _has_ascii_words(t) else "")

    # ================================================================
    # V16.3（2026-10-11）：**源清单改由插件注册表提供**。
    # 用户要求「能模块化的都可以这样」——加一个检索源不该再改这个函数。
    #   源清单     → app/source_plugins/（丢一个文件即注册）
    #   路由条件   → 插件自报的 applies_when（cn_terms / always / med_and_effective）
    #   优先级     → 插件自报的 order
    # 下方**保留内置清单作为回落**：本函数在每局检索的关键路径上，注册表出问题
    # 不能让整局变成空检索。但回落**不静默**——打印警告，便于发现注册表坏了。
    # ================================================================
    try:
        import plugin_registry as _PR
        _acts = _PR.active_sources(t, {"cn_terms": bool(cn_terms or _topic_terms(t)),
                                       "med": bool(med), "effective": bool(effective)})
        if _acts:
            return [(a["id"], a["search"]) for a in _acts]
        print("[检索源·插件] 注册表返回空清单，回落到内置清单", flush=True)
    except Exception as _e:
        print("[检索源·插件] 注册表不可用（%s: %s），回落到内置清单"
              % (type(_e).__name__, str(_e)[:120]), flush=True)

    sources = []

    # 中文源优先（中文课题上命中质量最高）
    if cn_terms or _topic_terms(t):
        sources.append(("ncpssd", search_ncpssd))     # 中文社科（教育学/心理学/社会学）
    # ChinaXiv 中科院预印本：实测中文教育类覆盖弱（"课堂参与度"→0 条），
    # 但对理工/技术类与英文课题有价值，保留为补充源。
    sources.append(("chinaxiv", search_chinaxiv))
    # NSTL 国家科技图书文献中心：文献量最大。实测教育类也能命中
    # （"师范生 课堂参与度"→5 条），故不再限定于理工类，全课题启用。
    sources.append(("nstl", search_nstl))
    if med and effective:
        sources.append(("pubmed", search_pubmed))
    sources.append(("crossref", search_crossref))     # 全学科，中英文都能命中
    sources.append(("doaj", search_doaj))             # 开放获取（含中文 OA 刊）
    return sources


def multi_source_search(topic: str, per_source: int = 5, max_age: int = MAX_AGE_YEARS,
                        include_s2: bool = False, query_en: str = None,
                        cn_terms: list = None, is_tech: bool = False,
                        status: dict = None) -> list:
    """多源学术检索（零 Key）：返回统一结构列表，已按 DOI/标题去重。
       · 中文课题会额外用英文检索式再跑一遍，兼顾中英文文献；
       · include_s2=False 时跳过 Semantic Scholar（免费额度极易 429，默认不拖慢主流程）；
       · status：可选，传入一个 dict 会写入**逐源台账**
         （每条记录 ok / count / error / elapsed），用于区分"真的没有"与"被限速/超时/被挡"。
         事故背景（2026-10-09）：这些源都是**间歇性失败**，而系统此前静默返回 0 条，
         导致无法判断"是这个源没数据，还是被限速了"。"""
    seen, out = set(), []
    eff_en = query_en if (query_en and _has_ascii_words(query_en)) else None
    pure_cn = not _has_ascii_words(topic)

    def _note(name, count, err="", t0=None):
        if status is None:
            return
        rec = status.setdefault(name, {"count": 0, "error": "", "elapsed": 0.0, "calls": 0})
        rec["count"] += count
        rec["calls"] += 1
        if err:
            rec["error"] = str(err)[:80]
        elif count == 0:
            # V15（2026-10-09）：0 条也要说清原因，否则无法区分"没数据"与"被挡"
            rec["error"] = LAST_STATUS.get(name) or "无命中（接口正常、该查询无结果）"
        if t0 is not None:
            rec["elapsed"] = round(rec["elapsed"] + (time.time() - t0), 1)

    def _add(items):
        for it in items or []:
            key = (it.get("doi") or "").lower().strip() or \
                  re.sub(r"\W+", "", (it.get("title") or "").lower())[:60]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(it)

    for name, fn in route_sources(topic, query_en=eff_en, cn_terms=cn_terms, is_tech=is_tech):
        kwargs = {}
        if name == "ncpssd":
            kwargs["terms"] = cn_terms
            q_list = [topic]
        elif name == "chinaxiv":
            # ChinaXiv 中文教育覆盖弱，只跑一次（有英文式就用英文式，减少无效请求）
            q_list = [eff_en] if (eff_en and pure_cn) else [topic]
        elif pure_cn and eff_en and name in ("pubmed", "doaj"):
            q_list = [eff_en, topic]        # 英文式 + 中文式都跑
        elif name == "crossref" and eff_en and eff_en != topic:
            q_list = [topic, eff_en]        # 中文式拿中文文献，英文式拿国际文献
        elif not pure_cn and eff_en:
            q_list = [topic, eff_en]
        else:
            q_list = [topic]
        # 去重（英文课题时 eff_en 可能与 topic 相同，避免重复请求）
        seen_q, uniq_q = set(), []
        for q in q_list:
            if q and q not in seen_q:
                seen_q.add(q)
                uniq_q.append(q)
        for q in uniq_q:
            _t0 = time.time()
            try:
                got = fn(q, rows=per_source, max_age=max_age, **kwargs)
                _add(got)
                _note(name, len(got or []), "", _t0)
            except Exception as e:
                _note(name, 0, f"{type(e).__name__}: {str(e)[:60]}", _t0)
                print(f"[多源检索] {name} 异常: {str(e)[:80]}", flush=True)

    if include_s2:
        _t0 = time.time()
        try:
            got = search_semanticscholar(eff_en or topic, rows=per_source, max_age=max_age)
            _add(got)
            _note("semanticscholar", len(got or []), "", _t0)
        except Exception as e:
            _note("semanticscholar", 0, f"{type(e).__name__}: {str(e)[:60]}", _t0)

    # 相关性过滤（国际源的模糊匹配噪声大，必须过这一关）
    out = filter_relevant(topic if not eff_en else (topic + " " + eff_en), out)
    return out


if __name__ == "__main__":
    for q in ["提升师范生课堂参与度的策略", "low temperature lithium battery SOC estimation",
              "大学生心理健康筛查体系"]:
        print(f"\n===== {q} =====")
        for it in multi_source_search(q, per_source=3):
            print(f"  [{it['source']}] {it['year']} | {it['title'][:70]}")
            print(f"      {it['url'][:90]}")
