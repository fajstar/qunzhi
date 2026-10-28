# -*- coding: utf-8 -*-
"""
群智主教练知识库 knowledge_base.py
==================================
混合检索 RAG：向量(Chroma) + BM25 → RRF 融合 → 交叉编码器重排。
缓存优先：课题先查库，命中直接返回；未命中实时搜索入库。

依赖：chromadb, rank_bm25, jieba, sentence-transformers
"""
import os, sys, re, json, time, hashlib, threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE, "kb_data")
os.makedirs(DB_DIR, exist_ok=True)

# HuggingFace 镜像（国内直连常超时）+ 缓存指向工作区（沙箱可写）
if not os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
if not os.environ.get("HF_HOME"):
    os.environ["HF_HOME"] = os.path.join(BASE, "hf_cache")
os.makedirs(os.environ["HF_HOME"], exist_ok=True)

import chromadb
from rank_bm25 import BM25Okapi
import jieba
import numpy as np

# ---------------- 嵌入与重排模型（懒加载） ----------------
_embed_model = None
_rerank_model = None
_model_lock = threading.Lock()

# 优先本地模型（ModelScope 下载），否则 HF 名称
LOCAL_MODEL_DIR = os.path.join(BASE, "models")
EMBED_MODEL = os.path.join(LOCAL_MODEL_DIR, "bge-small-zh-v1.5") if os.path.exists(
    os.path.join(LOCAL_MODEL_DIR, "bge-small-zh-v1.5")) else "BAAI/bge-small-zh-v1.5"
RERANK_MODEL = os.path.join(LOCAL_MODEL_DIR, "bge-reranker-base") if os.path.exists(
    os.path.join(LOCAL_MODEL_DIR, "bge-reranker-base")) else "BAAI/bge-reranker-base"


def _get_embedder():
    global _embed_model
    if _embed_model is None:
        with _model_lock:
            if _embed_model is None:
                from sentence_transformers import SentenceTransformer
                _embed_model = SentenceTransformer(EMBED_MODEL)
    return _embed_model


def _get_reranker():
    global _rerank_model
    if _rerank_model is None:
        with _model_lock:
            if _rerank_model is None:
                from sentence_transformers import CrossEncoder
                _rerank_model = CrossEncoder(RERANK_MODEL)
    return _rerank_model


# ---------------- 存储 ----------------
_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        with _model_lock:
            if _collection is None:
                _client = chromadb.PersistentClient(path=DB_DIR)
                _collection = _client.get_or_create_collection(
                    name="cnki_papers", metadata={"hnsw:space": "cosine"})
    return _collection


# 题录内存缓存（标题级去重）
_meta_cache = {}
_meta_lock = threading.Lock()


def _doc_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def _tokenize(text: str) -> list:
    return [t for t in jieba.lcut(text) if t.strip() and len(t.strip()) > 1]


# ---------------- 入库质量筛选 ----------------

# 报纸/新闻类来源（非学术论文，信息密度低）
NEWS_SOURCES = [
    "中国青年报", "中国科学报", "21世纪经济报道", "人民日报", "光明日报",
    "经济日报", "科技日报", "中国教育报", "北京晚报", "南方都市报",
]
# 疑似水刊/低质来源关键词（V15.5 扩充：教育类与工程类常见水刊）
LOW_QUALITY_SOURCES = [
    # 综合类水刊
    "工业出版社", "电子技术", "才智", "现代商贸工业", "科技资讯", "科技风",
    "科技经济导刊", "中国市场", "经济研究导刊", "纳税", "营销界",
    # 教育与传播类
    "传播力研究", "视界观", "魅力中国", "读写算", "考试周刊", "课程教育研究",
    "教育教学论坛", "教育界", "新课程", "文教资料", "科教文汇", "智库时代",
    "知识文库", "新一代", "成长", "中华少年", "赢未来",
    # 工程与技术类
    "汽车实用技术", "汽车文摘", "上海汽车", "中国新通信", "数码世界",
    "信息记录材料", "写真地理", "电脑迷", "无线互联科技", "通讯世界",
    "数字通信世界", "中国设备工程", "设备管理与维修",
]
# 注水标题特征（泛泛无实质）
WATER_TITLE_PATTERNS = [
    r"^关于.{0,8}的(几点)?思考$",
    r"^浅谈|^浅析|^试论|^刍议",
    r"^对.{0,10}的(认识|体会|感悟)",
    r"^新形势下",
    r"的(探讨|研究|分析|综述)($|——)",
]
import os, re, json, time
import datetime as _dt

