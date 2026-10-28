# -*- coding: utf-8 -*-
"""
群智 V15 · 主教练团队组建器（最优人格）
=====================================
主教练根据课题动态选择"最优人格"团队配置，而非固定组合。

核心逻辑：
  1. LLM 分析课题，判断需要哪些认知原型/专业/价值观组合（针对课题特征）
  2. 结合 v14 实验经验（564 轮）：批判者/检验者提升分歧质量，多样原型防同质化
  3. 输出 6 人配置（name → {proto, prof, value}）

设计参考（v14 实验分组）：
  - A组"最优人格+多样原型"：单学科+最优人格+高张力+多样原型
  - E组：开启人格+不同信息（信息不对称）
"""
import sys, os, json, re, random

# 认知原型（与 v14 一致）
PROTOTYPES = ["hunter", "trader", "ghost", "gambler", "diplomat", "skeptic", "critic"]
PROFESSIONS = ["计算机科学", "教育学", "心理学", "管理学", "社会学", "经济学", "法学", "医学"]
VALUES = ["效率优先", "公平优先", "创新优先", "人文关怀", "实用主义", "长远主义"]

# v14 实验经验：各原型的效果标签（来自实验分组设计）
PROTO_STRENGTHS = {
    "hunter": "推动讨论进程，提出可行下一步（行动导向）",
    "trader": "联结对立观点，促成共识（协调导向）",
    "ghost": "指出集体忽略的盲点（洞察导向）",
    "gambler": "提出替代方案，跳出框架（创新导向）",
    "diplomat": "维护讨论氛围，总结共识（氛围导向）",
    "skeptic": "反向推演可行性，追问边界（检验导向）",
    "critic": "攻击逻辑漏洞，追问伦理（红队导向）",
}

TEAM_BUILD_PROMPT = """你是群智系统的主教练，负责为科研讨论组建最优人格团队。
课题：{topic}
用户想法：{user_thought}

【可选认知原型】{protos}
【可选专业】{profs}
【可选价值观】{values}

【组队原则（基于564轮实验经验）】
1. 人格多样性优先：6人应覆盖不同的认知原型（proto），这是讨论质量的核心（实验证明人格同质化降低讨论质量）
2. 张力配置：团队中应至少包含1个批判者(critic)或检验者(skeptic)（高张力提升分歧质量）
3. 专业按需配置（允许重复）：专业背景根据课题需要分配，不强制6个不同专业——
   教学类课题可多配教育学/心理学（可重复），技术类多配计算机科学；
   专业服务于课题，人格决定讨论质量
4. 价值观互补：效率+公平+创新+人文的组合能覆盖更多决策维度

【输出格式】严格JSON（不要其他文字）：
{{
  "reasoning": "一句话说明组队思路",
  "players": [
    {{"name": "甲", "proto": "hunter", "prof": "计算机科学", "value": "效率优先"}},
    ...共6人...
  ]
}}

【name 字段硬性要求】必须依次是 甲、乙、丙、丁、戊、己 六个固定代号，
不得写成原型名（如"推动者""联结者"）或其它称呼——系统按这六个代号取配置，写错会导致组队失效。
【prof 字段】必须从下列专业名中选：{profs}
【value 字段】必须从下列价值观中选：{values}"""


def build_optimal_team(topic: str, user_thought: str = "", api_caller=None) -> dict:
    """主教练构建最优人格团队"""
    protos_desc = "；".join(f"{k}（{v}）" for k, v in PROTO_STRENGTHS.items())
    prompt = TEAM_BUILD_PROMPT.format(
        topic=topic, user_thought=user_thought or "无",
        protos=protos_desc,
        profs="、".join(PROFESSIONS),
        values="、".join(VALUES),
    )

    config = None
    if api_caller:
        try:
            result = api_caller(prompt, max_tokens=1200, temperature=0.5, timeout=30)
            config = _parse_team(result)
        except Exception as e:
            print(f"[组队] LLM 失败: {e}")
    if not config:
        config = _fallback_team(topic)
    return config


