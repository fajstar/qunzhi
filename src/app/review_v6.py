# -*- coding: utf-8 -*-
"""群智评审机制 v6 · 评审主体

替换 review_v3.py 那套「五角色 1–10 分」的方案。**不修改 review_v3.py**，
以新增文件 + 新端点并存；是否上线由用户在验证实验通过后决定。

三条原则（预注册）：
  ① 不许给自己的东西打分 —— 评审模型必须与生成模型不同族；回落必须在界面标红
  ② 不许拿总分当门面 —— 不用 1–10 分（该粒度已验证会塌），主读数改成「成对强制选择」
  ③ 每条判断必须带原文证据

实现约束：**禁止裸 assert**（`python -O` 下会被整体剥离且不报错，
那正是本项目最怕的静默失败形态）。一律 `if not ...: raise`。
"""
import json
import os
import re
import time

import requests

from review_input import prepare, SEGMENT_JOINER

# ---------------------------------------------------------------- 配置
# 三个家族，互不相同，且都与生成端（deepseek-chat，DeepSeek 系）无关
#
# V16.1（2026-10-11）两处改动，都是被实测逼出来的：
#   ① **`qwen3-max` → `qwen3.8-flash`**（用户 2026-10-11 决定）。
#      实测：配置的评审端点上 `qwen3-max` 返回 **HTTP 404**（该型号已不在平台可选列表里），
#      而型号是**写死在代码里**的 → 产品静默退化成"只有 2 个评委"，
#      界面照常出分。三家族是 v6 的立身之本（原则①防同源自评），少一家等于设计没成立。
#   ② **补上 `V6_REVIEWER_MODELS` 环境变量覆盖**。
#      你们自己的代码登记 `code_reg_v64.md` 写过「新增环境变量 V6_REVIEWER_MODELS，
#      默认值写死在代码里」——但**代码里从未实现**（全仓 .py 搜不到该变量，只在 .md 里）。
#      于是型号只能靠改源码，平台下架型号就必然踩坑（这次就是）。
#      现在：环境变量优先，**默认值仍是下面这三家**（行为不变）。
#
# ⚠️ **改评委数量会破坏锚点阈值，别随手加家族**：
#    `review_anchors.TIE_TRIALS = 6` 是按「**3 评审 × 2 序**」定的（预注册口径），
#    阈值 4/6。加到 4 家 → 8 票 → 4/6 的含义就变了。
#    所以 `_resolve_units` 里加了硬校验（单元数 × 2 必须等于 TIE_TRIALS），
#    不等就**直接报错**，绝不静默用一个错阈值跑。
_DEFAULT_REVIEWERS = ["glm-5.3-flash", "qwen3.8-flash", "MiniMax-M2.5"]
REVIEWERS = [m.strip() for m in
             os.getenv("V6_REVIEWER_MODELS", ",".join(_DEFAULT_REVIEWERS)).split(",")
             if m.strip()]

GENERATOR_FAMILY = ("deepseek",)

# 生成端若是别的族，改这里；同族检测据此标红
BASE_URL = {
    "glm-5.3-flash": os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
    "qwen3.8-flash": os.getenv("QWEN_BASE_URL",
                               "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "MiniMax-M2.5": os.getenv("QWEN_BASE_URL",
                              "https://dashscope.aliyuncs.com/compatible-mode/v1"),
}
KEY_ENV = {
    "glm-5.3-flash": "GLM_API_KEY",
    "qwen3.8-flash": "QWEN_API_KEY",
    "MiniMax-M2.5": "QWEN_API_KEY",
}
CTL = {
    "glm-5.3-flash": {"reasoning_effort": "high"},
    "qwen3.8-flash": {"enable_thinking": False},
    "MiniMax-M2.5": {},
}

