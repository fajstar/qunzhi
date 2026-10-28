# -*- coding: utf-8 -*-
"""
技术类论文检索模块 arxiv_search.py
====================================
- arXiv API（主源，完全开放无需key）：返回标题/作者/摘要/链接/年份
- Semantic Scholar（补充，可能限流失败不影响）
统一输出与知网一致的结构：{title, authors, source, date, cite, url, abstract}
"""
import os, sys, json, re, time, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ARXIV_URL = "https://export.arxiv.org/api/query"
SS_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_URL = "https://api.openalex.org/works"
NS = {"atom": "http://www.w3.org/2005/Atom"}

# arXiv 官方要求：带联系方式的 UA，且请求间隔 ≥3 秒
_UA = "QunZhiResearchBot/15.6 (academic retrieval; contact: qunzhi@example.com)"
_MIN_INTERVAL = 3.0
_last_call = 0.0


def _throttle():
    global _last_call
    now = time.time()
    wait = _MIN_INTERVAL - (now - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = now


def _http_get(url: str, timeout: int = 30, retries: int = 3) -> str:
    """带指数退避的 GET。429/503 是限流信号，退避 5s→10s→20s；
    其他 4xx 不重试（重试无意义）。"""
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503, 502, 500) and i < retries - 1:
                back = 5 * (2 ** i)
                print(f"[检索] HTTP {e.code} 限流，{back}s 后重试（{i+1}/{retries-1}）", flush=True)
                time.sleep(back)
                continue
            raise
        except Exception as e:  # 网络抖动也重试
            last = e
            if i < retries - 1:
                time.sleep(3 * (2 ** i))
                continue
            raise
    raise last


def _arxiv_search(query: str, max_results: int = 10) -> list:
    """arXiv API 检索（Atom XML 解析）"""
    _throttle()
    params = urllib.parse.urlencode({
        "search_query": f"all:{query}",
        "start": 0, "max_results": max_results,
        "sortBy": "relevance", "sortOrder": "descending",
    })
    xml_data = _http_get(f"{ARXIV_URL}?{params}", timeout=30)
    root = ET.fromstring(xml_data)
    papers = []
    for entry in root.findall("atom:entry", NS):
        title = " ".join((entry.findtext("atom:title", "", NS) or "").split())
        summary = " ".join((entry.findtext("atom:summary", "", NS) or "").split())
        # 链接（arxiv 详情页）
        url = ""
        for link in entry.findall("atom:link", NS):
            if link.get("rel") == "alternate":
                url = link.get("href", "")
                break
        # 作者
        authors = [a.findtext("atom:name", "", NS) for a in entry.findall("atom:author", NS)]
        # 日期
        published = entry.findtext("atom:published", "", NS)
        date = published[:10] if published else ""
        papers.append({
            "title": title,
            "authors": authors,
            "source": "arXiv",
            "date": date,
            "cite": "",
            "url": url,
            "abstract": summary[:500],
            "arxiv_id": url.rstrip("/").split("/")[-1] if url else "",
        })
    return papers


def _ss_search(query: str, max_results: int = 10) -> list:
    """Semantic Scholar 检索（补充源，可能 429）"""
    params = urllib.parse.urlencode({
        "query": query, "limit": max_results,
        "fields": "title,authors,year,venue,abstract,citationCount,externalIds",
    })
    data = json.loads(_http_get(f"{SS_URL}?{params}", timeout=20, retries=2))
    papers = []
    for p in data.get("data", []):
        ext = p.get("externalIds") or {}
        doi = ext.get("DOI", "")
        papers.append({
            "title": p.get("title", ""),
            "authors": [a.get("name", "") for a in (p.get("authors") or [])],
            "source": p.get("venue") or "Semantic Scholar",
            "date": str(p.get("year", "")),
            "cite": str(p.get("citationCount", "")),
            "url": f"https://doi.org/{doi}" if doi else "",
            "abstract": p.get("abstract") or "",
        })
    return papers


def _openalex_search(query: str, max_results: int = 10) -> list:
    """OpenAlex 兜底源（无需 key、限流宽松）。
    仅在 arXiv + SS 都拿不到结果时启用，作为可用性保险。"""
    params = urllib.parse.urlencode({
        "search": query, "per-page": max_results,
        "select": "id,doi,title,publication_year,authorships,primary_location,abstract_inverted_index,cited_by_count",
        "mailto": "qunzhi@example.com",
    })
    data = json.loads(_http_get(f"{OPENALEX_URL}?{params}", timeout=25, retries=3))
    papers = []
    for w in data.get("results", []):
        # 摘要以倒排索引形式返回，这里还原成正常词序
        inv = w.get("abstract_inverted_index") or {}
        abstract = ""
        if inv:
            pos = {}
            for word, idxs in inv.items():
                for i in idxs:
                    pos[i] = word
            abstract = " ".join(pos[k] for k in sorted(pos))[:500]
        loc = w.get("primary_location") or {}
        src = (loc.get("source") or {}).get("display_name") or "OpenAlex"
        papers.append({
            "title": (w.get("title") or "").strip(),
            "authors": [a.get("author", {}).get("display_name", "")
                        for a in (w.get("authorships") or [])],
            "source": src,
            "date": str(w.get("publication_year") or ""),
            "cite": str(w.get("cited_by_count") or ""),
            "url": w.get("doi") or w.get("id") or "",
            "abstract": abstract,
        })
    return papers


