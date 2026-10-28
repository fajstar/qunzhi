# -*- coding: utf-8 -*-
"""可信网络资料过滤网（检索链路 B9）
========================================
背景（实证）：2026-10-10 那局真实会话注入池里有 5 条网络资料，全部是自媒体/聚合站
（搜狐号、今日头条等），作者/年份/被引**全为空**，标题还是"专家却说你得这样看！"这类
标题党形态。当时的应对是"干脆只用论文"（`ALLOW_WEB_SOURCES=0`）。

但这个应对有个代价：**网络资料不是全都不可信**，只是我们当时没有筛它的规则。
STORM（Stanford，arXiv 2402.14207）的做法正是规则过滤器——按可靠来源清单剔除不可信来源，
而非"整个不信任网络"。它自己记录的失败点第一条就是 **source bias transfer**
（偏网络主流来源、混入推广内容），并建议加 content sifting 后处理。

本模块就是那张"网"：**纯规则、不调 LLM、可单测**。分四级：

    academic  有 DOI 或命中学术域名        → 硬信任（与现有论文链路同权）
    trusted   政府/国际组织/高校/权威媒体   → 软信任（白名单内）
    blocked   自媒体/文库/问答/推广        → 一律剔除，**不看它说什么**
    unknown   未列入白名单的网络源          → 剔除（宁可漏，不可错——这是本网的设计取向）

设计取向（与方案 §7.1 一致）
-----------------------------
1. **规则式，不让 LLM 自由发挥**：判定只看域名与路径，可复现、可审计、零 token。
2. **默认不启用**：`coach_search.ALLOW_WEB_SOURCES=0` 时本模块不被调用，
   现有"只用论文"行为**一个字节都不变**（回归 146 项必须全绿）。
3. **fail-closed，但只对网络源**：未列入白名单的网络源一律剔除——
   因为这张网存在的理由就是"不能保证质量就不注入"。学术源不受影响。
4. **每一次丢弃都可解释**：返回 `reason` 文本，进漏斗计数，任何一局能回放为什么丢它。

刻意不做（照抄"明确不做"清单）
------------------------------
* 不做掠夺性期刊黑名单硬判（Beall's list 已关闭，替代榜单偏见有争议，方案 §5.2）；
* 不做 LLM 相关性/可信度打分（不可复现，且与"规则式"取向冲突）；
* 不把"平台"当"内容"判死——`blocked` 只针对**质量不可控的平台形态**
  （自媒体号、文库、问答、推广落地页），不针对新闻机构的正规报道路径。
"""
import re
import unicodedata

__all__ = [
    "SOURCE_TIERS", "classify", "domain_of", "is_web_source",
    "explain", "WEB_POLICY_PAPER_ONLY", "WEB_POLICY_WHITELIST",
]

VERSION = "trusted_sources/1.0 (2026-10-10)"

# 策略名
WEB_POLICY_PAPER_ONLY = "paper_only"    # 现状：网络源一律不进（coach_search 侧已按三无剔除）
WEB_POLICY_WHITELIST = "whitelist"      # B9：只放行 academic / trusted

SOURCE_TIERS = ("academic", "trusted", "blocked", "unknown")

# ---------------------------------------------------------------- 域名抽取
_URL_HOST = re.compile(r"^[a-z][a-z0-9+.\-]*://([^/?#]+)", re.I)
_HOST_ONLY = re.compile(r"^([a-z0-9.\-]+\.[a-z]{2,})(?:[/?#]|$)", re.I)


def domain_of(url):
    """从 URL 抽主机名（小写、去端口、去 www.）。非法/空返回 ""。"""
    u = unicodedata.normalize("NFKC", str(url or "")).strip()
    if not u:
        return ""
    m = _URL_HOST.match(u)
    host = m.group(1) if m else ""
    if not host:
        m2 = _HOST_ONLY.match(u)
        host = m2.group(1) if m2 else ""
    if not host:
        return ""
    host = host.split("@")[-1].split(":")[0].strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_hit(host, suffixes):
    """host 等于某后缀，或以 .后缀 结尾（避免 'notgov.cn' 命中 'gov.cn'）。"""
    if not host:
        return None
    for s in suffixes:
        if host == s or host.endswith("." + s):
            return s
    return None