# 时效性门限：默认只收最近 3 年（可用环境变量 MAX_AGE_YEARS 覆盖）
_MAX_AGE_YEARS = int(os.environ.get("MAX_AGE_YEARS", "3"))
MIN_YEAR = _dt.datetime.now().year - _MAX_AGE_YEARS   # 太老的论文时效性差
MIN_ABSTRACT_LEN = 30  # 摘要太短信息量不足
# 注：摘要长是内容充实的表现（学位论文几百字正常），不做上限限制


def _is_quality_ok(p: dict) -> tuple:
    """入库质量筛选，返回 (是否通过, 拒绝原因)
    拒绝：新闻来源 / 疑似水刊 / 注水标题 / 过老 / 无摘要或摘要过短
    """
    title = (p.get("title") or "").strip()
    source = (p.get("source") or "").strip()
    abstract = (p.get("abstract") or "").strip()
    date = str(p.get("date") or "")

    # 1. 新闻类来源直接拒
    for ns in NEWS_SOURCES:
        if ns in source:
            return False, f"新闻来源({source})"
    # 2. 疑似水刊
    for ls in LOW_QUALITY_SOURCES:
        if ls in source:
            return False, f"疑似低质来源({source})"
    # 3. 注水标题
    for pat in WATER_TITLE_PATTERNS:
        if re.search(pat, title):
            return False, f"注水标题「{title[:30]}」"
    # 4. 标题太短且无实质（<8字 多为垃圾）
    if len(title) < 8:
        return False, f"标题过短「{title}」"
    # 5. 时效性
    if date:
        m = re.match(r"(\d{4})", date)
        if m and int(m.group(1)) < MIN_YEAR:
            return False, f"年份过老({date[:4]})"
    # 6. 摘要质量（知网题录模式下无摘要的允许通过，但降权标记）
    if abstract and len(abstract) < MIN_ABSTRACT_LEN:
        return False, f"摘要过短({len(abstract)}字)"
    return True, ""


