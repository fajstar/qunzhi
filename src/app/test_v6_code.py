# -*- coding: utf-8 -*-
"""群智评审机制 v6 · 代码验证测试 T1–T12

对应 code_reg_v6 预注册里写死的 9 项测试。**一条命令跑完，逐条打印通过/失败。**

跑法：
    cd /home/ubuntu/qunzhi/app && python3 /path/to/test_v6_code.py
    （也可带 --offline 只跑不花模型钱的确定性测试）

设计约束（来自预注册）：
  · T1 以 health 端点为准（日志可能被吞，不能当证据）
  · T7 按**实测契约**断言（顶层 ok/review/v3），不比 hash
  · T8 比对 `review_v3.py` 的 **sha256 基线**（写在基线文件里）
  · T9 扫描源码，确认 5 条断言以 `if not …: raise` 形式存在，且**全文不得有裸 assert**
"""
import hashlib
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

APP = "/home/ubuntu/qunzhi/app"
BASE_URL = "http://127.0.0.1:5020"
ACCESS_KEY = os.environ.get("QUNZHI_ACCESS_KEY", "CHANGE_ME_ACCESS_KEY")
BASELINE_JSON = "/home/ubuntu/qunzhi/experiments/gate/baseline_and_api.json"
# ⚠️ 两个被检路径提到模块级：T10 与 T11 都要用。若只在 T10 的 try 里定义，
#    T10 一旦早退，T11 就会 NameError —— 那会变成"测试自己坏了"，掩盖真问题。
EXP_PATH = "/home/ubuntu/qunzhi/experiments/gate/run_v6_experiment.py"
APP_V6 = f"{APP}/review_v6.py"

try:
    import requests
except Exception:
    requests = None

OFFLINE = "--offline" in sys.argv

results = []


def rec(tid, name, passed, note=""):
    results.append({"id": tid, "name": name, "passed": bool(passed), "note": note})
    print(f"  {'✅' if passed else '❌'} {tid} {name}" + (f"　{note}" if note else ""))


def read(p):
    return open(p, encoding="utf-8").read()


print("=" * 92)
print(f"v6 代码验证测试 T1–T12　{'（离线模式：只跑确定性测试）' if OFFLINE else '（含联网测试）'}")
print("=" * 92)

# ---------------- 确定性测试（不花钱、不需服务）----------------

# ---- T8：旧文件一行没改（比对 sha256 基线）----
try:
    base = json.load(open(BASELINE_JSON, encoding="utf-8"))
    want = base.get("review_v3.py", {}).get("sha256_full")
    got = hashlib.sha256(open(f"{APP}/review_v3.py", "rb").read()).hexdigest()
    rec("T8", "旧文件 review_v3.py 一行没改",
        (want is not None and got == want),
        f"sha256={got[:16]}（基线 {str(want)[:16]}）")
except Exception as e:
    rec("T8", "旧文件 review_v3.py 一行没改", False, f"比对失败：{str(e)[:100]}")

# ---- T9：断言存在性 + 禁止裸 assert ----
try:
    src_v6 = read(f"{APP}/review_v6.py")
    src_in = read(f"{APP}/review_input.py")
    allsrc = src_v6 + "\n" + src_in
    checks = {
        "F1-成功": ("models_used" in src_v6 and "len(" in src_v6),
        "F1-失败": ("unavailable" in src_v6 and "dimensions" in src_v6),
        "F2": ("sent_len" in src_in and "raw_len" in src_in and "0.98" in src_in),
        "F3": ("anchors_passed" in src_v6 and "dimensions" in src_v6),
        "F5": ("same_family_warning" in src_v6 and "GENERATOR_FAMILY" in src_v6),
    }
    missing = [k for k, v in checks.items() if not v]
    # 裸 assert 扫描（必须一条都没有）
    bare = [(f, i + 1) for f, s in (("review_v6.py", src_v6), ("review_input.py", src_in))
            for i, l in enumerate(s.splitlines()) if re.match(r"\s*assert\s", l)]
    rec("T9", "5 条断言在码里且无裸 assert",
        (not missing) and (not bare),
        f"缺={missing or '无'}；裸assert={bare or '无'}")

    # 反向自检：真的"报告里说存在、码里也有"
    rec("T9b", "断言用 if not … raise 形式（非裸 assert）",
        allsrc.count("raise AssertionError") >= 4,
        f"raise AssertionError 出现 {allsrc.count('raise AssertionError')} 次")
