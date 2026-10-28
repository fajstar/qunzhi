# -*- coding: utf-8 -*-
"""统一插件注册表 plugin_registry.py（V16.3，2026-10-11）
==================================================================
用户要求：「我希望不只是评审系统模块化，而是能模块化的都可以这样」
          「里面的工具要做成插头即插即用，也方便我们后面的维护」

所以本文件是**唯一**的插件注册表，按 `kind`（类别）组织：

    kind      目录（app/ 下）        入口函数        用在哪
    --------  --------------------  --------------  ----------------------------
    review    review_plugins/       run()           报告评审（v3 快 / v6 准）
    source    source_plugins/       search()        多源检索（11 个学术/网络源）

**加一个新插件 = 往对应目录丢一个 .py**，本文件与所有调用方都**不用改**。
这是"即插即用"的全部含义 —— 如果加插件还得改注册表，那就不叫即插即用。

插件契约（每个 kind 略有不同，但骨架一致）：
    PLUGIN = {"id", "kind", "name", "desc", ...}     # 元数据（必须含 id 与 kind）
    def available() -> (bool, 原因)                   # 能不能跑；不能跑要**说清哪一样缺**
    入口：review → run(report, topic="", baseline="")
          source → search(query, rows=6) -> list

两条纪律（沿用本项目一贯要求）：
  · **加载/运行失败一律留痕**，不静默。「插件没加载」与「插件不存在」必须能区分。
  · 注册表**只做发现与调度**，不含任何业务逻辑 —— 逻辑全在插件里。
"""
import importlib
import os
import pkgutil
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))

KINDS = {
    "review": {"dir": "review_plugins", "label": "评审器", "entry": "run",
               "desc": "对报告做评审/打分。各插件自报耗时与成本，由用户选快或准。"},
    "source": {"dir": "source_plugins", "label": "检索源", "entry": "search",
               "desc": "一条检索来源。默认是否参与「多源学术检索」由插件自报 default_on。"},
}

_REG = {}          # kind -> {id: module}
_ERRORS = {}       # kind -> {module_name: 原因}


def discover(kind=None, force=False) -> dict:
    """发现插件。kind=None 时发现全部类别。返回 {kind: {id: module}}。"""
    global _REG, _ERRORS
    kinds = [kind] if kind else list(KINDS.keys())
    if _REG and not force and all(k in _REG for k in kinds):
        return {k: _REG.get(k, {}) for k in kinds}
    for k in kinds:
        spec = KINDS.get(k)
        if not spec:
            raise KeyError("未登记的插件类别：%s（已登记：%s）" % (k, "、".join(KINDS)))
        d = os.path.join(_HERE, spec["dir"])
        bucket, errs = {}, {}
        if not os.path.isdir(d):
            _REG[k], _ERRORS[k] = bucket, {"(目录)": "插件目录不存在：%s" % d}
            continue
        for m in pkgutil.iter_modules([d]):
            if m.name.startswith("_"):
                continue
            full = "%s.%s.%s" % (__name__.rsplit(".", 1)[0], spec["dir"], m.name) \
                if "." in __name__ else "%s.%s" % (spec["dir"], m.name)
            try:
                mod = importlib.import_module(full)
                if force:
                    mod = importlib.reload(mod)
            except Exception as e:
                errs[m.name] = "%s: %s" % (type(e).__name__, str(e)[:200])
                print("[插件·%s] 加载失败 %s → %s" % (k, m.name, errs[m.name]), flush=True)
                continue
            meta = getattr(mod, "PLUGIN", None)
            if not isinstance(meta, dict) or not meta.get("id"):
                errs[m.name] = "缺少 PLUGIN 字典或 PLUGIN['id']（不符合契约，已跳过）"
                continue
            # 类别不符要**报出来**（放错目录是最常见的低级错，且症状是"插件不见了"）
            if meta.get("kind") != k:
                errs[m.name] = "PLUGIN['kind']=%r 与所在目录（%s=%r）不符，已跳过" % (
                    meta.get("kind"), k, spec["dir"])
                print("[插件·%s] %s" % (k, errs[m.name]), flush=True)
                continue
            if not callable(getattr(mod, spec["entry"], None)):
                errs[m.name] = "缺少可调用的 %s()（不符合契约，已跳过）" % spec["entry"]
                continue
            bucket[meta["id"]] = mod
        _REG[k], _ERRORS[k] = bucket, errs
    return {k: _REG.get(k, {}) for k in kinds}


