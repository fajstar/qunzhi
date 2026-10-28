# -*- coding: utf-8 -*-
"""检索源插件 · NCPSSD 国家哲学社会科学文献中心（**自动生成，勿手改** —— 规格见 ppt_hq/gen_source_plugins.py）

只做两件事：① 声明元数据（清单/路由条件/优先级都由插件自报）
            ② 把调用**按原函数真实签名**转发过去。**不改变任何检索逻辑。**
"""
import inspect
import os
import sys

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)
_RAG = os.path.join(_APP, "rag")
if _RAG not in sys.path:
    sys.path.insert(0, _RAG)

PLUGIN = {
    "id": "ncpssd",
    "kind": "source",
    "name": "NCPSSD 国家哲学社会科学文献中心",
    "desc": "中文社科（教育学/心理学/社会学）—— CSSCI 覆盖最好的中文源，但接口易变。裸词查询会 0 命中，必须拆词（terms）",
    "lang": "cn",             # cn / en / both
    "free": True,               # 是否免 key
    "web": False,                 # 是否网络检索（而非学术库）
    "eta_sec": 6,
    "default_on": True,           # 是否默认纳入「多源学术检索」
    "order": 10,             # 优先级（越小越先跑）
    "applies_when": "cn_terms",  # always / cn_terms / med_and_effective
}


def available():
    try:
        import scholarly_sources  # noqa: F401
    except Exception as e:
        return False, "scholarly_sources 不可导入：%s: %s" % (type(e).__name__, str(e)[:150])
    return True, ""


def search(query, rows=6, max_age=None, **kwargs):
    """把调用转发给原实现。

    **按原函数的真实签名过滤参数**（不是硬编码参数名）：
      各源的行数参数名不一致（rows / max_results / count），
      而且以后某个源改了签名，这里也不会 TypeError —— 只会少传那一个参数。
    这是上一版出回归（5 个源全挂、检索 0 条）后改用自适配的原因。
    """
    from scholarly_sources import search_ncpssd
    sig = inspect.signature(search_ncpssd)
    call = {"rows": rows}
    if max_age is not None:
        call["max_age"] = max_age
    call.update(kwargs or {})
    call = {k: v for k, v in call.items() if k in sig.parameters}
    out = search_ncpssd(query, **call)
    if isinstance(out, dict):        # 原实现若返回 dict（含 ok/papers），规整成列表
        return out.get("papers") or []
    return out or []
