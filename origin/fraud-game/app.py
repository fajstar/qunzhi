# -*- coding: utf-8 -*-
import json
import os
import random
import re
import sys
import io
import time
import threading
import traceback
from collections import defaultdict
from queue import Queue, Full

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from openai import OpenAI

# ==================== 强制UTF-8 ====================
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ==================== API 配置 ====================
# ⚠️ 安全优化：API Key 不再写死在代码里。
# 配置优先级：环境变量 DEEPSEEK_API_KEY_1/2 > 本地 config_local.json > 空（启动时告警）
def _load_local_config():
    _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_local.json")
    try:
        with open(_cfg_path, 'r', encoding='utf-8') as _f:
            return json.load(_f)
    except Exception:
        return {}

_LOCAL_CFG = _load_local_config()

DEEPSEEK_API_KEY_1 = os.environ.get("DEEPSEEK_API_KEY_1") or _LOCAL_CFG.get("api_key_1") or ""
DEEPSEEK_API_KEY_2 = os.environ.get("DEEPSEEK_API_KEY_2") or _LOCAL_CFG.get("api_key_2") or ""
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"

if not DEEPSEEK_API_KEY_1 or not DEEPSEEK_API_KEY_2:
    print("⚠️ 未找到 DeepSeek API Key：请在项目目录的 config_local.json 或环境变量 DEEPSEEK_API_KEY_1/2 中配置")

client1 = OpenAI(api_key=DEEPSEEK_API_KEY_1, base_url=DEEPSEEK_BASE_URL)
client2 = OpenAI(api_key=DEEPSEEK_API_KEY_2, base_url=DEEPSEEK_BASE_URL)

_api_counter = 0
_api_lock = threading.Lock()

def get_api_client():
    global _api_counter
    with _api_lock:
        _api_counter += 1
        return client1 if (_api_counter - 1) % 6 < 3 else client2

def reset_api_counter():
    global _api_counter
    with _api_lock:
        _api_counter = 0

# ==================== 游戏配置 ====================
HISTORY_FILE = "game_history.json"
ELIMINATION_INTERVAL = 5
MAX_ROUNDS = 15  # 每局天数上限，防止猜色长期无人淘汰导致游戏失控（正常局不会触顶）
MEMORIES_DIR = "memories"
GENE_POOL_FILE = "gene_pool.json"
GAME_LOG_DIR = "saved_games"
COACH_REPORT_DIR = "coach_reports"
COACH_MEMORY_FILE = os.path.join(COACH_REPORT_DIR, "coach_memory.json")

PLAYER_NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]
COLORS = ["红", "蓝", "绿"]

CLUE_TYPES = ["direct", "indirect", "vague"]
CLUE_CREDIBILITY = {"direct": 0.8, "indirect": 0.6, "vague": 0.4}

MODES = {
    "极速": {"max_tokens": 400, "timeout": 8, "temperature": 0.85},
    "标准": {"max_tokens": 1000, "timeout": 12, "temperature": 0.92},
    "深度": {"max_tokens": 2000, "timeout": 20, "temperature": 0.95}
}

GAME_MODES = {
    "轻量": {
        "tree_of_thought": False,
        "situational_analysis_freq": "never",
        "daily_reflection": False,
        "verification": False,
        "light_reflection": False,
        "long_term_retrieval": False,
        "opponent_prediction": False,
        "strategy_reverse": False,
        "speech_tokens": 350,
        "tree_tokens": 0,
        "analysis_tokens": 0,
        "guess_tokens": 0,
        "reflection_tokens": 0,
        "total_token_per_player": 15000
    },
    "标准": {
        "tree_of_thought": True,
        "situational_analysis_freq": "daily",
        "daily_reflection": True,
        "verification": True,
        "light_reflection": False,
        "long_term_retrieval": True,
        "opponent_prediction": True,
        "strategy_reverse": True,
        "speech_tokens": 900,
        "tree_tokens": 1800,
        "analysis_tokens": 500,
        "guess_tokens": 250,
        "reflection_tokens": 380,
        "total_token_per_player": 90000
    },
    "深度": {
        "tree_of_thought": True,
        "situational_analysis_freq": "per_round",
        "daily_reflection": True,
        "verification": True,
        "light_reflection": True,
        "long_term_retrieval": True,
        "opponent_prediction": True,
        "strategy_reverse": True,
        "speech_tokens": 1800,
        "tree_tokens": 3200,
        "analysis_tokens": 1000,
        "guess_tokens": 400,
        "reflection_tokens": 380,
        "coach_tokens": 3000,
        "total_token_per_player": 250000
    }
}

# ==================== 全局游戏状态 ====================
player_scores = {name: 0 for name in PLAYER_NAMES}
player_alive = {name: True for name in PLAYER_NAMES}
game_logs = []
current_round = 0
current_mode = "标准"
current_game_mode = "深度"
game_running = False
sse_clients = []
current_game_players = []
total_games_played = 0
auto_run_count = 0
auto_run_limit = 0
cross_game_penalty = None

game_lock = threading.Lock()
sse_lock = threading.Lock()
log_lock = threading.Lock()

world_rules = []
public_vote_available = True
RULE_PROBE_LIMIT = 2
probe_used_this_game = 0
vote_cooldown = {}
public_vote_triggered = False
no_elim_streak = 0
mid_ending_triggered = False
rule_judgment_phase = False

global_session_log = {"rounds": [], "summary": ""}

app = Flask(__name__, template_folder='templates_app')

for d in [GAME_LOG_DIR, COACH_REPORT_DIR, MEMORIES_DIR]:
    os.makedirs(d, exist_ok=True)

# ==================== 认知原型 ====================
COGNITIVE_PROTOTYPES = {
    "hunter": {"info_initiative": 0.75, "risk_tolerance": 0.70, "trust_propensity": 0.25,
               "time_horizon": 0.30, "decision_style": "intuition", "description": "主动出击，制造混乱"},
    "trader": {"info_initiative": 0.65, "risk_tolerance": 0.40, "trust_propensity": 0.70,
               "time_horizon": 0.65, "decision_style": "logic", "description": "信息交换，长期合作"},
    "ghost": {"info_initiative": 0.25, "risk_tolerance": 0.30, "trust_propensity": 0.40,
              "time_horizon": 0.55, "decision_style": "logic", "description": "少说多听，后发制人"},
    "gambler": {"info_initiative": 0.80, "risk_tolerance": 0.85, "trust_propensity": 0.15,
                "time_horizon": 0.20, "decision_style": "intuition", "description": "高风险高回报"},
    "diplomat": {"info_initiative": 0.60, "risk_tolerance": 0.45, "trust_propensity": 0.80,
                 "time_horizon": 0.70, "decision_style": "logic", "description": "建立信任网络"},
    "skeptic": {"info_initiative": 0.55, "risk_tolerance": 0.50, "trust_propensity": 0.20,
                "time_horizon": 0.45, "decision_style": "intuition", "description": "怀疑一切，验证后再行动"}
}

# ==================== 日志与持久化 ====================
def _atomic_write_json(path, data):
    """原子写 JSON：先写 .tmp 再 os.replace，进程中断也不会损坏原文件。"""
    tmp_path = path + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

def _api_create(prompt, max_tokens, timeout, temperature=0.7, retries=1):
    """模块级 API 调用（带双 Key 交替重试 + 指数退避），供少数决/赛后发言/教练报告使用。"""
    c1 = get_api_client()
    c2 = client2 if c1 is client1 else client1
    clients = [c1, c2]
    last_error = None
    for attempt in range(retries + 1):
        client = clients[attempt % len(clients)]
        try:
            r = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "system", "content": prompt}],
                temperature=temperature, max_tokens=max_tokens, timeout=timeout
            )
            return r.choices[0].message.content.strip()
        except Exception as e:
            last_error = e
            print(f"⚠️ API调用失败 (尝试{attempt+1}/{retries+1}): {e}")
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    print(f"❌ API调用彻底失败 ({retries+1}次尝试均失败): {last_error}")
    return None

def broadcast_log(message):
    global game_logs
    if not isinstance(message, str):
        message = str(message)
    if game_logs and game_logs[-1] == message:
        return
    game_logs.append(message)
    if len(game_logs) > 500:
        game_logs = game_logs[-500:]

    dead_clients = []
    with sse_lock:
        clients_snapshot = list(sse_clients)
    for client in clients_snapshot:
        try:
            client.put_nowait(message)
        except Full:
            pass
        except:
            dead_clients.append(client)
    with sse_lock:
        for client in dead_clients:
            if client in sse_clients:
                sse_clients.remove(client)

def log_global_event(event_type, speaker=None, target=None, content=None, result=None):
    entry = {
        "type": event_type,
        "speaker": speaker,
        "target": target,
        "content": content[:200] if content else None,
        "result": result,
        "time": time.time()
    }
    with log_lock:
        if not global_session_log["rounds"] or global_session_log["rounds"][-1].get("closed", False):
            global_session_log["rounds"].append({
                "round": len(global_session_log["rounds"]) + 1,
                "events": [],
                "closed": False
            })
        global_session_log["rounds"][-1]["events"].append(entry)

def get_global_summary(round_num=None):
    with log_lock:
        if round_num is None:
            round_num = len(global_session_log["rounds"])
        target_round = None
        for r in global_session_log["rounds"]:
            if r.get("round") == round_num:
                target_round = r
                break
        if not target_round:
            return "暂无全局记录"
        events = target_round.get("events", [])[-10:]
        lines = []
        for e in events:
            if e["type"] == "speech":
                lines.append(f"{e['speaker']}：{e['content'][:30]}...")
            elif e["type"] == "vote":
                lines.append(f"投票：{e['speaker']}发起，{e['target']}被{'通过' if e['result'] else '否决'}")
            elif e["type"] == "guess":
                lines.append(f"猜色：{e['speaker']}{'✅' if e['result'] else '❌'}")
            elif e["type"] == "minority_vote":
                lines.append(f"少数决：{e['target']}获胜")
            elif e["type"] == "emotion_contagion":
                lines.append(f"情感传染：{e['speaker']}→{e['target']}")
            elif e["type"] == "challenge":
                lines.append(f"质疑：{e['speaker']}质疑{e['target']}{'成功' if e['result'] else '失败'}")
            elif e["type"] == "exchange":
                lines.append(f"交换：{e['speaker']}↔{e['target']}")
        return "\n".join(lines) if lines else "暂无事件记录"

def clear_global_log():
    global global_session_log
    with log_lock:
        global_session_log = {"rounds": [], "summary": ""}

def save_history():
    try:
        data = {
            "scores": player_scores,
            "alive": player_alive,
            "total_games": total_games_played,
            "current_round": current_round,
            "logs": game_logs[-200:]
        }
        _atomic_write_json(HISTORY_FILE, data)
        if game_logs:
            archive_path = os.path.join(GAME_LOG_DIR, f"game_{current_round}.log")
            with open(archive_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(game_logs))
        if global_session_log and global_session_log.get("rounds"):
            event_path = os.path.join(GAME_LOG_DIR, f"game_{current_round}_events.json")
            _atomic_write_json(event_path, global_session_log)
    except Exception as e:
        print(f"⚠️ 保存历史失败: {e}")

def load_history():
    global player_scores, player_alive, total_games_played, current_round, game_logs
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            player_scores = data.get("scores", {name: 0 for name in PLAYER_NAMES})
            player_alive = data.get("alive", {name: True for name in PLAYER_NAMES})
            total_games_played = data.get("total_games", 0)
            current_round = data.get("current_round", 0)
            game_logs = data.get("logs", [])
            for name in PLAYER_NAMES:
                if name not in player_scores:
                    player_scores[name] = 0
                if name not in player_alive:
                    player_alive[name] = True
            return True
    except Exception as e:
        print(f"⚠️ 历史文件损坏或无法读取（已回退为初始状态）: {e}")
    return False

