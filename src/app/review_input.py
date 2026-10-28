# -*- coding: utf-8 -*-
"""群智评审机制 v6 · 送入逻辑（不截断）

为什么要有这个文件：
  现任机制（review_v3.py）把报告砍到 4,000 字才送去评。实测 32 份产出里
  **11 份（34%）超过 4,000 字**，最长那份 5,594 字被砍掉 1,594 字 ——
  用户看到的是"深度 6/10"，但他被扣分的真正原因是后半段根本没被读。

本模块的唯一职责：**把报告完整送进去**，并如实报告"送入字数 / 原文字数"。

实现约束（来自预注册）：
  · 不截断。超长则分段，段与段之间**不丢内容**；
  · 必须能证明 sent_len ≥ 0.98 × raw_len（分段标记的少量开销允许，<2%）；
  · **禁止裸 assert** —— `python -O` 下会被整体剥离且不报错，
    那正是本项目最怕的静默失败形态。一律 `if not ...: raise`。
"""
import re

# 单次送入的上限（字符）。超过则分段 —— 注意这是"分段"不是"截断"：
# 所有段落都会被送出，只是分多次调用。
SEGMENT_LIMIT = 12000
SEGMENT_JOINER = "\n\n---（以下为同一份报告的续段）---\n\n"


def prepare(text):
    """把报告准备成可送入的形式。

    返回 dict：
      segments : list[str]   送入用的分段（拼接后应与原文等价）
      raw_len  : int         原文字数
      sent_len : int         实际送出的字数（含分段连接符）
      ratio    : float       sent_len / raw_len
      truncated: bool        恒为 False（本模块不截断；保留字段以便调用方断言）
    """
    raw = text or ""
    raw_len = len(raw)

    if raw_len <= SEGMENT_LIMIT:
        segments = [raw] if raw_len else []
    else:
        # 按段落边界切，尽量切在换行处；切不动就硬切（硬切仍是分段，不丢内容）
        segments, buf = [], ""
        for para in re.split(r"(?<=\n)", raw):
            if len(buf) + len(para) <= SEGMENT_LIMIT:
                buf += para
            else:
                if buf:
                    segments.append(buf)
                # 单段本身超限 → 硬切成多段
                while len(para) > SEGMENT_LIMIT:
                    segments.append(para[:SEGMENT_LIMIT])
                    para = para[SEGMENT_LIMIT:]
                buf = para
        if buf:
            segments.append(buf)

    # 复算法一：把所有段落按送入方式拼回来再数字数（不靠算式自证）
    joined = SEGMENT_JOINER.join(segments)
    sent_len = len(joined)

    out = {"segments": segments, "raw_len": raw_len, "sent_len": sent_len,
           "ratio": (sent_len / raw_len) if raw_len else 1.0,
           "truncated": False}

    # ---- 硬校验：不许截断 ----
    if out["truncated"] is not False:
        raise AssertionError("prepare() 内部状态异常：truncated 必须恒为 False")
    if raw_len and sent_len < 0.98 * raw_len:
        raise AssertionError(
            f"送入字数不足：raw_len={raw_len} sent_len={sent_len} "
            f"ratio={out['ratio']:.4f} —— 疑似发生截断，拒绝继续")
    # 内容等价性：去掉分段连接符后，必须能还原出原文
    if raw_len and raw_len <= SEGMENT_LIMIT:
        if joined != raw:
            raise AssertionError("单段情况下送入内容与原文不一致 —— 疑似被改写")
    else:
        stripped = joined.replace(SEGMENT_JOINER, "")
        if len(stripped) < raw_len:
            raise AssertionError(
                f"分段拼接后内容少于原文：{len(stripped)} < {raw_len} —— 疑似丢段")

    return out


def join_segments(segments):
    """把分段拼回一份完整文本（供需要整篇的场景使用）。"""
    return SEGMENT_JOINER.join(segments or [])
