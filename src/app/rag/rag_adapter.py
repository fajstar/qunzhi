# -*- coding: utf-8 -*-
"""
RAG 结果 → coach_search info_pool 适配器
========================================
把 knowledge_base 命中结果转成主教练注入格式。

V15 优化（2026-09-25）：**保留结构化字段**。
旧实现把作者/来源/发表时间/摘要拼成一个 content 字符串，导致下游无法生成参考文献表、
玩家也不知道自己在引哪一篇。现在这些字段各自保留，content 仅作展示用文本。
"""
import os
import sys

# 同级模块（knowledge_base 等）在本目录下，按需加载，不硬编码任何绝对路径
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def rag_hits_to_info_pool(hits: list, wrap_fn=None, sanitize_fn=None) -> list:
    """hits: knowledge_base 返回的 meta 列表
    wrap_fn/sanitize_fn: 复用 coach_search 的净化包裹函数（可空）

    返回条目结构（V15 起为结构化）：
      {title, authors, source, year, cite, doi, url, abstract, content, wrapped, source_type}
    """
    pool = []
    for h in hits:
        title = h.get("title", "") or ""
        authors = h.get("authors", "") or ""
        source = h.get("source", "") or ""
        date = h.get("date") or h.get("year") or ""
        cite = h.get("cite") or h.get("citations") or h.get("citationCount") or ""
        doi = h.get("doi") or ""
        abstract = h.get("abstract") or ""

        # content 仍保留一份人类可读的拼接文本（下游净化/包裹/展示都用它）
        parts = [title]
        if authors:
            parts.append(u"作者：" + str(authors))
        if source:
            parts.append(u"来源：" + str(source))
        if date:
            parts.append(u"发表时间：" + str(date)[:10])
        if abstract:
            parts.append(u"摘要：" + str(abstract)[:300])
        content = u"；".join(parts)
        if sanitize_fn:
            content = sanitize_fn(content)
        url = h.get("url", "") or ""
        wrapped = wrap_fn(content, url) if wrap_fn else content

        pool.append({
            "title": title,
            "authors": str(authors),
            "source": str(source),
            "year": str(date)[:4],
            "cite": str(cite),
            "doi": str(doi),
            "url": url,
            "abstract": str(abstract),
            "content": content,
            "wrapped": wrapped,
            "source_type": "cnki_rag",
        })
    return pool


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    demo = [
        {"title": u"生成式人工智能在高校作业中的伦理规制", "authors": u"张三",
         "source": u"高教研究", "date": u"2024-03-01", "cite": "12", "url": "https://x/1",
         "abstract": u"提出过程性评价与AI辅助声明制度相结合的治理路径。"},
    ]
    pool = rag_hits_to_info_pool(demo)
    print(pool[0]["content"])
    print("---")
    print({k: pool[0][k] for k in ("title", "authors", "source", "year", "cite")})