# 中文技术关键词 → 英文查询词
TECH_ZH2EN = {
    "分布式缓存": "distributed cache", "缓存系统": "cache system",
    "高可用": "high availability", "低延迟": "low latency",
    "矩阵乘法": "matrix multiplication", "并行计算": "parallel computing",
    "嵌入式": "embedded", "内存管理": "memory management",
    "垃圾回收": "garbage collection", "去中心化": "decentralized",
    "身份认证": "identity authentication", "认证协议": "authentication protocol",
    "深度神经网络": "deep neural network", "神经网络": "neural network",
    "推理效率": "inference efficiency", "大模型": "large language model",
    "模型优化": "model optimization", "数据库": "database",
    "分布式系统": "distributed system", "一致性": "consistency",
    "性能优化": "performance optimization", "负载均衡": "load balancing",
    "并发": "concurrency", "故障恢复": "fault recovery",
    "安全": "security", "隐私": "privacy", "加密": "encryption",
    "调度": "scheduling", "任务调度": "task scheduling",
    "推荐系统": "recommendation system", "图神经网络": "graph neural network",
    "强化学习": "reinforcement learning", "自然语言处理": "natural language processing",
    "计算机视觉": "computer vision", "优化": "optimization",
    "设计": "design", "高效": "efficient", "大规模": "large-scale",
    "算法": "algorithm", "模型": "model", "系统": "system",
    "如何": "", "一个": "", "的": "", "与": "", "和": "", "实现": "",
}


def zh_to_en_query(topic: str) -> str:
    """中文技术课题 → 英文查询（关键词映射拼接）"""
    t = topic.lower()
    terms = []
    for zh, en in TECH_ZH2EN.items():
        if zh in t and en and en not in terms:
            terms.append(en)
    if terms:
        return " ".join(terms)
    return topic  # 已是英文或无法映射，原样


def tech_search(query: str, max_results: int = 10) -> dict:
    """技术类课题检索：arXiv 主源 + SS 补充 + OpenAlex 兜底
    query 为中文时自动翻译成英文查询
    返回 {ok, keyword, count, papers, source}
    """
    # 中文 → 英文查询
    en_query = zh_to_en_query(query)
    if en_query != query:
        print(f"[技术] 中文查询「{query[:20]}」→「{en_query[:40]}」", flush=True)

    papers = []
    srcs = []
    # 1. arXiv（主源）
    try:
        papers = _arxiv_search(en_query, max_results)
        print(f"[arXiv] 命中 {len(papers)} 篇", flush=True)
        if papers:
            srcs.append("arxiv")
    except Exception as e:
        print(f"[arXiv] 失败: {e}", flush=True)

    # 2. SS 补充（按标题去重）
    if len(papers) < max_results:
        try:
            ss_papers = _ss_search(en_query, max_results)
            known_titles = {p["title"][:20] for p in papers}
            for p in ss_papers:
                if p["title"][:20] not in known_titles:
                    papers.append(p)
                    if len(papers) >= max_results:
                        break
            print(f"[SS] 补充 {len(ss_papers)} 篇", flush=True)
            if ss_papers:
                srcs.append("ss")
        except Exception as e:
            print(f"[SS] 跳过（{e}）", flush=True)

    # 3. OpenAlex 兜底：前两个源都被限流/无结果时才用，保证这条路永远有产出
    if not papers:
        try:
            papers = _openalex_search(en_query, max_results)
            print(f"[OpenAlex] 兜底命中 {len(papers)} 篇", flush=True)
            if papers:
                srcs.append("openalex")
        except Exception as e:
            print(f"[OpenAlex] 跳过（{e}）", flush=True)

    if not papers:
        return {"ok": False, "keyword": query, "error": "技术源无结果", "papers": []}
    return {"ok": True, "keyword": query, "count": len(papers),
            "papers": papers[:max_results], "source": "+".join(srcs)}


if __name__ == "__main__":
    r = tech_search("distributed cache system", max_results=5)
    print(f"ok={r.get('ok')} n={r.get('count')} source={r.get('source')}")
    for p in r.get("papers", [])[:3]:
        print(f"  - {p['title'][:50]} | {p['source'][:20]} | {p['date'][:4]} | 摘要:{p['abstract'][:40]}")