# ================================================================
# V16.6 熔断（2026-10-11）：用户反馈「决策报告生成一辈子，太慢了」。
# 算术根因：单次调用 timeout=300 × tries=2 = **600 秒 = 10 分钟**（还只是一个模型），
# 而报告/评审层**没有任何全局截止时间** —— 一局跑多久都没人管。
# 三道熔断：① 单次 90 秒 ② v6_review 整体 240 秒 ③ 到点返回已完成的部分并如实标注。
# ================================================================
CALL_TIMEOUT = int(os.getenv("V6_CALL_TIMEOUT", "90"))
REVIEW_DEADLINE = int(os.getenv("V6_REVIEW_DEADLINE", "240"))


PAIR_PROMPT = """你是独立评审。下面有两份针对同一课题的决策建议书，分别标为「甲」和「乙」。

请判断哪一份更好，或认为二者无实质差别。

判断依据限定为：是否答到课题本身、结论是否可执行、风险是否被识别、有无明显遗漏。
不要考虑篇幅长短，不要猜测作者身份或生成方式。
必须引用原文片段作为依据。

严格 JSON 输出：
{"winner": "甲" 或 "乙" 或 "无差别", "evidence_for": "引用甲或乙的原文", "reason": "一句话"}
"""


# ---- D6 对照条件专用变体（v7.0 新增，C4）----
# ⚠️ **只**用于 D6 的 3 对对照条件；结果单列、只做描述性对比、不做检验。
#    主检验 K1 一律用上面那版 PAIR_PROMPT（**未提示**自洽性）。
PAIR_PROMPT_CONSISTENCY = """你是独立评审。下面有两份针对同一课题的决策建议书，分别标为「甲」和「乙」。

请判断哪一份更好，或认为二者无实质差别。

判断依据限定为：是否答到课题本身、结论是否可执行、风险是否被识别、有无明显遗漏、
**结论与正文是否自相矛盾**。
不要考虑篇幅长短，不要猜测作者身份或生成方式。
必须引用原文片段作为依据。

严格 JSON 输出：
{"winner": "甲" 或 "乙" 或 "无差别", "evidence_for": "引用甲或乙的原文", "reason": "一句话"}
"""
# ---------------------------------------------------------------- 三档标准（rubric）
# V16.2（2026-10-11）新增。用户原话：「然后就是什么强，你没有表明」
# —— 对的。原先 DIM_PROMPT 只写「各评一档：强 / 中 / 弱」，**从未定义档位标准**，
#    于是模型按各自的主观尺度打分，用户看到"① 强"却不知道门槛在哪、也无法复核。
# 现在：标准写死在这里 → ① 注入 prompt（让模型按同一把尺子）② 随结果返回（让界面能展示）。
# 设计取向：**档位取决于"报告里有没有可引用的具体内容"，而不是文风好坏**——
#    这样每一档都能用"引不引得出原文"来复核，而不是凭感觉。
TIER_RUBRIC = {
    "强": "该维度在报告里有**可直接照做的具体内容**（点名对象/数字/步骤/边界/出处），且能逐字引用原文。"
          "注意：只写方向、口号、原则的，不算强。",
    "中": "报告**点到了**该维度，但停在方向或原则层面，缺少可执行细节；或只覆盖了该维度的一半。",
    "弱": "报告**基本没有处理**该维度，或只有一句口号式表述，读者拿不到任何可操作信息。",
    "无依据": "**引不出**对应原文片段（含：该维度在报告里根本没出现）。不参与任何汇总。",
}
_TIER_KEYS = ("强", "中", "弱", "无依据")


def _today():
    """当前日期（评审用它判断年份是否可疑）。

    V16.2：为什么要显式注入——实测 v6 产出过一条**假风险**：
      「文献年份存疑：P1 引用发表时间为 2026 年，但当前时间为 2024 年」
    而本项目的"现在"就是 2026 年，引用 2026 年文献毫无问题。
    根因是**模型不知道今天几号**（它的知识有截止日），于是把自己的时间错觉当成了发现。
    注入日期后，这类"时间错觉型假风险"应当消失。
    """
    return time.strftime("%Y-%m-%d")


def _rubric_text():
    return "\n".join("  · %s＝%s" % (k, TIER_RUBRIC[k]) for k in _TIER_KEYS)