# ---------------- 入库 ----------------
def add_papers(papers: list, quality_filter: bool = True) -> int:
    """论文入库：{title, authors, source, date, cite, url, abstract?}
    标题 + 作者 + 来源 + 摘要 → 索引文本
    quality_filter=True 时执行质量筛选（拒新闻/水刊/注水/过老/摘要太短）
    """
    if not papers:
        return 0
    col = _get_collection()
    embedder = _get_embedder()

    ids, docs, metas = [], [], []
    added = 0
    rejected = 0
    for p in papers:
        did = _doc_id(p.get("url", ""))
        # 去重
        existing = col.get(ids=[did])
        if existing and existing.get("ids"):
            continue
        # 质量筛选
        if quality_filter:
            ok, reason = _is_quality_ok(p)
            if not ok:
                rejected += 1
                print(f"  [筛除] {reason} | {str(p.get('title'))[:30]}", flush=True)
                continue
        title = p.get("title", "")
        abstract = p.get("abstract") or ""
        # 检索索引文本：只含 标题+摘要（作者/来源不进匹配，避免稀释 BM25/向量分数）
        text = f"{title}。{abstract}"
        if len(text.strip()) < 10:
            rejected += 1
            continue
        ids.append(did)
        docs.append(text)
        # 来源类型标签（用于检索过滤）
        src = p.get("source", "") or ""
        doc_type = "arxiv" if "arXiv" in src else "cnki"
        metas.append({
            "title": title,
            "doc_type": doc_type,
            "authors": "、".join(p.get("authors", [])),
            "source": p.get("source", ""),
            "date": str(p.get("date", ""))[:10],
            "cite": p.get("cite", ""),
            "url": p.get("url", ""),
            "abstract": abstract,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        with _meta_lock:
            _meta_cache[did] = metas[-1]
        added += 1

    if ids:
        vecs = embedder.encode(docs, normalize_embeddings=True).tolist()
        col.add(ids=ids, documents=docs, embeddings=vecs,
                metadatas=metas)
    if rejected:
        print(f"  [筛选] 入库 {added} 篇，筛除 {rejected} 篇低质/注水", flush=True)
    return added


# ---------------- 检索 ----------------
def _bm25_search(query: str, docs: list, k: int = 50) -> list:
    """BM25 词面检索（对库内全部文档打分）"""
    if not docs:
        return []
    corpus = [_tokenize(d) for d in docs]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(_tokenize(query))
    ranked = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
    return [(docs[i], scores[i]) for i in ranked[:k] if scores[i] > 0]


def _vector_search(query: str, k: int = 50) -> list:
    """向量语义检索"""
    col = _get_collection()
    embedder = _get_embedder()
    qv = embedder.encode([query], normalize_embeddings=True).tolist()[0]
    res = col.query(query_embeddings=[qv], n_results=min(k, 200))
    items = []
    if res and res.get("ids") and res["ids"][0]:
        for i in range(len(res["ids"][0])):
            items.append((res["documents"][0][i], res["distances"][0][i]))
    return items


def _rrf(ranked_lists: list, k: int = 60) -> list:
    """RRF 融合：1/(k+rank) 累加"""
    scores = {}
    for lst in ranked_lists:
        for rank, (doc, _score) in enumerate(lst):
            scores[doc] = scores.get(doc, 0) + 1.0 / (k + rank + 1)
    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in merged]


def _rerank(query: str, docs: list, top_k: int = 8) -> list:
    """交叉编码器重排"""
    if not docs:
        return []
    reranker = _get_reranker()
    pairs = [[query, d] for d in docs]
    scores = reranker.predict(pairs)
    order = np.argsort(-scores)
    return [docs[i] for i in order[:top_k]]


def search(query: str, top_k: int = 8, use_rerank: bool = True,
           doc_type: str = None) -> dict:
    """混合检索：向量 + BM25 → RRF → 交叉编码器重排
    doc_type: 按来源类型过滤（"cnki" / "arxiv" / None=全部）
    返回 {ok, query, count, hits: [{meta...}]}
    """
    col = _get_collection()
    total = col.count()
    if total == 0:
        return {"ok": False, "query": query, "count": 0, "hits": [], "total": 0}

    # 拉全量文档（库小，直接全量；大了再改采样）
    all_data = col.get(include=["documents", "metadatas"])
    docs = all_data.get("documents", [])
    metas = all_data.get("metadatas", [])

    # 按 doc_type 过滤
    if doc_type:
        keep_idx = [i for i, m in enumerate(metas) if m.get("doc_type") == doc_type]
        if keep_idx:
            docs = [docs[i] for i in keep_idx]
            metas = [metas[i] for i in keep_idx]
        else:
            return {"ok": False, "query": query, "count": 0, "hits": [], "total": total}

    vec_hits = _vector_search(query, 50)
    bm_hits = _bm25_search(query, docs, 50)
    merged = _rrf([vec_hits, bm_hits])

    if use_rerank and merged:
        merged = _rerank(query, merged, top_k)

    # 映射回元数据
    doc2meta = {}
    for d, m in zip(docs, metas):
        doc2meta[d] = m
    hits = []
    for d in merged[:top_k]:
        m = doc2meta.get(d)
        if m:
            hits.append(m)
    return {"ok": bool(hits), "query": query, "count": len(hits),
            "hits": hits, "total": total}


def get_stats() -> dict:
    col = _get_collection()
    return {"total_papers": col.count(), "db_dir": DB_DIR}


if __name__ == "__main__":
    # 自测：入库 2 篇假论文 + 检索
    demo = [
        {"title": "生成式人工智能在高校作业中的伦理规制", "authors": ["张三"], "source": "高教研究",
         "date": "2024-03-01", "cite": "12", "url": "https://demo/1",
         "abstract": "随着ChatGPT等生成式人工智能的普及，高校作业中的学术诚信问题日益突出。本文提出过程性评价与AI辅助声明制度相结合的治理路径。"},
        {"title": "分布式缓存系统设计与实践", "authors": ["李四"], "source": "计算机学报",
         "date": "2023-06-01", "cite": "8", "url": "https://demo/2",
         "abstract": "针对高并发场景下的缓存一致性问题，本文比较了Redis Cluster与Memcached的架构差异，提出延迟双删策略的改进方案。"},
    ]
    n = add_papers(demo)
    print(f"入库 {n} 篇")
    for q in ["大学生用AI写作业怎么治理", "缓存系统"]:
        r = search(q, top_k=2)
        print(f"\n查询[{q}] count={r['count']} total={r['total']}")
        for h in r["hits"]:
            print("  -", h["title"][:40], "|", h["source"], "|", h["abstract"][:40] if h.get("abstract") else "")