def _default_value_weights(main_idx: int) -> dict:
    """多价值观权重：主价值观 50%，其余 50% 随机分配（V15 设计）"""
    weights = {i: 0.0 for i in range(len(VALUES))}
    weights[main_idx] = 0.5
    remaining = 0.5
    others = [i for i in range(len(VALUES)) if i != main_idx]
    random.shuffle(others)
    cuts = sorted(random.uniform(0, 1) for _ in range(len(others) - 1))
    cuts = [0.0] + cuts + [1.0]
    for i, o in enumerate(others):
        weights[o] = remaining * (cuts[i + 1] - cuts[i])
    return weights


def _parse_team(text) -> dict:
    """解析 LLM 输出的团队配置"""
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except Exception:
            return None
    players = data.get("players", [])
    if not players:
        return None
    # 校验并标准化
    config = {"reasoning": data.get("reasoning", ""), "config": {}}
    names = ["甲", "乙", "丙", "丁", "戊", "己"]
    for i, p in enumerate(players[:6]):
        # V15 修复（2026-10-09）：玩家代号**一律按位置分配**，不信任模型返回的 name。
        # 事故复盘：模型曾把 name 写成原型名（推动者/联结者/洞察者/破局者/检验者/批判者），
        #          而沙箱是按 PLAYER_NAMES（甲…己）取配置的 → 键对不上 → 取不到 →
        #          6 名玩家全部落到默认值「计算机科学/联结者/效率优先」，界面显示"参与者全是计算机"。
        name = names[i]
        raw_name = str(p.get("name") or "").strip()
        if raw_name and raw_name != name:
            print(f"[组队] 忽略模型返回的玩家名 {raw_name!r}，按位置使用 {name}")
        proto = p.get("proto", "hunter")
        prof = p.get("prof", "计算机科学")
        value = p.get("value", "效率优先")
        # 归一化
        if proto not in PROTOTYPES:
            proto = _map_proto(proto)
        prof_idx = _idx_of(prof, PROFESSIONS)
        value_idx = _idx_of(value, VALUES)
        # V15：多价值观权重（主价值观50% + 其余随机分配）
        value_weights = _default_value_weights(value_idx)
        config["config"][name] = {"proto": proto, "prof": prof_idx, "value": value_idx,
                                  "value_weights": value_weights}
    # V15 修复（2026-10-09）：模型返回不足 6 人时，用兜底组队的对应槽位补齐，
    # 而不是留给沙箱的默认值（那会造成多人同专业）。
    if len(config["config"]) < len(names):
        fb = _fallback_team("")["config"]
        for nm in names:
            if nm not in config["config"]:
                config["config"][nm] = fb[nm]
                print(f"[组队] 模型只返回 {len(players)} 人，槽位 {nm} 用兜底配置补齐")
    # 确保有批判者/检验者（张力）
    protos_used = [c["proto"] for c in config["config"].values()]
    if not any(p in ("critic", "skeptic") for p in protos_used):
        # 把最后一人改为批判者
        last = list(config["config"].keys())[-1]
        config["config"][last]["proto"] = "critic"
    return config


def _map_proto(text: str) -> str:
    """中文原型名 → key"""
    mapping = {
        "推进者": "hunter", "推动者": "hunter", "联结者": "trader", "洞察者": "ghost",
        "探索者": "gambler", "破局者": "gambler", "调和者": "diplomat", "检验者": "skeptic",
        "批判者": "critic", "批评者": "critic",
    }
    return mapping.get(text, "hunter")


def _idx_of(name: str, lst: list) -> int:
    for i, item in enumerate(lst):
        if name in item or item in name:
            return i
    return 0


