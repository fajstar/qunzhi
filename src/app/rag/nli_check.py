# -*- coding: utf-8 -*-
"""NLI 蕴含核验（检索链路 B7）
================================================================
**要解决的问题**：现在只核验"有没有出处"（编号存在与否），不核验
**"出处是否真的支持这句话"**。ALCE（arXiv 2305.14627）用 NLI 判"引用句是否被被引文档蕴含"，
是最贴近我们需求的指标；AIS（TACL 2023）补了一条更锋利的定义：
只判"人类能否仅凭该来源验证该论断"，**与"论断是否为真"解耦**。

所以本模块只回答一个问题：

    给定报告里的一句话 + 它引用的那条来源的原文，
    一个**没见过讨论过程**的独立模型，能否仅凭这条来源认定该句成立？

三条铁律
--------
1. **判定器必须独立——且独立不可用时宁可不算，也绝不回落。**
   这不是洁癖。`review_v3._call_model` 在 GLM 失败时会**静默回落 DeepSeek**；
   而我们报告的生成者正是 DeepSeek。一旦回落，就变成"生成器给自己打分"，
   指标看似有值、实则自证——比没有指标更糟。
   本模块因此**自建调用器**：跨模型家族不回落，独立判定器不可用 → 返回
   `status="no_independent_judge"`，一条都不判。

2. **判定只依据来源原文，不给判定器看讨论过程。** 提示词里只放
   「这句话」+「这条来源的标题/摘要」两项，避免它被上下文说服。

3. **判不了要能说判不了。** 来源没有摘要/正文时返回 `not_checkable`，
   不计入 precision/recall 的分母——否则会把"我们没存正文"混成"模型判断力差"。

校准（方案 §7.3 B7 的验收标准：人工抽样 50 句，报 precision/recall）
------------------------------------------------------------------
    python _nli_calibrate.py sample 50      # 抽 50 句，产出待标注 CSV
    # 人工把 gold 列填上：1=该来源确实支持，0=不支持
    python _nli_calibrate.py score          # 算 precision / recall / 一致性

成本：一次判定 1 次小模型调用（约 300 token）。50 句 ≈ ¥0.05–0.15。
"""
import json
import os
import re
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VERSION = "nli_check/1.0 (2026-10-10)"

# ---------------------------------------------------------------- 模型登记
# 开放兼容端点：DeepSeek / 智谱 GLM / 阿里云百炼 Qwen 都是 OpenAI 兼容协议
_MODELS = {
    "deepseek": {
        "family": "deepseek",
        "url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1") + "/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default": "deepseek-chat",
    },
    "glm": {
        "family": "zhipu",
        "url": os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4") + "/chat/completions",
        "key_env": "GLM_API_KEY",
        "model_env": "GLM_MODEL",
        "default": "glm-4-flash",
    },
    "qwen": {
        "family": "qwen",
        "url": os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
               + "/chat/completions",
        "key_env": "QWEN_API_KEY",
        "model_env": "QWEN_MODEL",
        "default": "qwen3.8-flash",
    },
}

# 家族判定：模型名里出现这些词就归属该家族（用于"生成器 vs 判定器"的独立性判断）
_FAMILY_HINTS = (
    ("deepseek", "deepseek"),
    ("glm", "zhipu"), ("zhipu", "zhipu"), ("chatglm", "zhipu"),
    ("qwen", "qwen"), ("tongyi", "qwen"),
    ("gpt", "openai"), ("claude", "anthropic"), ("gemini", "google"),
)


def family_of(model_name):
    """模型名 → 家族。认不出就返回名字本身（保守：不同名即视为不同家族）。"""
    n = str(model_name or "").strip().lower()
    for hit, fam in _FAMILY_HINTS:
        if hit in n:
            return fam
    return n or "unknown"


