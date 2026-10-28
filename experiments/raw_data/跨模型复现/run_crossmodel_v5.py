# -*- coding: utf-8 -*-
"""跨模型复现性实验 v5（过夜版）
设计见 PREREGISTRATION.md

因子：模型 × 思考(开/关) × 三臂 × 题目 × 种子
  三臂： E1_异构（6人异构） / E0_零异构（6人同构） / C_单次直答（最弱基线）

默认只用 qwen + deepseek（免费额度）；GLM 无额度，用 ENABLE_GLM=1 才启用。
"""
import sys, os, json, time, random, importlib.util, hashlib
from datetime import datetime

ROOT = "/home/ubuntu/qunzhi"
EXP = "/home/ubuntu/qunzhi/experiments/2026-09-17_跨模型复现"
RUNS = os.path.join(EXP, "runs_v5")
os.makedirs(RUNS, exist_ok=True)


def _load_env(p="/home/ubuntu/qunzhi/.env"):
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()
import requests

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sandbox"))
spec = importlib.util.spec_from_file_location("eng", os.path.join(ROOT, "sandbox", "app.py"))
eng = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eng)
ENGINE_MD5 = hashlib.md5(open(os.path.join(ROOT, "sandbox", "app.py"), "rb").read()).hexdigest()

DS_URL = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/") + "/chat/completions"
DS_KEY = os.getenv("QWEN_API_KEY", "")
ZP_URL = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/") + "/chat/completions"
ZP_KEY = os.getenv("GLM_API_KEY", "")

ENABLE_GLM = os.getenv("ENABLE_GLM", "0") == "1"

ENDPOINTS = {
    "qwen": {"url": DS_URL, "key": DS_KEY, "model": os.getenv("EXP_MODEL_QWEN", "qwen3.8-flash"),
             "channel": "阿里云百炼"},
    "deepseek": {"url": DS_URL, "key": DS_KEY, "model": os.getenv("EXP_MODEL_DEEPSEEK", "deepseek-v4.1-flash"),
                 "channel": "阿里云百炼"},
}
if ENABLE_GLM:
    ENDPOINTS["glm"] = {"url": ZP_URL, "key": ZP_KEY,
                        "model": os.getenv("EXP_MODEL_GLM", "glm-5.3-flash"), "channel": "智谱官方"}

THINK_OFF = {
    "qwen": {"enable_thinking": False},
    "deepseek": {"enable_thinking": False},
    "glm": {"reasoning_effort": "low"},
}
THINK_MODES = ["off", "on"]
CAP = int(os.getenv("MAXTOK_CAP", "16000"))
BASE_MT = int(os.getenv("BASE_MAXTOK", "6000"))


def make_caller(mkey, think):
    ep = ENDPOINTS[mkey]
    url, key, model = ep["url"], ep["key"], ep["model"]
    ctl = THINK_OFF.get(mkey, {}) if think == "off" else {}
    st = {"calls": 0, "failed": 0, "p": 0, "c": 0, "empty": 0, "grow": 0, "reason": 0,
          "returned": set(), "http_err": [], "exc": [], "ctl_off": False}

    def _call(prompt, max_tokens=400, temperature=0.75, timeout=15, retries=3):
        mt = min(max(max_tokens, 1200), CAP) if max_tokens <= 200 else min(max(max_tokens, BASE_MT), CAP)
        msgs = [{"role": "system", "content": prompt},
                {"role": "user", "content": "请按上述要求直接输出结果。"}]
        ct = max(timeout, 5) + 60
        for attempt in range(retries + 1):
            body = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": mt}
            if ctl and not st["ctl_off"]:
                body.update(ctl)
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {key}",
                                                "Content-Type": "application/json"},
                                  json=body, timeout=ct)
                if r.status_code == 200:
                    d = r.json()
                    msg = d["choices"][0]["message"]
                    txt = (msg.get("content") or "").strip()
                    rc = (msg.get("reasoning_content") or "").strip()
                    u = d.get("usage") or {}
                    st["p"] += u.get("prompt_tokens", 0) or 0
                    st["c"] += u.get("completion_tokens", 0) or 0
                    st["returned"].add(d.get("model") or "?")
                    if rc:
                        st["reason"] += int(len(rc) / 1.5)
                    if txt:
                        st["calls"] += 1
                        return txt
                    st["empty"] += 1
                    if rc and mt < CAP:
                        mt = min(max(mt * 6, 4000), CAP)
                        st["grow"] += 1
                        continue
                else:
                    try:
                        ec = r.json().get("error", {})
                        code = str(ec.get("code", ""))
                        st["http_err"].append(f"{r.status_code}:{code}")
                        if r.status_code == 400 and ctl and not st["ctl_off"]:
                            st["ctl_off"] = True
                            st["http_err"].append("ctl_off")
                    except Exception:
                        st["http_err"].append(str(r.status_code))
            except Exception as e:
                st["exc"].append(type(e).__name__)
            if attempt < retries:
                time.sleep(1.5)
        st["failed"] += 1
        return None      # ❗ 不回落

    return _call, st