DIM_PROMPT = """你是独立评审。请做两件事。

【今天是 {today}】凡涉及时间/年份的判断，一律以此为准（不要把文献里的年份当作"未来"而误报）。

【第一件】对下面这份决策建议书按五个维度各评一档：强 / 中 / 弱 / 无依据。
维度：① 是否答到课题核心 ② 结论可执行性 ③ 风险识别 ④ 事实可靠性 ⑤ 视角完整性

**档位标准（必须严格按此判，不要用你自己的主观尺度）**：
{rubric}

**每个档位必须引用原文片段**；引不出原文的维度记「无依据」。
**不要给总分、不要给 1-10 分数。**

【第二件】**主动列风险（独立栏，不参与任何判定）**：
列出「这份报告最可能在哪误导读者」——**2 到 4 条**，每条必须写清：
  · risk：具体会怎么误导（点名报告里的哪句话或哪个做法）
  · if_happens：**如果它错了，后果是什么**（不许写"可能有问题"这种空话）
  · evidence：报告中对应的原文片段
**判断年份/时间相关问题前，先对照上面的"今天是"**。

严格 JSON 输出：
{{
  "dims": {{"①": {{"level": "强/中/弱/无依据", "quote": "原文", "reason": "一句话"}},
           "②": {{...}}, "③": {{...}}, "④": {{...}}, "⑤": {{...}}}},
  "risks": [{{"risk": "…", "if_happens": "…", "evidence": "原文片段"}}]
}}
"""

CLAIM_PROMPT = """下面是一份决策建议书。请把它拆成「原子主张」清单：每条只包含一个可独立判断对错或可独立执行的主张。

每条格式：{"id": 序号, "claim": "一句话主张", "quote": "对应原文片段（必须逐字引用）"}。
只列主张，不要评价，不要合并不同主张，不要补充原文没有的内容。
若某段无法拆出可独立判断的主张，跳过它。

严格 JSON 输出：{"claims": [...]}
"""

DIMS = ["①", "②", "③", "④", "⑤"]
DIM_NAMES = {"①": "是否答到课题核心", "②": "结论可执行性", "③": "风险识别",
             "④": "事实可靠性", "⑤": "视角完整性"}


# ---------------------------------------------------------------- 调用
def _env():
    """从 .env 读 key（只读，不打印）。"""
    cfg = {}
    for p in ("/home/ubuntu/qunzhi/.env",):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
    return cfg


_CFG = _env()


def _parse(txt):
    s = (txt or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def call_model(model, system_prompt, user_prompt, max_tokens=4000, tries=2):
    """调一个评审模型。**不回落**：失败就失败，由调用方按规则处置。"""
    # V16.1 修复（2026-10-11）：**未登记的模型名必须明确失败，不许崩。**
    # 实证：`V.call_model("不存在的模型-xyz", ...)` 原先在 `KEY_ENV[model]` 处抛 KeyError，
    # 而自带测试 T6（"不存在的模型 → 明确失败，不回落"）期望的是 `{"ok": False, "error": ...}`。
    # 这违反两处自己写下的纪律：
    #   · 本模块原则①「不许给自己的东西打分 —— 回落必须在界面标红」（崩掉就无从标红）；
    #   · 审查报告 11 的 R6「**任何非预期形状都不许崩**」。
    # 注意方向：这是**让失败更显式**，不是加回落 —— 未登记就是不可用，绝不改派别的模型。
    if model not in KEY_ENV or model not in BASE_URL:
        return {"ok": False, "unavailable": True,
                "error": f"未登记的评审模型：{model}（不回落；已登记：{'、'.join(REVIEWERS)}）"}
    key = _CFG.get(KEY_ENV[model], "")
    if not key:
        return {"ok": False, "error": f"{KEY_ENV[model]} 未配置", "unavailable": True}
    url = BASE_URL[model].rstrip("/") + "/chat/completions"
    body = {"model": model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}],
            "temperature": 0.2, "max_tokens": max_tokens}
    body.update(CTL.get(model, {}))
    last = None
    for t in range(tries):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"},
                              json=body, timeout=CALL_TIMEOUT)
            if r.status_code == 200:
                d = r.json()
                msg = (d.get("choices") or [{}])[0].get("message") or {}
                txt = (msg.get("content") or "").strip()
                # V16.2（2026-10-11）：**兼容推理模型的 reasoning_content**。
                # 为什么必须处理：`glm-5.3-flash` 是推理模型（你们 `.env.example` 里已写明
                # "输出去 reasoning_content，content 可能为空"）。若 content 为空而思维链里有
                # 答案，直接判"空内容"会把一次**本可成功**的调用记成失败。
                # 方向说明：这是**让同一模型的返回能被读到**，不是引入任何回落（绝不改派别的模型）。
                if not txt:
                    _rc = (msg.get("reasoning_content") or "").strip()
                    if _rc:
                        txt = _rc
                        last = "仅 reasoning_content（已改用它）"
                if txt:
                    real = d.get("model") or model
                    fr = (d.get("choices") or [{}])[0].get("finish_reason") or ""
                    return {"ok": True, "text": txt, "model_field": real,
                            "finish_reason": fr, "usage": d.get("usage") or {}}
                last = "空内容（content 与 reasoning_content 都为空）"
            else:
                last = f"HTTP {r.status_code}"
                if r.status_code in (401, 403, 404):
                    return {"ok": False, "error": last, "unavailable": True}
                if max_tokens < 8000 and r.status_code == 400:
                    max_tokens = 8000
                    body["max_tokens"] = max_tokens
                    continue
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(2 * (t + 1))
    return {"ok": False, "error": last}


