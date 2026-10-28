# clean_memory.py
# 清理无效实验记录，保留完整统计维度
import json, os
from datetime import datetime

db_path = os.path.join("exports", "experiments_db.json")

# 备份原文件
with open(db_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

backup_path = db_path + ".bak"
with open(backup_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

before = len(data["experiments"])

# 只保留综合评分 > 0 的有效记录
data["experiments"] = [
    e for e in data["experiments"]
    if e.get("scores", {}).get("综合评分", 0) > 0
]
after = len(data["experiments"])

exps = data["experiments"]

def avg(s):
    return round(sum(s) / len(s), 2) if s else 0

def scores_for(key, value):
    return [e["scores"]["综合评分"] for e in exps if e.get(key) == value]

# 按人格模式统计（包含关闭人格）
data["summary"] = {
    "total_experiments": after,
    "by_personality": {
        "默认人格": {
            "count": len(scores_for("personality_mode", "默认人格")),
            "avg_score": avg(scores_for("personality_mode", "默认人格"))
        },
        "最优人格": {
            "count": len(scores_for("personality_mode", "最优人格")),
            "avg_score": avg(scores_for("personality_mode", "最优人格"))
        },
        "随机人格": {
            "count": len(scores_for("personality_mode", "随机人格")),
            "avg_score": avg(scores_for("personality_mode", "随机人格"))
        },
        "关闭人格": {
            "count": len(scores_for("personality_mode", "关闭人格")),
            "avg_score": avg(scores_for("personality_mode", "关闭人格"))
        }
    },
    "by_info_mode": {
        "真实信息": {
            "count": len(scores_for("info_mode", "真实信息")),
            "avg_score": avg(scores_for("info_mode", "真实信息"))
        },
        "推断信息": {
            "count": len(scores_for("info_mode", "推断信息")),
            "avg_score": avg(scores_for("info_mode", "推断信息"))
        },
        "混合信息": {
            "count": len(scores_for("info_mode", "混合信息")),
            "avg_score": avg(scores_for("info_mode", "混合信息"))
        }
    },
    "by_intervention": {
        "有干预": {
            "count": len(scores_for("intervention", "有")),
            "avg_score": avg(scores_for("intervention", "有"))
        },
        "无干预": {
            "count": len(scores_for("intervention", "无")),
            "avg_score": avg(scores_for("intervention", "无"))
        }
    },
    "last_updated": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
}

with open(db_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"✅ 清理完成。删除 {before - after} 条无效记录。备份在 {backup_path}")
print(f"📊 当前有效记录：{after} 条")
print(f"   人格：默认{len(scores_for('personality_mode','默认人格'))} / 最优{len(scores_for('personality_mode','最优人格'))} / 随机{len(scores_for('personality_mode','随机人格'))} / 关闭{len(scores_for('personality_mode','关闭人格'))}")
print(f"   信息：真实{len(scores_for('info_mode','真实信息'))} / 推断{len(scores_for('info_mode','推断信息'))} / 混合{len(scores_for('info_mode','混合信息'))}")
print(f"   干预：有{len(scores_for('intervention','有'))} / 无{len(scores_for('intervention','无'))}")