# ---------------------------------------------------------------- 学术源
# 有 DOI 直接算学术。以下域名是学术检索链路自己会命中的库/出版商/预印本平台。
ACADEMIC_DOMAINS = (
    # 学术基础设施 / 聚合
    "doi.org", "crossref.org", "openalex.org", "semanticscholar.org", "doaj.org",
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "europepmc.org", "scopus.com",
    "webofscience.com", "clarivate.com", "jstor.org", "ssrn.com", "osf.io",
    "repec.org", "eric.ed.gov", "scielo.org", "cnki.net", "ncpssd.org", "ncpssd.cn",
    "nstl.gov.cn", "wanfangdata.com.cn", "cqvip.com", "chinaxiv.org", "sinoss.net",
    # 预印本
    "arxiv.org", "biorxiv.org", "medrxiv.org", "preprints.org", "researchsquare.com",
    "ssrn.com", "osf.io", "arxiv.org",
    # 出版商
    "nature.com", "science.org", "sciencedirect.com", "springer.com", "springerlink.com",
    "wiley.com", "tandfonline.com", "sagepub.com", "frontiersin.org", "mdpi.com",
    "plos.org", "ieee.org", "acm.org", "aps.org", "iop.org", "acs.org", "rsc.org",
    "cell.com", "bmj.com", "jamanetwork.com", "nejm.org", "thelancet.com",
    "academic.oup.com", "cambridge.org", "degruyter.com", "emerald.com",
    "worldscientific.com", "karger.com", "thieme-connect.com", "biomedcentral.com",
)

# ---------------------------------------------------------------- 可信网络源
# 政府 / 国际组织 / 高校与学术机构 / 权威媒体 / 统计与标准机构。
TRUSTED_DOMAINS = (
    # 中国政务与科教主管部门
    "gov.cn", "moe.gov.cn", "most.gov.cn", "nsfc.gov.cn", "stats.gov.cn",
    "nhc.gov.cn", "mee.gov.cn", "mohrss.gov.cn", "cac.gov.cn", "nopss.gov.cn",
    # 国际组织
    "who.int", "un.org", "unesco.org", "unicef.org", "worldbank.org", "oecd.org",
    "imf.org", "undp.org", "wipo.int", "ilo.org", "fao.org", "unescap.org",
    # 高校与科研机构（后缀式，覆盖全部）
    "edu.cn", "ac.cn", "edu", "ac.uk", "ac.jp", "edu.hk", "edu.tw", "edu.sg",
    "edu.au", "ac.nz", "edu.de", "ac.at",
    # 国家级科研机构与学会
    "cas.cn", "cass.cn", "cae.cn", "caas.cn", "cags.ac.cn", "cae.org.cn",
    # 权威媒体与官方发布（只列正规新闻机构）
    "xinhuanet.com", "news.cn", "people.com.cn", "cctv.com", "china.com.cn",
    "chinadaily.com.cn", "gmw.cn", "ce.cn", "stdaily.com", "qstheory.cn",
    "chinanews.com.cn", "huanqiu.com", "thepaper.cn",
)

# ---------------------------------------------------------------- 明确低质
# 质量不可控的平台形态：自媒体号、文库、问答、推广落地页。
# 注意：这是**平台级**判断，不看单条内容质量——本网的设计取向就是"宁可漏"。
BLOCKED_DOMAINS = (
    # 自媒体 / 聚合号
    "toutiao.com", "baijiahao.baidu.com", "mp.weixin.qq.com", "weixin.qq.com",
    "sohu.com", "163.com", "sina.com.cn", "sina.cn", "ifeng.com", "qq.com",
    "360kuai.com", "yidianzixun.com", "zhihu.com", "jianshu.com", "csdn.net",
    "cnblogs.com", "juejin.cn", "segmentfault.com", "weibo.com", "tieba.baidu.com",
    "xiaohongshu.com", "douban.com", "bilibili.com", "ixigua.com", "douyin.com",
    # 文库 / 文档站
    "wenku.baidu.com", "docin.com", "doc88.com", "book118.com", "renrendoc.com",
    "jinchutou.com", "taodocs.com", "51wendang.com", "xuehai.net", "mbalib.com",
    "wendangku.net", "360doc.com", "docer.com", "mayiwenku.com", "docsou.com",
    "ishare.iask.sina.com.cn", "zixin.com.cn", "21cnjy.com", "zhuangpeitu.com",
    # 问答 / 作业 / 考试资料
    "zhidao.baidu.com", "wenda.so.com", "iask.sina.com.cn", "jyeoo.com",
    "zujuan.com", "xkw.com", "zxxk.com", "examcoo.com", "51jiaoxi.com",
    "dianshi.com", "kaoshi.com", "chazidian.com", "yuwenmi.com",
    # 电商 / 推广
    "taobao.com", "tmall.com", "jd.com", "pinduoduo.com", "1688.com",
    "mianbaoduo.com", "ppt118.com", "xiexiebang.com", "biyelunwen.com",
    "qikan.com", "lunwen.com", "fanwen.com", "zuowen.com",
)

# 路径级规则：门户的正规新闻路径放行，自媒体号路径拦截。
# 例：news.163.com/... 是新闻；163.com/dy/article/... 是自媒体号。
BLOCKED_URL_PATTERNS = (
    r"/(?:dy|dyarticle)/",          # 网易/搜狐自媒体号
    r"/a/\d{6,}",                   # 搜狐号文章
    r"/om/",                        # 腾讯企鹅号
    r"/c/\d{6,}",                   # 凤凰号
    r"/mp/",                        # 公众号镜像
    r"/s\?__biz=",                  # 微信文章
    r"/(?:wenku|doc|docs|download)/",
    r"\.(?:doc|docx|ppt|pptx|pdf)($|[?#])",   # 直接指向文档文件的链接
)