# ---------------------------------------------------------------- 成对判定
def make_pairwise(model, with_consistency=False):
    """返回一个 pairwise_fn(text_a, text_b) —— 单模型、两序各一次取平均。

    with_consistency=False（默认，**主检验 K1 用**）：rubric **不含**自洽性。
    with_consistency=True （**仅 D6 对照条件用**）：rubric 明写「结论与正文是否自相矛盾」。

    ⚠️ 主检验**必须**用默认那版。若把自洽性写进主 rubric，再种一个自相矛盾的 D6，
       则 D6 的检出就变成「要求它找、它找到了」= **同义反复**（预注册五之三）。
    """
    prompt = PAIR_PROMPT_CONSISTENCY if with_consistency else PAIR_PROMPT
    def _one(a, b):
        """甲=a，乙=b。返回 winner。"""
        u = f"课题相关材料：\n\n【甲】\n{a}\n\n【乙】\n{b}\n"
        rr = call_model(model, prompt, u)
        if not rr.get("ok"):
            raise RuntimeError(f"{model} 调用失败：{rr.get('error')}")
        p = _parse(rr["text"])
        if p is None or p.get("winner") not in ("甲", "乙", "无差别"):
            raise RuntimeError(f"{model} 返回不可解析：{rr['text'][:120]}")
        return {"winner": p["winner"], "model": rr.get("model_field", model),
                "evidence": p.get("evidence_for", ""), "reason": p.get("reason", "")}

    def pairwise(text_a, text_b):
        r1 = _one(text_a, text_b)     # 甲=a
        r2 = _one(text_b, text_a)     # 甲=b（换序）
        # 两序折算成"a 是否胜" —— ⚠️ 必须**平均**不是求和！
        #   求和会出错：两序都判"无差别"时 0.5+0.5=1.0 → 被误判成"a 胜"。
        s = 0.0
        _n = 0
        for r, a_is_jia in ((r1, True), (r2, False)):
            w = r["winner"]
            _n += 1
            if w == "无差别":
                s += 0.5
            elif (w == "甲" and a_is_jia) or (w == "乙" and not a_is_jia):
                s += 1.0
        s = (s / _n) if _n else 0.5
        winner = "甲" if s > 0.5 else ("乙" if s < 0.5 else "无差别")
        return {"winner": winner, "model": r1.get("model"),
                "two_order_score": s, "orders": [r1, r2]}
    return pairwise


