# -*- coding: utf-8 -*-
"""
群智 V15 · 预设教研课题库 + 场景化决策报告
========================================
A（师范教研场景）落地：内置教研课题，把通用报告重塑为"决策建议书"。

场景化报告将通用讨论报告转译为评委/用户能直接带走的产品形态：
  - 教研场景：输出"教研决策建议书"（方案/盲区/风险/行动）
  - 科研场景：输出"科研决策预演报告"
  - 通用：原样
"""
import json

# ==================== 预设教研课题库（A 场景） ====================

PRESET_TOPICS = [
    {
        "id": "p1",
        "title": "大学生使用AI写作业，学校该禁止还是引导？",
        "scenario": "教研",
        "type": "教改决策",
        "description": "AI工具普及引发作业诚信争议，需要制定合理的教学管理策略",
        "user_thought": "应该引导而非禁止，但不知道怎么落地",
        "tags": ["AI教育", "教学管理", "政策制定"],
    },
    {
        "id": "p2",
        "title": "如何提升师范生课堂参与度？",
        "scenario": "教研",
        "type": "教学改进",
        "description": "师范生课堂普遍存在参与度低、互动少的问题",
        "user_thought": "小组讨论效果不稳定，需要更系统的方法",
        "tags": ["师范教育", "课堂教学", "互动设计"],
    },
    {
        "id": "p3",
        "title": "混合式教学（线上+线下）如何设计才能保证学习效果？",
        "scenario": "教研",
        "type": "教学改革",
        "description": "线上线下混合教学已成常态，但效果参差不齐",
        "user_thought": "翻转课堂模式可能适合，但担心学生自主学习能力不足",
        "tags": ["混合式教学", "教学设计", "学习效果"],
    },
    {
        "id": "p4",
        "title": "师范生教育实习如何有效衔接理论与实操？",
        "scenario": "教研",
        "type": "培养方案",
        "description": "教育实习是师范生培养关键环节，但理论与实践常脱节",
        "user_thought": "需要更系统的实习指导体系",
        "tags": ["师范教育", "教育实习", "培养方案"],
    },
    {
        "id": "p5",
        "title": "学生心理健康筛查与干预体系如何构建？",
        "scenario": "教研",
        "type": "校园治理",
        "description": "大学生心理健康问题日益突出，需要科学干预体系",
        "user_thought": "筛查容易，干预落地难，缺乏专业人力",
        "tags": ["心理健康", "校园治理", "干预体系"],
    },
    {
        "id": "p6",
        "title": "科研课题申报中，如何提高课题的立项竞争力？",
        "scenario": "科研",
        "type": "科研决策",
        "description": "课题申报竞争激烈，需要打磨选题与论证逻辑",
        "user_thought": "选题新颖但论证不够扎实",
        "tags": ["科研", "课题申报", "论证"],
    },
]

# ==================== 场景化报告生成 ====================

def get_scenario_report(scenario: str, topic: str, report: str,
                        evaluation: dict = None, coach_audit: list = None) -> dict:
    """把通用报告重塑为场景化决策报告"""
    scenario = scenario or "通用"

    # 从报告提取关键段落（按 ### 分节）
    sections = _extract_sections(report or "")

    if scenario == "教研":
        return {
            "title": f"教研决策建议书：{topic}",
            "scenario": "教研",
            "sections": {
                "一、核心洞察": sections.get("涌现视角", "（讨论已生成完整报告）"),
                "二、关键分歧": sections.get("主要分歧", ""),
                "三、建设性方案": sections.get("建设性方向", ""),
                "四、风险提示": sections.get("风险提示", ""),
                "五、下一步行动": sections.get("下一步行动建议", ""),
                "六、你的认知盲区": sections.get("认知盲区", ""),
            },
            "evaluation": evaluation or {},
            "coach_audit": coach_audit or [],
            "use_hint": "本建议书由 6 人虚拟教研团队预演生成，建议结合本校实际情况落地。",
        }
    elif scenario == "科研":
        return {
            "title": f"科研决策预演报告：{topic}",
            "scenario": "科研",
            "sections": {
                "一、涌现视角": sections.get("涌现视角", ""),
                "二、主要分歧": sections.get("主要分歧", ""),
                "三、隐藏预设": sections.get("隐藏预设", ""),
                "四、风险提示": sections.get("风险提示", ""),
                "五、行动建议": sections.get("下一步行动建议", ""),
            },
            "evaluation": evaluation or {},
            "coach_audit": coach_audit or [],
            "use_hint": "本报告由 AI 科研团队预演生成，用于决策前的盲区排查。",
        }
    # 通用
    return {
        "title": f"决策预演报告：{topic}",
        "scenario": "通用",
        "sections": {"原始报告": report or ""},
        "evaluation": evaluation or {},
        "coach_audit": coach_audit or [],
        "use_hint": "",
    }


def _extract_sections(report: str) -> dict:
    """从 Markdown 报告中提取分节内容。
    主节：## 或 ### 带编号/中文名（一、涌现视角 / 你的认知盲区）
    子节：### 带"分歧/风险/阶段/方向/短期"等（归入最近主节）
    """
    sections = {}
    if not report:
        return sections
    # 子节关键词（### 下的小节标题）
    SUB_KEYWORDS = ("分歧", "风险", "阶段", "方向", "短期", "中期", "长期", "关键决策点", "方案A", "方案B", "方案C", "方案D")
    current = None
    for line in report.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("### "):
            title = line_stripped[4:].strip()
            is_sub = any(k in title for k in SUB_KEYWORDS) and not any(
                n in title for n in ("一、", "二、", "三、", "四、", "五、", "六、"))
            if is_sub:
                if current:
                    sections[current] = sections.get(current, "") + f"\n· {title}："
                continue
            current = title
            sections[current] = sections.get(current, "")
            continue
        elif line_stripped.startswith("## "):
            current = line_stripped[3:].strip()
            sections[current] = sections.get(current, "")
            continue
        elif current and line_stripped and not line_stripped.startswith("#"):
            sections[current] = sections.get(current, "") + line_stripped + " "
    # 别名归一化
    alias = {
        "一、涌现视角": "涌现视角", "涌现视角": "涌现视角",
        "二、主要分歧": "主要分歧", "主要分歧": "主要分歧",
        "三、隐藏预设": "隐藏预设", "隐藏预设": "隐藏预设",
        "四、建设性方向": "建设性方向", "建设性方向": "建设性方向",
        "五、风险提示": "风险提示", "风险提示": "风险提示",
        "六、下一步行动建议": "下一步行动建议", "下一步行动建议": "下一步行动建议",
        "🎯 你的认知盲区": "认知盲区", "你的认知盲区": "认知盲区",
    }
    normalized = {}
    for k, v in sections.items():
        key = alias.get(k, k)
        normalized[key] = v.strip()
    return normalized


if __name__ == "__main__":
    # 自测
    print("=== 预设课题库 ===")
    for p in PRESET_TOPICS:
        print(f"  [{p['id']}] {p['title']}（{p['scenario']}）")
    print("\n=== 场景化报告测试 ===")
    demo_report = "### 一、涌现视角\n群体讨论发现了个体未想到的角度\n### 二、主要分歧\n方案A与方案B存在冲突\n### 五、风险提示\n实施成本可能过高"
    r = get_scenario_report("教研", "测试课题", demo_report)
    print(json.dumps(r["sections"], ensure_ascii=False, indent=2))
