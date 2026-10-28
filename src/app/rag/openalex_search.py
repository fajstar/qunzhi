# -*- coding: utf-8 -*-
"""
OpenAlex 学术检索模块 openalex_search.py
=========================================
OpenAlex：全球开放学术图谱，2.5 亿+ 文献，**免费、无需 API key**。
优势（相对知网/arXiv）：
  - 覆盖全球期刊/会议/预印本（含中文文献的英文元数据）
  - 原生提供**引用数**（cited_by_count），可按影响力排序
  - 无频率限制（建议带 mailto 礼貌标识）
返回格式与 arxiv_search / cnki_search 保持一致，便于 RAG 流水线统一处理。
"""
import sys, os, json, time, urllib.parse, urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OPENALEX_URL = "https://api.openalex.org/works"
# 礼貌标识（OpenAlex 建议提供邮箱以获得更稳定的服务）
MAILTO = os.environ.get("OPENALEX_MAILTO", "qunzhi@example.edu.cn")

_last_call = [0.0]

def _throttle(min_interval: float = 0.2):
    """温和限速，避免被判定滥用"""
    dt = time.time() - _last_call[0]
    if dt < min_interval:
        time.sleep(min_interval - dt)
    _last_call[0] = time.time()


def _restore_abstract(inv_index: dict) -> str:
    """OpenAlex 用倒排索引存摘要：{word: [pos,...]} → 还原成文本"""
    if not inv_index or not isinstance(inv_index, dict):
        return ""
    pairs = []
    for word, positions in inv_index.items():
        if isinstance(positions, list):
            for p in positions:
                pairs.append((p, word))
    pairs.sort(key=lambda x: x[0])
    text = " ".join(w for _, w in pairs)
    return " ".join(text.split())


def _reconstruct_url(work: dict) -> str:
    """优先 DOI，其次 OpenAlex 页面，再次期刊页"""
    doi = work.get("doi")
    if doi:
        return doi if doi.startswith("http") else f"https://doi.org/{doi}"
    oa = work.get("open_access") or {}
    if oa.get("oa_url"):
        return oa["oa_url"]
    primary = (work.get("primary_location") or {}).get("landing_page_url")
    if primary:
        return primary
    return work.get("id", "")


def openalex_search(query: str, max_results: int = 10,
                    from_year: int = None, sort_by_cited: bool = True) -> dict:
    """OpenAlex 检索。

    query: 中文/英文均可（OpenAlex 支持中英文检索）
    from_year: 只取该年及以后（如 2022）
    sort_by_cited: 按引用数降序（默认 True，便于识别高影响力工作）
    返回 {ok, keyword, count, papers, source}
    """
    params = {
        "search": query,
        "per-page": str(min(max(max_results, 1), 25)),
        "mailto": MAILTO,
    }
    if from_year:
        params["filter"] = f"from_publication_date:{from_year}-01-01"
    if sort_by_cited:
        params["sort"] = "cited_by_count:desc"

    url = f"{OPENALEX_URL}?{urllib.parse.urlencode(params)}"
    try:
        _throttle()
        req = urllib.request.Request(url, headers={
            "User-Agent": f"QunZhi/1.0 (mailto:{MAILTO})",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "keyword": query, "error": f"OpenAlex 请求失败: {str(e)[:120]}", "papers": []}

    results = data.get("results") or []
    papers = []
    for w in results:
        title = (w.get("title") or "").strip()
        if not title:
            continue
        # 作者（取前 5 位）
        authors = []
        for a in (w.get("authorships") or [])[:5]:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                authors.append(name)
        # 载体（期刊/会议）
        host = ((w.get("primary_location") or {}).get("source") or {})
        venue = host.get("display_name") or ""
        year = w.get("publication_year") or ""
        cited = w.get("cited_by_count") or 0
        abstract = _restore_abstract(w.get("abstract_inverted_index"))
        papers.append({
            "title": title,
            "authors": authors,
            "source": f"OpenAlex{'·' + venue if venue else ''}",
            "venue": venue,
            "date": str(year),
            "year": year,
            "cite": str(cited),
            "cited_by_count": cited,
            "url": _reconstruct_url(w),
            "abstract": abstract[:800],
            "openalex_id": (w.get("id") or "").rstrip("/").split("/")[-1],
            "type": w.get("type", ""),
        })

    if not papers:
        return {"ok": False, "keyword": query, "error": "OpenAlex 无结果", "papers": []}
    return {"ok": True, "keyword": query, "count": len(papers),
            "total_available": (data.get("meta") or {}).get("count"),
            "papers": papers, "source": "openalex"}


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "低温锂电池 SOC 估算"
    r = openalex_search(q, max_results=5)
    print(f"ok={r.get('ok')} count={r.get('count')} total={r.get('total_available')}")
    for p in r.get("papers", []):
        line = f"  - {p['title'][:60]} | {p['date']} | 引用{p['cite']} | {p['venue'][:20]}"
        print(line)
        if p.get("abstract"):
            print(f"      摘要: {p['abstract'][:70]}...")