def _fallback_team(topic: str) -> dict:
    """兜底：人格多样性优先 + 批判者张力；专业按课题聚焦（允许重复）"""
    # 根据课题关键词聚焦专业分布（可重复，服务于课题）
    # 索引：0计算机 1教育 2心理 3管理 4社会 5经济 6法学 7医学
    # 注意：具体分支在前（健康/治理），宽泛分支（教育/AI）在后，避免误匹配
    if any(k in topic for k in ["心理", "健康", "情绪", "压力", "疾病", "医疗", "卫生", "康复"]):
        prof_weights = [7, 7, 2, 2, 1, 4]   # 医学×2 + 心理×2 + 教育 + 社会
    elif any(k in topic for k in ["禁止", "政策", "合规", "伦理", "治理", "法律", "监管", "隐私", "规范", "制度"]):
        prof_weights = [6, 6, 0, 4, 1, 2]   # 法学×2 + 计算机 + 社会 + 教育 + 心理
    elif any(k in topic for k in ["教育", "教学", "课堂", "学生", "师范", "课程", "学习"]):
        prof_weights = [1, 1, 1, 2, 2, 3]   # 教育学×3 + 心理×2 + 管理×1
    elif any(k in topic for k in ["AI", "智能", "系统", "技术", "计算", "算法", "数据", "模型", "推理", "大模型", "软件", "网络"]):
        prof_weights = [0, 0, 0, 1, 2, 3]   # 计算机×3 + 教育 + 心理 + 管理
    else:
        prof_weights = [0, 1, 2, 3, 4, 5]   # 通用：各1
    config = {
        "reasoning": "兜底组队：人格多样化（含批判者张力）+ 专业按课题聚焦（可重复）",
        "config": {
            "甲": {"proto": "hunter", "prof": prof_weights[0], "value": 0, "value_weights": _default_value_weights(0)},
            "乙": {"proto": "trader", "prof": prof_weights[1], "value": 1, "value_weights": _default_value_weights(1)},
            "丙": {"proto": "ghost", "prof": prof_weights[2], "value": 2, "value_weights": _default_value_weights(2)},
            "丁": {"proto": "gambler", "prof": prof_weights[3], "value": 3, "value_weights": _default_value_weights(3)},
            "戊": {"proto": "skeptic", "prof": prof_weights[4], "value": 4, "value_weights": _default_value_weights(4)},
            "己": {"proto": "critic", "prof": prof_weights[5], "value": 5, "value_weights": _default_value_weights(5)},
        },
    }
    return config


def generate_interventions(topic: str, api_caller=None) -> list:
    """主教练**开局候选**干预指令。

    ⚠️ 语义变更（V15 优化 2026-09-25）：
      旧行为：此处产出的指令会被**强制注入**，且全部集中在讨论第 2 轮；
      新行为：本函数只产出「候选」，由引擎在本局内**逐轮按客观信号决策**
              （sandbox/app.py 的 Coach.decide_intervention），候选仅作为提示传入，
              不保证被采纳，也不限定在第 2 轮。
    """
    if not api_caller:
        return []
    try:
        prompt = (
            f"你是科研讨论主教练。课题：{topic}\n"
            "根据课题特点，从以下干预指令中选择2-3个最可能暴露盲区的："
            "概念冻结、可验证性声明、红队测试、机制预算、伦理审查、视角切换、降级运行、反事实推演\n"
            "只输出JSON数组，如[\"红队测试\",\"反事实推演\"]"
        )
        result = api_caller(prompt, max_tokens=100, temperature=0.4, timeout=15)
        if result:
            try:
                return json.loads(result)
            except Exception:
                m = re.search(r"\[.*?\]", result, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group())
                    except Exception:
                        pass
    except Exception:
        pass
    return []


if __name__ == "__main__":
    import sys as _s
    _s.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox"))
    from app import call_with_retry  # noqa
    print("=== 最优人格组队测试 ===")
    team = build_optimal_team("如何提升师范生课堂参与度？", "小组讨论效果不稳定", call_with_retry)
    print(f"推理: {team.get('reasoning')}")
    for name, cfg in team.get("config", {}).items():
        print(f"  {name}: {cfg['proto']} / {PROFESSIONS[cfg['prof']]} / {VALUES[cfg['value']]}")
    print("\n=== 干预指令生成测试 ===")
    iv = generate_interventions("大学生使用AI写作业，学校该禁止还是引导？", call_with_retry)
    print(f"干预: {iv}")
