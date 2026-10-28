# -*- coding: utf-8 -*-
"""
群智主教练检索编排器 rag_pipeline.py (v2)
=========================================
完整流水线：
  课题 → ①查库(缓存优先) → 命中直接返回
       → ②未命中 → 来源路由：
           教育/教研类 → 知网(题录+摘要)
           技术类     → arXiv + Semantic Scholar（开放API）
           其他       → 博查（外部）
       → ③入库(带质量筛选) → ④混合检索(向量+BM25+RRF+重排) → 返回
"""
import sys, os, time, json

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, ".."))               # app 目录
sys.path.insert(0, os.path.join(BASE, "..", "..", "cnki_probe"))

import knowledge_base as kb
import abstract_fetcher as af

try:
    import cnki_search as cks
    CNKI_OK = True
except ImportError:
    CNKI_OK = False

try:
    import arxiv_search as axs
    ARXIV_OK = True
except ImportError:
    ARXIV_OK = False

try:
    import openalex_search as oas
    OPENALEX_OK = True
except ImportError:
    OPENALEX_OK = False


# ---------------- 来源路由 ----------------

# 技术类关键词与路由统一由零依赖的 tech_router 提供（V15.5）
sys.path.insert(0, os.path.join(BASE, ".."))
try:
    from tech_router import TECH_KEYWORDS, route_source  # noqa: F401
except ImportError:
    # 兜底：tech_router 不可用时用最小关键词表
    TECH_KEYWORDS = ["分布式", "缓存", "算法", "系统", "电池", "材料"]

    def route_source(topic: str) -> str:
        t = (topic or "").lower()
        return "arxiv" if any(k in t for k in TECH_KEYWORDS) else "cnki"


# ---------------- 主入口 ----------------
def search_topic_with_rag(topic: str, top_k: int = 8,
                          use_rerank: bool = True,
                          fetch_abstracts: bool = True) -> dict:
    """主教练检索入口：缓存优先 → 来源路由 → 搜索 → 入库 → 混合检索"""
    t0 = time.time()
    route = route_source(topic)
    filter_type = "arxiv" if route == "arxiv" else "cnki"

    # ① 缓存优先（按来源类型过滤，避免跨领域误命中）
    cached = kb.search(topic, top_k=top_k, use_rerank=use_rerank, doc_type=filter_type)
    if cached.get("ok"):
        return {
            "ok": True,
            "topic": topic,
            "source": "cache",
            "count": cached["count"],
            "total_in_kb": cached["total"],
            "hits": cached["hits"],
            "time_ms": int((time.time() - t0) * 1000),
            "cached": True,
        }

    # ② 未命中：来源路由
    papers = None
    err = None

    if route == "arxiv" and ARXIV_OK:
        # 技术类：arXiv（中文自动翻译英文）
        ar = axs.tech_search(topic, max_results=10)
        if ar.get("ok"):
            papers = ar.get("papers", [])
        else:
            err = ar.get("error")
    else:
        if CNKI_OK:
            cr = cks.cnki_search(topic, max_results=10)
            if cr.get("ok"):
                papers = cr.get("papers", [])
                # 抓摘要（复用检索会话）
                if fetch_abstracts and papers:
                    urls = [p["url"] for p in papers[:8] if p.get("url")]
                    abs_map = af.fetch_abstracts(urls, max_workers=3,
                                                 session=cr.get("session"),
                                                 cookies=cr.get("cookies"))
                    for p in papers:
                        p["abstract"] = abs_map.get(p.get("url"))
            else:
                err = cr.get("error")
        else:
            err = "知网不可用（未配置 cookies/cnki.json）"

    # ②b 兜底：主源无结果 → OpenAlex（免费无 key，覆盖 2.5 亿文献，含中英文+引用数）
    if not papers and OPENALEX_OK:
        try:
            oa = oas.openalex_search(topic, max_results=10)
            if oa.get("ok") and oa.get("papers"):
                papers = oa["papers"]
                route = "openalex"
                err = None
                print(f"[RAG] 主源无结果 → OpenAlex 兜底命中 {len(papers)} 篇", flush=True)
        except Exception as e:
            print(f"[RAG] OpenAlex 兜底失败: {str(e)[:80]}", flush=True)

    # ②c 最终兜底：博查网络搜索（由 coach_search 层处理，此处仅标记）
    if not papers:
        return {"ok": False, "topic": topic, "error": err or f"{route} 源无结果",
                "hits": [], "time_ms": int((time.time()-t0)*1000)}

    # ③ 入库（带质量筛选）
    added = kb.add_papers(papers, quality_filter=True)
    print(f"[RAG] 来源={route} 检索{len(papers)}篇 入库{added}篇", flush=True)

    # OpenAlex 文献在知识库中归入学术池（doc_type=cnki），检索过滤需对应
    if route == "openalex":
        filter_type = "cnki"

    # ④ 混合检索（按来源类型过滤）
    result = kb.search(topic, top_k=top_k, use_rerank=use_rerank, doc_type=filter_type)
    result["time_ms"] = int((time.time() - t0) * 1000)
    result["cached"] = False
    result["added"] = added
    result["source"] = route
    result["topic"] = topic
    return result


if __name__ == "__main__":
    import sys as _s
    topic = _s.argv[1] if len(_s.argv) > 1 else "如何设计一个高可用、低延迟的分布式缓存系统"
    print(f"=== RAG 流水线测试: {topic} ===")
    r = search_topic_with_rag(topic, top_k=5)
    print(f"ok={r.get('ok')} source={r.get('source')} count={r.get('count')} time={r.get('time_ms')}ms")
    if r.get("ok"):
        for h in r["hits"]:
            print(f"  - {h['title'][:45]} | {h['source'][:20]} | 摘要:{str(h.get('abstract'))[:35]}")
        # 二次查询验证缓存
        print("\n=== 二次查询（缓存）===")
        r2 = search_topic_with_rag(topic, top_k=5)
        print(f"source={r2.get('source')} count={r2.get('count')} cached={r2.get('cached')} time={r2.get('time_ms')}ms")
