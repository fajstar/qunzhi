本地 RAG 模型目录（提交版不含，约1.2GB）：
- bge-small-zh-v1.5/   嵌入模型（~100MB）
- bge-reranker-base/   交叉编码器重排（~1.1GB）

缺失时系统自动降级：RAG 不可用 → 退回纯知网/博查检索（功能不中断）。
如需完整 RAG：从 hf-mirror.com 下载 BAAI/bge-small-zh-v1.5 和 BAAI/bge-reranker-base 放入本目录。