def list_plugins(kind=None, force=False) -> list:
    """插件清单（含可用性与原因）。每条都带 `kind` 字段，前端可分组展示。"""
    reg = discover(kind=kind, force=force)
    out = []
    for k, bucket in reg.items():
        for pid, mod in sorted(bucket.items()):
            meta = dict(mod.PLUGIN)
            try:
                ok, why = mod.available()
            except Exception as e:
                ok, why = False, "available() 抛异常：%s: %s" % (type(e).__name__, str(e)[:120])
            meta["available"] = bool(ok)
            meta["unavailable_reason"] = "" if ok else (why or "原因未给出")
            meta.setdefault("kind", k)
            out.append(meta)
    return out


def get_plugin(pid, kind=None):
    """按 id 取插件。kind=None 时跨类别查找（id 需唯一）。"""
    reg = discover(kind=kind)
    for k, bucket in reg.items():
        if pid in bucket:
            return bucket[pid]
    return None


def run_plugin(pid, kind=None, **kwargs) -> dict:
    """统一调用。失败一律返回结构化错误，**不抛给调用方**（调用方通常是 HTTP 层）。"""
    reg = discover(kind=kind)
    mod, _k = None, None
    for k, bucket in reg.items():
        if pid in bucket:
            mod, _k = bucket[pid], k
            break
    if not mod:
        avail = "、".join("%s:%s" % (k, ",".join(sorted(b.keys())) or "无")
                          for k, b in reg.items())
        return {"ok": False, "error": "未注册的插件：%s（可用 → %s）" % (pid, avail),
                "known_load_errors": dict(_ERRORS)}
    try:
        ok, why = mod.available()
        if not ok:
            return {"ok": False, "plugin": pid, "kind": _k, "unavailable": True,
                    "error": "插件不可用：%s" % (why or "原因未给出")}
    except Exception as e:
        return {"ok": False, "plugin": pid, "kind": _k,
                "error": "available() 抛异常：%s: %s" % (type(e).__name__, str(e)[:150])}
    entry = getattr(mod, KINDS[_k]["entry"])
    try:
        return {"ok": True, "plugin": pid, "kind": _k, "result": entry(**kwargs)}
    except Exception as e:
        return {"ok": False, "plugin": pid, "kind": _k,
                "error": "%s: %s" % (type(e).__name__, str(e)[:300]),
                "trace": traceback.format_exc()[-600:]}


def load_errors(kind=None) -> dict:
    """加载失败清单（供 health/清单接口暴露 —— 否则"插件没加载"无人知晓）。"""
    discover(kind=kind)
    if kind:
        return dict(_ERRORS.get(kind, {}))
    return {k: dict(v) for k, v in _ERRORS.items() if v}


# ---------------------------------------------------------------- 检索源专用
def active_sources(topic="", ctx=None) -> list:
    """按「插件自报的 default_on + applies_when」筛出本局要跑的检索源，按 order 排序。

    返回 [{"id","name","search","order","meta"}, ...]。
    这里**不再硬编码任何源清单** —— 清单来自插件目录，路由条件来自插件自身的
    `applies_when` 字段。加源 = 丢文件，路由函数一行不改。

    ctx 需要提供 route_sources 的判定输入：
      cn_terms(bool)：本课题是否生成了中文关键词（NCPSSD 裸词查询会 0 命中，必须先拆词）
      med(bool)：是否医学课题
      effective(bool)：是否有可用的英文检索式
    """
    ctx = ctx or {}
    out = []
    for p in list_plugins(kind="source"):
        if not p.get("available") or not p.get("default_on"):
            continue
        cond = p.get("applies_when") or "always"
        if cond == "cn_terms" and not ctx.get("cn_terms"):
            continue
        if cond == "med_and_effective" and not (ctx.get("med") and ctx.get("effective")):
            continue
        mod = get_plugin(p["id"], kind="source")
        if not mod:
            continue
        out.append({"id": p["id"], "name": p.get("name", p["id"]),
                    "search": mod.search, "order": p.get("order", 999), "meta": p})
    out.sort(key=lambda x: x["order"])
    return out
