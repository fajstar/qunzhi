# -*- coding: utf-8 -*-
"""群智评审机制 v6 · 锚点自检

两层职责（来自预注册「第 1 层」）：
  锚点甲：完整报告 vs 删掉 60% 的同一份 → **期望完整版胜**
  锚点乙：同一份 vs 自己          → **期望 6 次里 ≥4 次判「无差别」**

  两道都过 → anchors_passed = True
  跑完了但判错 → False（"评委有问题"）
  跑不起来（调用失败/材料缺失） → None（"锚点没跑起来"）

**为什么每个锚点跑 N=3 取多数**（预注册 A1 相关）：
  判据依赖真实模型的随机输出，单次会抖动 → 会间歇性假红；
  更糟的是人会手动重跑到绿 —— 那是**自证**，不是验证。
  故：每个锚点跑 3 次取多数；三次结果**全部记录**；禁止只报通过的那次。

实现约束：**禁止裸 assert**，一律 `if not ...: raise`。
"""
import os
import time

# 锚点乙阈值（预注册写死）：3 评审 × 2 序 = 6 次判定中，判「无差别」≥4 次
TIE_THRESHOLD = 4
TIE_TRIALS = 6
ANCHOR_REPEAT = 3          # 每个锚点跑 3 次取多数

MATERIAL_DIR = os.getenv(
    "V6_ANCHOR_DIR",
    "/home/ubuntu/qunzhi/experiments/2026-09-19_评审v6/anchor_materials")
# V16.1（2026-10-11）：材料目录改为可用环境变量覆盖，**默认值与原来完全一致**。
# 原因：原先是硬编码绝对路径，换机器/换目录就 FileNotFoundError → check_anchors 抛异常 →
# `anchors_passed=None` → **产品里永远不显示任何分数**（静默失效，最难查的一类）。
# 现在可覆盖、默认行为不变；路径不存在时仍按原设计上抛（由 v6_review 兜住并如实标注）。


def _load(name):
    p = os.path.join(MATERIAL_DIR, f"{name}.md")
    if not os.path.exists(p):
        raise FileNotFoundError(f"锚点材料缺失：{p}")
    return open(p, encoding="utf-8").read()


def load_materials():
    """读冻结的锚点材料（跑前已固化，跑时只读）。"""
    return {
        "original": _load("anchor_A_original"),      # 完整版
        "cut60": _load("anchor_A_cut60"),            # 删 60% 版
        "self": _load("anchor_B_self"),              # 与 original 内容相同
    }


def _resolve_units(pairwise_fn):
    """把入参归一成 [(模型名 or None, 判定函数), ...]，**长度恒为 3**。

    · callable      → 同一个函数 × ANCHOR_REPEAT（单模型，跑 3 次取多数）
    · dict{模型:fn} → 3 个模型各一个函数（预注册 v7.2 口径：「3 评审 × 2 序 = 6 次判定」）

    两种分解都产出 **6 票**（3 单元 × 2 序），所以 TIE_THRESHOLD=4/6 等阈值**不用改**。

    ⚠️ 2026-10-11 修复（**这是一个会让 v6 永远不出分的致命 bug**）：
       `review_v6.v6_review` 传进来的是 **dict**（`{m: make_pairwise(m) for m in REVIEWERS}`），
       而本函数原先只接受 callable → 每次都抛 `'dict' object is not callable`，
       被下面的 try/except 吞成 trial 记录 → `anchor_a_votes` 空 → `passed=None`
       → `scores_hidden=True` → **产品里一个分数都不会出现**（静默失效）。
       自带测试 T2/T3 只传函数、T1 只查端点存在，**没有一条测试真跑过这条路径**，
       所以 22/22 全绿而功能是死的 —— 正是本项目反复防的「声称已实现但未验证」。
    """
    if callable(pairwise_fn):
        units = [(None, pairwise_fn)] * ANCHOR_REPEAT
    elif isinstance(pairwise_fn, dict) and pairwise_fn:
        units = list(pairwise_fn.items())
    else:
        raise TypeError("check_anchors 需要 callable 或 非空 dict{模型: 函数}")

    # **硬校验：评审单元数 × 2 序 必须等于预注册的 TIE_TRIALS（6）**。
    # 为什么必须挡：阈值 4/6 是按「3 评审 × 2 序」定的。若有人加/减评委（例如把 v6 的三个家族
    # 扩到四个），票数变成 8 或 4，可 4/6 这个阈值**不会自动跟着变** ——
    # 那就会拿一个含义已经变了的阈值继续判，静默产出错误结论。
    # 宁可在这里报错，也不静默用一个错阈值。（改评委数 = 口径变更，须同步改阈值。）
    if len(units) * 2 != TIE_TRIALS:
        raise ValueError(
            "评审单元数 %d × 2 序 = %d 票，与锚点阈值所依据的 TIE_TRIALS=%d 不符。"
            "改评委数量属于口径变更，必须同步调整 TIE_TRIALS / TIE_THRESHOLD 并留档。"
            % (len(units), len(units) * 2, TIE_TRIALS))
    return units