except Exception as e:
    rec("T9", "断言存在性扫描", False, str(e)[:120])

# ---- T4：截断率断言（本地构造，不需服务）----
try:
    sys.path.insert(0, APP)
    from review_input import prepare
    long_text = "这是一段用于测试的很长的报告内容。" * 500      # 约 9,000 字
    p = prepare(long_text)
    rec("T4a", "长报告不被截断（sent ≥ 98% raw）",
        p["sent_len"] >= 0.98 * p["raw_len"] and p["truncated"] is False,
        f"raw={p['raw_len']} sent={p['sent_len']} 段数={len(p['segments'])}")
    # 超长（>1 段上限）也要不丢内容
    verylong = "另一段用于测试超长分段的文本。" * 2000          # 约 3.2 万字
    p2 = prepare(verylong)
    joined = "".join(p2["segments"])
    rec("T4b", "超长报告分段后不丢内容",
        len(joined) == p2["raw_len"] and p2["sent_len"] >= 0.98 * p2["raw_len"],
        f"raw={p2['raw_len']} 拼接={len(joined)} 段数={len(p2['segments'])}")
    # 占位：单段原文必须逐字一致
    p3 = prepare("短报告。")
    rec("T4c", "短报告逐字一致", p3["segments"] == ["短报告。"])
except Exception as e:
    rec("T4", "截断率断言", False, f"{type(e).__name__}: {str(e)[:120]}")

# ---- 锚点模块的判据逻辑（用假模型，不花钱）----
try:
    sys.path.insert(0, APP)
    from review_anchors import check_anchors, TIE_THRESHOLD

    # 假模型①：永远选"甲" → 锚点甲（完整在甲位）会赢，但换序后也选甲 → 两序平均 0.5 → 平局
    def fake_always_jia(a, b):
        return {"winner": "甲", "model": "fake"}

    # 假模型②：永远判无差别 → 锚点乙应通过；锚点甲应失败
    def fake_always_tie(a, b):
        return {"winner": "无差别", "model": "fake"}

    mats = {"original": "完整报告内容" * 50, "cut60": "被删减的", "self": "同一份内容" * 50}
    r1 = check_anchors(fake_always_tie, materials=mats)
    rec("T2/T3-甲", "法一：恒判无差别时，锚点甲应失败（完整版没胜）",
        r1["detail"]["anchor_a_passed"] is False,
        f"甲多数决={r1['detail']['anchor_a_majority']}")
    rec("T2/T3-乙", "法一：恒判无差别时，锚点乙应通过（平局够多）",
        r1["detail"]["anchor_b_passed"] is True,
        f"平局 {r1['detail']['anchor_b_ties']}/{r1['detail']['anchor_b_total']}，阈值 {TIE_THRESHOLD}")
    rec("T2/T3-总", "两道都过才 passed=True；这里甲失败故应为 False",
        r1["passed"] is False)
except Exception as e:
    rec("T2/T3", "锚点判据逻辑", False, f"{type(e).__name__}: {str(e)[:150]}")

# ---------------- 联网测试 ----------------