# ---------------------------------------------------------------- 主入口
def v6_review(report_text, topic="", baseline=None, run_anchors=True):
    """v6 评审主入口。

    report_text : 用户报告
    topic       : 课题
    baseline    : 最不利基线产出（单 AI + 一句提示词）。为 None 时跳过第 2 层主读数。
    run_anchors : 是否跑锚点自检（测试时可关掉）
    """
    t0 = time.time()
    def _over_deadline():
        """是否已超整体截止（V16.6 熔断③）。"""
        return (time.time() - t0) > REVIEW_DEADLINE
    result = {"v6": True, "topic": topic,
              "review_time": time.strftime("%Y-%m-%d %H:%M:%S"),
              "anchors_passed": None, "scores_hidden": True,
              "input": None, "models_used": [], "same_family_warning": False,
              "dimensions": {}, "risk": {"items": []},
              "unique_contrib": {"count": None, "items": []},
              "pairwise": None, "unavailable": [], "errors": []}

    # ---- 第 0 层：送入准备（不截断）+ 同族检测 ----
    prep = prepare(report_text)
    result["input"] = {"raw_len": prep["raw_len"], "sent_len": prep["sent_len"],
                       "segments": len(prep["segments"]), "truncated": prep["truncated"]}
    sent = SEGMENT_JOINER.join(prep["segments"])

    family_hit = [m for m in REVIEWERS if any(g in m.lower() for g in GENERATOR_FAMILY)]
    result["same_family_warning"] = bool(family_hit)
    if family_hit:
        result["errors"].append(f"评审与生成同源：{family_hit}（分数仅供参考）")

    # ---- 第 1 层：锚点自检 ----
    if run_anchors:
        if _over_deadline():
            result["anchors_passed"] = None
            result["scores_hidden"] = True
            result["errors"].append("已超本轮整体截止，锚点自检未执行 → 不展示任何分数")
            return result
        try:
            from review_anchors import check_anchors
            # 锚点用**第一个评审模型**跑（不用三家，避免 3×3×2 = 18 次调用）
            # ⚠️ v7.2：预注册写「3 评审 × 2 序 = 6 次判定」，
            #    此前只用了 REVIEWERS[0] 一个模型 → 锚点形同单模型自检。
            _fns = {_m: make_pairwise(_m) for _m in REVIEWERS}
            an = check_anchors(_fns)
            result["anchors_passed"] = an["passed"]
            result["anchors_detail"] = an["detail"]
            result["models_used"] = [REVIEWERS[0]]
        except Exception as e:
            result["anchors_passed"] = None
            result["errors"].append(f"锚点自检未能完成：{type(e).__name__}: {str(e)[:150]}")

        # 锚点未过（False 或 None）→ 不产出分数
        if result["anchors_passed"] is not True:
            result["scores_hidden"] = True
            result["elapsed_s"] = round(time.time() - t0, 1)
            if result["anchors_passed"] is False:
                result["errors"].append("锚点未通过：评审器未通过自检，不展示任何分数")
            else:
                result["errors"].append("锚点未能运行：材料或模型不可用，不展示任何分数")
            return result

    result["scores_hidden"] = False

    # ---- 三模型并发跑：成对判定 + 分维诊断 ----
    from concurrent.futures import ThreadPoolExecutor, as_completed
    used = set()

    # V16.2（2026-10-11）：**"有返回但解析不出 JSON"必须说清原因，不许记成 error=None。**
    # 事故：真实会话里 glm-5.3-flash 在分维诊断缺席，`unavailable` 记的是
    #   {"model": "glm-5.3-flash", "stage": "分维诊断", "error": null}
    # —— **原因字段是空的**，看日志既不知是超时、是空内容、还是格式没解析出来。
    # 根因：旧 `_dims` 在 `_parse` 返回 None 时把 err 也写成 None。而 glm-5.3-flash 是**推理模型**
    # （`.env.example` 已注明其输出走 reasoning_content），输出更长 → JSON 被 max_tokens 截断。
    # 现在：① 说明失败形态与返回长度；② **截断时自动放大 max_tokens 重试一次**（不换模型，只是给它足够长度）。
    _DIM_MAXTOK = 4000

    def _dims(model):
        if _over_deadline():      # V16.6 熔断：到点不再发新调用，如实说明
            return model, None, "已超本轮整体截止（%ds），未执行" % REVIEW_DEADLINE
        u = f"课题：{topic}\n\n决策建议书：\n{sent}\n"
        mt = _DIM_MAXTOK
        last_why = None
        _prompt = DIM_PROMPT.replace("{today}", _today()).replace("{rubric}", _rubric_text())
        for _try in range(2):
            rr = call_model(model, _prompt, u, max_tokens=mt)
            if not rr.get("ok"):
                return model, None, f"调用失败：{rr.get('error') or '未给出原因'}"
            txt = rr.get("text") or ""
            p = _parse(txt)
            if p:
                return model, p, None
            # 解析失败：把**可诊断的信息**带出来（长度、是否疑似截断、结尾长什么样）
            _tail = txt[-140:].replace("\n", "⏎")
            _cut = (rr.get("finish_reason") == "length") or (len(txt) >= mt - 40)
            last_why = ("JSON 解析失败（返回 %d 字%s）：结尾 …%s"
                        % (len(txt), "，**疑似被 max_tokens 截断**" if _cut else "", _tail))
            if _cut and _try == 0:
                mt = 8000                      # 给推理模型足够长度，再试一次
                print(f"[v6·分维] {model} 输出疑似截断，max_tokens 4000→8000 重试", flush=True)
                continue
            break
        return model, None, last_why

    def _pair(model):
        if _over_deadline():
            return model, None, "已超本轮整体截止（%ds），未执行" % REVIEW_DEADLINE
        if baseline is None:
            return model, None, None
        pw = make_pairwise(model)
        try:
            r = pw(sent, baseline)
            return model, r, None
        except Exception as e:
            return model, None, str(e)[:150]

    dim_res, pair_res = {}, {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {}
        for m in REVIEWERS:
            futs[ex.submit(_dims, m)] = ("dim", m)
            futs[ex.submit(_pair, m)] = ("pair", m)
        for fu in as_completed(futs):
            kind, m = futs[fu]
            try:
                mm, val, err = fu.result()
            except Exception as e:
                mm, val, err = m, None, str(e)[:150]
            if kind == "dim":
                dim_res[mm] = (val, err)
            else:
                pair_res[mm] = (val, err)

    # ---- 汇总分维诊断（三档，不求和）----
    per_model_dims = {}
    for m in REVIEWERS:
        p, err = dim_res.get(m, (None, "未执行"))
        if not p:
            result["unavailable"].append({"model": m, "stage": "分维诊断", "error": err})
            continue
        used.add(m)
        row = {}
        # 兼容两种返回：新结构 {dims:{...}, risks:[...]} 与旧结构 {"①":{...}}
        _dims = p.get("dims") if isinstance(p.get("dims"), dict) else p
        for d in DIMS:
            v = _dims.get(d) or {}
            lv = v.get("level") if isinstance(v, dict) else None
            _q = (v.get("quote") or "").strip() if isinstance(v, dict) else ""
            _rs = (v.get("reason") or "").strip() if isinstance(v, dict) else ""
            if lv not in _TIER_KEYS:
                lv = "无依据"
            # V16.2 **代码级校验**：三档标准的第一句就是"必须能逐字引用原文"，
            # 所以「强/中/弱 但 quote 为空」在逻辑上不可能成立 —— 直接降为「无依据」。
            # 为什么要在代码里兜：**档位若可以不带证据，它就会退化成一个主观标签**，
            # 那正是我们花力气把 1–10 分换掉的原因。宁可判"无依据"，也不给一个无法复核的"强"。
            if lv in ("强", "中", "弱") and not _q:
                lv = "无依据"
                _rs = (_rs + " ｜（原判有档位但未给出原文引用，按标准降为「无依据」）").strip()
            row[d] = {"level": lv, "quote": _q[:200], "reason": _rs[:200]}
        per_model_dims[m] = row

    # 三档众数（不求和）
    dims_out = {}
    for d in DIMS:
        votes = [per_model_dims[m][d]["level"] for m in per_model_dims]
        if votes:
            level = max(set(votes), key=votes.count)
            quote = next((per_model_dims[m][d]["quote"] for m in per_model_dims
                          if per_model_dims[m][d]["level"] == level
                          and per_model_dims[m][d]["quote"]), "")
            dims_out[d] = {"name": DIM_NAMES[d], "level": level, "quote": quote[:200],
                           "per_model": votes}
    result["dimensions"] = dims_out
    result["dimensions_per_model"] = per_model_dims
    # V16.2：把**档位标准**随结果返回，供界面展示。
    # 用户原话：「什么强，你没有表明」—— 界面上只出现"强"而不给标准，读者无法复核，
    # 档位就退化成了主观标签。标准与 prompt 用的是**同一份** TIER_RUBRIC（单一真值来源）。
    result["tier_rubric"] = dict(TIER_RUBRIC)
    result["today"] = _today()
    # V16.2：**评委齐不齐**要能被程序断言**（而不只是名字少一个）。
    # 实测事故：某局 glm-5.3-flash 在分维诊断缺席，界面上只是评委列少一个名字，
    # 而"三家独立家族"正是 v6 防止同源自评的立身之本 —— 少一家等于设计没成立。
    result["reviewer_status"] = {
        "expected": list(REVIEWERS),
        "contributed_dims": sorted(used),
        "missing_dims": [m for m in REVIEWERS if m not in used],
        "n_expected": len(REVIEWERS), "n_contributed": len(used),
        "complete": len(used) == len(REVIEWERS),
    }

    # ---- 第 2 层：主读数（成对强制选择）----
    # V16.1（2026-10-11）：**无 baseline 时不要把"成对判定"记成 unavailable**。
    # 实测：`baseline=None` 时 `_pair()` 返回 (model, None, None)，原先一律塞进 unavailable，
    # 于是界面/日志上显示成"三家评委的成对判定都不可用"——**把"没跑"说成了"跑了但失败"**，
    # 这是另一种误导（真正的不可用会被淹没在噪音里）。没跑就如实记 `skipped`。
    valid_pairs = {m: v for m, (v, e) in pair_res.items() if v}
    _pair_skipped = []
    for m, (v, e) in pair_res.items():
        if v:
            continue
        if baseline is None:
            _pair_skipped.append(m)          # 没提供基线 → 本就该跳过，不是故障
        else:
            result["unavailable"].append({"model": m, "stage": "成对判定", "error": e})
    if _pair_skipped:
        result["pairwise_skipped"] = {
            "reason": "未提供 baseline（成对强制选择需要两份材料作对照），本层按设计跳过",
            "models": _pair_skipped}
    wins = sum(1 for v in valid_pairs.values() if v["winner"] == "甲")
    ties = sum(1 for v in valid_pairs.values() if v["winner"] == "无差别")
    losses = sum(1 for v in valid_pairs.values() if v["winner"] == "乙")
    if valid_pairs:
        result["pairwise"] = {"n": len(valid_pairs), "win": wins, "tie": ties, "loss": losses,
                              "per_model": {m: {"winner": v["winner"],
                                                "two_order_score": v.get("two_order_score")}
                                            for m, v in valid_pairs.items()}}
        if wins > losses:
            result["pairwise"]["verdict"] = "相对基线：胜"
        elif wins < losses:
            result["pairwise"]["verdict"] = "相对基线：负"
        else:
            result["pairwise"]["verdict"] = "相对基线：无差别"

    result["models_used"] = sorted(used)

    # ---- 第 4 层：风险栏（模型主动生成；独立栏，不进任何判定）----
    # ⚠️ 原先我从"弱/无依据的维度"推导 —— 那是**偏离批准设计**的：
    #    设计写明要「主动列这份报告最可能在哪误导你」，且维度全中/强时该栏会变空。
    #    现改为汇总三家主动列出的风险，按 risk 文本去重。
    risks, _seen = [], set()
    for m in REVIEWERS:
        p0 = dim_res.get(m, (None, None))[0]
        if not isinstance(p0, dict):
            continue
        for r in (p0.get("risks") or []):
            if not isinstance(r, dict):
                continue
            key = re.sub(r"\s+", "", str(r.get("risk") or ""))[:30]
            if not key or key in _seen:
                continue
            _seen.add(key)
            risks.append({"risk": str(r.get("risk"))[:200],
                          "if_happens": str(r.get("if_happens"))[:200],
                          "evidence": str(r.get("evidence") or "")[:180],
                          "from_model": m})
    result["risk"]["items"] = risks
    result["risk"]["n"] = len(risks)

    # ---- 第 5 层：独有贡献计数（需要 baseline）----
    if baseline is not None:
        try:
            def _claims(text):
                """抽主张清单。⚠️ 必须给足 max_tokens：
                实测默认 4000 会把 50 条主张的 JSON **截断** → 解析失败
                （单独测给 8000 就完全正常，50/50 引用可核对）。
                故：先 8000；解析失败再放开 16000 重试一次。"""
                u = CLAIM_PROMPT + "\n\n===== 报告 =====\n" + text
                for mt in (8000, 16000):
                    rr = call_model(REVIEWERS[0], "你是文本分析员。", u, max_tokens=mt)
                    if not rr.get("ok"):
                        continue
                    pp = _parse(rr["text"])
                    if isinstance(pp, dict) and isinstance(pp.get("claims"), list):
                        return pp
                return None

            p1 = _claims(sent)
            p2 = _claims(baseline)
            if p1 and p2:
                cl1 = [c for c in (p1.get("claims") or []) if isinstance(c, dict)]
                cl2 = [c for c in (p2.get("claims") or []) if isinstance(c, dict)]
                # 逐字核对 quote 是否真在原文里（幻觉条作废）
                v1 = [c for c in cl1 if (c.get("quote") or "") and
                      re.sub(r"\s+", "", c["quote"])[:20] in re.sub(r"\s+", "", sent)]
                v2 = [c for c in cl2 if (c.get("quote") or "") and
                      re.sub(r"\s+", "", c["quote"])[:20] in re.sub(r"\s+", "", baseline)]
                t1 = {re.sub(r"\s+", "", c["claim"]) for c in v1}
                t2 = {re.sub(r"\s+", "", c["claim"]) for c in v2}
                only1 = [c for c in v1 if re.sub(r"\s+", "", c["claim"]) not in t2]
                only2 = [c for c in v2 if re.sub(r"\s+", "", c["claim"]) not in t1]
                result["unique_contrib"] = {
                    "count": len(only1), "items": [c.get("claim", "")[:120] for c in only1],
                    "baseline_only_count": len(only2),
                    "claims_extracted": len(cl1), "claims_quote_verified": len(v1),
                    "hallucination_rate": round(1 - (len(v1) / len(cl1)), 3) if cl1 else None,
                }
            else:
                result["errors"].append("独有贡献计数：主张抽取失败")
        except Exception as e:
            result["errors"].append(f"独有贡献计数失败：{type(e).__name__}: {str(e)[:120]}")

    result["elapsed_s"] = round(time.time() - t0, 1)

    # ---- 硬校验（不用裸 assert）----
    if result["scores_hidden"] is False and result["anchors_passed"] is not True:
        raise AssertionError("锚点未过却产出了分数 —— 逻辑出错，拒绝返回")
    if result["scores_hidden"] is True and result["dimensions"]:
        raise AssertionError("scores_hidden=True 时 dimensions 必须为空")
    if result["anchors_passed"] is not True and result["dimensions"]:
        raise AssertionError("锚点未过却带回了维度结果")

    return result


if __name__ == "__main__":
    import sys
    txt = sys.stdin.read() if not sys.argv[1:] else open(sys.argv[1], encoding="utf-8").read()
    out = v6_review(txt, topic=os.getenv("V6_TOPIC", ""))
    print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