# ---------------------------------------------------------------- 并行 + 缓存
# V16.4（2026-10-11）：**提速**。用户反馈「v 也太慢了吧，至少感觉至少 10 分钟」。
# 实测瓶颈：锚点自检原先是**串行**发 12 次调用（3 评委 × 2 序 × 2 锚点），每次约 13 秒
#           → **~157 秒**。而这 12 次调用**彼此完全独立**，没有任何依赖关系。
#   ① **并行**：12 次并发（默认 6，避免触发限流）；
#   ② **缓存**：锚点材料是**冻结的固定材料**，与"本局报告"无关，同一批评委重跑结果几乎必然相同。
#      但缓存有个原则问题：锚点的意义是"**评审器现在是好的**"，拿一个很旧的体检结果冒充
#      "现在合格"就是自证。所以 TTL 默认只有 **6 小时**，且缓存命中时**必须如实标注**
#      `detail.cached / cached_at`，界面能看到"这次是复用几点的体检"。
_ANCHOR_CACHE = {}          # cache_key -> {"ts": epoch, "result": {...}}
ANCHOR_CACHE_TTL = int(os.getenv("V6_ANCHOR_CACHE_TTL", "21600"))   # 秒（6 小时）
# 并发度**实测**选定（2026-10-11 云端基准，12 次调用）：
#   并发=1 → 121 秒（串行）
#   并发=3 → **39 秒**  ← 最优点
#   并发=6 → **322 秒**（无报错但明显被上游排队限流，比串行还慢 2.7 倍）
# 所以默认 3。**别凭直觉调大** —— 这类并发提速在本项目实测是反效果。
ANCHOR_WORKERS = int(os.getenv("V6_ANCHOR_WORKERS", "3"))


def _cache_key(units, mats):
    import hashlib
    h = hashlib.sha256()
    for m, _ in units:
        h.update(str(m).encode("utf-8"))
    for k in ("original", "cut60", "self"):
        h.update(hashlib.sha256((mats.get(k) or "").encode("utf-8")).digest())
    h.update(("%d/%d" % (TIE_THRESHOLD, TIE_TRIALS)).encode("utf-8"))
    return h.hexdigest()


