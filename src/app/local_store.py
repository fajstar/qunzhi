# -*- coding: utf-8 -*-
"""
本地数据留存 local_store.py
============================
每次预演结果落盘到本地 JSON（公网同学跑的数据也留存）。
路径：sandbox_v15_课堂模拟器版/data/sessions/<时间戳>.json
"""
import os, json, hashlib
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "sessions")
# V16.6：内容指纹去重。前端每 3 秒轮询一次结果接口，而接口里有 save_session
# → 同一局内容不变却反复写盘（实测 16 秒内 8 个文件）。内容没变就不写。
_SAVE_SIG = {}      # session_id -> 内容指纹
_SAVE_PATH = {}     # session_id -> 上次写的路径
os.makedirs(DATA_DIR, exist_ok=True)


def save_session(session) -> str:
    """保存一次预演会话的完整数据，返回文件路径"""
    record = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session.session_id,
        "topic": session.topic,
        "user_thought": getattr(session, "user_thought", ""),
        "max_rounds": session.max_rounds,
        "complexity_level": (getattr(session, "v15_meta", {}) or {}).get("complexity_level", ""),
        "report": session.report,
        "human_report": getattr(session, "human_report", None),
        "emergence": session.emergence_analysis,
        "evaluation": session.report_evaluation,
        "coach_audit": getattr(session, "coach_audit_opinions", []),
        "logs": session.logs,
        "v15_meta": getattr(session, "v15_meta", {}),
        # ---- V15 优化（2026-09-25）：引用与干预也要落盘 ----
        # 事故复盘：接口返回了引用核验/干预记录/每轮信号，但存档里没有——
        # 结果是"线上看得见、事后查不到"，证据链断在本地留存这一环。
        "references": getattr(session, "references", []),                # 参考文献结构化清单
        "citation_check": getattr(session, "citation_check", None),      # 有出处/无出处/编造编号
        "info_pool": getattr(session, "info_pool", []),                  # 实际注入的信息池（含编号）
        "intervention_records": getattr(session, "intervention_records", []),  # 本局逐轮干预记录
        "intervention_timing": getattr(session, "intervention_timing", ""),    # per_round / predefined_once
        "round_signals": getattr(session, "round_signals", []),          # 每轮客观信号（可复现）
        "next_session_suggestions": getattr(session, "next_session_suggestions", []),  # 审计留待下局
        "v3_review": getattr(session, "v3_review_result", None),
        # 评审 v6（2026-10-11）：**替代 1–10 分尺子**的主读数——
        # 含成对强制选择、分维三档、风险栏、独有贡献、锚点自检结果。
        # 必须落盘，否则实验数据里没有"新尺子"的读数可比（深批者就是因为漏了
        # 这个字段的位置/时序，60% 的局落盘为空）。
        "v6_review": getattr(session, "v6_review_result", None),
        # V16.2 插件化：**通用**字段。哪个插件跑的（review_plugin）、结果（review）、
        # 失败原因（review_error）。有三个字段才能在事后回答"这一局的读数是哪把尺子给的、
        # 它到底跑成没跑成"——只有结果字段的话，插件失败与插件没选长得一模一样。
        "review_plugin": getattr(session, "review_plugin_id", None),
        "review": getattr(session, "review_result", None),
        "review_error": getattr(session, "review_error", None),
        "deep_critic": getattr(session, "deep_critic", ""),
    }
    # V16.6：内容没变就不落盘（轮询会反复调用本函数）
    try:
        _sig = hashlib.sha256(json.dumps(
            {k: record.get(k) for k in ("report", "human_report", "deep_critic",
                                         "v3_review", "v6_review", "citation_check",
                                         "intervention_records")},
            ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        _sid = record.get("session_id")
        if _sid and _SAVE_SIG.get(_sid) == _sig:
            return _SAVE_PATH.get(_sid) or ""
    except Exception:
        _sig, _sid = None, None
    safe = "".join(c for c in session.topic[:20] if c.isalnum() or c in "-_") or "session"
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe}.json"
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    if _sid and _sig:
        _SAVE_SIG[_sid] = _sig
        _SAVE_PATH[_sid] = filepath
    return filepath


def load_session(session_id: str):
    """按 session_id 从磁盘读回一次会话（读不到返回 None）。

    V16.1（2026-10-11）新增。**这是一个真实缺陷的修复**：
      用户原话「我在页面刷新过后，我原有的报告也消失了，跟我们正常用的网页不一样」。
      根因有两层，第二层才是要命的：
        ① 前端把会话号只放在内存变量里 → 刷新即丢（已在前端修）；
        ② **结果接口只从内存读会话** —— 而这个模块**只有 save_session、没有读回的函数**。
           于是**服务一重启，磁盘上所有会话文件就再也没人读**（实测：重启后旧会话 404）。
      换句话说：数据一直存着，只是从来没人读。刷新能不能看到，取决于服务进程活没活着。
    """
    sid = (session_id or "").strip()
    if not sid:
        return None
    # ① 文件名里带 topic 与时间戳，但 session_id 不一定在文件名里 → 只能扫目录比对。
    #    会话数量级在千以内，扫描成本可忽略；为稳妥按修改时间**从新到旧**找。
    try:
        names = sorted(os.listdir(DATA_DIR), reverse=True)
    except Exception:
        return None
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(DATA_DIR, n)
        try:
            with open(p, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if rec.get("session_id") == sid:
            rec["_loaded_from"] = p          # 便于排查"这条是内存里的还是磁盘上的"
            return rec
    return None