def _looks_like_key(k):
    """占位符/中文 key 放进 HTTP Header 会触发 latin-1 编码错误——直接判为不可用。"""
    s = str(k or "").strip()
    if len(s) < 12:
        return False
    if not re.match(r"^[\x20-\x7e]+$", s):
        return False
    return not re.search(r"your[-_]?key|here|xxxx|placeholder|未配置", s, re.I)


def available_judges():
    """可用（Key 合法）的判定器 → {别名: 模型名}。"""
    out = {}
    for alias, cfg in _MODELS.items():
        if _looks_like_key(os.getenv(cfg["key_env"], "")):
            out[alias] = os.getenv(cfg["model_env"], cfg["default"])
    return out


def pick_independent_judge(generator_model, prefer=("qwen", "glm", "deepseek")):
    """挑一个**与生成器不同家族**的判定器。

    返回 (alias, model_name, note)。挑不到 → (None, None, 原因)。
    **绝不返回与生成器同家族的模型**——宁可没有判定器。
    """
    gfam = family_of(generator_model)
    avail = available_judges()
    for alias in prefer:
        if alias not in avail:
            continue
        if family_of(avail[alias]) == gfam:
            continue
        return alias, avail[alias], u"独立判定器（生成器家族=%s）" % gfam
    same = [a for a in avail if family_of(avail[a]) == gfam]
    if avail:
        return None, None, (u"只有与生成器同家族的可用模型（%s），拒绝自评"
                            % ",".join(same or list(avail)))
    return None, None, u"没有可用的独立判定器（未配置任何跨家族 Key）"