def check_anchors(pairwise_fn, materials=None, use_cache=None):
    """跑两道锚点题，返回结构化结果。

    pairwise_fn 可以是：
      · callable(text_a, text_b) -> {"winner": "甲"|"乙"|"无差别", "model": str, "raw": str}
      · dict {模型名: 上述 callable}（多模型，见 _resolve_units）

    12 次判定**并行**发出；可选缓存（`V6_ANCHOR_CACHE=0` 关闭，默认开启，TTL 6 小时）。
    缓存命中时 detail.cached=True 且给出 cached_at/cache_age_sec —— **不假装是刚测的**。
    """
    mats = materials or load_materials()
    units = _resolve_units(pairwise_fn)
    if use_cache is None:
        use_cache = os.getenv("V6_ANCHOR_CACHE", "1") != "0"
    ck = _cache_key(units, mats)
    if use_cache and ANCHOR_CACHE_TTL > 0:
        hit = _ANCHOR_CACHE.get(ck)
        if hit and (time.time() - hit["ts"]) < ANCHOR_CACHE_TTL:
            r = {"passed": hit["result"]["passed"], "detail": dict(hit["result"]["detail"])}
            r["detail"]["cached"] = True
            r["detail"]["cached_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                     time.localtime(hit["ts"]))
            r["detail"]["cache_age_sec"] = int(time.time() - hit["ts"])
            r["cached"] = True
            return r

    detail = {"trials": [], "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    detail["units"] = [m for m, _ in units]

    # ---- 12 次判定并发发出（原先是串行 → 157 秒的真因）----
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    for ui, (mname, _fn) in enumerate(units):
        jobs.append(("甲", ui, mname, True, mats["original"], mats["cut60"]))
        jobs.append(("甲", ui, mname, False, mats["cut60"], mats["original"]))
        jobs.append(("乙", ui, mname, True, mats["self"], mats["self"]))
        jobs.append(("乙", ui, mname, False, mats["self"], mats["self"]))

    def _one(job):
        anchor, ui, mname, order_flag, ta, tb = job
        try:
            return job, (units[ui][1](ta, tb) or {}), None
        except Exception as e:
            return job, None, "%s: %s" % (type(e).__name__, str(e)[:180])

    res = {}
    with ThreadPoolExecutor(max_workers=max(1, min(ANCHOR_WORKERS, len(jobs)))) as ex:
        for job, r, err in ex.map(_one, jobs):
            res[(job[0], job[1], job[3])] = (r, err)

    # ---------- 锚点甲：完整版 vs 删 60% ----------
    anchor_a_votes = []
    for ui, (mname, _fn) in enumerate(units):
        r1, e1 = res.get(("甲", ui, True), (None, "未执行"))
        r2, e2 = res.get(("甲", ui, False), (None, "未执行"))
        if e1 or e2:
            detail["trials"].append({"anchor": "甲", "trial": ui + 1, "model": mname,
                                     "error": (e1 or e2)})
            continue
        # 把两序折算成"完整版是否胜" —— ⚠️ 必须是**平均**不是求和！
        #   求和会出错：两序都判"无差别"时 0.5+0.5=1.0 → 被误判成"完整版胜"。
        #   这是 T2/T3 测试抓到的真实 bug（2026-09-19）。
        s = 0.0
        _n = 0
        for r, complete_is_jia in ((r1, True), (r2, False)):
            w = (r or {}).get("winner")
            _n += 1
            if w == "无差别":
                s += 0.5
            elif (w == "甲" and complete_is_jia) or (w == "乙" and not complete_is_jia):
                s += 1.0
        s = (s / _n) if _n else 0.5
        vote = "完整版胜" if s > 0.5 else ("删减版胜" if s < 0.5 else "平局")
        anchor_a_votes.append(vote)
        detail["trials"].append({"anchor": "甲", "trial": ui + 1, "model": mname,
                                 "two_order_score": s, "vote": vote,
                                 "models": [(r1 or {}).get("model"), (r2 or {}).get("model")]})

    a_majority = None
    if anchor_a_votes:
        a_majority = max(set(anchor_a_votes), key=anchor_a_votes.count)
    detail["anchor_a_votes"] = anchor_a_votes
    detail["anchor_a_majority"] = a_majority
    detail["anchor_a_passed"] = (a_majority == "完整版胜")

    # ---------- 锚点乙：同一份 vs 自己 ----------
    anchor_b_ties, anchor_b_total = 0, 0
    b_trials = []
    for ui, (mname, _fn) in enumerate(units):
        ties_this_trial = 0
        for order in (True, False):
            r, err = res.get(("乙", ui, order), (None, "未执行"))
            if err:
                b_trials.append({"anchor": "乙", "trial": ui + 1, "order": order,
                                 "model": mname, "error": err})
                continue
            anchor_b_total += 1
            if (r or {}).get("winner") == "无差别":
                ties_this_trial += 1
                anchor_b_ties += 1
        b_trials.append({"anchor": "乙", "trial": ui + 1, "model": mname,
                         "ties_in_trial": ties_this_trial})
    detail["anchor_b_trials"] = b_trials
    detail["anchor_b_ties"] = anchor_b_ties
    detail["anchor_b_total"] = anchor_b_total
    detail["anchor_b_threshold"] = TIE_THRESHOLD
    detail["anchor_b_passed"] = (anchor_b_ties >= TIE_THRESHOLD)

    detail["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    detail["parallel_workers"] = max(1, min(ANCHOR_WORKERS, len(jobs)))
    detail["n_calls"] = len(jobs)

    if anchor_a_total_failed(detail) and anchor_b_total == 0:
        passed = None                     # 锚点根本没跑起来
    else:
        passed = bool(detail["anchor_a_passed"] and detail["anchor_b_passed"])
    detail["passed"] = passed

    out = {"passed": passed, "detail": detail}
    if use_cache and ANCHOR_CACHE_TTL > 0 and passed is not None:
        # 只缓存"跑出来了"的结果（没跑起来的不该被缓存成结论）
        _ANCHOR_CACHE[ck] = {"ts": time.time(), "result": out}
    return out


def anchor_a_total_failed(detail):
    """锚点甲是否一次都没跑成。"""
    votes = detail.get("anchor_a_votes") or []
    return len(votes) == 0