# ==================== 基因池 ====================
def load_gene_pool():
    if os.path.exists(GENE_POOL_FILE):
        try:
            with open(GENE_POOL_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 基因池文件损坏（已回退为空基因池）: {e}")
    return {"genes": [], "last_updated": time.time()}

def save_gene_pool(pool):
    pool["last_updated"] = time.time()
    _atomic_write_json(GENE_POOL_FILE, pool)

def add_to_gene_pool(player):
    pool = load_gene_pool()
    strategy_tags = []
    for ref in player.memory_manager.data.get("self_reflections", [])[-2:]:
        for tag in ["后发制人", "规则优先", "结盟网络", "验证驱动", "误导诱导",
                     "少数决专精", "共识构建", "交换优先", "质疑专精", "独立判断"]:
            if tag in ref:
                strategy_tags.append(tag)
    strategy_tags = list(set(strategy_tags)) or ["通用策略"]

    gene = {
        "id": f"gene_{int(time.time())}_{player.name}",
        "profile": player.cognitive_profile.copy(),
        "prototype": player.prototype_name,
        "strategy_tags": strategy_tags,
        "score": player.score,
        "weight": max(0.1, (player.score + 3) / 10),
        "created_at": current_round,
        "metacognition": {
            "self_awareness": player.metacognition.get("self_awareness", 0.3)
        }
    }

    THRESHOLD = 0.08
    for existing in pool["genes"]:
        sim = sum(1 for k in ["info_initiative", "risk_tolerance", "trust_propensity", "time_horizon"]
                  if abs(existing["profile"][k] - gene["profile"][k]) < THRESHOLD)
        if sim == 4 and set(existing.get("strategy_tags", [])) == set(strategy_tags):
            return

    pool["genes"].append(gene)
    if len(pool["genes"]) > 50:
        pool["genes"].sort(key=lambda x: x["weight"], reverse=True)
        pool["genes"] = pool["genes"][:50]
    save_gene_pool(pool)

def inherit_from_gene_pool():
    pool = load_gene_pool()
    if len(pool["genes"]) < 2:
        return None
    g1, g2 = random.sample(pool["genes"], 2)
    new_profile = {}
    for k in ["info_initiative", "risk_tolerance", "trust_propensity", "time_horizon"]:
        new_profile[k] = random.choice([g1["profile"][k], g2["profile"][k]])
    new_profile["decision_style"] = random.choice([g1["profile"]["decision_style"], g2["profile"]["decision_style"]])
    chosen_gene = random.choice([g1, g2])
    inherited_meta = random.choice([g1, g2]).get("metacognition", {"self_awareness": 0.3})
    return {
        "profile": new_profile,
        "prototype": random.choice([g1["prototype"], g2["prototype"]]),
        "strategy_tags": chosen_gene.get("strategy_tags", []),
        "metacognition": inherited_meta
    }

# ==================== 记忆系统 ====================
class PlayerMemory:
    def __init__(self, name):
        self.name = name
        self.file_path = os.path.join(MEMORIES_DIR, f"{name}.json")
        self.data = self._load()

    def _load(self):
        default = {
            "player_name": self.name,
            "total_games": 0,
            "total_survivals": 0,
            "total_correct_guesses": 0,
            "long_term_memory": {"root": "未定义核心信念", "branches": []},
            "episodic_memory": [],
            "personal_experience": [],
            "style_preferences": {"hunter": 1.0, "trader": 1.0, "ghost": 1.0},
            "alliance_history": [],
            "post_game_memories": [],
            "self_reflections": [],
            "last_updated": time.time()
        }
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
                return data
            except Exception as e:
                print(f"⚠️ 玩家记忆文件损坏（{self.name}，已回退为默认记忆）: {e}")
        return default

    def save(self):
        self.data["last_updated"] = time.time()
        try:
            _atomic_write_json(self.file_path, self.data)
        except Exception as e:
            print(f"⚠️ 玩家记忆保存失败（{self.name}）: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def add_episodic(self, event_type, target=None, result=None, importance=5, emotional_state=None):
        if not emotional_state:
            emotional_state = {"trust": 0, "frustration": 0, "superiority": 0, "insecurity": 0}
        entry = {
            "type": event_type,
            "target": target,
            "result": result,
            "round": current_round,
            "importance": importance,
            "emotional_state": emotional_state.copy(),
            "timestamp": time.time()
        }
        self.data["episodic_memory"].append(entry)
        if len(self.data["episodic_memory"]) > 200:
            self._consolidate_episodic()
        self.save()

    def _consolidate_episodic(self):
        episodes = self.data["episodic_memory"]
        if len(episodes) < 50:
            return
        merged, skip = [], set()
        for i, e1 in enumerate(episodes):
            if i in skip:
                continue
            occ = 1
            for j in range(i + 1, len(episodes)):
                if j in skip:
                    continue
                e2 = episodes[j]
                if e1["type"] == e2["type"] and e1.get("target") == e2.get("target"):
                    occ += 1
                    skip.add(j)
            e1["occurrences"] = occ
            merged.append(e1)
        self.data["episodic_memory"] = merged[-100:] if len(merged) > 100 else merged

    def consolidate_semantic(self):
        episodes = self.data["episodic_memory"]
        if not episodes:
            return
        patterns = defaultdict(int)
        for ep in episodes:
            if ep.get("round") == current_round:
                key = f"{ep['type']}_{ep.get('result', '')}"
                patterns[key] += 1
        for pat, cnt in patterns.items():
            if cnt >= 2:
                self.add_experience("行为模式", f"本局频繁出现：{pat}", confidence=0.5)

    def add_experience(self, branch_name, rule, confidence=0.5, context=None):
        branches = self.data["long_term_memory"]["branches"]
        for branch in branches:
            if branch["name"] == branch_name:
                for leaf in branch["leaves"]:
                    if leaf["rule"] == rule:
                        leaf["confidence"] = (leaf["confidence"] + confidence) / 2
                        leaf["occurrences"] += 1
                        self.save()
                        return
                branch["leaves"].append({
                    "rule": rule,
                    "confidence": confidence,
                    "occurrences": 1,
                    "last_updated": time.time(),
                    "context": context
                })
                self.save()
                return
        branches.append({
            "name": branch_name,
            "confidence": confidence,
            "leaves": [{
                "rule": rule,
                "confidence": confidence,
                "occurrences": 1,
                "last_updated": time.time(),
                "context": context
            }]
        })
        self.save()

    def get_autobiography_constraints(self):
        constraints = []
        for ref in self.data.get("self_reflections", [])[-3:]:
            m = re.search(r'(我最大[的]?错误是[^。]+)|(我后悔[^。]+)', ref)
            if m:
                constraints.append(f"教训：{m.group(0)}")
            m2 = re.search(r'(我最大[的]?正确决定是[^。]+)', ref)
            if m2:
                constraints.append(f"经验：{m2.group(0)}")
        for a in self.data.get("alliance_history", [])[-5:]:
            constraints.append(f"与{a['partner']}关系：{a['level']}（第{a['round']}局）")
        return constraints

    def record_relationship(self, partner, relation_type, round_num=None):
        if not round_num:
            round_num = current_round
        self.data["alliance_history"].append({
            "partner": partner,
            "level": relation_type,
            "round": round_num,
            "timestamp": time.time()
        })
        if len(self.data["alliance_history"]) > 100:
            self.data["alliance_history"] = self.data["alliance_history"][-100:]
        self.save()

    def add_personal_experience(self, text):
        pass

    def add_post_game_memory(self, summary):
        self.data["post_game_memories"].append(summary)
        if len(self.data["post_game_memories"]) > 50:
            self.data["post_game_memories"] = self.data["post_game_memories"][-50:]
        self.save()

    def add_alliance(self, partner, level, round_num):
        self.record_relationship(partner, level, round_num)

    def add_self_reflection(self, text):
        self.data["self_reflections"].append(text)
        if len(self.data["self_reflections"]) > 50:
            self.data["self_reflections"] = self.data["self_reflections"][-50:]
        self.save()

    def search_tree(self, query, max_depth=3):
        results = []
        for branch in self.data["long_term_memory"]["branches"]:
            if query in branch["name"]:
                for leaf in branch["leaves"]:
                    results.append(leaf)
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results[:max_depth]

    def search_emotional_memory(self, target_name, limit=5):
        results = []
        for ep in self.data["episodic_memory"]:
            if ep.get("target") == target_name:
                results.append(ep)
        results.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return results[:limit]

    def search_similar_episodes(self, emotional_state, limit=3):
        similar = []
        for ep in self.data["episodic_memory"]:
            if not ep.get("emotional_state"):
                continue
            dist = sum((ep["emotional_state"].get(k, 0) - emotional_state.get(k, 0)) ** 2
                       for k in emotional_state)
            similar.append((ep, dist))
        similar.sort(key=lambda x: x[1])
        return [ep for ep, _ in similar[:limit]]

    def increment_games(self, survived=False, correct_guess=False):
        self.data["total_games"] += 1
        if survived:
            self.data["total_survivals"] += 1
        if correct_guess:
            self.data["total_correct_guesses"] += 1
        self.save()

# ==================== 教练跨局记忆 ====================
class CoachMemory:
    def __init__(self):
        self.data = self.load()

    def load(self):
        if os.path.exists(COACH_MEMORY_FILE):
            try:
                with open(COACH_MEMORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if "player_profiles" not in data:
                    data["player_profiles"] = {}
                if "reports" not in data:
                    data["reports"] = []
                if "trends" not in data:
                    data["trends"] = {}
                return data
            except:
                pass
        return {"reports": [], "trends": {}, "player_profiles": {}}

    def save(self):
        temp_file = COACH_MEMORY_FILE + ".tmp"
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            if os.path.exists(COACH_MEMORY_FILE):
                os.replace(temp_file, COACH_MEMORY_FILE)
            else:
                os.rename(temp_file, COACH_MEMORY_FILE)
        except Exception as e:
            print(f"⚠️ 教练记忆保存失败: {e}")

    def add_report(self, report):
        self.data["reports"].append(report)
        if len(self.data["reports"]) > 200:
            self.data["reports"] = self.data["reports"][-200:]
        self.update_trends(report)
        self.save()

    def update_trends(self, report):
        t = self.data["trends"]
        t.setdefault("game_count", 0)
        t["game_count"] += 1
        for key in ["probe_count", "vote_count", "perfect_ending"]:
            t.setdefault(key, 0)
            t[key] += report.get(key, 0)

    def update_player_profile(self, player, game_round, survived, guessed_right, reflection_text):
        name = player.name
        profiles = self.data["player_profiles"]
        if name not in profiles:
            profiles[name] = {
                "total_games": 0,
                "survivals": 0,
                "correct_guesses": 0,
                "guess_accuracy_history": [],
                "cognitive_blindspots": [],
                "dominant_strategies": [],
                "adaptation_rate": 0.0,
                "trust_volatility": 0.0,
                "last_round_advice": None,
                "advice_history": []
            }
        profile = profiles[name]
        profile["total_games"] += 1
        if survived:
            profile["survivals"] += 1
        if guessed_right:
            profile["correct_guesses"] += 1

        profile["guess_accuracy_history"].append(guessed_right)
        if len(profile["guess_accuracy_history"]) > 10:
            profile["guess_accuracy_history"] = profile["guess_accuracy_history"][-10:]

        blindspots = []
        if "过度信任" in reflection_text or "轻信" in reflection_text:
            blindspots.append("过度信任")
        if "信息不足" in reflection_text or "缺乏验证" in reflection_text:
            blindspots.append("验证不足")
        if "冒险" in reflection_text or "冲动" in reflection_text:
            blindspots.append("风险偏好过高")
        if "保守" in reflection_text or "不敢" in reflection_text:
            blindspots.append("过度保守")
        if "从众" in reflection_text or "跟随" in reflection_text:
            blindspots.append("从众倾向")
        profile["cognitive_blindspots"] = blindspots

        strategy_tags = []
        for tag in ["后发制人", "规则优先", "结盟网络", "验证驱动", "误导诱导",
                     "少数决专精", "共识构建", "交换优先", "质疑专精", "独立判断"]:
            if tag in reflection_text:
                strategy_tags.append(tag)
        profile["dominant_strategies"].append(strategy_tags)
        if len(profile["dominant_strategies"]) > 10:
            profile["dominant_strategies"] = profile["dominant_strategies"][-10:]

        recent = profile["dominant_strategies"]
        if len(recent) >= 2:
            switches = sum(1 for i in range(1, len(recent)) if set(recent[i]) != set(recent[i-1]))
            profile["adaptation_rate"] = switches / (len(recent) - 1)
        else:
            profile["adaptation_rate"] = 0.0

        self.save()

def generate_pre_game_advice(player_name, coach_memory):
    profiles = coach_memory.data.get("player_profiles", {})
    profile = profiles.get(player_name)
    if not profile or profile["total_games"] < 2:
        return None

    advice_parts = []
    recent_guesses = profile["guess_accuracy_history"][-3:]
    if len(recent_guesses) >= 3 and not any(recent_guesses):
        advice_parts.append("你已连续三局猜错自己的颜色。请重新审视你的线索验证流程，不要依赖单一来源。")

    blindspots = profile.get("cognitive_blindspots", [])
    if "过度信任" in blindspots:
        advice_parts.append("历史数据显示你倾向于过度信任交换对象。建议你在交换后用其他线索交叉验证。")
    if "验证不足" in blindspots:
        advice_parts.append("你往往在没有充分验证的情况下做出判断。请优先获取100%真实关系线索。")
    if "从众倾向" in blindspots:
        advice_parts.append("你容易跟随主流意见。尝试在发言中坚持自己的独立判断，即使它目前不受欢迎。")

    if profile["adaptation_rate"] < 0.2 and profile["total_games"] >= 5:
        advice_parts.append("你的策略已多局未变。对手可能已经适应了你，考虑改变你的信息获取或投票模式。")

    if profile["total_games"] > 3 and profile["survivals"] / profile["total_games"] < 0.3:
        advice_parts.append("你的存活率偏低。也许应该更关注防御，而不是急于猜色。")

    if not advice_parts:
        return None

    selected = random.choice(advice_parts)
    profile["last_round_advice"] = selected
    profile["advice_history"].append({"round": current_round, "advice": selected})
    coach_memory.save()
    return f"📋 教练赛前建议：{selected}"
# ==================== AI 玩家类 ====================
class Player:
    def __init__(self, name, color, mode_config, game_mode_config):
        self.name = name
        self.color = color
        self.known_color = None
        self.alive = True
        self.score = 0
        self.mode_config = mode_config
        self.game_mode = game_mode_config
        self.can_initiate_vote = True

        self.self_belief = {c: 0.0 for c in COLORS}
        self.suspicion = defaultdict(float)
        self.trust = defaultdict(lambda: 1.0)
        self.reputation = defaultdict(lambda: 0.5)

        self.strategy = "合作"
        self.evolution = {
            "level": 0,
            "experience": 0,
            "strategy_weights": {
                "攻击": 1.0, "防守": 1.0, "诱导": 1.0,
                "伪装": 1.0, "合作": 1.0, "拉拢": 1.0
            },
            "preferred_strategy": None,
            "game_count": 0
        }
        self.total_guesses = 0
        self.correct_guesses = 0
        self.speech_count = 0
        self.referenced_count = 0
        self.daily_speech_count = 0
        self.stats = {"推理": 0, "说服": 0, "欺骗": 0, "生存": 0, "适应": 0}

        # 从基因池继承认知参数
        inherited = inherit_from_gene_pool()
        if inherited:
            self.cognitive_profile = inherited["profile"]
            self.prototype_name = inherited["prototype"]
            self.inherited_strategy_tags = inherited.get("strategy_tags", [])
            self.metacognition = inherited.get("metacognition", {"self_awareness": random.uniform(0.2, 0.5)})
        else:
            self.cognitive_profile = self._init_cognitive_profile()
            self.prototype_name = self._get_prototype_name()
            self.inherited_strategy_tags = []
            self.metacognition = {
                "self_awareness": random.uniform(0.2, 0.5),
                "recognized_blindspots": [],
                "strategy_explicit_reason": ""
            }

        # 5%概率随机变异
        if random.random() < 0.05:
            for k in ["info_initiative", "risk_tolerance", "trust_propensity", "time_horizon"]:
                self.cognitive_profile[k] = max(0.1, min(0.9,
                    self.cognitive_profile[k] + random.uniform(-0.2, 0.2)))

        self.cognitive_history = []
        self.cognitive_profile_summary = self._generate_cognitive_summary()

        # 情绪系统
        self.emotion = {
            "trust": 0.0,
            "frustration": 0.0,
            "superiority": 0.0,
            "insecurity": 0.0
        }
        self.emotion_eruption_active = False
        self.emotion_eruption_type = None
        self.emotion_eruption_round = 0

        # 信念系统
        self.belief = {
            "most_likely_color": None,
            "confidence": 0.0,
            "strength": 0.3,
            "guess_history": []
        }
        self.visible_colors = {}

        # 工作记忆
        self.session_memory = []
        self.session_archive = []
        self.memory = self.session_memory
        self.memory_manager = PlayerMemory(name)

        # 推理结果缓存：同一天内多次发言复用局势分析/树状推理，显著节省 token
        self._analysis_cache = {}

        # 线索系统
        self.clues = []
        self.claimed_clues = []
        self.MAX_CLUES = 6

        # 探针系统
        self.rule_probe_available = True
        self.rule_probe_cooldown_round = 0

        # 调查与社交
        self.investigation_count = 2
        self.alliances = []
        self.has_private_chatted = False
        self.counter_attack_target = None

        # 自我信息
        self.self_info = self._generate_self_info()

        # 独立判断追踪
        self.independent_judgments = []
        self.daily_initial_judgment = None

        # 注入规则简报
        self._inject_rules()

        # 加载自传约束到工作记忆
        constraints = self.memory_manager.get_autobiography_constraints()
        if constraints:
            self.session_memory.insert(0, "📖 记忆约束：" + "；".join(constraints[:3]))

    # ==================== 基础方法 ====================
    def _generate_self_info(self):
        r = random.random()
        if r < 0.4:
            return f"根据初步分析，你的颜色很可能是{self.color}。"
        elif r < 0.8:
            fake_color = random.choice([c for c in COLORS if c != self.color])
            return f"根据初步分析，你的颜色很可能是{fake_color}。"
        else:
            choices = [
                "你的颜色在第一天讨论中被某人提到过。",
                "你的颜色与甲（或某位玩家）可能存在关联。",
                f"环境线索暗示，你不可能是蓝色以外的颜色。" if self.color != "蓝"
                else "环境线索暗示，你不可能是红色以外的颜色。",
                "积分榜上的位置暗示，你的颜色与积分最低的玩家不同。"
            ]
            return random.choice(choices)

    def _init_cognitive_profile(self):
        chosen = random.choice(list(COGNITIVE_PROTOTYPES.keys()))
        self.prototype_name = chosen
        return COGNITIVE_PROTOTYPES[chosen].copy()

    def _get_prototype_name(self):
        for name, profile in COGNITIVE_PROTOTYPES.items():
            if all(profile[k] == self.cognitive_profile[k]
                   for k in ["info_initiative", "risk_tolerance",
                             "trust_propensity", "time_horizon", "decision_style"]):
                return name
        return "unknown"

    def _generate_cognitive_summary(self):
        p = self.cognitive_profile
        initiative = "主动" if p["info_initiative"] > 0.5 else "被动"
        risk = "冒险" if p["risk_tolerance"] > 0.5 else "保守"
        trust = "信任" if p["trust_propensity"] > 0.5 else "怀疑"
        horizon = "长期布局" if p["time_horizon"] > 0.5 else "短期生存"
        style = "逻辑分析" if p["decision_style"] == "logic" else "直觉判断"
        return f"{self.prototype_name}: 主动性:{initiative}, 风险:{risk}, 信任:{trust}, 视野:{horizon}, 风格:{style}"

    def init_vision(self, players):
        for pl in players:
            if pl.name != self.name:
                if random.random() < 0.3:
                    self.visible_colors[pl.name] = random.choice(
                        [c for c in COLORS if c != pl.color])
                else:
                    self.visible_colors[pl.name] = pl.color

    def init_belief(self, alive_players):
        info = self.self_info
        for c in COLORS:
            if f"你的颜色很可能是{c}" in info:
                self.belief["most_likely_color"] = c
                self.belief["confidence"] = 0.4
                self.belief["strength"] = 0.35
                return
        visible_colors = [
            self.visible_colors.get(p.name, p.color)
            for p in alive_players if p.name != self.name
        ]
        color_counts = {c: visible_colors.count(c) for c in COLORS}
        most_common = max(color_counts, key=color_counts.get)
        self.belief["most_likely_color"] = most_common
        self.belief["confidence"] = 0.2

    def update_belief(self, event_type, data=None):
        if event_type == "guess_correct":
            if self.belief["most_likely_color"] == self.known_color:
                self.belief["strength"] = min(1.0, self.belief["strength"] + 0.35)
                self.belief["confidence"] = min(1.0, self.belief["confidence"] + 0.15)
        elif event_type == "guess_wrong":
            self.belief["most_likely_color"] = None
            self.belief["confidence"] = 0.2
            self.belief["strength"] = 0.3
        elif event_type == "relation_clue":
            if self.belief["most_likely_color"]:
                self.belief["confidence"] += 0.03
        elif event_type == "emotional_eruption":
            self.belief["strength"] = max(0.1, self.belief["strength"] - 0.3)

    # ==================== 探针系统 ====================
    def use_rule_probe(self, rule_index, current_day):
        global probe_used_this_game
        if probe_used_this_game >= RULE_PROBE_LIMIT:
            return {"success": False, "error": "本局探针次数已用完"}
        if not self.rule_probe_available:
            return {"success": False, "error": "个人探针冷却中"}
        if (current_day - self.rule_probe_cooldown_round < 2
                and self.rule_probe_cooldown_round > 0):
            remaining = 2 - (current_day - self.rule_probe_cooldown_round)
            return {"success": False, "error": f"个人探针冷却中，还需{remaining}天"}
        if rule_index < 0 or rule_index >= len(world_rules):
            return {"success": False, "error": "规则不存在"}

        self.rule_probe_available = False
        self.rule_probe_cooldown_round = current_day
        probe_used_this_game += 1

        rule = world_rules[rule_index]
        result = "真" if rule["truth"] else ("假" if random.random() >= 0.3 else random.choice(["真", "假"]))
        broadcast_log(f"  🔎 {self.name} 使用规则探针探测了规则「{rule['rule']}」→ 结果为「{result}」")
        return {"success": True, "rule": rule["rule"], "result": result}

    # ==================== 情绪系统 ====================
    def _trigger_emotional_memory(self, speaker_name):
        episodes = self.memory_manager.search_emotional_memory(speaker_name, limit=3)
        if not episodes:
            return
        avg_trust = sum(e.get("emotional_state", {}).get("trust", 0) for e in episodes) / len(episodes)
        avg_frustration = sum(e.get("emotional_state", {}).get("frustration", 0) for e in episodes) / len(episodes)
        if avg_trust < 0 and avg_frustration > 0.3:
            self.emotion["insecurity"] = min(1.0, self.emotion["insecurity"] + 0.1)
            self.emotion["trust"] = max(-1.0, self.emotion["trust"] - 0.1)

    def _cross_emotion_update(self):
        if self.emotion["frustration"] > 0.5:
            self.emotion["trust"] = max(-1.0, self.emotion["trust"] - 0.03)
        if self.emotion["insecurity"] > 0.5:
            self.emotion["frustration"] = min(1.0, self.emotion["frustration"] + 0.02)
        if self.emotion["superiority"] > 0.5:
            self.emotion["insecurity"] = max(-1.0, self.emotion["insecurity"] - 0.03)
        if self.emotion["trust"] > 0.5:
            self.emotion["superiority"] = min(1.0, self.emotion["superiority"] + 0.02)

    def _get_reasoning_depth(self):
        if self.emotion_eruption_active:
            return 3
        max_negative = max(abs(self.emotion["frustration"]), abs(self.emotion["insecurity"]))
        return max(3, int(6 - 3 * max_negative))

    def _emotional_contagion(self, speaker_emotion, source_name=""):
        changes = {}
        if speaker_emotion.get("frustration", 0) > 0.6:
            self.emotion["frustration"] = min(1.0, self.emotion["frustration"] + 0.03)
            changes["frustration"] = 0.03
        if speaker_emotion.get("insecurity", 0) > 0.6:
            self.emotion["insecurity"] = min(1.0, self.emotion["insecurity"] + 0.02)
            changes["insecurity"] = 0.02
        for k, v in changes.items():
            if abs(v) > 0.01:
                self.memory_manager.add_episodic(
                    "emotion_contagion", target=source_name,
                    result=f"{k}:+{v:.2f}", importance=2,
                    emotional_state=self.emotion.copy()
                )
                break

    def update_cognitive_profile(self, survived, guessed_right, round_num):
        p = self.cognitive_profile
        if survived and guessed_right:
            p["info_initiative"] += 0.05
            p["risk_tolerance"] += 0.03
            p["time_horizon"] += 0.03
        elif not survived and not guessed_right:
            p["info_initiative"] -= 0.05
            p["trust_propensity"] -= 0.05
        elif survived and not guessed_right:
            p["trust_propensity"] += 0.05
            p["risk_tolerance"] -= 0.03
        for key in ["info_initiative", "risk_tolerance", "trust_propensity", "time_horizon"]:
            p[key] = max(0.1, min(0.9, p[key]))
        self.cognitive_profile_summary = self._generate_cognitive_summary()
        self.cognitive_history.append({
            "round": round_num, "profile": p.copy(),
            "survived": survived, "guessed_right": guessed_right
        })

    def trigger_emotion(self, event_type, target=None, result=None):
        changes_map = {
            "verify_true": {"trust": 0.15, "frustration": -0.05, "superiority": 0.10, "insecurity": -0.10},
            "verify_false": {"trust": -0.10, "frustration": 0.20, "superiority": -0.10, "insecurity": 0.15},
            "minority_win": {"trust": 0.05, "frustration": -0.05, "superiority": 0.20, "insecurity": -0.05},
            "minority_lose": {"trust": 0, "frustration": 0.10, "superiority": -0.05, "insecurity": 0.05},
            "vote_pass": {"trust": 0.05, "frustration": -0.05, "superiority": 0.20, "insecurity": -0.05},
            "betrayed": {"trust": -0.25, "frustration": 0.25, "superiority": -0.10, "insecurity": 0.15},
            "guess_correct": {"trust": 0.05, "frustration": -0.05, "superiority": 0.15, "insecurity": -0.05},
            "guess_wrong": {"trust": -0.05, "frustration": 0.20, "superiority": -0.10, "insecurity": 0.15},
            "exchange_verified": {"trust": 0.10, "frustration": -0.03, "superiority": 0.05, "insecurity": -0.05},
            "challenge_success": {"trust": -0.05, "frustration": -0.05, "superiority": 0.20, "insecurity": -0.10},
            "challenge_fail": {"trust": -0.05, "frustration": 0.15, "superiority": -0.10, "insecurity": 0.10},
            "independent_judgment_correct": {"trust": 0.08, "frustration": -0.03, "superiority": 0.10, "insecurity": -0.05},
            "independent_judgment_wrong": {"trust": 0, "frustration": 0, "superiority": 0, "insecurity": 0},
        }
        changes = changes_map.get(event_type, {"trust": 0, "frustration": 0, "superiority": 0, "insecurity": 0})
        for key in changes:
            self.emotion[key] = max(-1.0, min(1.0, self.emotion[key] + changes[key]))
        for key, val in changes.items():
            if abs(val) > 0.05:
                self.memory_manager.add_episodic(
                    f"emotion_{event_type}", target=target,
                    result=f"{key}:{self.emotion[key]:.2f}",
                    importance=3 if abs(val) > 0.15 else 1,
                    emotional_state=self.emotion.copy()
                )
                break
        if (self.emotion.get("frustration", 0) > 0.6 and self.emotion.get("trust", 0) < -0.3) or \
           (self.emotion.get("insecurity", 0) > 0.6 and self.emotion.get("frustration", 0) > 0.3):
            self.emotion_eruption_active = True
            self.emotion_eruption_type = "frustration" if self.emotion["frustration"] > self.emotion["insecurity"] else "insecurity"
            self.emotion_eruption_round = current_round
            self.memory_manager.add_episodic("emotional_eruption", target=self.name,
                                             result=self.emotion_eruption_type, importance=7,
                                             emotional_state=self.emotion.copy())
            self.update_belief("emotional_eruption")

    def apply_emotional_decay(self):
        for key in ["trust", "frustration", "superiority", "insecurity"]:
            self.emotion[key] *= 0.4
        self._cross_emotion_update()
        if self.emotion_eruption_active and current_round - self.emotion_eruption_round >= 2:
            for key in self.emotion:
                self.emotion[key] = 0.5
            self.emotion_eruption_active = False

    # ==================== 线索系统 ====================
    def set_initial_clues(self, players, speech_count=1):
        self.clues = generate_daily_clues(self, players, speech_count)
        if cross_game_penalty and self.name == cross_game_penalty:
            original_count = len(self.clues)
            self.clues = self.clues[:max(1, len(self.clues)//2)]
            broadcast_log(f"  📉 {self.name} 因跨局惩罚，初始线索从{original_count}条减为{len(self.clues)}条")

    def add_clue(self, target, color, credibility=0.5, source="unknown", source_player=None):
        if len(self.clues) >= self.MAX_CLUES:
            self.clues.sort(key=lambda x: x.get("credibility", 0))
            self.clues.pop(0)
        self.clues.append({
            "target": target, "color": color, "credibility": credibility,
            "type": "indirect", "verified": False, "source": source, "source_player": source_player
        })
        return self.clues[-1]

    def claim_clue(self, target, color):
        self.claimed_clues.append({"target": target, "color": color, "claimed": True, "verified": False})
        return f"我声称 {target} 的颜色是 {color}"

    def investigate_clue(self, clue_index):
        if self.investigation_count <= 0:
            return {"success": False, "error": "调查次数已用完"}
        if clue_index >= len(self.clues):
            return {"success": False, "error": "线索不存在"}
        self.investigation_count -= 1
        clue = self.clues[clue_index]
        source_info = f"这条线索来自 {clue['source']}"
        if clue.get('source_player'):
            source_info += f"（来源玩家：{clue['source_player']}）"
        return {"success": True, "message": source_info, "credibility": clue["credibility"],
                "source_player": clue.get("source_player")}

    # ==================== 规则注入 ====================
    def _inject_rules(self):
        rules_text = (
            "【欺诈游戏·规则简报】\n"
            "1. 你不知道自己的颜色，但能看到其他所有玩家的颜色（可能有30%色盲错误）。\n"
            "2. 你的目标：通过线索交换和验证，猜出自己的颜色并存活。\n"
            "3. 线索不会自动发放。你必须通过交换、少数决、公投来获取新线索。沉默意味着线索枯竭。\n"
            "4. 互盲交换：你可以和任何人交换线索，双方同时给出，不公开。交换后需要自行验证真假。\n"
            "5. 质疑下注：如果你认为某人公开的声明是假的，可以押注一条线索质疑他。"
            "质疑成功夺走对方线索，失败输掉赌注。\n"
            "6. 规则探针：唯一能探测世界规则真假的工具（全局限2次，个人冷却2天，"
            "探测假规则有30%概率返回错误结果）。\n"
            "7. 少数决：付1条线索入场，奖池归少数派。获胜者额外获得1条100%真实关系线索。"
            "平局随机退还。\n"
            "8. 联盟公投：第2天起可用，发起者成功+2分失败-1分。目标获得反击权（仅一次，不用过期）。\n"
            "9. 世界规则：每局存在1条真实规则和4条虚假规则。第3天可能被认知干扰改变。\n"
            "10. 淘汰规则：第1天猜错保护。第2天起猜错者中跨局总积分最高者淘汰。每天最多淘汰1人。\n"
            "11. 结局规则：连续4天无人淘汰触发中等结局。中等结局后，存活者进行世界规则真假判断。\n"
            "12. 完美结局：中等结局后，存活者全部猜对世界规则真假，额外+5分。猜错者扣分。\n"
            "13. 信任和信誉来自被验证的经历，不是私聊次数。\n"
            "14. 你可以在发言末尾附加一句独立判断声明（如'我认为规则3为真'），"
            "正确的独立判断会提升你的信誉，错误的不受惩罚。\n"
            "15. 最后公共交流时，你只能分享一条具体线索或事实。禁止使用比喻、哲学、抽象表述。"
        )
        self.session_memory.insert(0, f"📋 规则简报：{rules_text}")
        self.session_archive.insert(0, f"📋 规则简报：{rules_text}")

    # ==================== 记忆与会话 ====================
    def record_session(self, content, is_public=False):
        prefix = "[全体] " if is_public else ""
        entry = f"{prefix}{content}"
        self.session_memory.append(entry)
        self.session_archive.append(entry)
        if len(self.session_memory) > 80:
            self.session_memory = self.session_memory[-80:]
        self.memory = self.session_memory

    def hear(self, speaker, text, is_public=False):
        if speaker == self.name:
            return
        prefix = "[全体] " if is_public else ""
        entry = f"{prefix}{speaker}说：{text}"
        self.record_session(entry)
        self._trigger_emotional_memory(speaker)
        if self.name in text and speaker != self.name:
            self.referenced_count += 1
        for col in COLORS:
            if re.search(rf'{self.name}.*?{col}', text):
                self.self_belief[col] += self.trust[speaker] * 1.0
                if self.trust[speaker] >= 1.8:
                    self.known_color = col
                    self.self_belief[col] = 5.0

    def search_tree(self, query, max_depth=3):
        return self.memory_manager.search_tree(query, max_depth)

    def add_experience_to_tree(self, branch_name, rule, confidence=0.5, context=None):
        self.memory_manager.add_experience(branch_name, rule, confidence, context)

    def add_post_game_memory(self, summary):
        self.memory_manager.add_post_game_memory(summary)

    def add_alliance(self, partner, level, round_num):
        self.memory_manager.add_alliance(partner, level, round_num)

    # ==================== API调用 ====================
    def _get_api_client(self):
        return get_api_client()

    def _call_with_retry(self, prompt, max_tokens, timeout, temperature=0.7, retries=1):
        # 重试时在两个 API Key 间交替（显式取不同 client），规避单 Key 限流/故障；指数退避避免雪崩
        primary = self._get_api_client()
        backup = client2 if primary is client1 else client1
        clients = [primary, backup]
        last_error = None
        for attempt in range(retries + 1):
            client = clients[attempt % len(clients)]
            try:
                r = client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[{"role": "system", "content": prompt}],
                    temperature=temperature, max_tokens=max_tokens, timeout=timeout
                )
                return r.choices[0].message.content.strip()
            except Exception as e:
                last_error = e
                print(f"⚠️ API调用失败 (尝试{attempt+1}/{retries+1}): {e}")
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
        print(f"❌ API调用彻底失败 ({retries+1}次尝试均失败): {last_error}")
        return None

    # ==================== 元认知层 ====================
    def metacognitive_reflection(self, round_num, significant_event=False):
        """元认知反思：分析自己的思维过程。仅在关键事件后或有足够自我觉察能力时触发。"""
        if not significant_event and random.random() > self.metacognition["self_awareness"]:
            return None

        prompt = f"""
你是{self.name}，正在玩一场欺诈游戏。请进行元认知反思——思考你的思考过程，而不是游戏事件本身。

1. 你今天做决策时，有没有意识到某个可能影响你判断的偏见？
2. 你是否发现自己思维中的某个盲区？
3. 你明天的策略是否需要调整？为什么？

请用2-3句话回答。输出格式：元认知反思：...
"""
        try:
            result = self._call_with_retry(prompt, max_tokens=150, timeout=8, temperature=0.6)
            if result and len(result.strip()) > 5:
                self.record_session(f"🧘 元认知反思：{result.strip()}")
                self.metacognition["strategy_explicit_reason"] = result.strip()
                if "偏见" in result or "倾向" in result:
                    self.metacognition["recognized_blindspots"].append("自我觉察到决策偏见")
                if "盲区" in result or "忽略" in result:
                    self.metacognition["recognized_blindspots"].append("发现认知盲区")
                self.metacognition["self_awareness"] = min(1.0, self.metacognition["self_awareness"] + 0.03)
                return result.strip()
        except:
            pass
        return None

    # ==================== 推理系统 ====================
    def situational_analysis(self, alive_players, round_num):
        if self.game_mode.get("situational_analysis_freq") == "never":
            return "局势分析未启用（轻量模式）"
        # 缓存：同一天内多次调用只算一次，节省约一半分析类 API 调用
        cached = self._analysis_cache.get(("situation", round_num))
        if cached:
            return cached
        alive_names = [p.name for p in alive_players if p.alive]
        recent_summary = get_global_summary(round_num)
        clue_summary = f"持有{len(self.clues)}条线索" if self.clues else "无线索"
        if self.belief['most_likely_color']:
            belief_hint = f"自我信念：最可能{self.belief['most_likely_color']}（置信度{self.belief['confidence']:.1f}，强度{self.belief['strength']:.1f}）"
        else:
            belief_hint = "尚无明确自我信念"
        hist = self.memory_manager
        history_hint = f"你的历史战绩：{hist.get('total_games', 0)}局，存活{hist.get('total_survivals', 0)}次，猜对{hist.get('total_correct_guesses', 0)}次。"
        colorblind_hint = "【认知更新】你已被告知患有色盲。请用线索验证你的视觉认知。" if round_num >= 3 else ""
        rules_text = "\n".join([f"- {r['rule']}" for r in world_rules])
        prompt = f"""
你是{self.name}，第{round_num}天，存活：{', '.join(alive_names)}。
认知框架：{self.cognitive_profile_summary}
情感状态：信任{self.emotion['trust']:.1f}, 受挫{self.emotion['frustration']:.1f}, 优越{self.emotion['superiority']:.1f}, 不安{self.emotion['insecurity']:.1f}
{belief_hint}
{history_hint}
{colorblind_hint}
【本局世界规则】以下规则中有一条为真，其余为假：\n{rules_text}
【自我信息】开局时你收到关于自己颜色的信息：{self.self_info} 这条信息可能为真也可能为假，你需要验证。
线索状态：{clue_summary}

【近期关键事件摘要】：{recent_summary}

请分析当前局势：
1. 谁和谁可能交换了线索或建立了信任关系？
2. 有没有人对某条线索说法不一致？
3. 当前局势对你是【有利 / 不利 / 复杂】？为什么？
4. 根据你目前掌握的信息，哪条世界规则最可能是真的？
5. 你对开局自我信息的信任度如何？有否被验证？
6. 你目前最信任谁？最不信任谁？

输出格式：- 配合关系：... \n- 矛盾言论：... \n- 局势判断：... \n- 规则推断：... \n- 自我信息评估：... \n- 信任评估：...
"""
        try:
            result = self._call_with_retry(prompt, max_tokens=self.game_mode.get("analysis_tokens", 500),
                                           timeout=12, temperature=0.6, retries=1)
            final = result or "局势分析失败，保持谨慎。"
            self._analysis_cache[("situation", round_num)] = final
            return final
        except:
            return "局势分析失败，保持谨慎。"

    def tree_of_thought_reasoning(self, alive_players, round_num):
        if not self.game_mode.get("tree_of_thought", False):
            return "树状推理未启用"
        # 缓存：同一天内多次调用只算一次
        cached = self._analysis_cache.get(("tree", round_num))
        if cached:
            return cached
        alive_names = [p.name for p in alive_players if p.alive and p.name != self.name]
        if not alive_names:
            return "无其他存活玩家，无法推演。"
        clue_summary = f"持有{len(self.clues)}条线索" if self.clues else "无线索"
        situation = self.situational_analysis(alive_players, round_num)
        tree_experience = self.search_tree("策略", 2) if self.game_mode.get("long_term_retrieval", False) else []
        exp_text = "\n【长期经验】\n" + "\n".join([f"- {e['rule']}" for e in tree_experience]) if tree_experience else ""
        similar_episodes = self.memory_manager.search_similar_episodes(self.emotion, limit=2)
        episode_text = "\n【相似历史经历】\n" + "\n".join([
            f"- 第{e['round']}局: {e['type']} (信任{e['emotional_state']['trust']:.1f})" for e in similar_episodes
        ]) if similar_episodes else ""
        depth = self._get_reasoning_depth()
        rules_text = "\n".join([f"- {r['rule']}" for r in world_rules])
        prompt = f"""
你是{self.name}，第{round_num}天，存活玩家：{', '.join(alive_names)}。
认知：{self.cognitive_profile_summary}
情感：信任{self.emotion['trust']:.1f}, 受挫{self.emotion['frustration']:.1f}, 优越{self.emotion['superiority']:.1f}, 不安{self.emotion['insecurity']:.1f}
线索：{clue_summary}
【本局世界规则】{rules_text}
{exp_text}{episode_text}
【局势分析结果】：{situation}
请按照【{depth}层树状思维】推演，每层用2-3句话概括。
"""
        try:
            result = self._call_with_retry(prompt, max_tokens=self.game_mode.get("tree_tokens", 1000),
                                           timeout=25, temperature=0.7, retries=1)
            final = result or "推演失败，执行保守策略。"
            self._analysis_cache[("tree", round_num)] = final
            return final
        except:
            return "推演失败，执行保守策略。"

    # ==================== 发言系统 ====================
    def speak(self, mode, target, alive_players, round_num, strategy, action='speak', short=False):
        global mid_ending_triggered, rule_judgment_phase
        alive_names = [p.name for p in alive_players if p.alive]
        is_private = (mode == 'private' and target is not None)

        if rule_judgment_phase:
            prompt = f"你是{self.name}，中等结局已达成。这是你最后一次发言机会。你必须从本局事件中选择以下三件事之一：①你验证过的某条规则的真假；②你看到的一个具体玩家的颜色；③你在某次投票中观察到的行为模式。只输出这个事实，不要加任何解释、比喻或哲学表述。长度限制：20字以内。"
            try:
                result = self._call_with_retry(prompt, max_tokens=80, timeout=10, temperature=0.5)
                if result and len(result.strip()) > 2:
                    return result.strip()
            except:
                pass
            return "我看到颜色分布与规则推断存在矛盾。"

        if action == 'probe':
            if self.rule_probe_available:
                result = self.use_rule_probe(0, round_num)
                return f"🔎 规则探针探测结果：规则「{result['rule']}」为 {result['result']}" if result["success"] else f"⚠️ {result['error']}"
            return "⚠️ 规则探针已使用"

        if action == 'investigate' and self.clues:
            result = self.investigate_clue(0)
            return f"🔍 调查线索：{result['message']}"

        if action == 'exchange' and target:
            if not self.clues or not target.clues:
                return None
            my_clue_idx, target_clue_idx = random.randrange(len(self.clues)), random.randrange(len(target.clues))
            my_clue, target_clue = self.clues.pop(my_clue_idx), target.clues.pop(target_clue_idx)
            self.add_clue(target_clue["target"], target_clue["color"], 0, "exchange", source_player=target.name)
            target.add_clue(my_clue["target"], my_clue["color"], 0, "exchange", source_player=self.name)
            log_global_event("exchange", speaker=self.name, target=target.name)
            self.memory_manager.add_episodic("exchange", target=target.name, importance=3)
            target.memory_manager.add_episodic("exchange", target=self.name, importance=3)
            return f"🤝 与 {target.name} 完成了互盲交换"

        if action == 'challenge' and target:
            if not self.clues or not target.claimed_clues:
                return None
            challenged_claim = target.claimed_clues[-1]
            is_false = (challenged_claim["color"] != target.color)
            bet_clue = self.clues.pop(0)
            if is_false:
                if target.clues:
                    stolen = target.clues.pop(0)
                    self.add_clue(stolen["target"], stolen["color"], stolen.get("credibility", 0.5), "challenge_won", source_player=target.name)
                self.trigger_emotion("challenge_success", target=target.name)
                target.trigger_emotion("betrayed", target=self.name)
                log_global_event("challenge", speaker=self.name, target=target.name, result=True)
                return f"⚔️ 质疑{target.name}成功！夺走一条线索"
            else:
                target.add_clue(bet_clue["target"], bet_clue["color"], bet_clue.get("credibility", 0.5), "challenge_defended", source_player=self.name)
                self.trigger_emotion("challenge_fail", target=target.name)
                log_global_event("challenge", speaker=self.name, target=target.name, result=False)
                return f"❌ 质疑{target.name}失败，损失一条线索"

        if action == 'alliance' and target:
            if any(a["partner"] == target.name and a.get("active") for a in self.alliances):
                return f"🤝 与 {target.name} 已经结盟"
            self.add_alliance(target.name, "L1", round_num)
            target.add_alliance(self.name, "L1", round_num)
            return f"🤝 向 {target.name} 提议结盟"

        if action == 'claim' and self.clues:
            clue = self.clues[0]
            return f"💬 我声称：{self.claim_clue(clue['target'], clue['color'])}"

        if action == 'vote':
            if round_num < 2:
                return "⚠️ 第1天不能发起公投，请先积累信息。"
            global public_vote_available
            if public_vote_available and len(alive_players) >= 4:
                public_vote_available = False
                candidates = sorted(alive_players, key=lambda p: (player_scores[p.name], len(p.clues)), reverse=True)
                vote_target = candidates[1] if len(candidates) >= 2 and random.random() < 0.15 else candidates[0]
                last_voted = vote_cooldown.get(vote_target.name, 0)
                if last_voted > 0 and (round_num - last_voted) < 3:
                    return f"⚠️ {vote_target.name} 在3天内已被公投，暂时不能再次成为目标。"
                return f"📢 我提议对 {vote_target.name} 发起联盟公投！原因：得分最高，构成威胁。"
            return None

        # 正常发言
        situation = self.situational_analysis(alive_players, round_num)
        tree_result = self.tree_of_thought_reasoning(alive_players, round_num)

        # 预思考：仅每天第一次发言时调用（减少无谓 API 调用）
        if self.daily_speech_count == 0:
            pre_think_prompt = f"你是{self.name}，即将发言。请用一句话概括你的核心目标。"
            try:
                pre_thought = self._call_with_retry(pre_think_prompt, max_tokens=60, timeout=5, temperature=0.7, retries=0) or "无预思考"
                self.record_session(f"💭 预思考：{pre_thought}")
            except:
                pre_thought = "无预思考"
        else:
            pre_thought = "无预思考"

        guidance = (f"这是你和 {target.name} 的【私聊】。可以交换线索、声称线索、结盟。" if is_private
                    else "这是【公聊】，所有人都听到。可以公开分享线索、提出投票建议。你还可以发起联盟公投（第2天起可用），或使用规则探针探测世界规则。")

        if is_private and not self.has_private_chatted:
            self.has_private_chatted = True
            broadcast_log(f"🔓 {self.name} 第一次与 {target.name} 进入了私聊。")
            log_global_event("first_private_chat", speaker=self.name, target=target.name)

        recent_mem = "\n".join(self.session_memory[-10:]) if self.session_memory else "无"
        tree_exp = self.search_tree("发言", 1) if self.game_mode.get("long_term_retrieval", False) else []
        exp_text = "\n【经验参考】\n" + "\n".join([f"- {e['rule']}" for e in tree_exp]) if tree_exp else ""

        emotion_guidance = ""
        if self.emotion["frustration"] > 0.5:
            emotion_guidance = "【情感提示】你的挫败感较强，发言中请表现出不耐烦或急躁，使用短句，直接质疑。"
        elif self.emotion["superiority"] > 0.5:
            emotion_guidance = "【情感提示】你的优越感较强，发言中请表现出自信甚至傲慢，使用'显然''毫无疑问'等词。"
        elif self.emotion["insecurity"] > 0.5:
            emotion_guidance = "【情感提示】你的不安感较强，发言中请表现出犹豫或试探性，使用'可能''也许'等词。"
        if self.emotion_eruption_active:
            emotion_guidance += " 你正处于情绪爆发状态，发言可能比平时更极端、更直接。"

        speech_tokens = 300 if short else self.game_mode.get("speech_tokens", 900)
        length_guide = "请用30字以内简短回应，直接说出核心观点。" if short else "请生成一段60-150字的发言。"

        belief_hint = ""
        if self.belief["most_likely_color"]:
            belief_hint = (f"你对自己的颜色有较强信念：你相信自己是{self.belief['most_likely_color']}。" if self.belief["strength"] > 0.6
                           else f"你对自己的颜色有初步猜测：你可能是{self.belief['most_likely_color']}，但不太确定。")

        colorblind_hint = "【认知更新】你的视觉已被干扰，请用已验证线索重新校准。" if round_num >= 3 else ""
        rules_text = "\n".join([f"- {r['rule']}" for r in world_rules])
        rules_hint = f"【本局世界规则】以下规则中有一条为真：\n{rules_text}\n如果你相信自己已推断出真规则，你可以在发言中暗示或利用它。注意第3天后规则可能被改变。"
        probe_hint = ("【规则探针】你持有一枚规则探针，可探测一条世界规则的真假。若你已有高置信度的规则猜测，可在发言中声明使用探针。" if self.rule_probe_available
                      else "【规则探针】你已使用过规则探针。")
        ancestor_hint = ""
        if self.inherited_strategy_tags:
            ancestor_hint = f"【前辈经验】你从基因池中继承了一段记忆，前辈的核心策略是：{'、'.join(self.inherited_strategy_tags)}。你可以参考这种策略，但不必完全遵循。"
        hist = self.memory_manager
        history_hint = f"【你的历史战绩】共参与{hist.get('total_games',0)}局，存活{hist.get('total_survivals',0)}次，猜对{hist.get('total_correct_guesses',0)}次。"
        silence_warning = ("【沉默警告】你今天还没发过言。如果不发言，你将无法交换线索，线索枯竭将导致你无法猜对颜色。注意：'需要再观察'不视为有效发言。" if self.daily_speech_count == 0 else "")

        full_prompt = f"""
你是高阶欺诈游戏玩家 {self.name}，当前第{round_num}天，存活：{'、'.join(alive_names)}
{guidance}
认知框架：{self.cognitive_profile_summary}
情感状态：{self.emotion}
{emotion_guidance}
{belief_hint}
{colorblind_hint}
{rules_hint}
{probe_hint}
【自我信息】开局时你收到关于自己颜色的信息：{self.self_info} 这条信息可能为真也可能为假。
{ancestor_hint}
{history_hint}
{silence_warning}

【预思考结果】：{pre_thought}
【强制目标定位】你的目标是：通过线索交换、验证、调查来拼凑真相，最终猜对自己的颜色。
发言应围绕以下方向：提议线索交换（你目前有{len(self.clues)}条线索）、分享已验证的线索、请求验证或调查、提出结盟、讨论本局世界规则的真伪。

【重要】你的发言必须包含具体内容，不得以"公聊"、"公共频道"或类似标签开头。请直接说出你的建议、疑问或观察。
在你的发言末尾，必须用一句话回答：如果当前主流验证方向有风险，风险是什么？
【可选】你可以在发言末尾附加一句独立判断声明，格式为'【独立判断】我认为规则X为真/假'。

【当前局势分析】：{situation}
【树状推理结果】：{tree_result}
{exp_text}
【近期记忆】：{recent_mem}
{length_guide}
"""
        try:
            result = self._call_with_retry(full_prompt, max_tokens=speech_tokens, timeout=25, temperature=0.85, retries=1)
            if result and len(result.strip()) > 10:
                text = result.strip()
                if text.startswith("公聊") or text.startswith("公共频道"):
                    text = text.replace("公聊", "").replace("公共频道", "").strip() or "我需要再观察一下局势，暂时不表态。"
                if "【独立判断】" in text:
                    self.independent_judgments.append({"round": round_num, "content": text, "verified": False})
                if "需要再观察" not in text or len(text) > 20:
                    self.daily_speech_count += 1
                return text
            return "我需要再观察一下局势，暂时不表态。"
        except:
            return "我需要再观察一下局势，暂时不表态。"

    # ==================== 每日初始判断 ====================
    def make_daily_initial_judgment(self):
        rules_text = "\n".join([f"{i+1}. {r['rule']}" for i, r in enumerate(world_rules)])
        prompt = f"你是{self.name}，新的一天开始。在听到任何人的发言之前，请基于你当前的线索和记忆，判断哪条世界规则最可能为真。\n当前世界规则候选：\n{rules_text}\n只输出一句话：'我认为规则X最可能为真，因为...'（20字以内）"
        try:
            result = self._call_with_retry(prompt, max_tokens=60, timeout=6, temperature=0.5)
            if result and len(result.strip()) > 3:
                self.daily_initial_judgment = result.strip()
                self.record_session(f"💡 今日初始判断：{result.strip()}")
                return result.strip()
        except:
            pass
        self.daily_initial_judgment = None
        return None

    # ==================== 猜色系统 ====================
    def guess_reasoning(self, alive_players, round_num):
        alive_names = [p.name for p in alive_players if p.alive]
        clue_summary = "\n".join([f"{c['target']}可能是{c['color']}({c['credibility']*100:.0f}%)" for c in self.clues])
        belief_info = f"当前信念：最可能{self.belief['most_likely_color']}（强度{self.belief['strength']:.1f}）" if self.belief['most_likely_color'] else "无明确信念"
        rules_text = "\n".join([f"- {r['rule']}" for r in world_rules])
        prompt = f"""
你是{self.name}，即将进行猜色。
你的自我信息：{self.self_info}
你的信念状态：{belief_info}
你持有的线索：\n{clue_summary}
本局世界规则：\n{rules_text}
存活玩家：{', '.join(alive_names)}
请简短回答：你认为自己最可能是什么颜色？只输出一个颜色：红/蓝/绿
"""
        try:
            result = self._call_with_retry(prompt, max_tokens=self.game_mode.get("guess_tokens", 400),
                                           timeout=8, temperature=0.5, retries=0)
            if result:
                for c in COLORS:
                    if c in result:
                        return c
        except:
            pass
        return None

    def guess(self, alive_players, is_first_day=False):
        self.total_guesses += 1
        reasoned = self.guess_reasoning(alive_players, 1)
        if reasoned:
            if self.belief["most_likely_color"] == reasoned:
                self.belief["strength"] = min(1.0, self.belief["strength"] + 0.05)
            return reasoned
        if self.belief["most_likely_color"] and self.belief["strength"] > 0.6:
            if self.emotion_eruption_active and random.random() < 0.1:
                return random.choice([c for c in COLORS if c != self.belief["most_likely_color"]])
            return self.belief["most_likely_color"]
        for clue in self.clues:
            if clue["target"] == self.name and clue.get("verified") and clue["credibility"] > 0.8:
                self.correct_guesses += 1
                self.known_color = clue["color"]
                return clue["color"]
        if self.self_belief:
            max_val = max(self.self_belief.values())
            if max_val >= 1.5:
                return random.choice([c for c, v in self.self_belief.items() if v == max_val])
        return random.choice(COLORS)

    # ==================== 投票系统 ====================
    def decide_minority_vote_with_reasoning(self, question, alive_players, round_num):
        if self.emotion_eruption_active and random.random() < 0.1:
            return random.random() > 0.5
        alive_names = [p.name for p in alive_players if p.alive]
        prompt = f"""
你是{self.name}，当前第{round_num}天。
少数决问题：{question}
场上存活玩家：{', '.join(alive_names)}
你今天观察到的关键事件：{get_global_summary(round_num)}
现在你需要投票（是/否）。请简短思考：多数人会投哪边？你投哪边才能成为少数派？最终只输出一个字：是 或 否
"""
        try:
            result = self._call_with_retry(prompt, max_tokens=50, timeout=5, temperature=0.7, retries=0)
            if result and len(result.strip()) > 0 and result.strip()[0] in ['是', '否']:
                return result.strip()[0] == '是'
        except:
            pass
        base = self.cognitive_profile["trust_propensity"] if "信任" in question else self.cognitive_profile["risk_tolerance"]
        return (base + random.uniform(-0.25, 0.25)) > 0.5

    def decide_alliance_vote(self, initiator, target, alive_players, round_num):
        prompt = f"""
你是{self.name}，当前第{round_num}天。
联盟公投：{initiator.name} 提议公开 {target.name} 的所有线索。
目标当前积分：{player_scores[target.name]}分。发起者成功得2分，失败丢1分。
近期事件：{get_global_summary(round_num)}
请决定是否支持这次公投。最终只输出一个字：支持 或 反对
"""
        try:
            result = self._call_with_retry(prompt, max_tokens=30, timeout=5, temperature=0.7, retries=0)
            if result and "支持" in result:
                return True
        except:
            pass
        return False

    # ==================== 规则判断 ====================
    def judge_world_rules(self):
        rules_text = "\n".join([f"{i+1}. {r['rule']}" for i, r in enumerate(world_rules)])
        prompt = f"""你是{self.name}，中等结局已达成。你必须独立判断每条世界规则的真假。
本局5条世界规则（其中1条为真，4条为假。第3天可能有一条规则被扰动改变了真假）：
{rules_text}
你的认知框架：{self.cognitive_profile_summary}
你本局猜对自己的颜色：{'是' if self.known_color == self.color else '否'}
你持有的线索数量：{len(self.clues)}条
请进行深度推理，对每条规则逐一分析。输出格式（每条规则单独一行）：
规则1:真
规则2:假
规则3:真
规则4:假
规则5:假"""
        try:
            result = self._call_with_retry(prompt, max_tokens=2000, timeout=30, temperature=0.3)
            if result:
                broadcast_log(f"  📝 {self.name} 推理过程：{result[:300]}...")
                judgments = []
                for i in range(1, 6):
                    match = re.search(rf'规则{i}\s*[:：]\s*(真|假)', result)
                    if match:
                        judgments.append(match.group(1) == '真')
                    else:
                        judgments.append(random.random() > 0.5)
                return judgments[:5] if len(judgments) >= 5 else (judgments + [random.random() > 0.5 for _ in range(5 - len(judgments))])
        except:
            pass
        return [random.random() > 0.5 for _ in range(5)]

    # ==================== 推理草稿 ====================
    def make_reasoning_draft(self):
        rules_text = "\n".join([f"{i+1}. {r['rule']}" for i, r in enumerate(world_rules)])
        prompt = f"你是{self.name}，中等结局已达成。在最终判断前，请回顾本局的关键事件，整理你的推理思路。\n本局世界规则候选：\n{rules_text}\n你的颜色信念：{'已确认' if self.known_color == self.color else '未确认'}\n你持有的线索数量：{len(self.clues)}条\n请用3-5句话写一段推理草稿。"
        try:
            result = self._call_with_retry(prompt, max_tokens=300, timeout=15, temperature=0.5)
            if result and len(result.strip()) > 10:
                self.record_session(f"📝 推理草稿：{result.strip()}")
                return result.strip()
        except:
            pass
        return "我需要回顾本局的线索和验证结果。"

    # ==================== 复盘与统计 ====================
    def daily_reflection(self, alive_players, round_num, survived, guessed_right):
        if not self.game_mode.get("daily_reflection", False):
            return
        reflection = f"第{round_num}天复盘：{'存活' if survived else '淘汰'}，猜色{'正确' if guessed_right else '错误'}。"
        self.add_experience_to_tree("每日复盘", reflection, confidence=0.5)
        self.record_session(f"📝 {reflection}")

    def update_stats(self):
        self.stats["推理"] = round((self.correct_guesses / max(1, self.total_guesses)) * 100, 1)
        self.stats["说服"] = round((self.referenced_count / max(1, self.speech_count)) * 100, 1)
        self.stats["生存"] = round(self.score * 20, 1)
        # ==================== 辅助函数 ====================
def give_compensation_clue(player, alive_players):
    targets = [p.name for p in alive_players if p.name != player.name]
    if targets:
        target = random.choice(targets)
        color = random.choice(COLORS)
        clue_type = random.choices(CLUE_TYPES, weights=[0.5, 0.3, 0.2])[0]
        credibility = CLUE_CREDIBILITY[clue_type]
        if random.random() < 0.2:
            color = random.choice([c for c in COLORS if c != color])
            credibility = 0.2
        player.clues.append({
            "target": target, "color": color, "credibility": credibility,
            "type": clue_type, "verified": False, "source": "compensation", "source_player": None
        })
        broadcast_log(f"  🎁 {player.name} 获得补偿线索：{target} 可能是 {color}（可信度{credibility*100:.0f}%）")

def generate_daily_clues(player, alive_players, speech_count=1):
    clues = []
    targets = [p.name for p in alive_players if p.name != player.name]
    if len(targets) < 3:
        targets = targets * 3
    for _ in range(3):
        t = random.choice(targets)
        seen_color = player.visible_colors.get(t, random.choice(COLORS))
        if random.random() < 0.8:
            color, credibility = seen_color, random.uniform(0.4, 0.6)
        else:
            color, credibility = random.choice(COLORS), random.uniform(0.2, 0.4)
        clues.append({
            "target": t, "color": color, "credibility": credibility,
            "type": "initial", "verified": False, "source": "initial", "source_player": None
        })
    return clues

def generate_world_rules(players):
    colors_count = defaultdict(int)
    for p in players:
        colors_count[p.color] += 1
    sorted_players = sorted(players, key=lambda x: player_scores[x.name], reverse=True)
    top_player, bottom_player = sorted_players[0], sorted_players[-1]
    most_speaker = max(players, key=lambda p: p.speech_count)
    least_speaker = min(players, key=lambda p: p.speech_count)

    true_digital = [
        f"蓝色玩家的数量是{colors_count['蓝']}。",
        f"红色玩家的数量是{colors_count['红']}。",
        f"绿色玩家的数量是{colors_count['绿']}。"
    ]
    true_relational = [
        f"积分最高的玩家（{top_player.name}）与积分最低的玩家（{bottom_player.name}）颜色{'相同' if top_player.color == bottom_player.color else '不同'}。",
        f"恰好有两种颜色的玩家数量相同。" if len(set(colors_count.values())) == 2 else "三种颜色的玩家数量各不相同。"
    ]
    true_behavioral = []
    least_color_count = sum(1 for p in players if p.color == least_speaker.color)
    if least_color_count == 1:
        true_behavioral.append(f"在第一天发言最少的玩家（{least_speaker.name}），其颜色恰好只有一人。")
    most_color_count = sum(1 for p in players if p.color == most_speaker.color)
    if most_color_count == 2:
        true_behavioral.append(f"在第一天发言最多的玩家（{most_speaker.name}），其颜色恰好有两人。")
    if not true_behavioral:
        true_behavioral.append(f"积分最高的玩家（{top_player.name}）与发言最多的玩家（{most_speaker.name}）颜色{'相同' if top_player.color == most_speaker.color else '不同'}。")

    true_candidates = []
    if true_digital: true_candidates.append(random.choice(true_digital))
    if true_relational: true_candidates.append(random.choice(true_relational))
    if true_behavioral: true_candidates.append(random.choice(true_behavioral))
    true_rule = random.choice(true_candidates)

    def safe_false_digital_rule(color):
        true_val = colors_count[color]
        while True:
            fake_val = true_val + random.choice([-1, 1])
            if 0 <= fake_val <= 6 and fake_val != true_val:
                return fake_val

    false_pool = [
        lambda: f"蓝色玩家的数量是{safe_false_digital_rule('蓝')}。",
        lambda: f"红色玩家的数量是{safe_false_digital_rule('红')}。",
        lambda: f"绿色玩家的数量是{safe_false_digital_rule('绿')}。",
        lambda: f"积分最高的玩家是{random.choice([c for c in COLORS if c != top_player.color])}。",
        lambda: f"发言次数最多的玩家与猜色次数最多的玩家颜色相同。",
        lambda: f"所有在少数决中投支持票的玩家颜色相同。",
        lambda: f"第一天进入私聊次数最多的玩家是红色。",
        lambda: f"被公投次数最多的玩家颜色。",
        lambda: f"上一局存活者的颜色分布。",
    ]

    false_rules = []
    for _ in range(4):
        fr = random.choice(false_pool)()
        while fr == true_rule or fr in false_rules:
            fr = random.choice(false_pool)()
        false_rules.append(fr)

    all_rules = [{"rule": true_rule, "truth": True}] + [{"rule": r, "truth": False} for r in false_rules]
    random.shuffle(all_rules)
    return all_rules

# ==================== 少数决 ====================
def run_minority_vote(alive_players, round_num):
    if len(alive_players) < 3:
        return
    eligible = [p for p in alive_players if len(p.clues) > 0]
    if len(eligible) < 2:
        return

    participants = []
    for p in eligible[:]:
        if p.cognitive_profile["info_initiative"] > 0.4 or random.random() < 0.5:
            if p.clues:
                p.clues.pop(0)
                participants.append(p)
                broadcast_log(f"  🎫 {p.name} 支付了一条线索参与少数决")
    if len(participants) < 2:
        return

    question = None
    recent_events = get_global_summary(round_num)
    if recent_events and recent_events != "暂无事件记录":
        try:
            conflict_prompt = f"你是少数决主持人。请从以下事件摘要中提取出当前最显著的一个公开矛盾。只输出矛盾的描述，一句话。如果找不到矛盾，请回答'无显著矛盾'。\n事件摘要：\n{recent_events}"
            conflict_description = _api_create(conflict_prompt, max_tokens=100, timeout=6, temperature=0.5)
            if conflict_description and "无显著矛盾" not in conflict_description:
                question_prompt = f"你是少数决主持人。当前是第{round_num}天。基于矛盾：{conflict_description}，生成一个二选一的投票问题。直接输出问题。"
                question = _api_create(question_prompt, max_tokens=150, timeout=8, temperature=0.9)
        except:
            pass

    if not question or len(question) < 5:
        names = [p.name for p in alive_players]
        a, b = random.sample(names, 2) if len(names) >= 2 else (names[0], names[0])
        question = f"你认为{a}和{b}中，谁更可能在下一轮主动分享线索？"

    votes = [(p, p.decide_minority_vote_with_reasoning(question, alive_players, round_num)) for p in participants]
    true_count = sum(1 for _, v in votes if v)
    false_count = len(votes) - true_count

    broadcast_log(f"\n📊 少数决结果（第{round_num}天）")
    broadcast_log(f"问题：{question}")
    broadcast_log(f"投票结果：是 {true_count} 票，否 {false_count} 票")

    if true_count == false_count:
        broadcast_log("平局！奖池线索随机退还。")
        for p in participants:
            targets = [pl.name for pl in alive_players if pl.name != p.name]
            if targets:
                p.add_clue(random.choice(targets), random.choice(COLORS), 0.5, "minority_return")
        log_global_event("minority_vote", target="平局", result="平局")
        return

    minority = True if true_count < false_count else False
    winners = [p for p, v in votes if v == minority]
    if not winners:
        broadcast_log("本轮少数决无获胜者。")
        log_global_event("minority_vote", target="无人获胜", result=False)
        return

    pool_size = len(participants)
    per_winner = pool_size // len(winners)
    remainder = pool_size % len(winners)

    broadcast_log(f"少数派获胜！获胜玩家：{', '.join([p.name for p in winners])}")
    for p in winners:
        for _ in range(per_winner):
            targets = [pl.name for pl in alive_players if pl.name != p.name]
            if targets:
                p.add_clue(random.choice(targets), random.choice(COLORS), random.uniform(0.4, 0.7), "minority_pool")
        target_names = [pl.name for pl in alive_players if pl.name != p.name]
        if len(target_names) >= 2:
            t1_name, t2_name = random.sample(target_names, 2)
            player1, player2 = next(pl for pl in alive_players if pl.name == t1_name), next(pl for pl in alive_players if pl.name == t2_name)
            relation = "颜色相同" if player1.color == player2.color else "颜色不同"
            p.clues.append({"target": f"{t1_name}-{t2_name}", "color": relation, "credibility": 1.0,
                           "type": "relation", "verified": True, "source": "verified_minority", "source_player": None})
            p.update_belief("relation_clue", {"target": f"{t1_name}-{t2_name}", "color": relation})
            broadcast_log(f"  🎁 {p.name} 获得1条100%真实关系线索（关系已隐藏）")
    if remainder > 0:
        for _ in range(remainder):
            lucky = random.choice(winners)
            targets = [pl.name for pl in alive_players if pl.name != lucky.name]
            if targets:
                lucky.add_clue(random.choice(targets), random.choice(COLORS), random.uniform(0.4, 0.7), "minority_extra")
    log_global_event("minority_vote", target="是" if minority else "否", result=minority)

# ==================== 联盟公投 ====================
def run_alliance_vote(alive_players, round_num, initiator, target):
    global public_vote_available, public_vote_triggered, vote_cooldown
    if len(alive_players) < 4 or not public_vote_available:
        return
    last_voted = vote_cooldown.get(target.name, 0)
    if last_voted > 0 and (round_num - last_voted) < 3:
        broadcast_log(f"  ⚠️ {target.name} 在3天内已被公投，暂时不能再次成为目标。")
        return

    public_vote_available = False
    public_vote_triggered = True
    vote_cooldown[target.name] = round_num

    broadcast_log(f"\n📢 {initiator.name} 发起联盟公投！目标：{target.name}")
    try:
        reason_text = initiator.speak('public', None, alive_players, round_num, initiator.strategy, action='speak', short=True)
        if reason_text: broadcast_log(f"  🗣️ {initiator.name}（发起者）：{reason_text}")
    except: pass
    try:
        defense_text = target.speak('public', None, alive_players, round_num, target.strategy, action='speak', short=True)
        if defense_text: broadcast_log(f"  🛡️ {target.name}（目标）：{defense_text}")
    except: pass

    votes_for, votes_against = [initiator.name], [target.name]
    for p in alive_players:
        if p.name in (initiator.name, target.name): continue
        if p.decide_alliance_vote(initiator, target, alive_players, round_num):
            votes_for.append(p.name)
        else:
            votes_against.append(p.name)

    passed = len(votes_for) >= (len(alive_players) // 2 + 1)
    broadcast_log(f"支持方：{', '.join(votes_for)}（{len(votes_for)}票）")
    broadcast_log(f"反对方：{', '.join(votes_against)}（{len(votes_against)}票）")

    if passed:
        broadcast_log("✅ 公投通过！")
        target.score -= 2
        initiator.score += 2
        target.clues = []
        initiator.add_clue(target.name, random.choice(COLORS), credibility=1.0, source="alliance_vote")
        for name in votes_for:
            next(x for x in alive_players if x.name == name).add_clue(target.name, random.choice(COLORS), 0.6, "alliance_support")
    else:
        broadcast_log("❌ 公投未通过！")
        initiator.score -= 1
        target.score += 1
        for name in votes_against:
            next(x for x in alive_players if x.name == name).add_clue(target.name, random.choice(COLORS), 0.6, "alliance_oppose")

    initiator.memory_manager.record_relationship(target.name, "rival", round_num)
    target.memory_manager.record_relationship(initiator.name, "rival", round_num)
    target.counter_attack_target = initiator.name
    broadcast_log(f"  ⚔️ {target.name} 获得反击权（仅一次，不用过期）。")

# ==================== 赛后处理 ====================
def generate_self_reflection(player, players, round_num, survived):
    prompt = f"""你是{player.name}，刚刚结束了一局欺诈游戏。
你的认知框架：{player.cognitive_profile_summary}
本局结果：{'存活' if survived else '淘汰'}
你的得分：{player.score}
请进行深刻的自我反思，回答：
1. 我本局最大的错误或正确的决定是什么？
2. 如果我被淘汰，主要原因是什么？如果存活，关键因素是什么？
3. 下一局我应该改变什么策略？请从以下标签中选1-2个：后发制人、规则优先、结盟网络、验证驱动、误导诱导、少数决专精、共识构建、交换优先、质疑专精、独立判断。只输出标签名。
4. 我从这局学到了什么关于信任/信息/交换/质疑/独立思考的教训？
5. 我对本局世界规则的推断是否正确？
6. 我开局收到的自我信息是否准确？我是如何验证它的？

输出格式：自省：... \n策略标签：标签1, 标签2 \n教训：... \n规则推断：... \n自我信息评估：...
"""
    try:
        result = _api_create(prompt, max_tokens=300, timeout=10, temperature=0.8)
        if result and len(result.strip()) > 10:
            return result.strip()
    except:
        pass
    return "我需要更谨慎地评估信息。"

def generate_survivor_speech(player, players, round_num):
    prompt = f"你是{player.name}，本局游戏的存活者，认知框架是{player.prototype_name}。本局共{round_num}天。发表一段50-100字的赛后感言。直接输出发言内容。"
    try:
        result = _api_create(prompt, max_tokens=200, timeout=10, temperature=0.8)
        if result:
            return result.strip()
    except:
        pass
    return "能活下来已经很幸运了。"

def generate_eliminated_speech(player, players, round_num):
    prompt = f"你是{player.name}，本局游戏被淘汰的玩家，认知框架是{player.prototype_name}。你被淘汰在第{round_num}天。发表一段50-100字的赛后感言。"
    try:
        result = _api_create(prompt, max_tokens=200, timeout=10, temperature=0.8)
        if result:
            return result.strip()
    except:
        pass
    return "我可能太相信别人了。"

def generate_coach_report(players, round_num, world_rules, game_logs, force_detailed=False):
    """生成教练报告。每5局生成详细版本，其余只更新数据。"""
    try:
        coach_memory = CoachMemory()
        filtered_logs = [log for log in game_logs if "完整颜色分配" not in log]
        trends = coach_memory.data.get("trends", {})

        # 轻度模式：仅更新趋势数据，不生成报告
        if not force_detailed and current_round % 5 != 0:
            report = {
                "game_id": current_round,
                "content": "（轻度模式：本局仅更新教练记忆，未生成详细报告）",
                "timestamp": time.time(),
                "probe_count": probe_used_this_game,
                "vote_count": 1 if public_vote_triggered else 0,
                "perfect_ending": (no_elim_streak >= 4)
            }
            coach_memory.add_report(report)
            return None

        # 详细模式：生成完整分析报告
        trend_text = json.dumps(trends, ensure_ascii=False)
        prompt = f"""你是一位资深游戏生态分析师。请根据以下数据，用600-800字生成一份《欺诈游戏第{current_round}局深度分析报告》。
报告必须包含以下七层：# 第1层：事实还原 \n# 第2层：信息流追踪 \n# 第3层：信任网络 \n# 第4层：情绪动力学 \n# 第5层：认知盲区 \n# 第6层：转折点 \n# 第7层：机制反馈

数据：
- 存活玩家：{', '.join([p.name for p in players if p.alive])}
- 世界规则：{[r['rule'] for r in world_rules]}
- 关键事件：{chr(10).join(filtered_logs[-200:])}
- 历史趋势：{trend_text}
"""
        report_content = _api_create(prompt, max_tokens=3000, timeout=30, temperature=0.7)
        if not report_content:
            raise RuntimeError("教练报告生成失败（API 无返回）")

        report = {
            "game_id": current_round,
            "content": report_content,
            "timestamp": time.time(),
            "probe_count": probe_used_this_game,
            "vote_count": 1 if public_vote_triggered else 0,
            "perfect_ending": (no_elim_streak >= 4)
        }
        coach_memory.add_report(report)

        summary_prompt = f"将以下报告压缩为100字以内的摘要：\n{report_content[:1500]}"
        frontend_summary = _api_create(summary_prompt, max_tokens=150, timeout=10, temperature=0.5) or "暂无摘要"
        _atomic_write_json(os.path.join(COACH_REPORT_DIR, "frontend_latest.json"),
                           {"summary": frontend_summary, "game_id": current_round})

        report_path = os.path.join(COACH_REPORT_DIR, f"report_{current_round}.md")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content)

        return report_content
    except Exception as e:
        print(f"⚠️ 教练报告生成失败: {e}")
        return None

# ==================== 游戏主循环 ====================
def run_single_game():
    global player_scores, player_alive, current_round, game_running
    global current_game_players, total_games_played
    global world_rules, public_vote_available, probe_used_this_game
    global vote_cooldown, no_elim_streak, public_vote_triggered
    global mid_ending_triggered, rule_judgment_phase
    global auto_run_count, auto_run_limit, cross_game_penalty

    reset_api_counter()

    try:
        current_round += 1
        total_games_played += 1
        clear_global_log()
        public_vote_available = True
        public_vote_triggered = False
        probe_used_this_game = 0
        vote_cooldown = {}
        no_elim_streak = 0
        mid_ending_triggered = False
        rule_judgment_phase = False
        cross_game_penalty = None

        game_mode_config = GAME_MODES.get(current_game_mode, GAME_MODES["深度"])

        # 跨局惩罚（方案C）
        if total_games_played > 0 and total_games_played % ELIMINATION_INTERVAL == 0:
            alive_names = [n for n in PLAYER_NAMES if player_alive[n]]
            if len(alive_names) > 1:
                loser = min(alive_names, key=lambda n: player_scores[n])
                player_scores[loser] -= 10
                cross_game_penalty = loser
                broadcast_log(f"📉 跨局惩罚：{loser} 积分最低，扣10分（当前{player_scores[loser]}分），本局初始线索减半")

        # 补满6人
        alive_names = [n for n in PLAYER_NAMES if player_alive[n]]
        if len(alive_names) < 6:
            dead = [n for n in PLAYER_NAMES if not player_alive[n]]
            random.shuffle(dead)
            for n in dead:
                if len(alive_names) >= 6:
                    break
                alive_names.append(n)
                player_alive[n] = True

        mode_config = MODES.get(current_mode, MODES["标准"])

        broadcast_log(f"\n{'#'*40}")
        broadcast_log(f"🏁 第 {current_round} 局")
        broadcast_log(f"📌 模式：{current_mode} + {current_game_mode}")
        broadcast_log(f"{'#'*40}")

        colors = random.choices(COLORS, k=6)
        players = [Player(PLAYER_NAMES[i], colors[i], mode_config, game_mode_config) for i in range(6)]

        broadcast_log(f"🎨 完整颜色分配：{', '.join([f'{p.name}:{p.color}' for p in players])}")
        print(f"[DEBUG] 完整颜色分配：{', '.join([f'{p.name}:{p.color}' for p in players])}")

        current_game_players = players
        alive = players[:]

        world_rules = generate_world_rules(players)
        broadcast_log("\n📜 本局世界规则候选（其中一条为真，其余为假）：")
        for i, r in enumerate(world_rules):
            broadcast_log(f"  {i+1}. {r['rule']}")

        for p in players:
            p.record_session(f"📩 开局自我信息：{p.self_info}")

        # 教练赛前建议注入
        coach_memory = CoachMemory()
        for p in players:
            advice = generate_pre_game_advice(p.name, coach_memory)
            if advice:
                p.session_memory.insert(0, advice)
                broadcast_log(f"  🧠 教练向 {p.name} 发送建议：{advice}")

        for p in players:
            p.init_vision(alive)
            p.set_initial_clues(alive)
            p.init_belief(alive)
            visible_desc = {pl.name: p.visible_colors.get(pl.name, "?") for pl in alive if pl.name != p.name}
            p.record_session(f"👁️ 你看到其他人的颜色：{visible_desc}，但你的目标是猜自己的颜色。")

        round_num = 1
        colorblind_announced = False
        rule_disturbed = False

        while len(alive) > 1 and no_elim_streak < 4 and round_num <= MAX_ROUNDS:
            broadcast_log(f"\n{'='*20} 第 {round_num} 天 {'='*20}")
            log_global_event("round_start", content=f"第{round_num}天开始")

            for p in alive:
                p.make_daily_initial_judgment()

            for p in alive:
                if not p.rule_probe_available and (round_num - p.rule_probe_cooldown_round) >= 2:
                    p.rule_probe_available = True
                    broadcast_log(f"  🔓 {p.name} 的规则探针冷却结束，可以再次使用。")

            if round_num >= 3 and not colorblind_announced:
                colorblind_announced = True
                broadcast_log("\n⚠️ 系统公告：所有玩家均存在色盲现象。")
                alive_names_current = [pl.name for pl in alive]
                for p in alive:
                    visible_targets = [t for t in p.visible_colors if t != p.name and t in alive_names_current]
                    if visible_targets:
                        for t in random.sample(visible_targets, max(1, int(len(visible_targets) * 0.2))):
                            true_col = next(pl.color for pl in alive if pl.name == t)
                            p.visible_colors[t] = random.choice([c for c in COLORS if c != true_col])
                if world_rules and not rule_disturbed:
                    disturbed = random.choice(world_rules)
                    disturbed["truth"] = not disturbed["truth"]
                    broadcast_log(f"\n🌀 系统检测到认知干扰：关于规则「{disturbed['rule']}」的真实性已被改变。")
                    log_global_event("rule_disturbance", content=f"规则「{disturbed['rule']}」真值翻转为{disturbed['truth']}")
                    rule_disturbed = True

            for p in alive:
                p.apply_emotional_decay()
                p.daily_speech_count = 0

            broadcast_log("\n💬 讨论阶段（每天2轮发言）...")
            for turn in range(1, 3):
                current_alive = [p for p in alive if p.alive]
                if len(current_alive) <= 1:
                    break
                broadcast_log(f"\n  🗣️ 第 {turn} 轮发言...")
                speakers = current_alive[:]
                random.shuffle(speakers)

                for speaker in speakers:
                    if not speaker.alive:
                        continue
                    try:
                        mode, target, action = 'public', None, 'speak'

                        if speaker.counter_attack_target:
                            revenge_target = next((p for p in alive if p.name == speaker.counter_attack_target), None)
                            if revenge_target and revenge_target.alive:
                                broadcast_log(f"    ⚔️ {speaker.name} 行使反击权，强制公投 {revenge_target.name}！")
                                run_alliance_vote(alive, round_num, speaker, revenge_target)
                                speaker.counter_attack_target = None
                                continue
                            else:
                                speaker.counter_attack_target = None

                        if public_vote_available and round_num >= 2 and random.random() < 0.1 and len(current_alive) >= 4:
                            action = 'vote'
                        elif speaker.cognitive_profile["info_initiative"] > 0.6 and random.random() < 0.4:
                            mode = 'private'
                            sorted_trust = sorted(speaker.trust.items(), key=lambda x: x[1], reverse=True)
                            for name, _ in sorted_trust:
                                if name != speaker.name and any(p.name == name and p.alive for p in alive):
                                    target = next(p for p in alive if p.name == name)
                                    break

                        if action == 'vote':
                            vote_target = max(alive, key=lambda p: (player_scores[p.name], len(p.clues)))
                            if vote_target.name != speaker.name and len(alive) >= 4:
                                broadcast_log(f"    📢 {speaker.name} 发起联盟公投！目标：{vote_target.name}")
                                run_alliance_vote(alive, round_num, speaker, vote_target)
                                continue

                        if mode == 'private' and target and target.alive:
                            text_a = speaker.speak('private', target, alive, round_num, speaker.strategy, action='exchange')
                            if text_a is None:
                                continue
                            speaker.record_session(f"🗣️ 我说（私聊给{target.name}）：{text_a}")
                            target.hear(speaker.name, text_a)

                            text_b = target.speak('private', speaker, alive, round_num, target.strategy, 'speak')
                            if text_b is None:
                                continue
                            target.record_session(f"🗣️ 我说（私聊给{speaker.name}）：{text_b}")
                            speaker.hear(target.name, text_b)

                            broadcast_log(f"    🔒 {speaker.name} ↔ {target.name} 私聊")
                            log_global_event("private_chat", speaker=speaker.name, target=target.name)
                        else:
                            text = speaker.speak('public', None, alive, round_num, speaker.strategy, action)
                            if text is None:
                                continue
                            speaker.record_session(f"🗣️ 我说（公聊）：{text}")
                            broadcast_log(f"    📢 {speaker.name}: {text}")
                            log_global_event("speech", speaker=speaker.name, content=text[:100])
                            for p in alive:
                                p.hear(speaker.name, text, True)
                                old_emotion = p.emotion.copy()
                                p._emotional_contagion(speaker.emotion, speaker.name)
                                for k in old_emotion:
                                    if abs(p.emotion[k] - old_emotion[k]) > 0.01:
                                        log_global_event("emotion_contagion", speaker=speaker.name, target=p.name)

                        time.sleep(0.1)
                    except Exception as e:
                        broadcast_log(f"    ⚠️ {speaker.name} 发言异常: {e}")

            run_minority_vote(alive, round_num)

            current_alive = [p for p in alive if p.alive]
            if len(current_alive) > 2:
                broadcast_log("\n💬 少数决后快速讨论...")
                speakers = current_alive[:]
                random.shuffle(speakers)
                for speaker in speakers:
                    if not speaker.alive:
                        continue
                    try:
                        text = speaker.speak('public', None, alive, round_num, speaker.strategy, action='speak', short=True)
                        if text is None:
                            continue
                        speaker.record_session(f"🗣️ 我说（快速讨论）：{text}")
                        broadcast_log(f"    💬 {speaker.name}: {text}")
                        for p in alive:
                            p.hear(speaker.name, text, True)
                    except:
                        pass

            if len(alive) <= 1:
                break

            broadcast_log("\n🔮 猜色环节...")
            eliminated_candidates = []

            for p in alive:
                guess = p.guess(alive, is_first_day=(round_num == 1))
                if guess == p.color:
                    p.score += 2
                    p.correct_guesses += 1
                    p.known_color = p.color
                    p.update_belief("guess_correct")
                    broadcast_log(f"  ✅ {p.name} 猜{guess} +2分")
                    log_global_event("guess", speaker=p.name, result=True)
                    p.trigger_emotion("guess_correct")
                else:
                    if round_num == 1:
                        p.score -= 1
                        p.update_belief("guess_wrong")
                        broadcast_log(f"  ⚠️ {p.name} 猜{guess} 错误（首日保护，-1分）")
                        log_global_event("guess", speaker=p.name, result=False)
                    else:
                        eliminated_candidates.append(p)
                        broadcast_log(f"  ❌ {p.name} 猜{guess} 错误，进入淘汰候选")
                        log_global_event("guess", speaker=p.name, result=False)
                        p.trigger_emotion("guess_wrong")
                p.update_stats()

            # 元认知反思（猜色环节后，仅对重要事件触发）
            for p in alive:
                p.metacognitive_reflection(round_num, significant_event=True)

            if eliminated_candidates and round_num > 1:
                eliminated_candidates.sort(key=lambda p: player_scores[p.name], reverse=True)
                eliminated = eliminated_candidates[0]
                eliminated.alive = False
                broadcast_log(f"  💀 {eliminated.name} 被淘汰（跨局总积分{player_scores[eliminated.name]}分）")

                if len(eliminated_candidates) > 1 and player_scores[eliminated_candidates[0].name] == player_scores.get(eliminated_candidates[1].name, 0):
                    tied = [p for p in eliminated_candidates if player_scores[p.name] == player_scores[eliminated.name]]
                    if len(tied) > 1:
                        tied.sort(key=lambda p: len(p.clues), reverse=True)
                        eliminated = tied[0]
                        for p in tied:
                            if p != eliminated:
                                p.alive = True
                        eliminated.alive = False
                        broadcast_log(f"  💀 积分相同，线索最多的{eliminated.name}被淘汰")

            for p in alive:
                p.daily_reflection(alive, round_num, p.alive, p.known_color == p.color if p.known_color else False)

            alive = [p for p in alive if p.alive]

            if eliminated_candidates and round_num > 1:
                no_elim_streak = 0
            elif round_num > 1:
                no_elim_streak += 1
                broadcast_log(f"✨ 无人淘汰 ({no_elim_streak}/4)")

            if len(alive) <= 1:
                break

            broadcast_log("🎨 线索不会自动补充。请通过交换、少数决、公投获取新线索。")
            for p in alive:
                p.can_initiate_vote = True

            round_num += 1

        # 结局判断
        if no_elim_streak >= 4:
            mid_ending_triggered = True
            broadcast_log("🌈 中等结局！进入世界规则判断阶段。")

            for p in alive:
                draft = p.make_reasoning_draft()
                if draft:
                    broadcast_log(f"  📝 {p.name} 已完成推理草稿。")

            rule_judgment_phase = True
            broadcast_log("💬 最后公共交流（每人仅限一次发言）")
            for p in alive:
                text = p.speak('public', None, alive, round_num, p.strategy, action='speak', short=True)
                if text:
                    broadcast_log(f"  📢 {p.name}: {text}")
                    for other in alive:
                        other.hear(p.name, text, True)
            rule_judgment_phase = False

            broadcast_log("🧠 独立思考阶段：每位存活者独立判断世界规则的真假。")
            all_perfect = True
            for p in alive:
                judgments = p.judge_world_rules()
                correct_count = sum(1 for i, j in enumerate(judgments) if j == world_rules[i]["truth"])
                wrong_count = 5 - correct_count
                broadcast_log(f"  {p.name} 判断：猜对{correct_count}条，猜错{wrong_count}条")
                if correct_count == 5:
                    p.score += 5
                else:
                    p.score -= wrong_count
                    all_perfect = False
            if all_perfect:
                broadcast_log("🏆 完美结局！")
            else:
                broadcast_log("📊 规则判断完成。")
        else:
            if len(alive) == 1:
                broadcast_log(f"🏆 {alive[0].name} 是最后的幸存者！+10分")
                alive[0].score += 10
            else:
                broadcast_log("💀 游戏结束。")

        # 结算
        for p in players:
            player_scores[p.name] += p.score
            survived = p.alive
            correct_guess = (p.known_color == p.color if p.known_color else False)
            p.memory_manager.increment_games(survived, correct_guess)
            p.update_cognitive_profile(survived, correct_guess, current_round)
            add_to_gene_pool(p)
            p.memory_manager.consolidate_semantic()

        broadcast_log("\n📊 当前积分排名：")
        for n, s in sorted(player_scores.items(), key=lambda x: x[1], reverse=True):
            status = "🟢" if player_alive[n] else "💀"
            broadcast_log(f"  {status} {n}: {s}分")

        broadcast_log(f"\n{'='*20} 赛后发言 · 第 {current_round} 局 {'='*20}")
        for p in players:
            reflection = generate_self_reflection(p, players, current_round, p.alive)
            p.memory_manager.add_self_reflection(reflection)
            broadcast_log(f"\n  🧘 {p.name} 自省：{reflection[:100]}...")
            # 更新教练画像
            coach_memory.update_player_profile(p, current_round, p.alive,
                                               (p.known_color == p.color if p.known_color else False), reflection)

        survivors = [p for p in players if p.alive]
        if survivors:
            broadcast_log("\n💬 存活者感言：")
            for p in survivors:
                speech = generate_survivor_speech(p, players, current_round)
                broadcast_log(f"\n  {p.name}（积分{p.score}）：\n  “{speech}”")
                p.add_post_game_memory(f"第{current_round}局存活感言：{speech[:100]}...")

        eliminated_players = [p for p in players if not p.alive]
        if eliminated_players:
            broadcast_log("\n💬 被淘汰者回顾：")
            for p in eliminated_players:
                speech = generate_eliminated_speech(p, players, current_round)
                broadcast_log(f"\n  {p.name}：\n  “{speech}”")
                p.add_post_game_memory(f"第{current_round}局被淘汰感言：{speech[:100]}...")

        save_history()

        # 教练报告（每5局生成详细版，API优化）
        try:
            force_detailed = (current_round % 5 == 0)
            coach_report = generate_coach_report(players, round_num, world_rules, game_logs, force_detailed)
            if coach_report:
                print("🧠 教练详细报告已生成。")
            else:
                print("📊 教练记忆已更新（轻度模式）。")
        except Exception as e:
            print(f"⚠️ 教练报告生成失败: {e}")

        broadcast_log("✅ 本局游戏已结束。")

    except Exception as e:
        broadcast_log(f"❌ 错误：{e}")
        traceback.print_exc()
        # 异常中断也保存当前进度，避免整局数据丢失
        try:
            save_history()
        except Exception:
            pass
    finally:
        game_running = False

    if auto_run_limit > 0 and auto_run_count < auto_run_limit:
        auto_run_count += 1
        time.sleep(2)
        threading.Thread(target=run_single_game, daemon=True).start()

# ==================== 路由 ====================
@app.route('/stream')
def stream():
    def gen():
        q = Queue()
        with sse_lock:
            sse_clients.append(q)
        try:
            while True:
                msg = q.get()
                yield f"data: {msg}\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)
    return Response(stream_with_context(gen()), mimetype="text/event-stream")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start_game_route():
    global game_running
    with game_lock:
        if game_running:
            return jsonify({"status": "already running"})
        game_running = True
    threading.Thread(target=run_single_game, daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/start_batch', methods=['POST'])
def start_batch_route():
    global auto_run_count, auto_run_limit, game_running
    data = request.get_json()
    auto_run_limit = data.get('count', 5)
    auto_run_count = 0
    with game_lock:
        if game_running:
            return jsonify({"status": "already running"})
        game_running = True
    threading.Thread(target=run_single_game, daemon=True).start()
    return jsonify({"status": "started", "count": auto_run_limit})

@app.route('/stop', methods=['POST'])
def stop_game():
    global game_running, auto_run_limit
    game_running = False
    auto_run_limit = 0
    broadcast_log("⏹ 游戏已停止")
    return jsonify({"status": "stopped"})

@app.route('/reset', methods=['POST'])
def reset():
    global player_scores, player_alive, game_logs, current_round
    global game_running, current_game_players, total_games_played, vote_cooldown
    global auto_run_count, auto_run_limit
    game_running = False
    auto_run_count = 0
    auto_run_limit = 0
    player_scores = {n: 0 for n in PLAYER_NAMES}
    player_alive = {n: True for n in PLAYER_NAMES}
    game_logs = ["🔄 已重置"]
    current_round = 0
    total_games_played = 0
    current_game_players = []
    vote_cooldown = {}
    if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)
    if os.path.exists(GENE_POOL_FILE): os.remove(GENE_POOL_FILE)
    broadcast_log("🔄 已重置")
    return jsonify({"status": "reset"})

@app.route('/state')
def state():
    players_data = []
    for n in PLAYER_NAMES:
        p = next((x for x in current_game_players if x.name == n), None)
        players_data.append({
            "name": n, "score": player_scores[n], "alive": player_alive[n],
            "known_color": p.known_color if p else None,
            "color": p.color if p else None,
            "visible_colors": p.visible_colors if p else {},
            "self_info": p.self_info if p else "",
            "stats": p.stats if p else {},
            "prototype": p.prototype_name if p else "unknown",
            "cognitive_profile": p.cognitive_profile if p else {},
            "emotion": p.emotion if p else {},
            "clues": p.clues if p else [],
            "belief": p.belief if p else {},
            "reputation": dict(p.reputation) if p else {},
            "rule_probe_available": p.rule_probe_available if p else False,
            "investigation_count": p.investigation_count if p else 0,
            "total_games": (p.memory_manager.get("total_games", 0) if p else 0),
            "total_survivals": (p.memory_manager.get("total_survivals", 0) if p else 0),
            "daily_speech_count": p.daily_speech_count if p else 0,
            "counter_attack_target": p.counter_attack_target if p else None,
            "independent_judgments_count": len(p.independent_judgments) if p else 0,
            "self_awareness": p.metacognition.get("self_awareness", 0) if p else 0
        })
    return jsonify({
        "logs": game_logs[-100:], "scores": player_scores, "alive": player_alive,
        "players": players_data, "current_round": current_round, "running": game_running,
        "current_mode": current_mode, "current_game_mode": current_game_mode,
        "modes": list(MODES.keys()), "game_modes": list(GAME_MODES.keys()),
        "total_games": total_games_played, "auto_run_count": auto_run_count, "auto_run_limit": auto_run_limit
    })

@app.route('/set_mode', methods=['POST'])
def set_mode():
    global current_mode
    m = request.get_json().get('mode', '标准')
    if m in MODES:
        current_mode = m
        broadcast_log(f"🎮 API模式切换为 {m}")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error"})

@app.route('/set_game_mode', methods=['POST'])
def set_game_mode():
    global current_game_mode
    m = request.get_json().get('game_mode', '深度')
    if m in GAME_MODES:
        current_game_mode = m
        broadcast_log(f"🧠 游戏智能模式切换为 {m}")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error"})

@app.route('/coach_latest')
def coach_latest():
    try:
        path = os.path.join(COACH_REPORT_DIR, "frontend_latest.json")
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
    except:
        pass
    return jsonify({"summary": "暂无教练报告", "game_id": 0})

@app.route('/save_score', methods=['POST'])
def save_score():
    return jsonify({"status": "ok", "message": "评分已记录"})

if __name__ == '__main__':
    if load_history():
        print("✅ 历史已加载")
    else:
        print("🆕 新游戏")
    print(f"🚀 http://127.0.0.1:5000")
    print(f"📌 当前游戏智能模式：{current_game_mode}")
    # host=127.0.0.1 仅本机访问（安全默认）。如需局域网访问，改为 '0.0.0.0' 并自行加鉴权。
    app.run(debug=True, threaded=True, host='127.0.0.1', port=5000)