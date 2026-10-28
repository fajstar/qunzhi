# -*- coding: utf-8 -*-
"""
检索来源路由 tech_router.py
============================
零依赖模块：不导入 chromadb / 向量库，任何环境都能用。
被 coach_search（主教练检索）与 rag_pipeline（RAG 流水线）共同引用。

V15.5：技术类关键词表扩充至覆盖理工/工程领域，
原表过窄导致"锂电池 SOC 估算"这类科研问题被误判为教育类。
"""

# 技术类关键词（命中即视为技术/理工类问题）
TECH_KEYWORDS = [
    # 计算机/软件
    "分布式", "缓存", "并行", "矩阵", "嵌入式", "内存", "垃圾回收",
    "去中心化", "身份认证", "神经网络", "深度学习", "推理", "大模型",
    "算法", "系统", "软件", "网络", "数据库", "架构", "协议",
    "cache", "distributed", "algorithm", "GPU", "CPU", "并发",
    # 理工/工程领域
    "电池", "储能", "能源", "光伏", "材料", "化学", "物理", "生物",
    "医药", "传感", "控制", "信号", "机械", "航空", "航天", "土木",
    "环境", "催化", "合金", "纳米", "芯片", "半导体", "通信", "雷达",
    "图像", "视觉", "机器人", "电机", "电网", "热力学", "流体",
    "battery", "energy", "material", "sensor", "control", "signal",
    "photovoltaic", "catalyst", "semiconductor", "nanoparticle", "robot",
    "circuit", "thermal", "mechanical", "chemical", "optical",
]


def route_source(topic: str) -> str:
    """来源路由：技术类 → arxiv（配 OpenAlex 补充）；其他 → cnki（知网/博查）"""
    t = (topic or "").lower()
    if any(k in t for k in TECH_KEYWORDS):
        return "arxiv"
    return "cnki"


def is_tech_topic(topic: str) -> bool:
    return route_source(topic) == "arxiv"


if __name__ == "__main__":
    for q in ["低温环境下锂电池SOC估算误差超8%", "如何提升师范生课堂参与度？",
              "分布式缓存系统设计", "如何设计一门选修课大纲"]:
        print(f"  {route_source(q):6} <- {q}")
