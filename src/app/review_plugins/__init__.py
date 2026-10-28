# -*- coding: utf-8 -*-
"""评审插件目录（V16.2 建；V16.3 起**注册逻辑已上收到 plugin_registry**）
==================================================================
本文件现在是一层**兼容垫片**：老调用方（`main_gateway` 的 `/api/review/plugins`、
`_selftest_fixes_v16.py`）仍按 `review_plugins.list_plugins()` / `run_plugin()` 调用，
但真正的发现与调度**只有一份实现**（`app/plugin_registry.py`）。

为什么这样收口：用户要求"能模块化的都模块化，方便后面维护"——
如果评审一套注册表、检索源另一套，那就是**两处要维护、两处会不一致**（本项目吃过这个亏：
`_META_KEYS` 与 `entry_journal_fields` 各写一份，结果"改了一处忘了另一处"直接导致
学位论文被误标为期刊）。所以：**一个注册表，按 kind 分组。**

评审插件的契约（与 plugin_registry 一致）：
    PLUGIN = {"id","kind":"review","name","desc","eta_sec","cost","metrics","default"[,"caveat"]}
    def available() -> (bool, 原因)
    def run(report, topic="", baseline="") -> dict
"""
import os
import sys

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import plugin_registry as _PR  # noqa: E402

KIND = "review"


def discover(force: bool = False) -> dict:
    """返回 {插件id: 模块}（仅评审类）。"""
    return _PR.discover(kind=KIND, force=force).get(KIND, {})


def list_plugins(force: bool = False) -> list:
    """评审插件清单（含可用性/耗时/成本/产出/已知缺陷），并保证**默认项唯一且可用**。"""
    out = _PR.list_plugins(kind=KIND, force=force)
    # 默认项规则：不可用的不许当默认；可用的里面恰好一个默认
    for p in out:
        if not p.get("available"):
            p["default"] = False
    avail = [p for p in out if p["available"]]
    if avail:
        # V16.7：环境变量 QZ_DEFAULT_REVIEW_PLUGIN 可**一键改默认**（不改代码、不重新部署）。
        # 用户要求「让用户自己选择」——选择器在界面上；默认值也要能随场景调：
        #   答辩/演示想要快 → 在 .env 里设 QZ_DEFAULT_REVIEW_PLUGIN=v3
        #   想要准（默认）   → 不设，或设成 v6
        _want = (os.getenv("QZ_DEFAULT_REVIEW_PLUGIN") or "").strip()
        if _want:
            for p in out:
                p["default"] = bool(p["id"] == _want and p["available"])
        if not any(p.get("default") for p in avail):
            declared = [p for p in avail if p.get("default")]
            if len(declared) > 1:
                for p in declared[1:]:
                    p["default"] = False
            elif not declared:
                avail[0]["default"] = True
    return out


def get_plugin(pid: str):
    return _PR.get_plugin(pid, kind=KIND)


def run_plugin(pid: str, report: str, topic: str = "", baseline: str = "") -> dict:
    return _PR.run_plugin(pid, kind=KIND, report=report, topic=topic, baseline=baseline)


def load_errors() -> dict:
    return _PR.load_errors(kind=KIND)