if not OFFLINE:
    # ---- T1：health 端点（生效证据的唯一判据）----
    try:
        r = requests.get(f"{BASE_URL}/api/review/v6/health",
                         headers={"X-Access-Key": ACCESS_KEY}, timeout=20)
        d = r.json() if r.status_code == 200 else {}
        rec("T1", "health 端点证明 v6 已注册",
            (r.status_code == 200 and d.get("v6") is True
             and isinstance(d.get("reviewers"), list) and len(d["reviewers"]) == 3),
            f"HTTP {r.status_code} v6={d.get('v6')} reviewers={len(d.get('reviewers') or [])}")
    except Exception as e:
        rec("T1", "health 端点", False, f"{type(e).__name__}: {str(e)[:100]}")

    # ---- T7：向后兼容（按实测契约断言）----
    try:
        body = {"report": "课题：如何提升师范生课堂参与度？\n建议：预演式参与、同伴互评、匿名氛围调查。",
                "topic": "如何提升师范生课堂参与度？"}
        r = requests.post(f"{BASE_URL}/api/review/evaluate", json=body,
                          headers={"X-Access-Key": ACCESS_KEY}, timeout=600)
        d = r.json() if r.status_code == 200 else {}
        top_ok = set(d.keys()) == {"ok", "review", "v3"}
        need_paths = ["topic", "review_time", "scores", "audit_scores",
                      "methodology_scores", "models_used", "v3"]
        rev = d.get("review") or {}
        miss = [k for k in need_paths if k not in rev]
        rec("T7", "旧端点结构未变（顶层 ok/review/v3，review 内 7 个必需 key）",
            top_ok and not miss,
            f"顶层={sorted(d.keys())} 缺={miss or '无'}")
    except Exception as e:
        rec("T7", "旧端点兼容性", False, f"{type(e).__name__}: {str(e)[:120]}")

    # ---- T5：同族标红 ----
    try:
        sys.path.insert(0, APP)
        import review_v6 as V
        fam_hit = [m for m in V.REVIEWERS if any(g in m.lower() for g in V.GENERATOR_FAMILY)]
        # 三个评审都不该与生成端同族
        rec("T5", "评审组无同族（生成端 DeepSeek 系）",
            not fam_hit, f"同族={fam_hit or '无'}；评审组={V.REVIEWERS}")
    except Exception as e:
        rec("T5", "同族检测", False, f"{type(e).__name__}: {str(e)[:120]}")

    # ---- T6：不回落 ----------------
    try:
        sys.path.insert(0, APP)
        import review_v6 as V
        r = V.call_model("不存在的模型-xyz", "s", "u", max_tokens=8, tries=1)
        rec("T6", "不存在的模型 → 明确失败，不回落",
            (r.get("ok") is False) and bool(r.get("error")),
            f"ok={r.get('ok')} error={str(r.get('error'))[:60]}")
    except Exception as e:
        rec("T6", "不回落断言", False, f"{type(e).__name__}: {str(e)[:120]}")

# ---- T10：A5 中期检查（v7.0 两档 + 硬触发立即终止）----
# v6.8 加 T10 是为了覆盖「声称已实现但未验证」；
# v7.0 把 A5 改成两档、且硬触发必须**立即终止**（v6.9 代码走的是「继续跑完」，违反预注册）。
try:
    EXP = EXP_PATH
    src = open(EXP, encoding="utf-8").read()

    # T10a：三档要素齐全（两档阈值 + 逐模型 + 硬/软两个分支）
    need = {
        "硬触发常量 A5_HARD_MAX": "A5_HARD_MAX" in src,
        "软提示下界 A5_SOFT_LO": "A5_SOFT_LO" in src,
        "软提示上界 A5_SOFT_HI": "A5_SOFT_HI" in src,
        "第1批含 D3/D6": 'BATCHES = [["D3", "D6"]' in src,
        "中期检查块存在": "中期检查（A5" in src,
        "逐模型各自算": "for m in REVIEWERS:" in src and "mid_check[m]" in src,
        "硬触发分支": "c <= A5_HARD_MAX" in src,
        "软提示分支": "A5_SOFT_LO <= c <= A5_SOFT_HI" in src,
        "写入 summary": 'summary["mid_check_rules"]' in src,
        "家族口径常量 A5_HARD_MIN_MODELS": "A5_HARD_MIN_MODELS" in src,
        "家族判定（≥N 个模型）": "len(hard) >= A5_HARD_MIN_MODELS" in src,
    }
    miss = [k for k, v in need.items() if not v]
    rec("T10a", "A5 两档 + 家族口径的要素都在码里", not miss, f"缺={miss or '无'}")

    # T10b：阈值单一真值来源 + **旧阈值零残留**
    n_hard = src.count("A5_HARD_MAX =")
    n_soft = src.count("A5_SOFT_LO, A5_SOFT_HI =")
    n_old = src.count("MID_CHECK_RATIO")
    rec("T10b", "阈值单一真值来源，且旧阈值 MID_CHECK_RATIO 零残留",
        (n_hard == 1 and n_soft == 1 and n_old == 0),
        f"A5_HARD_MAX 赋值 {n_hard} 次；两档软阈值赋值 {n_soft} 次；旧阈值残留 {n_old} 次")

    # T10c：硬触发 → **立即终止**，且终止点必须在分析段之前；已跑数据加 EXPLORE_ONLY_ 前缀
    i_exit = src.find("sys.exit(3)")
    i_ana = src.find("# ---------------------------------------------------------------- 分析")
    rec("T10c", "硬触发立即终止（sys.exit 3 且在分析段之前）+ 数据降级为 EXPLORE_ONLY_",
        (i_exit > 0 and i_ana > 0 and i_exit < i_ana
         and "EXPLORE_ONLY_raw_results_partial.json" in src
         and "EXPLORE_ONLY_mid_check_A5.json" in src),
        f"sys.exit(3)@{i_exit} 分析段@{i_ana}（前者须在前）")

    # T10d：触发后的结果模板写死存在
    rec("T10d", "硬触发后必须按写死模板报告",
        "中期功效不足，无法对检出能力下结论" in src)
