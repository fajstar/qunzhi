# -*- coding: utf-8 -*-
"""评审插件 · v6：成对强制选择 + 分维三档（**准，但慢**）
==================================================================
这是"打分尺子下线"后的主读数。它是**已验证**的那一套：
  · 你 2026-09-19/20 的实验：三家评委在 18 对材料上 18/18、17/18、17/18 全部达标，
    且两轮一致；而旧的 1–10 分尺子在同一批上只挑对 8/18（44%，低于瞎猜）。
  · 归因（报告 01 §七）：**不是旧尺子笨，是它没有刻度**——分数全挤在 6.4–7.2（跨度 0.8），
    而"判定有差别"的门槛是 0.5 → 18 对里 10 对算"看起来一样"。

代价如实说：每次要跑「锚点自检 12 次调用 + 三模型分维 3 次」，
其中 check_anchors 是**串行**的，实测 **约 150–160 秒**。
"""
import os
import sys

# 允许从 app/ 目录导入 review_v6（插件目录是 app/review_plugins/）
_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)

PLUGIN = {
    "id": "v6",
    "kind": "review",
    "name": "v6 成对判定 + 分维三档（准）",
    "desc": "独立三家评委。先做锚点自检（不通过就不给任何读数），再出分维三档与风险栏；"
            "每条判断必须带原文引用。",
    "eta_sec": 160,
    "cost": "中",
    "metrics": ["锚点自检", "分维三档（强/中/弱/无依据）", "风险栏（带原文证据）", "独有贡献"],
    "default": True,
}


def _v6():
    import importlib
    import review_v6
    return importlib.reload(review_v6) if os.getenv("V6_RELOAD") else review_v6


def available():
    """能不能跑：评审模型 key 齐不齐、锚点材料在不在。

    **不可用就说清是哪一样缺**，不返回笼统的 False ——
    因为"没有分数"的原因必须能被用户看见（v6 的立身之本就是"不过体检就不给分"）。
    """
    try:
        mod = _v6()
    except Exception as e:
        return False, "review_v6 不可导入：%s: %s" % (type(e).__name__, str(e)[:150])
    cfg = mod._env()
    missing = [mod.KEY_ENV[m] for m in mod.REVIEWERS
               if not cfg.get(mod.KEY_ENV.get(m, ""))]
    if missing:
        return False, "缺少 key：%s" % "、".join(sorted(set(missing)))
    try:
        import review_anchors
        review_anchors.load_materials()      # 缺材料 → anchors_passed=None → 永远不出分
    except Exception as e:
        return False, "锚点材料不可用（缺了会导致永远不出分）：%s: %s" % (
            type(e).__name__, str(e)[:150])
    return True, ""


def run(report, topic="", baseline=""):
    mod = _v6()
    out = mod.v6_review(report, topic=topic, baseline=(baseline or None))
    return {"v6": out}