TOPICS = {
    "T1": "如何提升师范生课堂参与度？",
    "T2": "大学生用AI写作业，学校该禁止还是引导？",
    "T3": "Buck变换器输出电压纹波超标，如何改进控制策略？",
    "T4": "分布式缓存的冷热数据分层架构如何设计？",
}
SEEDS = [1, 2]
MODE = {"v15": True, "discussion_mode": "structured", "info_mode": "real",
        "personality_mode": "optimal", "personality_enabled": True}
PROTOS = ["hunter", "skeptic", "gambler", "trader", "ghost", "critic"]
NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]
CONFIG_A = {
    "甲": {"proto": "hunter",  "prof": 0, "value": 0},
    "乙": {"proto": "skeptic", "prof": 1, "value": 5},
    "丙": {"proto": "gambler", "prof": 2, "value": 2},
    "丁": {"proto": "trader",  "prof": 3, "value": 4},
    "戊": {"proto": "ghost",   "prof": 4, "value": 5},
    "己": {"proto": "critic",  "prof": 5, "value": 1},
}
CONFIGS = {
    "E1_异构":   {k: dict(v) for k, v in CONFIG_A.items()},
    "E0_零异构": {NAMES[i]: {"proto": "hunter", "prof": 0, "value": 0} for i in range(6)},
}
# 三臂：两个多智能体条件 + 一个最弱基线
ARMS = ["E1_异构", "E0_零异构", "C_单次直答"]

SINGLE_PROMPT = """你是资深决策顾问。课题：{topic}

请给出你的分析与建议：①核心判断 ②行动方案（短中长期）③主要风险 ④提问者可能忽略的角度（3条）。
直接输出正文，不要客套话。"""


def run_single(topic, seed):
    """C 臂：单次直答（最弱基线，1 次调用）
    注意：必须 return 结果。曾漏写 return 导致 C 臂 32 次全部空报告（已修）。"""
    txt = eng.call_with_retry(SINGLE_PROMPT.format(topic=topic), max_tokens=6000,
                              temperature=0.7, timeout=180, retries=3)
    return (txt or "").strip()


def run_one(arm, topic, seed):
    if arm == "C_单次直答":
        return run_single(topic, seed)
    random.seed(seed)
    sess = eng.SandboxSession(topic, CONFIGS[arm], 3, "", custom_infos=None,
                              preset_name="通用科研", experiment_mode=MODE,
                              force_dynamic_info=False, coach_interventions=[],
                              lite_mode=False)
    sess.run()
    return sess.report or ""


DRIFT_PROMPT = "请只回复四个字：稳定可复现"


