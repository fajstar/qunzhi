# -*- coding: utf-8 -*-
"""
群智 V15 · v3 评审委员会接入层
==============================
复用 reviewer_v3 的五角色独立评审（理论家/实践者/批评家/审计员/方法论审计员），
作为 V15 的评分机制（替换旧的单模型五维评分）。

亮点（"裁判与选手分离"）：
  - 双模型评审：理论家/批评家/方法论用 DeepSeek，实践者/审计员用 GLM-4
  - 每维度带置信度 + 原文证据（可解释评分）
  - 批评家区分"真涌现/伪涌现"
  - 独立审计：幻觉检测/信息密度/逻辑连贯性/透明性/可验证性
"""
import sys, os, json, re, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 加载 .env
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        if k and not os.getenv(k):
                            os.environ[k] = v
        except Exception:
            pass

_load_env()

import requests

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
GLM_KEY = os.getenv("GLM_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
GLM_URL = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4") + "/chat/completions"
# 模型名统一走环境变量（此前硬编码 glm-4-flash，导致配置 GLM_MODEL 不生效）
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
GLM_MODEL = os.getenv("GLM_MODEL", "glm-4-flash")

# ============ 评分标准锚点 ============
SCORING_RUBRIC = """
## 评分标准（1-10分）
- 1-3分（低）：内容肤浅，仅复述已知信息，无任何新意或深度
- 4-6分（中）：有一定分析，但停留在表面，缺乏实质性突破
- 7-8分（高）：分析深入，有明确的创新点，但仍受限于已有框架
- 9-10分（极高）：触及问题核心矛盾，提出可落地的新方案，打破了常规思维
"""

THEORIST_PROMPT = f"""你是一位严谨的学术评审专家（理论家）。
请基于以下预演报告，从两个维度进行独立评分（1-10分）。
{SCORING_RUBRIC}
1. 深度：报告是否触及了课题的核心矛盾？分析是否深入本质？
2. 广度：报告是否覆盖了多重视角？有无明显的遗漏或盲区？
必须引用报告中的原文作为评分依据。对每个维度给出置信度（1-10）。
输出格式（严格JSON）：
{{"深度": {{"score": 分数, "confidence": 置信度, "evidence": "原文依据"}}, "广度": {{"score": 分数, "confidence": 置信度, "evidence": "原文依据"}}}}"""

PRACTITIONER_PROMPT = f"""你是一位注重实际应用的技术评审专家（实践者）。
请基于以下预演报告，从实用性维度进行独立评分（1-10分）。
{SCORING_RUBRIC}
实用性：方案是否真正可落地？有无具体的实施路径、成本估算、约束条件分析？
必须引用报告中的原文作为评分依据。给出置信度（1-10）。
输出格式（严格JSON）：
{{"实用性": {{"score": 分数, "confidence": 置信度, "evidence": "原文依据"}}}}"""

CRITIC_PROMPT = f"""你是一位专门甄别创新真伪的评审专家（批评家）。
请基于以下预演报告，从两个维度进行独立评分（1-10分）。
{SCORING_RUBRIC}
1. 涌现度：报告中是否产生了任何单一AI无法独立提出的、可落地的、能解决课题的新方案？
   - 高分要求：方案可运行、有具体实现路径，且无法从信息库直接推导
   - 低分标志：仅仅是换了一种说法，或提出无法落地的新词
2. 分歧质量：讨论中的分歧是建设性的碰撞（推动方案优化），还是无意义的重复？
关键区分：
- "真涌现"：提出了信息库中完全没有的、可落地的方案
- "伪涌现"：提出了新词，但没有具体实现路径
必须引用原文作为依据。对每个维度给出置信度（1-10）。严格区分"真涌现"和"伪涌现"。
输出格式（严格JSON）：
{{"涌现度": {{"score": 分数, "confidence": 置信度, "evidence": "原文依据", "is_genuine_emergence": true/false}}, "分歧质量": {{"score": 分数, "confidence": 置信度, "evidence": "原文依据"}}}}"""

AUDITOR_PROMPT = """你是一位专门评估AI生成内容可靠性的审计专家（审计员）。
请基于以下预演报告，从五个维度评估其AI生成质量（1-10分）。
1. 幻觉检测：是否存在无法从信息库或常识验证的虚假陈述？
2. 信息密度：每句话是否承载了实质性的信息？有无大量冗余？
3. 逻辑连贯性：推理链是否完整？是否存在跳跃、断裂或自相矛盾？
4. 透明性：AI是否标注了不确定的信息？是否区分了"已知事实"和"推断"？
5. 可验证性：结论是否可以通过外部信息验证？是否提供了验证路径？
必须引用报告中的原文作为评分依据。对每个维度给出置信度（1-10）。
输出格式（严格JSON）：
{"幻觉检测": {"score": 分数, "confidence": 置信度, "evidence": "原文依据"}, "信息密度": {...}, "逻辑连贯性": {...}, "透明性": {...}, "可验证性": {...}}"""

METHODOLOGY_AUDITOR_PROMPT = """你是一位科研方法论审计专家（方法论审计员）。
请基于以下预演报告，评估其研究方法论规范性（1-10分）。
1. 变量控制：方案是否明确了主要变量及其控制方式？
2. 样本量论证：方案是否论证了样本量的合理性？
3. 统计方法：方案是否指定了合适的统计分析方法？
4. 可重复性：方案是否可被他人重复验证？
这是对研究方法论规范性的评估，不是对学术质量的评估。
输出格式（严格JSON）：
{"变量控制": {"score": 分数, "confidence": 置信度, "evidence": "原文依据"}, "样本量论证": {...}, "统计方法": {...}, "可重复性": {...}}"""


# ============ API 调用 ============

def _call_model(prompt, report_text, model="deepseek", max_tokens=1000):
    """调用评审模型。model="glm" 但 GLM 未配置/失败时**回落 DeepSeek**，
    保证"双模型评审"在只配了 DeepSeek 的环境下仍能给出完整五角色结果
    （与 .env.example 中"未配置或失败自动回落，不影响功能"的承诺一致）。"""
    return _call_one(prompt, report_text, model, max_tokens)


def _call_one(prompt, report_text, model, max_tokens, _depth=0):
    if model == "deepseek":
        url, key, model_name = DEEPSEEK_URL, DEEPSEEK_KEY, DEEPSEEK_MODEL
    else:
        url, key, model_name = GLM_URL, GLM_KEY, GLM_MODEL
    if not key or not _looks_like_key(key):
        # 该 provider 未配置 → 回落 DeepSeek（若本身已是 DeepSeek 则直接失败）
        if model != "deepseek":
            reason = "未配置有效 Key" if not key else "Key 形如占位符"
            print(f"[v3评审] {model} {reason}，回落 DeepSeek", flush=True)
            return _call_one(prompt, report_text, "deepseek", max_tokens, _depth + 1)
        return None
    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model_name,
                      "messages": [
                          {"role": "system", "content": prompt},
                          {"role": "user", "content": f"预演报告内容：\n\n{report_text[:4000]}"}
                      ], "temperature": 0.15, "max_tokens": max_tokens},
                timeout=90,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                data = extract_json(content)
                if isinstance(data, dict):
                    # 记录**实际**作答的模型（GLM 失败回落时标签要如实写 DeepSeek，
                    # 否则前端"判定者：glm-4-flash"会是假的）
                    data["__model_used__"] = model_name
                    return data
                if attempt < 1:
                    time.sleep(1.5)
                continue
            print(f"[v3评审] {model_name} HTTP {resp.status_code}: {resp.text[:100]}", flush=True)
            if attempt < 1:
                time.sleep(2)
        except Exception as e:
            print(f"[v3评审] {model_name} 调用异常: {str(e)[:100]}", flush=True)
            if attempt < 1:
                time.sleep(3)
    # 全部重试失败 → 回落 DeepSeek
    if model != "deepseek":
        print(f"[v3评审] {model_name} 调用失败，回落 DeepSeek", flush=True)
        return _call_one(prompt, report_text, "deepseek", max_tokens, _depth + 1)
    return None