# 内容级规则：推广/软文特征（与 coach_search 的 LOW_QUALITY_CONTENT_PATTERNS 互补，
# 这里只放"内容自证是推广"的强特征，避免与那边重复）。
PROMO_CONTENT_PATTERNS = (
    r"扫码(?:关注|咨询|领取)", r"长按(?:识别|关注)", r"点击关注", r"关注公众号",
    r"加(?:微信|客服|老师)", r"(?:免费|限时)(?:领取|领取资料|试听|咨询)",
    r"(?:招生|报名)(?:简章|咨询|入口)", r"一对一辅导", r"代写(?:论文|作业)",
    r"包过|保过|速成班", r"(?:优惠|折扣|促销)(?:活动|价)",
)


def _content_is_promo(title, content):
    t = "%s\n%s" % (str(title or ""), str(content or ""))
    for pat in PROMO_CONTENT_PATTERNS:
        if re.search(pat, t):
            return pat
    return None


def is_web_source(item):
    """是否网络检索来源（而非学术库/出版商）。

    判定顺序有意如此：**先看有没有 DOI**——有 DOI 的条目就是学术文献，
    不管它挂在哪个域名上都不该被这张"网络资料网"审。再看 provider/source_type，
    最后才回退到域名。
    """
    it = item or {}
    doi = str(it.get("doi") or it.get("doi_norm") or "").strip()
    if doi and re.match(r"^10\.\d{4,9}/\S+$", doi):
        return False
    prov = str(it.get("provider") or it.get("source") or "").strip().lower()
    if prov in ("bocha", "web", "crawler", "bing", "google", "baidu", "sogou"):
        return True
    st = str(it.get("source_type") or "").strip().lower()
    if st in ("web", "webpage", "news", "blog"):
        return True
    host = domain_of(it.get("url"))
    if not host:
        return False
    if _host_hit(host, ACADEMIC_DOMAINS):
        return False
    return True


def classify(item):
    """判定单条来源等级。

    返回 dict：{tier, label, reason, host, category}
      tier     academic | trusted | blocked | unknown
      label    中文短标签（可进前端与审计）
      reason   为什么这么判（可解释、可回放）
      host     抽出的主机名
      category 命中的规则类别（academic_domain / trusted_domain / blocked_domain /
               blocked_path / promo_content / no_url / unlisted）
    """
    it = item or {}
    url = str(it.get("url") or "").strip()
    host = domain_of(url)
    low_url = url.lower()

    # 1) 有 DOI → 学术，直接硬信任（DOI 本身来自权威注册机构）
    doi = str(it.get("doi") or it.get("doi_norm") or "").strip()
    if doi and re.match(r"^10\.\d{4,9}/\S+$", doi):
        return {"tier": "academic", "label": u"学术（DOI）", "category": "doi",
                "reason": u"有归一 DOI", "host": host}

    # 2) 学术域名
    hit = _host_hit(host, ACADEMIC_DOMAINS)
    if hit:
        return {"tier": "academic", "label": u"学术来源", "category": "academic_domain",
                "reason": u"命中学术域名 %s" % hit, "host": host}

    # 3) 明确低质域名（平台级）
    hit = _host_hit(host, BLOCKED_DOMAINS)
    if hit:
        return {"tier": "blocked", "label": u"不可信平台", "category": "blocked_domain",
                "reason": u"命中不可信平台 %s" % hit, "host": host}

    # 4) 路径级低质（自媒体号 / 文库 / 文档直链）
    for pat in BLOCKED_URL_PATTERNS:
        if re.search(pat, low_url):
            return {"tier": "blocked", "label": u"不可信路径", "category": "blocked_path",
                    "reason": u"命中路径规则 %s" % pat, "host": host}

    # 5) 可信白名单域名
    hit = _host_hit(host, TRUSTED_DOMAINS)
    if hit:
        return {"tier": "trusted", "label": u"可信来源", "category": "trusted_domain",
                "reason": u"命中可信域名 %s" % hit, "host": host}

    # 6) 内容自证是推广/软文
    pat = _content_is_promo(it.get("title"), it.get("content") or it.get("snippet"))
    if pat:
        return {"tier": "blocked", "label": u"推广内容", "category": "promo_content",
                "reason": u"命中推广特征 %s" % pat, "host": host}

    # 7) 无 URL 或未列入白名单
    if not host:
        return {"tier": "unknown", "label": u"无来源链接", "category": "no_url",
                "reason": u"条目没有可解析的 URL", "host": ""}
    return {"tier": "unknown", "label": u"未列入白名单", "category": "unlisted",
            "reason": u"域名 %s 不在可信清单内" % host, "host": host}


def explain(item):
    """一行人类可读解释（日志用）。"""
    v = classify(item)
    return u"[%s] %s ← %s（%s）" % (
        v["tier"], str((item or {}).get("title") or "")[:40],
        v["label"], v["reason"])