except Exception as e:
    rec("T10", "A5 两档与终止路径覆盖", False, f"{type(e).__name__}: {str(e)[:150]}")

# ---- T11：D6 对照条件（v7.0 新增）----
# 预注册五之三：答案不是"把它写进 rubric"，而是"不改 rubric、加对照、写死口径"。
# 所以测试必须同时证明两件事：对照 rubric 存在；**主 rubric 没被污染**。
try:
    PAGE = APP_V6
    rsrc = open(PAGE, encoding="utf-8").read()
    esrc = open(EXP, encoding="utf-8").read()

    # T11a：对照 rubric 存在且明写自洽性
    rec("T11a", "存在含「自相矛盾」的对照 rubric（仅用于 D6 对照条件）",
        "PAIR_PROMPT_CONSISTENCY" in rsrc and "自相矛盾" in rsrc)

    # T11b：**主 rubric 不得含自洽性** —— 否则 D6 的检出变成同义反复
    i = rsrc.find("PAIR_PROMPT = ")
    j = rsrc.find('"""', rsrc.find('"""', i) + 3) + 3
    main_prompt = rsrc[i:j]
    rec("T11b", "主 rubric 未含自洽性（防同义反复）",
        ("自相矛盾" not in main_prompt) and ("PAIR_PROMPT = " in rsrc),
        "主 rubric 逐字保持四条判据")

    # T11c：开关存在且**默认走主 rubric**
    rec("T11c", "make_pairwise 有 with_consistency 开关，默认 False（主检验用未提示版）",
        "def make_pairwise(model, with_consistency=False)" in rsrc
        and "PAIR_PROMPT_CONSISTENCY if with_consistency else PAIR_PROMPT" in rsrc)

    # T11d：运行侧 D6 对照条件单列、18 次、不做检验
    rec("T11d", "运行侧 D6 对照条件单列（18 次、描述性、不进主检验）",
        ("d6_arm" in esrc) and ("3 对 × 2 序 × 3 评审 = 18 次" in esrc)
        and ("不进主检验、不做检验" in esrc)
        and ('summary["d6_condition_arm"]' in esrc or "d6_arm" in esrc))
except Exception as e:
    rec("T11", "D6 对照条件覆盖", False, f"{type(e).__name__}: {str(e)[:150]}")


# ---- T12：D7 作废联动的口径（v7.1 用户裁决：≥2/3 原版胜即通过）----
# 为什么必须测：现行容错口径下，本实验在**自身假设**下约有 61.3% 概率因 D7 正常波动
# 被作废联动 → 一致性规则必然未过 → 主结论降级。改为 ≥2/3 后降到 8.2%。
try:
    src = open(EXP_PATH, encoding="utf-8").read()
    need = {
        "常量 D7_PASS_MIN": "D7_PASS_MIN = 2" in src,
        "判据用的是 wins >= 阈值（非 all(...)）": "d7_ok_by_model[m] = (_wins >= D7_PASS_MIN)" in src,
        "**旧口径 all(v == \"甲\") 已删除**": 'all(v == "甲" for v in votes)' not in src,
        "逐票明细入 summary": '"d7_rule"' in src and "d7_vote_detail" in src,
        "写明家族作废率": "void_family_rate" in src,
    }
    miss = [k for k, v in need.items() if not v]
    rec("T12", "D7 作废改为 ≥2/3 原版胜即通过（旧「全须胜」零残留）",
        not miss, f"缺={miss or '无'}")
except Exception as e:
    rec("T12", "D7 口径覆盖", False, f"{type(e).__name__}: {str(e)[:150]}")

print()
print("=" * 92)
npass = sum(1 for x in results if x["passed"])
print(f"结果：{npass}/{len(results)} 通过")
for x in results:
    if not x["passed"]:
        print(f"  ❌ {x['id']} {x['name']}　{x['note']}")
print("=" * 92)
sys.exit(0 if npass == len(results) else 1)