def drift_probe(tag):
    out = {}
    for mkey, ep in ENDPOINTS.items():
        for think in THINK_MODES:
            body = {"model": ep["model"], "messages": [{"role": "user", "content": DRIFT_PROMPT}],
                    "max_tokens": 800, "temperature": 0.0}
            if think == "off" and mkey in THINK_OFF:
                body.update(THINK_OFF[mkey])
            k = f"{mkey}_{think}"
            try:
                r = requests.post(ep["url"], headers={"Authorization": f"Bearer {ep['key']}",
                                                      "Content-Type": "application/json"},
                                  json=body, timeout=150)
                if r.status_code == 200:
                    d = r.json(); msg = d["choices"][0]["message"]
                    txt = (msg.get("content") or "").strip()
                    rc = (msg.get("reasoning_content") or "").strip()
                    out[k] = {"declared": ep["model"], "channel": ep["channel"],
                              "served": d.get("model"), "reply": txt[:30],
                              "reasoning_chars": len(rc),
                              "md5": hashlib.md5(txt.encode("utf-8")).hexdigest()[:12]}
                else:
                    out[k] = {"declared": ep["model"], "error": f"HTTP {r.status_code}"}
            except Exception as e:
                out[k] = {"declared": ep["model"], "error": type(e).__name__}
    json.dump({"tag": tag, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "probe": out},
              open(os.path.join(EXP, f"drift_v5_{tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"[漂移/{tag}] " + " | ".join(
        f"{k}={v.get('served','?')}(思维{v.get('reasoning_chars','-')})" for k, v in out.items()),
        flush=True)
    return out


rounds = []
for tid in TOPICS:
    for s in SEEDS:
        combos = [(mk, th, arm) for mk in ENDPOINTS for th in THINK_MODES for arm in ARMS]
        random.Random(f"{tid}-{s}").shuffle(combos)
        rounds.append((tid, s, combos))

TOTAL = sum(len(c) for _, _, c in rounds)
COST_CAP = 8_000_000

mfp = os.path.join(EXP, "manifest_v5.json")
if os.path.exists(mfp):
    man = json.load(open(mfp, encoding="utf-8"))
    man["runs"] = [r for r in man.get("runs", []) if r.get("status") == "ok"]
else:
    man = {"experiment": "跨模型复现性实验 v5（过夜版：模型 × 思考开关 × 三臂）",
           "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "preregistration": "PREREGISTRATION.md", "engine_md5": ENGINE_MD5,
           "endpoints": {k: {"channel": v["channel"], "model": v["model"]} for k, v in ENDPOINTS.items()},
           "glm_enabled": ENABLE_GLM,
           "think_off_params": THINK_OFF,
           "design": {"arms": ARMS, "topics": list(TOPICS), "seeds": SEEDS,
                      "think_modes": THINK_MODES, "runs_total": TOTAL},
           "history_note": ("历史 172 次实验用 deepseek-chat（非推理，思维链 0 字），"
                            "事实上等于「无思考」档，但当时未作为受控因子"),
           "runs": []}

done = {r["run_id"] for r in man["runs"]}
tok_total = sum(r.get("tokens", 0) for r in man["runs"])
i = 0
t_start = time.time()
D0 = drift_probe("start")

for tid, s, combos in rounds:
    for mkey, think, arm in combos:
        caller, st = make_caller(mkey, think)
        eng.call_with_retry = caller
        i += 1
        tag = f"{mkey}_{think}_{arm}_{tid}_s{s}"
        path = os.path.join(RUNS, f"{tag}.json")
        if tag in done or os.path.exists(path):
            continue
        print(f"[{i}/{TOTAL}] {tag} ...", flush=True)
        t0 = time.time()
        try:
            report = run_one(arm, TOPICS[tid], s) or ""
            rec = {"run_id": tag, "model_key": mkey, "think": think, "arm": arm,
                   "topic_id": tid, "topic": TOPICS[tid], "seed": s,
                   "channel": ENDPOINTS[mkey]["channel"],
                   "declared_model": ENDPOINTS[mkey]["model"],
                   "status": "ok" if report else "empty", "report": report,
                   "duration_s": round(time.time() - t0, 1),
                   "returned_models": sorted(st["returned"]),
                   "api_calls": st["calls"], "failed_calls": st["failed"],
                   "empty_content": st["empty"], "budget_grow": st["grow"],
                   "reasoning_tokens_est": st["reason"], "think_ctl_off": st["ctl_off"],
                   "http_errors": st["http_err"][:10], "exceptions": st["exc"][:10],
                   "prompt_tokens": st["p"], "completion_tokens": st["c"],
                   "total_tokens": st["p"] + st["c"]}
            tmp = path + ".tmp"
            json.dump(rec, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            tok_total += rec["total_tokens"]
            print(f"      ok: {len(report)} 字 / {rec['api_calls']} 调用 / "
                  f"{rec['failed_calls']} 失败 / {rec['total_tokens']:,} tok "
                  f"(思维约{rec['reasoning_tokens_est']:,}) / {rec['duration_s']}s", flush=True)
            man["runs"].append({"run_id": tag, "status": rec["status"], "model_key": mkey,
                                "think": think, "arm": arm, "tokens": rec["total_tokens"],
                                "duration_s": rec["duration_s"], "api_calls": rec["api_calls"],
                                "failed_calls": rec["failed_calls"]})
            json.dump(man, open(mfp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception as e:
            import traceback
            traceback.print_exc()
            man["runs"].append({"run_id": tag, "status": "failed", "error": str(e)[:200]})
            json.dump(man, open(mfp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if tok_total > COST_CAP:
            print(f"\n⚠️ 达成本上限 {COST_CAP:,}，停止")
            raise SystemExit(0)

D1 = drift_probe("end")
same = {k: (D0.get(k, {}).get("served") == D1.get(k, {}).get("served")
            and D0.get(k, {}).get("md5") == D1.get(k, {}).get("md5")) for k in D0}
print("\n[漂移对比] " + " | ".join(f"{k}: {'一致' if v else '不一致 ⚠️'}" for k, v in same.items()))
man["drift_start"], man["drift_end"], man["drift_consistent"] = D0, D1, same
json.dump(man, open(mfp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

ok = [r for r in man["runs"] if r["status"] == "ok"]
print(f"\n=== 完成 ===  成功 {len(ok)}/{TOTAL}  token {tok_total:,}  "
      f"耗时 {(time.time()-t_start)/60:.1f} 分钟")