# ---------------------------------------------------------------- 判定器调用
def _chat(alias, system, user, max_tokens=400, timeout=60, temperature=0.0):
    """**跨家族不回落**的单模型调用。失败返回 (None, 错误文本)。"""
    cfg = _MODELS.get(alias)
    if not cfg:
        return None, "unknown alias: %s" % alias
    key = os.getenv(cfg["key_env"], "")
    if not _looks_like_key(key):
        return None, "no valid key for %s" % alias
    model_name = os.getenv(cfg["model_env"], cfg["default"])
    body = json.dumps({
        "model": model_name,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=body, headers={
        "Authorization": "Bearer %s" % key,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        txt = (msg.get("content") or "").strip()
        if not txt:
            # 推理模型（如 glm-5.3）会把 token 烧在 reasoning 上、content 为空
            txt = (msg.get("reasoning_content") or "").strip()
        if not txt:
            return None, "empty content"
        return txt, ""
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])


_SYS = (u"你是学术事实核验员。你只能依据用户给出的【来源】，判断【论断】是否能被该来源支持。"
        u"你看不到任何讨论过程，也不要使用你自己的背景知识补充证据。"
        u"严格输出一行 JSON，不要任何其他文字："
        u'{"verdict":"supported|partial|unsupported|not_checkable","confidence":0-10,"reason":"不超过30字"}'
        u"\n判据：supported=来源明确支持该论断；partial=来源支持其中一部分或方向一致但强度/范围不符；"
        u"unsupported=来源不支持、或与该论断无关、或来源不足以支撑；"
        u"not_checkable=来源正文缺失或与论断主题完全无关，无法判定。"
        u"注意：只判『该来源能否支持这句话』，不判『这句话在现实中是否为真』。")

_JSON_RE = re.compile(r"\{.*\}", re.S)

# 题录式正文识别：形如「标题；作者：X；来源：Y；发表时间：Z」——只有元数据，没有实质内容。
# 事故背景（2026-10-10 实测）：中文源（知网/NCPSSD 等）注入池里 `abstract` 为 0 字，
# `content` 平均 54–65 字，**全部是这种题录**。此时报告里却写着「…交流恐惧者常把课堂发言
# 等同于被检验教学能力 [P5]」——这句话不可能来自一条题录。若不把这种情况单独识别出来，
# 判定器会对着一串题录硬判"不支持"，指标看似有值，实际测的是"我们压根没存正文"。
_META_ONLY = re.compile(
    u"^[^。！？]{2,80}[；;]\\s*作者[：:][^；;]{0,60}[；;]\\s*来源[：:][^；;]{0,60}"
    u"(?:[；;]\\s*(?:发表时间|时间|年份)[：:][^；;]{0,30})?[。\\s]*$")


def looks_like_metadata_only(text):
    """正文是否只是题录（无摘要/无实质内容）。用于把 not_checkable 与 unsupported 分开。"""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return True
    if _META_ONLY.match(t):
        return True
    # 兜底：很短、且除标题外没有任何句末标点 → 视为题录
    if len(t) < 120 and t.count(u"。") + t.count(u"！") + t.count(u"？") <= 1:
        return True
    return False


def judge_claim(claim, source_title, source_text, alias, max_tokens=400):
    """判定单条论断。返回 dict（含 verdict / confidence / reason / model / error）。

    **来源只有题录时直接判 not_checkable**，不烧模型调用、也不伪装成 unsupported——
    "我们没存正文"和"模型认为不支持"是两件事，混在一起指标就废了。
    """
    src = (source_text or "").strip()
    if len(src) < 40:
        return {"verdict": "not_checkable", "confidence": 0,
                "reason": u"来源正文缺失或过短（%d 字）" % len(src), "model": "",
                "skipped": True}
    if looks_like_metadata_only(src):
        return {"verdict": "not_checkable", "confidence": 0,
                "reason": u"来源仅有题录（无摘要/正文），无从判定", "model": "",
                "skipped": True, "metadata_only": True}
    user = (u"【论断】%s\n\n【来源】标题：%s\n正文：%s" %
            (str(claim).strip()[:600], str(source_title or "")[:200], src[:2500]))
    txt, err = _chat(alias, _SYS, user, max_tokens=max_tokens)
    if txt is None:
        return {"verdict": "error", "confidence": 0, "reason": err, "model": "", "error": err}
    m = _JSON_RE.search(txt)
    if not m:
        return {"verdict": "error", "confidence": 0, "reason": u"返回非 JSON",
                "model": os.getenv(_MODELS[alias]["model_env"], _MODELS[alias]["default"]),
                "error": txt[:120]}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"verdict": "error", "confidence": 0, "reason": u"JSON 解析失败", "model": alias}
    v = str(d.get("verdict") or "").strip().lower()
    if v not in ("supported", "partial", "unsupported", "not_checkable"):
        v = "error"
    return {"verdict": v, "confidence": d.get("confidence", 0),
            "reason": str(d.get("reason") or "")[:120],
            "model": os.getenv(_MODELS[alias]["model_env"], _MODELS[alias]["default"])}


# ---------------------------------------------------------------- 论断抽取
_SENT_SPLIT = re.compile(u"(?<=[。！？!?；;])\\s*")
_CITE = re.compile(r"\[(P\d+)\]")


def extract_claims(report, pool, limit=None):
    """从报告里抽出**带编号引用**的句子，并配好被引来源的正文。

    只收"句子里有 [Pn]"的；一个句子里多个编号时，为每个编号各出一条
    （一句话引两个来源、只被一个支持，正是最该被抓到的情况）。
    """
    by_no = {}
    for rec in (pool or []):
        no = str(rec.get("ref_no") or "").strip()
        if no:
            by_no[no] = rec
    out = []
    for para in str(report or "").split("\n"):
        p = para.strip()
        if not p or p.startswith("#") or p.startswith("|"):
            continue
        for sent in _SENT_SPLIT.split(p):
            s = sent.strip()
            if not s or len(s) < 8:
                continue
            nos = _CITE.findall(s)
            if not nos:
                continue
            for no in nos:
                rec = by_no.get(no)
                if rec is None:
                    out.append({"sentence": s, "ref_no": no, "source_title": "",
                                "source_text": "", "phantom": True})
                    continue
                out.append({
                    "sentence": s, "ref_no": no,
                    "source_title": rec.get("title") or "",
                    "source_text": (rec.get("abstract") or rec.get("content") or ""),
                    "phantom": False,
                })
    return out[:limit] if limit else out