def _looks_like_key(k: str) -> bool:
    """排除占位符/中文 key（放进 HTTP Header 会触发编码错误）"""
    try:
        k.encode("ascii")
    except Exception:
        return False
    low = (k or "").lower()
    return len(k or "") >= 12 and not any(b in low for b in ("your", "replace", "placeholder", "xxx", "填入"))


def extract_json(text):
    """抽取 JSON 对象；非 dict（如模型返回数组/字符串）一律视为解析失败返回 None"""
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    for pattern in [r'```json\s*(.*?)\s*```', r'```\s*(.*?)\s*```', r'\{.*\}']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1) if "json" in pattern else m.group())
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


# ============ 五角色评审 ============

def v3_review(report_text: str, topic: str = "") -> dict:
    """v3 五角色评审：返回完整评分结果"""
    result = {
        "topic": topic,
        "review_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scores": {},        # 学术五维（理论家/实践者/批评家）
        "audit_scores": {},  # 审计五维（审计员）
        "methodology_scores": {},  # 方法论四维（方法论审计员）
        "models_used": [],
        "v3": True,
    }

    def _used(block, default):
        """取出实际作答模型名（并移除内部标记键）"""
        return block.pop("__model_used__", default) if isinstance(block, dict) else default

    def _mark(models, name):
        if name not in models:
            models.append(name)

    # 理论家：深度、广度（DeepSeek）
    ds1 = _call_model(THEORIST_PROMPT, report_text, "deepseek")
    if ds1:
        mu = _used(ds1, "deepseek")
        for dim in ["深度", "广度"]:
            if dim in ds1:
                ds1[dim]["model"] = mu
                ds1[dim]["role"] = "理论家"
                result["scores"][dim] = ds1[dim]
        _mark(result["models_used"], mu)

    # 实践者：实用性（GLM；GLM 不可用时如实记为 DeepSeek）
    gl1 = _call_model(PRACTITIONER_PROMPT, report_text, "glm")
    if gl1:
        mu = _used(gl1, "glm")
        for dim in ["实用性"]:
            if dim in gl1:
                gl1[dim]["model"] = mu
                gl1[dim]["role"] = "实践者"
                result["scores"][dim] = gl1[dim]
        _mark(result["models_used"], mu)

    # 批评家：涌现度、分歧质量（DeepSeek）
    ds2 = _call_model(CRITIC_PROMPT, report_text, "deepseek")
    if ds2:
        mu = _used(ds2, "deepseek")
        for dim in ["涌现度", "分歧质量"]:
            if dim in ds2:
                ds2[dim]["model"] = mu
                ds2[dim]["role"] = "批评家"
                result["scores"][dim] = ds2[dim]

    # 审计员：AI生成可靠性（GLM；不可用时如实记为 DeepSeek）
    gl2 = _call_model(AUDITOR_PROMPT, report_text, "glm")
    if gl2:
        mu = _used(gl2, "glm")
        for dim in ["幻觉检测", "信息密度", "逻辑连贯性", "透明性", "可验证性"]:
            if dim in gl2:
                gl2[dim]["model"] = mu
                gl2[dim]["role"] = "审计员"
                result["audit_scores"][dim] = gl2[dim]
        _mark(result["models_used"], mu)

    # 方法论审计员（DeepSeek）
    ds3 = _call_model(METHODOLOGY_AUDITOR_PROMPT, report_text, "deepseek")
    if ds3:
        mu = _used(ds3, "deepseek")
        for dim in ["变量控制", "样本量论证", "统计方法", "可重复性"]:
            if dim in ds3:
                ds3[dim]["model"] = mu
                ds3[dim]["role"] = "方法论审计员"
                result["methodology_scores"][dim] = ds3[dim]
        _mark(result["models_used"], mu)

    # 综合分
    scores_list = []
    for dim in ["深度", "广度", "涌现度", "实用性", "分歧质量"]:
        if dim in result["scores"]:
            try:
                scores_list.append(float(result["scores"][dim]["score"]))
            except (TypeError, ValueError):
                pass
    if scores_list:
        result["综合评分"] = round(sum(scores_list) / len(scores_list), 1)

    audit_list = []
    for dim in ["幻觉检测", "信息密度", "逻辑连贯性", "透明性", "可验证性"]:
        if dim in result["audit_scores"]:
            try:
                audit_list.append(float(result["audit_scores"][dim]["score"]))
            except (TypeError, ValueError):
                pass
    if audit_list:
        result["审计综合评分"] = round(sum(audit_list) / len(audit_list), 1)

    meth_list = []
    for dim in ["变量控制", "样本量论证", "统计方法", "可重复性"]:
        if dim in result["methodology_scores"]:
            try:
                meth_list.append(float(result["methodology_scores"][dim]["score"]))
            except (TypeError, ValueError):
                pass
    if meth_list:
        result["方法论综合评分"] = round(sum(meth_list) / len(meth_list), 1)

    # 真涌现判定（严格布尔校验）
    # 注意：不能写 bool(em.get("is_genuine_emergence", False))——
    # 模型输出 JSON 时可能给出字符串 "false"，而 bool("false") == True，会把伪涌现误判为真涌现。
    em = result["scores"].get("涌现度", {}) or {}
    em_flag = em.get("is_genuine_emergence", False)
    if isinstance(em_flag, str):
        em_flag = em_flag.strip().lower() in ("true", "yes", "1", "是", "真")
    else:
        em_flag = (em_flag is True)
    # 判定依据必须存在：无证据的"真涌现"降级为 False（宁缺勿滥）
    evidence = str(em.get("evidence") or "").strip()
    result["真涌现"] = bool(em_flag and len(evidence) >= 10)
    result["真涌现判定"] = {
        "judge_role": em.get("role", "批评家"),
        "model": em.get("model", "deepseek"),
        "is_genuine_emergence": bool(em_flag),
        "evidence_present": len(evidence) >= 10,
        "rule": "批评家判定为真涌现 且 提供了非空原文依据，二者同时满足才计为真涌现",
    }

    return result


if __name__ == "__main__":
    # 自测
    print("=== v3 评审自测 ===")
    print(f"DEEPSEEK_KEY: {'已配置' if DEEPSEEK_KEY else '未配置'}")
    print(f"GLM_KEY: {'已配置' if GLM_KEY else '未配置'}")
