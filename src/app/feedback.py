# -*- coding: utf-8 -*-
"""
用户反馈收集 feedback.py
=========================
同学体验后打分+留言，落盘到 data/feedback.json
用于比赛"用户实证"材料（挑战杯现实意义/移动杯应用价值/互联网+商业性）
"""
import os, json
from datetime import datetime

DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "feedback.json")


def _load():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"feedback": []}


def save_feedback(topic: str, rating: int, comment: str, session_id: str = "") -> dict:
    """保存一条反馈"""
    data = _load()
    item = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "topic": topic[:60],
        "rating": int(rating),          # 1-5
        "comment": (comment or "")[:300],
        "session_id": session_id[:50],
    }
    data["feedback"].append(item)
    # 统计
    ratings = [f["rating"] for f in data["feedback"]]
    data["stats"] = {
        "total": len(ratings),
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
        "positive": sum(1 for r in ratings if r >= 4),
    }
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return {"ok": True, "stats": data["stats"]}


def get_feedback() -> dict:
    data = _load()
    return {"ok": True, **data}