# ---------------------------------------------------------------- 主入口
def check_report(report, pool, generator_model=None, limit=50, judge=None,
                 sleep=0.0, verbose=False):
    """对报告做一遍蕴含核验。

    返回 dict：
      status            ok / no_independent_judge / no_claims
      judge / generator 实际使用的判定器与生成器
      independence      True 表示判定器与生成器不同家族
      claims            逐条结果
      summary           计数与比例
    **判定器不独立时 status=no_independent_judge 且 claims 为空——不产出任何"分数"。**
    """
    gen = generator_model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    if judge:
        alias = judge
        jname = os.getenv(_MODELS[judge]["model_env"], _MODELS[judge]["default"]) if judge in _MODELS else judge
        note = u"调用方指定"
    else:
        alias, jname, note = pick_independent_judge(gen)
    if not alias:
        return {"status": "no_independent_judge", "generator": gen, "judge": None,
                "independence": False, "note": note, "claims": [], "summary": {}}

    claims = extract_claims(report, pool, limit=limit)
    if not claims:
        return {"status": "no_claims", "generator": gen, "judge": jname,
                "independence": True, "note": note, "claims": [], "summary": {}}

    results = []
    for i, c in enumerate(claims, 1):
        if c.get("phantom"):
            r = {"verdict": "unsupported", "confidence": 10,
                 "reason": u"引用了信息池中不存在的编号（疑编造）", "model": ""}
        else:
            r = judge_claim(c["sentence"], c["source_title"], c["source_text"], alias)
        row = dict(c)
        row.update(r)
        results.append(row)
        if verbose:
            print("  [%2d/%d] %s %s ← %s" % (i, len(claims), c["ref_no"],
                                             r["verdict"], (r.get("reason") or "")[:40]), flush=True)
        if sleep:
            time.sleep(sleep)

    cnt = {}
    meta_only = 0
    for r in results:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
        if r.get("metadata_only"):
            meta_only += 1
    judged = cnt.get("supported", 0) + cnt.get("partial", 0) + cnt.get("unsupported", 0)
    summ = {
        "n_claims": len(results),
        "supported": cnt.get("supported", 0),
        "partial": cnt.get("partial", 0),
        "unsupported": cnt.get("unsupported", 0),
        "not_checkable": cnt.get("not_checkable", 0),
        "metadata_only": meta_only,      # 来源只有题录——**这是"证据链缺口"，不是模型判断力问题**
        "error": cnt.get("error", 0),
        "judged": judged,
        # 严格口径：只有 supported 算"被支持"
        "support_rate": round(cnt.get("supported", 0) / judged, 3) if judged else None,
        # 宽松口径：partial 也算
        "support_rate_lenient": round((cnt.get("supported", 0) + cnt.get("partial", 0)) / judged, 3)
                                 if judged else None,
    }
    return {"status": "ok", "generator": gen, "judge": jname, "independence": True,
            "note": note, "claims": results, "summary": summ,
            "version": VERSION, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def summarize_line(res):
    """一行摘要（日志/报告用）。"""
    if res.get("status") != "ok":
        return u"[蕴含核验] 未执行：%s（%s）" % (res.get("status"), res.get("note", ""))
    s = res["summary"]
    tail = u""
    if s.get("metadata_only"):
        tail = u"｜⚠ 其中 %d 句的来源**只有题录**（无摘要正文），属证据链缺口" % s["metadata_only"]
    return (u"[蕴含核验] 判定器 %s（生成器 %s，独立）｜%d 句：支持 %d / 部分 %d / 不支持 %d / "
            u"判不了 %d / 失败 %d｜严格支持率 %s%s") % (
        res["judge"], res["generator"], s["n_claims"], s["supported"], s["partial"],
        s["unsupported"], s["not_checkable"], s["error"], s["support_rate"], tail)
