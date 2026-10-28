# -*- coding: utf-8 -*-
"""
群智 V15 · FastAPI 统一网关
===========================
外层统一入口：集成 v14 讨论引擎（SandboxSession/Coach/ExperimentMemory）
+ V15 新增的主教练联网搜索 + 预设教研课题库 + 场景化决策报告。

设计：
  - 复用 sandbox/app.py 的类（importlib 加载，避免重复实现引擎）
  - 新增 /api/coach/search（主教练联网）、/api/sandbox/start-v15（带信息注入）
  - 所有路由统一 /api/* 前缀
  - v15 数据独立（data/ 目录，与 v14 隔离）

启动：python app/main_gateway.py（默认 5020 端口）
"""
import sys, os, json, re, threading, time, importlib.util, uuid
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

# v15 根目录加入 path（便于 import 共享模块）
V15_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V15_ROOT)
sys.path.insert(0, os.path.join(V15_ROOT, "sandbox"))

# UTF-8 输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 静态资源（logo 等）
_static_dir = os.path.join(V15_ROOT, "app", "static")
os.makedirs(_static_dir, exist_ok=True)

# ---------- 加载 v14/v15 讨论引擎（复用类，不启动 Flask） ----------
def _load_engine_module():
    """用 importlib 加载 sandbox/app.py（v14 引擎），返回模块"""
    engine_path = os.path.join(V15_ROOT, "sandbox", "app.py")
    spec = importlib.util.spec_from_file_location("v15_engine", engine_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

engine = _load_engine_module()
SandboxSession = engine.SandboxSession
Coach = engine.Coach
ExperimentMemory = engine.ExperimentMemory
call_with_retry = engine.call_with_retry
# V15.4：多模型分流（外围环节用 Qwen/GLM，核心讨论仍 DeepSeek）
call_llm = getattr(engine, "call_llm", None)
PLAYER_NAMES = engine.PLAYER_NAMES

# ---------- 加载 V15 新增模块 ----------
sys.path.insert(0, os.path.join(V15_ROOT, "app"))
from coach_search import CoachSearcher, coach_search_and_inject
from presets import PRESET_TOPICS, get_scenario_report
from coach_team import build_optimal_team, generate_interventions
from review_v3 import v3_review

# ---------- FastAPI 应用 ----------
app = FastAPI(title="群智 · 科研决策预演智能体 V15")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 公网防护（口令 + 限流）
from guard import setup_guard
setup_guard(app)

# 页面禁用缓存（防止同学浏览器缓存旧版 JS，导致 API 地址还是 127.0.0.1）
@app.middleware("http")
async def no_cache_middleware(request, call_next):
    import time as _t
    response = await call_next(request)
    if request.url.path in ("/", "/classroom") or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

# 活跃会话（复用引擎的机制）
active_sessions = {}
session_queues = {}

# ==================== 请求模型 ====================

class SandboxStartRequest(BaseModel):
    topic: str
    user_thought: str = ""
    max_rounds: int = 3
    preset_name: Optional[str] = None          # 预设课题（教研场景）
    info_mode: str = "coach_search"            # coach_search=主教练联网注入
    personality_enabled: bool = True
    scenario: Optional[str] = None             # 场景化报告：教研/科研/通用
    allow_web: Optional[bool] = None           # B9：本局是否启用网络资料白名单网（None=跟随服务端默认）
    # V16.2 插件化：用户自选评审器（'v3'=快约40秒 / 'v6'=准约160秒 / None=用默认插件）
    review_plugin: Optional[str] = None

class CoachSearchRequest(BaseModel):
    topic: str
    count: int = 6

class CoachChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class ReviewRequest(BaseModel):
    report: str
    topic: str = ""

# ==================== 预设课题 ====================

@app.get("/api/presets")
def get_presets():
    """预设教研课题库（A 场景落地）"""
    return {"ok": True, "presets": PRESET_TOPICS}

# ==================== 主教练联网搜索 ====================

@app.post("/api/coach/search")
def coach_search(req: CoachSearchRequest):
    """主教练独占联网搜索：动态构建信息库"""
    searcher = CoachSearcher(call_with_retry)
    data = searcher.search_topic(req.topic, n_queries=2)
    return {
        "ok": True,
        "topic": req.topic,
        "queries": data.get("queries", []),
        "results_count": data.get("results_count", 0),
        "info_pool": data.get("info_pool", []),
    }

# ==================== 讨论会话（V15 版：带主教练信息注入） ====================

@app.post("/api/sandbox/start")
def start_session(req: SandboxStartRequest):
    """启动 V15 讨论：主教练先联网构建信息库，注入给玩家，再启动讨论"""
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="请输入课题")

    # 0. 复杂度分级（V15.2：区分简单/中等/复杂，防"简单问题复杂化"）
    try:
        from complexity import assess_complexity, get_level_config
        level = assess_complexity(topic, call_with_retry)
        lvl_cfg = get_level_config(level)
        max_rounds = lvl_cfg["max_rounds"]
        output_mode = lvl_cfg["output"]
        print(f"[V15.2] 课题复杂度: {level}（{max_rounds}轮，输出{output_mode}）", flush=True)
    except Exception as e:
        level, max_rounds, output_mode = "中等", req.max_rounds, "standard"
        print(f"[V15.2] 复杂度评估失败，用默认: {e}", flush=True)

    # 1. 主教练组建最优人格团队（V15：动态选择，而非固定组合）
    #    健壮性：组队/干预失败时回退默认配置，绝不让 start 接口 500
    try:
        team = build_optimal_team(topic, req.user_thought, call_with_retry) or {}
    except Exception as e:
        print(f"[V15] 组队失败，使用默认配置: {str(e)[:120]}", flush=True)
        team = {}
    config = team.get("config", {}) or {}
    team_reasoning = team.get("reasoning", "") or "（组队降级：使用默认人格配置）"
    print(f"[V15] 主教练组队: {team_reasoning}")

    # 2. 主教练预审：根据课题生成干预指令（V15：具备干预能力）
    try:
        coach_interventions = generate_interventions(topic, call_with_retry)
    except Exception as e:
        print(f"[V15] 干预生成失败，跳过: {str(e)[:120]}", flush=True)
        coach_interventions = None
    print(f"[V15] 主教练预审干预: {coach_interventions}")

    # 3. 主教练联网注入信息（V15 核心新能力）
    coach_infos = None
    search_meta = None
    coach_pool = None                     # V15 优化：结构化信息池（含编号 P1..Pn）
    if req.info_mode == "coach_search":
        try:
            # B9：本局是否启用网络资料白名单网（req.allow_web 为 None 时跟随服务端默认）
            searcher = CoachSearcher(call_with_retry, allow_web=req.allow_web)
            fragments, search_data = searcher.build_info_fragments(topic, list(range(6)))
            coach_infos = fragments
            coach_pool = search_data.get("info_pool") or []
            search_meta = {
                "queries": search_data.get("queries", []),
                "results_count": search_data.get("results_count", 0),
                "raw_count": search_data.get("raw_count", search_data.get("results_count", 0)),
                "injected_count": len(search_data.get("info_pool") or []),
                # V15（2026-10-09）：逐源台账 → 区分"没有数据"与"被限速/超时/被挡"
                "source_status": search_data.get("source_status") or {},
                # V15（2026-10-10，B5）：检索漏斗——检索→去重→类型过滤→撤稿→注入 逐级计数，
                # 含快照日期与级别覆盖率，供事后审计与"改动前后对比"。
                "funnel": search_data.get("funnel") or {},
                "allow_web": bool(getattr(searcher, "allow_web", False)),
            }
        except Exception as e:
            print(f"[V15] 主教练搜索失败，使用预设信息: {e}")
            coach_infos = None

    # 4. 构建会话（experiment_mode 标记 V15 + 最优人格 + 干预）
    experiment_mode = {
        "personality_enabled": req.personality_enabled,
        "info_mode": req.info_mode,
        "v15": True,
        "discussion_mode": "structured",
        "personality_mode": "optimal",   # V15：最优人格
        "team_reasoning": team_reasoning,
        # V15 优化（2026-09-25）：干预时机标记。
        # 旧行为 = predefined_once（开局预设、第2轮一次性注入）；
        # 新行为 = per_round（本局内逐轮决策）。两者不可合并统计。
        "intervention_timing": "per_round",
    }

    # 会话 ID：课题前 10 字（可读）+ 时间戳 + 4 位随机后缀（避免同秒同课题撞 ID 覆盖）
    sid = f"v15_{topic[:10]}_{datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"
    session = SandboxSession(
        topic, config, max_rounds, req.user_thought,
        custom_infos=coach_infos,  # 主教练注入的信息
        preset_name=req.preset_name,
        experiment_mode=experiment_mode,
        force_dynamic_info=(req.info_mode == "coach_search"),
        coach_interventions=coach_interventions,  # V15：开局候选（不再强制注入）
        info_pool=coach_pool,                     # V15 优化：结构化信息池 → 参考文献与核验
    )
    # 标记 V15 附加数据
    session.v15_meta = {
        "search": search_meta,
        "scenario": req.scenario or "通用",
        "complexity_level": level,        # V15.2：课题复杂度
        "output_mode": output_mode,       # V15.2：输出形态
        "started_at": datetime.now().isoformat(),
        "team_reasoning": team_reasoning,
        "team_config": config,
        "coach_interventions": coach_interventions,
        "intervention_timing": "per_round",
        "references": getattr(session, "references", []),   # V15 优化：参考文献结构化清单
    }
    # V16.2：把用户选定的评审插件记在会话上（插件层随后据此执行；未选则取默认插件）
    try:
        import review_plugins as _RP
        _plugs = {x['id']: x for x in _RP.list_plugins()}
        _want = req.review_plugin    # 函数参数是 req（原先误写成裸变量 review_plugin → NameError，被 review_error 如实记下）
        if _want and _want in _plugs and _plugs[_want]['available']:
            session.review_plugin_id = _want
        elif _want and _want in _plugs:
            # 用户点的插件不可用 —— **不悄悄换**，如实记下来，前端会显示原因
            session.review_plugin_id = None
            session.review_error = ('所选评审插件 %s 不可用：%s'
                                    % (_want, _plugs[_want].get('unavailable_reason') or '原因未给出'))
        else:
            session.review_plugin_id = next(
                (x['id'] for x in _RP.list_plugins() if x.get('default')), None)
    except Exception as _e:
        session.review_plugin_id = None
        session.review_error = '插件清单不可用：%s: %s' % (type(_e).__name__, str(_e)[:150])
    active_sessions[sid] = session
    session_queues[sid] = []

    def run_and_notify():
        orig = session.add_log
        def hooked(lt, msg, player=None, plain="", collision=False):
            orig(lt, msg, player, plain, collision)
            if sid in session_queues:
                try:
                    payload = {"type": lt, "message": msg, "plain_text": plain, "collision": collision}
                    if player:
                        payload.update({
                            "player": player.name,
                            "prototype_name": player.prototype['name'],
                            "profession_name": player.profession['name'],
                            "profession_icon": player.profession['icon'],
                            "value_name": player.value['name'],
                            "value_icon": player.value['icon'],
                            "color": player.prototype['color'],
                        })
                    session_queues[sid].append(payload)
                except Exception:
                    pass
        session.add_log = hooked
        # 健壮性：讨论线程任何异常都必须把 done 事件发出去，
        # 否则前端 /api/sandbox/stream 会永久等待、result 永远 running（实测过的翻车形态）
        try:
            session.run()
        except Exception as e:
            import traceback
            print(f"[讨论异常] {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            if sid in session_queues:
                session_queues[sid].append({
                    "type": "system",
                    "message": f"⚠️ 讨论过程出现异常，已中断：{type(e).__name__}。可返回重新发起预演。",
                })
        finally:
            if sid in session_queues:
                session_queues[sid].append({"type": "done", "message": "完成"})

        # 讨论结束后立即预生成「人话报告 + v3 评审 + 深批者」（后台并行预热，
        # 避免用户点开报告时干等 2-3 分钟）
        def _precompute():
            session._precomputing = True
            try:
                # ================================================================
                # V16.6b：**预生成自身也并行**。
                # 原先是串行「人话化 → v3 → 深批者」，实测约 95 秒；用户点开报告时
                # 若预生成没完，等待时间就由这条链的长度决定。
                # 其中只有深批者需要人话报告，另两个互不依赖 → 可并行。
                # ================================================================
                _tp = time.time()
                from concurrent.futures import ThreadPoolExecutor

                def _p_hr():
                    if not (session.report and not getattr(session, "human_report", None)):
                        return
                    try:
                        session.human_report = _humanize_report(session.topic, session.report, "full")
                        print("[预生成] 人话报告完成（%.1fs）" % (time.time() - _tp), flush=True)
                    except Exception as e:
                        print(f"[预生成] 人话报告失败: {e}", flush=True)

                def _p_v3():
                    # V16.4：只在用户真的选了 v3 时才跑（否则白花 ~31 秒，且它是已下线的旧尺子）
                    if getattr(session, "review_plugin_id", None) != "v3":
                        print("[预生成] v3 跳过（本局选的是 %s）"
                              % (getattr(session, "review_plugin_id", None) or "默认"), flush=True)
                        return
                    if not (session.report and not getattr(session, "v3_review_result", None)):
                        return
                    try:
                        session.v3_review_result = v3_review(session.report, session.topic)
                        print("[预生成] v3 评审完成（%.1fs）" % (time.time() - _tp), flush=True)
                    except Exception as e:
                        print(f"[预生成] v3 评审失败: {e}", flush=True)

                with ThreadPoolExecutor(max_workers=2) as _ex:
                    for _fu in (_ex.submit(_p_hr), _ex.submit(_p_v3)):
                        _fu.result()
                print("[预生成] 人话化/v3 并行段完成（%.1fs）" % (time.time() - _tp), flush=True)

                # 深批者排在其后（素材是人话报告；拿不到则退原文）
                if session.report and not getattr(session, "deep_critic", None):
                    try:
                        hr = getattr(session, "human_report", None) or ""
                        session.deep_critic = _deep_critic_review(session.topic, session.report, hr)
                        print("[预生成] 深批者完成（%.1fs）" % (time.time() - _tp), flush=True)
                    except Exception as e:
                        print(f"[预生成] 深批者失败: {e}", flush=True)
            finally:
                session._precomputing = False

        threading.Thread(target=_precompute, daemon=True).start()

        # ④ 评审（V16.2 **插件化**）——跑用户选的插件，单独开线程，不占上面那条串行链。
        # 用户要求：「老版本就是快，新版本需要更多的时间，让用户自己选」——
        # 所以这里不再写死 v6，而是按 `session.review_plugin_id`（来自启动参数）挑。
        # 为什么不复用 _precompute：v6 要跑锚点自检 + 三模型分维，实测约 160 秒；
        # 塞进串行链会把「人话报告」一起拖慢（用户点开报告要干等）。
        # 独立线程 → 人话报告/v3/深批者照常先出，评审后到。
        # **不回落**：所选插件不可用就如实记不可用，绝不悄悄换一个顶上。
        def _precompute_review():
            _pid = getattr(session, "review_plugin_id", None)
            print("[预生成] 评审插件线程入口：plugin=%s report=%s 已有结果=%s" % (
                _pid, bool(session.report), bool(getattr(session, "review_result", None))), flush=True)
            try:
                if not session.report:
                    print("[预生成] 评审跳过：session.report 为空", flush=True)
                    return
                if not _pid:
                    print("[预生成] 评审跳过：本局未选择评审插件", flush=True)
                    return
                if getattr(session, "review_result", None):
                    print("[预生成] 评审跳过：已有结果", flush=True)
                    return
                import review_plugins as _RP
                t0 = time.time()
                _out = _RP.run_plugin(_pid, session.report, topic=session.topic)
                _res = _out.get("result") if _out.get("ok") else None
                session.review_result = _res
                session.review_plugin_id = _pid
                print("[预生成] 评审插件 %s 完成（%.0f 秒｜ok=%s）" % (
                    _pid, time.time() - t0, _out.get("ok")), flush=True)
                if not _out.get("ok"):
                    session.review_error = _out.get("error")
                    print("[预生成] 评审插件失败：%s" % str(_out.get("error"))[:200], flush=True)
                # **算完立刻补存一次**：评审约 40–160 秒，而主流程的 save_session 在结果返回时就
                # 执行了，那时评审往往还没跑完 → 落盘里没有这个字段（深批者就是这么丢了 60% 的局）。
                try:
                    from local_store import save_session
                    save_session(session)
                    print("[预生成] 评审结果已补存进会话文件", flush=True)
                except Exception as _e:
                    print("[预生成] 评审补存失败（结果仍在内存里，前端可看）: %s" % _e, flush=True)
            except Exception as e:
                session.review_error = "%s: %s" % (type(e).__name__, str(e)[:200])
                print("[预生成] 评审线程异常: %s" % str(e)[:200], flush=True)

        threading.Thread(target=_precompute_review, daemon=True).start()

    threading.Thread(target=run_and_notify, daemon=True).start()

    return {
        "status": "started",
        "session_id": sid,
        "topic": topic,
        "search_meta": search_meta,
        "v15": True,
    }

@app.get("/api/sandbox/stream")
def stream_session(session_id: str = ""):
    """SSE 实时讨论流"""
    RETRY_MS = 3600000       # 1 小时；SSE 规范字段，单位毫秒

    def _closed(reason):
        def g():
            yield "retry: %d\n\n" % RETRY_MS
            yield "data: %s\n\n" % json.dumps(
                {"type": "done", "message": reason, "closed": True}, ensure_ascii=False)
        return StreamingResponse(g(), media_type="text/event-stream")

    if session_id not in session_queues:
        if active_sessions.get(session_id) is None:
            return _closed("该会话已结束（或服务已重启），不再实时推送；报告请看「决策报告」。")
        return _closed("该会话没有实时流，请看「决策报告」。")
    q = session_queues[session_id]

    def gen():
        yield "retry: %d\n\n" % RETRY_MS     # 先声明重连间隔，防浏览器 3 秒狂连
        idx = 0
        while True:
            while idx < len(q):
                item = q[idx]
                idx += 1
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if any(e.get("type") == "done" for e in q):
                break
            time.sleep(0.3)
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/api/sandbox/result/{session_id}")
def get_result(session_id: str):
    """获取会话完整结果（讨论结束后的报告/涌现/评估）

    V16.1（2026-10-11）：**内存里找不到时，回落到磁盘上的会话文件**。
    为什么必须这样做（用户原话：「我在页面刷新过后，我原有的报告也消失了，
    跟我们正常用的网页不一样」）：
      会话一直在落盘（`local_store.save_session`），但**这个接口只从内存读**，
      而 `local_store` 原先**只有写、没有读回的函数**。后果是——
      **服务一重启，磁盘上所有会话就再也没人读得到**（实测：重启后旧会话 404）。
      数据没丢，只是没人读。刷新能不能看到，取决于服务进程活没活着，这不合理。
    """
    session = active_sessions.get(session_id)
    if not session:
        # ---- 回落：从磁盘读回（内存里没有 ≠ 数据不存在）----
        try:
            from local_store import load_session
            rec = load_session(session_id)
        except Exception as e:
            rec = None
            print(f"[V16.1] 会话回落读取失败: {e}", flush=True)
        if not rec:
            raise HTTPException(status_code=404, detail="会话不存在（内存与磁盘均无此 session_id）")
        print(f"[V16.1] 会话 {session_id} 不在内存，已从磁盘读回：{rec.get('_loaded_from')}", flush=True)
        # 磁盘记录的字段名与接口载荷不同（v3_review / v6_review vs *_result），逐项映射。
        # 另外磁盘里没有"重启前才在内存里的东西"（如 interventions 的实时快照），
        # 所以拿不到就如实给空，**不要编**。
        return {
            "status": "done",
            "session_id": session_id,
            "topic": rec.get("topic", ""),
            "report": rec.get("report"),
            "human_report": rec.get("human_report"),
            "complexity_level": rec.get("complexity_level", ""),
            "emergence": rec.get("emergence"),
            "evaluation": rec.get("evaluation"),
            "v3_review": rec.get("v3_review"),
            "v6_review": rec.get("v6_review"),          # ◀ 新尺子的读数，重启后依然可看
            # V16.2 插件化：历史记录也要能回答"这局读数哪把尺子给的、跑成没跑成"
            "review_plugin": rec.get("review_plugin"),
            "review": rec.get("review"),
            "review_error": rec.get("review_error"),
            "deep_critic": rec.get("deep_critic", ""),
            "coach_audit": _aggregate_audit(rec.get("coach_audit") or []),
            "references": rec.get("references") or [],
            "citation_check": rec.get("citation_check"),
            "info_pool": rec.get("info_pool") or [],
            "intervention_records": rec.get("intervention_records") or [],
            "intervention_timing": rec.get("intervention_timing", ""),
            "round_signals": rec.get("round_signals") or [],
            "next_session_suggestions": rec.get("next_session_suggestions") or [],
            "logs": rec.get("logs") or [],
            "restored_from_disk": True,                 # 前端可据此提示"这是历史记录"
            "saved_at": rec.get("saved_at", ""),
        }
    if session.running:
        return {"status": "running", "session_id": session_id}
    # 审计意见聚合（v14 引擎按行分割导致 30+ 条，V15 展示聚合为要点）
    audit_raw = session.coach_audit_opinions or []
    audit = _aggregate_audit(audit_raw)

    # v3 评审（优先复用讨论结束时的预生成结果）
    # V16.4 提速：**用户没选 v3 就完全不跑**（含这里的"现场计算"）。
    # 原先无论用户选了什么，只要预生成没命中，就会**在用户正等待的这条路径上**补算 v3
    # （约 31 秒）—— 用户明明选了 v6，却要为一个已下线的旧尺子干等。
    _want_v3_now = (getattr(session, "review_plugin_id", None) == "v3")
    if _want_v3_now and not getattr(session, "v3_review_result", None):
        try:
            report_text = session.report or ""
            session.v3_review_result = v3_review(report_text, session.topic) if report_text else None
            print("[V15] v3 五角色评审完成（现场计算）")
        except Exception as e:
            print(f"[V15] v3 评审失败: {e}")
            session.v3_review_result = None
    elif getattr(session, "v3_review_result", None):
        print("[V15] v3 评审命中预生成缓存", flush=True)
    else:
        print("[V15] v3 跳过（本局未选 v3）", flush=True)

    # 人话化输出（V15.3：所有模式都生成大白话版，公网同学可读）
    v15_meta = getattr(session, "v15_meta", {})
    output_mode = v15_meta.get("output_mode", "standard")
    # ================================================================
    # V16.6 提速（2026-10-11）：用户反馈「决策报告生成一辈子，太慢了」
    # 实测：讨论后处理 91 秒，且**结果路径与预生成线程在算同一件事**（各算一遍）。
    #   · 先**等预生成**（它做的就是人话化/v3/深批者），不重复算；
    #   · 再**并行**跑还缺的环节（v3 与人话化互不依赖，只有深批者需要人话报告）。
    # ================================================================
    # V16.6b：**只等我们真正需要的字段**，不等"整个预生成"。
    # 实测教训：等整个预生成要 67–80 秒，而我们要的 `human_report` 早就好了；
    # 深批者是后置模块，没必要为它挡住整页。
    def _wait_field(attr, timeout):
        if getattr(session, attr, None):
            return True
        if not getattr(session, "_precomputing", False):
            return False
        _tw = time.time()
        while (time.time() - _tw) < timeout:
            if getattr(session, attr, None):
                return True
            if not getattr(session, "_precomputing", False):
                return bool(getattr(session, attr, None))
            time.sleep(0.3)
        return bool(getattr(session, attr, None))

    if getattr(session, "_precomputing", False):
        _tw0 = time.time()
        print("[V16.6b] 预生成在跑：只等需要的字段（人话报告≤90s）...", flush=True)
        _got_hr = _wait_field("human_report", 90)
        print("[V16.6b] 人话报告 %s（等了 %.0f 秒；预生成整体仍在后台跑）"
              % ("已就绪" if _got_hr else "未就绪，将自行计算", time.time() - _tw0), flush=True)

    human_report = getattr(session, "human_report", None)   # 优先用预生成
    _need_hr = (not human_report) and bool(session.report)
    _need_v3 = (not getattr(session, "v3_review_result", None)) and bool(session.report)
    if not _need_hr and human_report:
        print("[V15.2] 人话报告命中预生成缓存", flush=True)
    if not _need_v3 and getattr(session, "v3_review_result", None):
        print("[V15] v3 评审命中预生成缓存", flush=True)

    if _need_hr or _need_v3:
        from concurrent.futures import ThreadPoolExecutor
        _t0p = time.time()

        def _job_hr():
            try:
                hr = _humanize_report(session.topic, session.report,
                                      "full" if output_mode == "standard" else output_mode)
                print("[V15.2] 人话化完成（现场计算 %.1fs）" % (time.time() - _t0p), flush=True)
                return "hr", hr, None
            except Exception as e:
                print(f"[V15.2] 人话化转译失败: {e}")
                return "hr", None, str(e)[:150]

        def _job_v3():
            # 复用与预生成相同的实现（v3_review）——**只在一处定义**，避免两套逻辑漂移
            try:
                r = v3_review(session.report, session.topic)
                print("[V15] v3 五角色评审完成（现场计算 %.1fs）" % (time.time() - _t0p), flush=True)
                return "v3", r, None
            except Exception as e:
                print(f"[V15] v3 评审失败: {e}")
                return "v3", None, str(e)[:150]

        # V16.6 熔断③：**后处理总截止**。原先是无限等 —— 一次模型挂住就会把
        # 「决策报告」拖到十分钟（用户实测原话："生成一辈子"）。
        # 现在到点就用已有结果返回；缺哪个模块由前端如实标注，绝不空着等。
        _POST_DEADLINE = int(os.getenv("QZ_POST_DEADLINE", "240"))
        _deadline = _t0p + _POST_DEADLINE
        _res = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            _futs = {}
            if _need_hr:
                _futs[ex.submit(_job_hr)] = "hr"
            if _need_v3:
                _futs[ex.submit(_job_v3)] = "v3"
            for fu in _futs:
                _left = max(1.0, _deadline - time.time())
                try:
                    k, val, err = fu.result(timeout=_left)
                    _res[k] = (val, err)
                except Exception as _e:
                    print("[V16.6] 熔断：%s 超过后处理截止（%.0fs），放弃等待（%s）"
                          % (_futs[fu], _POST_DEADLINE, type(_e).__name__), flush=True)
                    _res[_futs[fu]] = (None, "超过后处理截止 %ds" % _POST_DEADLINE)
        print("[V16.6] v3 与人话化并行完成（共 %.1fs）" % (time.time() - _t0p), flush=True)
        if _need_hr and not human_report:
            human_report = _res.get("hr", (None, None))[0]
            if not human_report:
                human_report = session.report        # 兜底：至少拿原文，不让报告空着
        if _need_v3 and not getattr(session, "v3_review_result", None):
            session.v3_review_result = _res.get("v3", (None, None))[0]
    session.human_report = human_report

    # 深批者：先算，**后存盘**。
    # 事故（2026-10-11）：原顺序是「先 save_session → 后算深批者」，而预生成跑在 daemon 线程里
    # 串行做「人话报告 → v3 评审 → 深批者」，存盘时深批者常常还没跑完 →
    # **最近 10 局里 6 局落盘的 deep_critic 是空字符串**，实验数据里这个字段基本不可用。
    # 现在把计算提到存盘之前：预生成命中就零等待，未命中就现场算完再存。
    deep_critic = getattr(session, "deep_critic", "")
    if not deep_critic:
        # V16.6 熔断③续：深批者也放进线程并设截止，不让它把整页拖住
        try:
            print("[深批者] 开始质疑示范（现场计算）...", flush=True)
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex1:
                _fu1 = _ex1.submit(_deep_critic_review, session.topic,
                                   session.report or "", human_report or "")
                try:
                    deep_critic = _fu1.result(timeout=int(os.getenv("QZ_DC_DEADLINE", "90"))) or ""
                except Exception as _e1:
                    print("[V16.6] 熔断：深批者超时，本局不展示（%s）" % type(_e1).__name__, flush=True)
                    deep_critic = ""
            session.deep_critic = deep_critic
        except Exception as e:
            print(f"[深批者] 失败: {e}", flush=True)
            session.deep_critic = ""
            deep_critic = ""
    else:
        print("[深批者] 命中预生成缓存", flush=True)

    # 本地数据留存（V15.3：每次讨论结果落盘）——**放在深批者之后**，保证落盘字段完整
    saved_path = None
    try:
        from local_store import save_session
        saved_path = save_session(session)
        print(f"[V15.3] 已留存本地数据: {saved_path}", flush=True)
    except Exception as e:
        print(f"[V15.3] 本地留存失败: {e}", flush=True)

    # 诚实声明（底部小字）：AI 输出偏理想化，需人类判断
    honesty_note = ("⚠️ AI 团队讨论天然偏理想化——它假设资源到位、人配合、制度可执行。"
                    "真实世界的预算约束、利益博弈、执行阻力，需要你在决策时自行补上。"
                    "本报告的讨论机制来自 798 轮实验（口径与数据见提交材料：实验证据总表/实验数据表/实验背书报告），"
                    "评审为 AI 独立评审、暂无人类盲评，仅供参考，不替代专业判断。")

    return {
        "status": "done",
        "session_id": session_id,
        "topic": session.topic,
        "deep_critic": deep_critic,          # 深批者视角（对抗性评审）
        "honesty_note": honesty_note,        # 底部诚实声明
        "report": session.report,
        "human_report": human_report,              # V15.2：人话化报告
        "saved_path": saved_path,                  # V15.3：本地数据留存路径
        "complexity_level": v15_meta.get("complexity_level", "中等"),  # V15.2
        "output_mode": output_mode,
        "emergence": session.emergence_analysis,
        "evaluation": session.report_evaluation,       # 引擎旧评分（兼容保留）
        "v3_review": session.v3_review_result,          # v3 五角色评审（1–10 分已下线，仅留原文证据）
        # V16.2 插件化：`review` 是**通用**字段（哪个插件跑的/结果/错误），
        # `v6_review` 保留为兼容别名（老前端与老脚本仍读它）。
        "review_plugin": getattr(session, "review_plugin_id", None),
        "review": getattr(session, "review_result", None),
        "review_error": getattr(session, "review_error", None),
        "v6_review": ((getattr(session, "review_result", None) or {}).get("v6")
                      if getattr(session, "review_plugin_id", None) == "v6"
                      else getattr(session, "v6_review_result", None)),
        "cards": session.cards,
        "golden_insight": session.golden_insight,
        "coach_audit": audit,
        "v15_meta": v15_meta,
        # ---- V15 优化（2026-09-25）：引用与干预 ----
        "references": getattr(session, "references", []),          # 参考文献结构化清单
        "citation_check": getattr(session, "citation_check", None),  # 三类标记
        "interventions": getattr(session, "intervention_records", []),   # 本局逐轮干预记录
        "intervention_timing": getattr(session, "intervention_timing", "per_round"),
        "round_signals": getattr(session, "round_signals", []),          # 每轮客观信号（复现用）
        # V16.4：**讨论记录必须进载荷**（原先只有"磁盘回落"那条路径带 logs）。
        # 用户反馈「我的讨论记录怎么又不见了」：日志只在 SSE 实时流里渲染过，
        # 一刷新前端就没有可重放的数据 —— 而 `session.logs` 一直都是有的（落盘 50 条）。
        # 数据没丢，是接口没把它给出去、前端也没画。两边都补上。
        "logs": getattr(session, "logs", []),                            # 完整讨论过程（可重放）
        "next_session_suggestions": getattr(session, "next_session_suggestions", []),  # 留给下一局
        # 场景化决策报告（A/B 融合）——单独兜底：场景报告失败不应连累整个结果接口
        "scenario_report": _safe_scenario_report(
            v15_meta.get("scenario", "通用"),
            session.topic, session.report, session.report_evaluation,
            audit,
        ),
    }


def _safe_scenario_report(*args):
    """调用 get_scenario_report 并兜底（失败返回 None，不影响主报告展示）"""
    try:
        return get_scenario_report(*args)
    except Exception as e:
        print(f"[场景报告] 生成失败（已降级为不展示该模块）: {str(e)[:150]}", flush=True)
        return None

# ==================== 平台同步接口（学校低代码平台用） ====================

class PlatformRunRequest(BaseModel):
    topic: str
    user_thought: str = ""

@app.post("/api/platform/run")
def platform_run(req: PlatformRunRequest):
    """同步执行一次完整预演，返回决策建议书（供学校低代码平台 HTTP 插件直接调用）。
    实现：内部通过已验证的 start→轮询 result 路径（复用同一网关的异步流程），
    避免直接操作 session 内部状态导致的不稳定。
    """
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="请输入课题")

    # 1. 走 start_session 逻辑（异步启动讨论线程）
    start_req = SandboxStartRequest(
        topic=topic, user_thought=req.user_thought,
        max_rounds=2, info_mode="real", scenario="通用", preset_name="通用科研",
    )
    started = start_session(start_req)
    sid = started.get("session_id")
    if not sid:
        return {"status": "error", "message": "预演启动失败", "topic": topic}

    # 2. 轮询结果（最长等 7 分钟）
    deadline = time.time() + 420
    while time.time() < deadline:
        sess = active_sessions.get(sid)
        if sess and not sess.running:
            # 已完成，组装精简结果
            v15_meta = getattr(sess, "v15_meta", {})
            human_report = getattr(sess, "human_report", None)
            if human_report is None and sess.report:
                try:
                    human_report = _humanize_report(sess.topic, sess.report, "full")
                    sess.human_report = human_report
                except Exception as e:
                    print(f"[platform] 人话化失败: {e}")
                    human_report = sess.report
            scenario_report = None
            try:
                scenario_report = get_scenario_report(
                    "通用", sess.topic, sess.report, sess.report_evaluation,
                    sess.coach_audit_opinions or [],
                )
            except Exception as e:
                print(f"[platform] 场景化失败: {e}")
            return {
                "status": "done",
                "topic": topic,
                "report": sess.report,
                "human_report": human_report or sess.report or "",
                "scenario_report": scenario_report,
                "emergence": sess.emergence_analysis,
                "complexity_level": v15_meta.get("complexity_level", "中等"),
            }
        time.sleep(5)
    return {"status": "timeout", "message": "预演超时，请稍后重试", "topic": topic}

def _humanize_report(topic: str, report: str, output_mode: str) -> str:
    """人话化转译：把讨论报告翻译成决策者直接能懂的话"""
    if output_mode == "quick":
        prompt = f"""你是教研导师。课题：{topic}
请把下面这份讨论报告，压缩成【快速建议】，说人话，不展开理论。
格式：
一句话结论：【30字内】
三步行动：
①【本周能做的具体动作】
②【下一步的改进动作】
③【一个最常踩的坑】
报告：
{report[:2500]}"""
        result = call_llm(prompt, provider="qwen", max_tokens=1500, temperature=0.5, timeout=60)
        return result or report
    else:
        prompt = f"""你是决策顾问。课题：{topic}
把下面这份讨论报告，改写成【人话化决策建议书】。
要求：每部分先给结论再给支撑，专业术语翻译成大白话。
格式：
一句话结论：【30字内，直接回答该不该/怎么做】
给决策者的三步行动：
①【本周可做的具体动作】
②【一个月内的推进动作】
③【长期的结构性动作】
你的认知盲区：【讨论中发现、提问者没想到的角度】
支撑细节：【1-2句说明依据】
报告：
{report[:6000]}"""
    result = call_llm(prompt, provider="qwen", max_tokens=4000, temperature=0.5, timeout=120)
    return result or report

def _deep_critic_review(topic: str, report: str, human_report: str = "") -> str:
    """深批者：**给用户看的「质疑示范」**。

    ⚠ 设计意图（2026-10-11 据作者澄清后重写，这是本次改动的核心）：
      深批者**不是**又一个对抗性评审，而是**把「质疑 AI」这件事做给用户看**——
      告诉大家：AI 时代要有自己的思考，要敢于质疑 AI。

    因此评价标准不是"批得对不对"，而是"**用户看完，会不会质疑了**"：
      · 原先的 prompt 通篇是"你是专门负责拆台的资深从业者"——**它不知道自己是示范**，
        于是产出的是"拆台"，不是"示范质疑的方法"；
      · 实测暴露的问题：全是"**可能**难以找到资源"式的空想、⑤给出"确保教师培训到位"这种
        它自己 prompt 明令禁止的空话、以及无任何依据的"理念 4/10 落地 2/10"。
        **用户会模仿它示范的语言**——示范空话，就是在教用户说空话。

    现在的三条硬改造：
      ① 每条质疑必须挂一行「💬 你可以这样问 AI」——**把锤子递给用户**，让他拿走可复用的提问；
      ② **禁空话清单 + 代码级检查**（见 _deep_critic_has_empty_talk）：命中就重生成一次；
      ③ 结尾**邀请用户反驳深批者自己** —— 三层质疑：AI 生成 → AI 质疑 → 用户质疑 AI 的质疑。
    """
    source = (human_report or report) or ""
    prompt = f"""你是群智的「深批者」。

【你存在的意义 —— 先读这一段】
群智的信念是：**AI 时代，人要有自己的思考，要敢于质疑 AI。**
但"敢于质疑"不是一句口号——多数人不知道该从哪儿下手。所以由你**先示范一遍**：
让用户看到"原来可以这样质疑"，然后他才有底气去质疑别的 AI 输出，**包括质疑你**。

所以你的输出会被当成**范例**来看。这不只是"批得对"的问题，而是"**批得让人能学会**"的问题：
用户会模仿你的句式、你的推理方式、你的分寸。**你示范空话，就是在教用户说空话。**

课题：{topic}

【硬性要求（违反即不合格）】
1. 每条质疑必须**点名报告里的具体建议**（引用其原话或内容片段），禁止"存在风险""需注意执行"这类空话；
2. 每条质疑必须给出**可运转的机制**：谁、在什么情况下、会怎么做、导致什么后果。
   **禁止用"可能"代替机制**（"可能难以找到资源"不是质疑，是猜测）；
3. 区分两类并标注：❌ **方向错**＝这条建议的前提本身不成立；⚠️ **执行难**＝方向对但推不动，卡在哪一环；
4. **【本次新增，最重要】** 每条质疑后面必须另起一行给一个**追问**，让用户下次能自己用：
   `💬 你可以这样追问 AI：`
   它只有两种合格形态，**必须是这两种之一**：
   · **甲｜逼 AI 交底**：问得让 AI 要么拿出**可核查的依据**、要么**承认拿不出来**。
     ❌ 不合格：`匿名提问的实施比例是多少？`——AI 会随口给个数字或套话，你什么都没得到；
     ✅ 合格：`你说"学生怕被评价"——这句话在你引的那几篇里有原文吗？把标题和原句贴出来；贴不出来就直说没有。`
   · **乙｜去现实里验证**：给一个用户**今天就能做**的动作。
     ✅ 合格：`随机找 10 个学生问一句"这学期最让你睡不着的是哪件事"，看答案里是期末考还是保研。`
   **判断标准：照这句话做完，用户能拿到一个原本拿不到的信息**（一个承认、一份原文、一条真实反馈）。
5. 结尾必须**邀请用户反驳你**：点名你认为自己**最站不住的那一条**，说清它可能错在哪。
   这是本环节的立意——**连"教你质疑的 AI"本身也应该被质疑**。

【禁用语（出现即判不合格，会被程序打回重写）】
- "可能难以""可能会""或许会"这类**替代机制**的模糊猜测；
- "确保…到位""建立有效的…体系""加强…建设""提高…意识"这类**没有动作的空话**；
- "总体而言""综合来看""建议还是有一定价值的"这类和稀泥；
- **任何没有列出扣分项依据的分数**。

【检查维度】
① **最脆弱的假设**：报告默认了哪些"人会配合／资源到位／制度会执行"？哪一条不成立整套就崩？
② **现实阻力的落点**：点名哪条建议在真实环境（预算／人力／部门利益／流程惯性）下推不动，卡在谁那里；
③ **会被怎么钻空子**：利益足够大时谁会"反向适应"这套建议？具体怎么钻；
④ **最坏的代价**：按这套执行，最坏情况伤害谁？有什么被忽略的副作用；
⑤ **落地前置条件**：坚持要做，必须先具备什么（1-2 条**可检查**的条件，不要空话）。

【范例 —— 这是**另一个课题**，只看形状，内容要换成当前课题】
课题：大学该不该取消期末考试
**① 最脆弱的假设**
报告假设"取消期末考就能降低学生焦虑"，这依赖"焦虑主要来自考试"。
但补考、绩点排名、保研竞争都没变——焦虑只是换了个出口。
💬 你可以这样追问 AI：你写"考试是焦虑主因"，这话在你引的那几篇里有原文吗？
把标题和原句贴出来；贴不出来就直说没有。

**② 现实里会卡在哪**
卡在教务处。课程成绩要进学籍系统、要参与保研排序，
教务员一个人管 3000 人，他不会为某一门课的"无成绩"单独开一条流程。
💬 你可以这样追问 AI：这门课不出成绩，学籍系统那一栏具体填什么？请给出字段名和操作路径。

【输出格式】600-900 字。**每节都要先写 2-4 句质疑正文，再写 💬 行**——只写 💬 行是不合格的。
标题与 💬 行照抄，其余写内容：

### 🧐 深批者：AI 也会错，给你打个样

**① 最脆弱的假设**

💬 你可以这样追问 AI：

**② 现实里会卡在哪**

💬 你可以这样追问 AI：

**③ 会被怎么钻空子**

💬 你可以这样追问 AI：

**④ 最坏的代价**

💬 你可以这样追问 AI：

**⑤ 如果一定要做，先解决**

**📊 打分**

**🙋 现在轮到你了**

（两处格式说明，只此一次：**📊 打分** 写两行，形如
`理念 8/10：扣分项 —— <你实际列出的依据>`
`落地 7/10：扣分项 —— <你实际列出的依据>`，分数由你自己给（0-10 整数）。
**🙋** 写明你认为自己哪一条最站不住、可能错在哪，并请用户反驳你。）

【写完自查三件事】
1. 四条 💬 是否**逼 AI 交底**或**给现实动作**（不是泛泛征询，也不是把质疑句加问号）？
2. 把每个"可能"都改写成"**谁、在什么情况下、会怎么做**"——数一数还剩几个"可能"；
3. 有没有出现"确保…到位""建立有效的…"这类没有动作的话？

团队产出：
{source[:5000]}"""
    ALIAS = "深批者"
    for _attempt in (1, 2):
        try:
            # V15.4：深批者分流到 GLM——该模型在批判/挑刺类任务上表现突出
            r = call_llm(prompt, provider="glm", max_tokens=2400, temperature=0.6, timeout=120)
        except Exception as e:
            print(f"[{ALIAS}] 调用失败: {e}", flush=True)
            return ""
        txt = (r or "").strip()
        if not txt:
            return ""
        hits = _deep_critic_empty_talk(txt)
        if not hits:
            return _deep_critic_soft_notes(_deep_critic_sanitize(txt))
        print(f"[{ALIAS}] 第 {_attempt} 次产出命中空话清单 {hits}，重写", flush=True)
        if _attempt == 2:
            # 第二次仍命中 → **如实标注**，不假装通过（与项目"不利结果如实报告"一致）
            return (_deep_critic_sanitize(txt) +
                    "\n\n> ⚠️ 本节经程序检查仍有疑似空话：%s。"
                    "请以此为例注意——**AI 的质疑本身也可能不合格**。" % u"、".join(hits))
    return ""


# 脚手架清洗：模型会把"给它看的格式说明"照抄进正文（实测两轮都发生：
# 把【点名 + 具体机制】、（逐条）、（说清可能错在哪）原样输出）。
# 换成只留标题后应大幅减少，但**不能只靠 prompt**——prompt 已经失败过两次，
# 所以在代码层再兜一道，并在触发时打日志（日志出现即说明 prompt 还需收紧）。
_SCAFFOLD_RE = re.compile(
    u"[（(][^）)]{0,140}?(?:点名|说清|具体钻法|逐条|可检查|谁会受损|卡在哪|图什么|"
    u"一个能立刻执行|答案会暴露|报告里那句话|写给你看)[^）)]{0,140}?[）)]")


def _deep_critic_sanitize(text: str) -> str:
    """剥掉漏进正文的格式说明（脚手架），并记录。"""
    t = str(text or "")
    hits = _SCAFFOLD_RE.findall(t)
    if hits:
        t = _SCAFFOLD_RE.sub(u"", t)
        print(f"[深批者] 清洗掉 {len(hits)} 处脚手架泄漏：{str(hits[:3])[:120]}", flush=True)
    # 空标题行（清理后可能留下只有标题、内容被清掉的节）——把连续空行压成一个
    t = re.sub(u"\n{3,}", u"\n\n", t)
    # **残句处理**：剥掉括号后可能留下断句（"扣分项 ——"、"因为它。"）。
    # 直接留着比脚手架更难看，所以补一句**如实**的说明，而不是编内容。
    t = re.sub(u"扣分项\\s*——\\s*$", u"扣分项 ——（模型未填写，本节不合格）", t, flags=re.M)
    t = re.sub(u"因为它\\s*[。，,；;]?\\s*$", u"因为它**没有给出具体理由**——这本身就是一处反面示范。",
               t, flags=re.M)
    # "第 N 条"字面残留（模型没替换编号）
    t = t.replace(u"第 N 条", u"其中一条").replace(u"第N条", u"其中一条")
    return t


# 空话清单：这些句式是"**看起来在质疑、实际没给出任何机制**"的典型。
# 之所以要做程序级检查：深批者的输出是**给用户看的质疑范例**，
# 而**示范的语言就是用户会模仿的语言**——示范空话等于教用户说空话。
_EMPTY_TALK = (
    # V16 第六轮实测补漏：旧清单只有"建立有效的"，而模型写的是"建立**科学合理的**评估体系"、
    # "确保教师具备有效引导课堂互动的**能力**"——**换了形容词就绕过了检查**。
    # 所以这里按**句式骨架**匹配（建立…体系 / 确保…能力 / 合理分配…），不绑定具体形容词。
    # 注意长度上界：实测句是"确保教师具备有效引导课堂互动的**能力**"——「能力」前有 **13** 个字，
    # 最初写成 {0,12} 恰好漏掉，自检当场抓到。留足余量写 {0,20}。
    u"确保[^。；\n]{0,20}(?:到位|能力|实施|支持|落实)",
    u"建立[^。；\n]{0,20}(?:体系|机制|制度)",
    u"加强[^。；\n]{0,8}建设", u"提高[^。；\n]{0,8}(?:意识|水平|能力)",
    u"合理分配", u"科学合理", u"进一步完善", u"积极探索",
    u"总体而言", u"综合来看", u"有一定价值", u"可能存在一定", u"或许会",
)


def _deep_critic_empty_talk(text: str) -> list:
    """返回命中的空话句式（空列表 = 通过）。"""
    import re as _re
    t = str(text or "")
    return [p for p in _EMPTY_TALK if _re.search(p, t)]


def _deep_critic_soft_notes(text: str) -> str:
    """软提示（不重写）：把"可能的密度"如实标注出来。

    "可能"在**确实给了机制**时是合法的限定词，所以不做硬检查、不触发重写；
    但实测旧版产出通篇是"**可能**难以找到资源""教师**可能**不愿意"——**用可能替代机制**。
    密度过高时给读者一句提示，让他知道该往哪儿追问。
    """
    t = str(text or "")
    n = t.count(u"可能")
    if n < 4:
        return t
    return (t + "\n\n> ℹ️ 本节出现 %d 次「可能」。**限定词不等于机制**——"
                 "读到「可能会…」时，请追问一句：**谁、在什么情况下、会怎么做？**"
            % n)

def _aggregate_audit(raw: list, max_items: int = 4) -> list:
    """把引擎产生的审计意见行聚合为 3-5 条要点（对非字符串输入做兜底，避免 AttributeError）"""
    if not raw:
        return []
    # 去掉 markdown 装饰行（先统一转成字符串：raw 里可能混入 None / dict）
    cleaned = [str(l).strip() for l in raw
               if l is not None and str(l).strip()
               and not str(l).strip().startswith("#") and str(l).strip() != "---"]
    # 按 "意见" / 序号 / 关键行 提取
    items = []
    for line in cleaned:
        if any(k in line for k in ("意见", "观察到", "建议", "挑战", "验证", "下一步", "风险")):
            # 聚合连续行
            if items and len(items[-1]) < 60 and line.startswith(("**", "-")):
                items[-1] += " " + line.strip("*- ")
            else:
                items.append(line.strip("*- "))
    # 去重 + 截断
    seen = set()
    result = []
    for item in items:
        key = item[:20]
        if key not in seen and len(item) > 10:
            seen.add(key)
            result.append(item[:150])
        if len(result) >= max_items:
            break
    return result if result else cleaned[:max_items]

# ==================== 主教练对话 ====================

@app.post("/api/coach/chat")
def coach_chat(req: CoachChatRequest):
    """主教练对话（提交版：无实验记忆库）"""
    try:
        coach = Coach(call_with_retry)
        response = coach.chat(req.message, None)
        return {"ok": True, "reply": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==================== 评审 ====================

@app.post("/api/review/evaluate")
def review_evaluate(req: ReviewRequest):
    """v3 五角色独立评审（理论家/实践者/批评家/审计员/方法论审计员，双模型）

    ⚠ 2026-10-11 起：**1–10 分这把尺子已下线**（v6 取代），但本端点**逐字保留**——
    原因有二：① 它有 T7 回归测试守着（旧端点结构未变）；② v6 是"并存"，不是"覆盖"。
    前端不再展示它的 score，只保留其"原文证据"部分。
    """
    try:
        result = v3_review(req.report, req.topic)
        return {"ok": True, "review": result, "v3": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 群智评审机制 v6 接入层（**打分尺子下线，改用成对强制选择**）
# ------------------------------------------------------------
# 依据（全部来自你自己的实验报告，引自 04_评审系统实验/评审系统报告/）：
#   · 01_v6实验结果_人话版.md §七：**v3 在同一批 18 对上只挑对 8/18 = 44%，低于瞎猜**。
#     归因已查清 —— **不是它笨，是它的尺子没有刻度**：所有报告分数全挤在 6.4–7.2
#     （跨度 0.8 分），而"判定有差别"的门槛是 0.5 分 → 18 对里 10 对落进"看起来一样"。
#   · 01 §三：v6（成对强制选择）三家 18/18、17/18、17/18 全部达标，且**两轮一致**。
#   · 03_今晚新增_探路与闸门_人话版.md：当初那个"体检没过"的悬案已定因 ——
#     **"是记分记错了，不是模型认不出"**，且已修（核对口径松紧再也左右不了主结论）。
# 设计纪律（v6 预注册，逐条遵守）：
#   ① 不许给自己的东西打分 —— 评审模型必须与生成模型不同族（deepseek）；**不回落**
#   ② 不许拿总分当门面 —— 不用 1–10 分；**锚点未过则一个分数都不给**（scores_hidden）
#   ③ 每条判断必须带原文证据
# 只**新增**端点，不修改任何现有端点行为（上面 /api/review/evaluate 逐字未动）。
# ============================================================

@app.get("/api/review/v6/health")
def review_v6_health():
    """v6 生效证据。

    预注册写明：**启动日志不能作为生效证据（可能被吞）**，
    必须以本端点的可自动断言为准（自带测试 T1 就是断言这个）。
    """
    try:
        import review_v6 as _v6
        reviewers = list(getattr(_v6, "REVIEWERS", []))
    except Exception as e:
        return {"v6": False, "error": f"review_v6 不可导入：{type(e).__name__}: {str(e)[:120]}"}
    # 锚点材料是否就位 —— 缺了会导致 anchors_passed=None → **永远不出分**（静默失效）。
    # 这里主动报出来，让"没分数"这件事在 health 上就能看见，而不是让用户猜。
    _anchor_dir, _anchor_ok, _anchor_n = "", None, 0
    try:
        import review_anchors as _ra
        _anchor_dir = getattr(_ra, "MATERIAL_DIR", "")
        _anchor_n = len(_ra.load_materials())
        _anchor_ok = True
    except Exception as e:
        _anchor_ok, _anchor_dir = False, f"{type(e).__name__}: {str(e)[:120]}"
    return {"v6": True, "reviewers": reviewers, "n_reviewers": len(reviewers),
            "anchors_required": True, "no_fallback": True,
            "anchor_materials_ok": _anchor_ok, "anchor_dir": _anchor_dir,
            "anchor_materials": _anchor_n}


class ReviewV6Request(BaseModel):
    report: str
    topic: str = ""
    baseline: str = ""


@app.post("/api/review/v6")
def review_v6_evaluate(req: ReviewV6Request):
    """v6 评审：成对强制选择 + 分维**三档**（强/中/弱/无依据，不求和）+ 风险栏 + 独有贡献。

    **不回落**：任一评审模型不可用 → 返回 unavailable，绝不悄悄用少数模型顶替。
    **锚点未过** → `scores_hidden=True`，不产出任何分数。
    """
    try:
        import review_v6 as _v6
    except Exception as e:
        return {"ok": False, "v6": False,
                "error": f"review_v6 不可导入：{type(e).__name__}: {str(e)[:200]}"}
    try:
        out = _v6.v6_review(req.report, topic=req.topic,
                            baseline=(req.baseline or None))
        return {"ok": True, "review_v6": out}
    except Exception as e:
        # 硬校验失败（如"锚点未过却产出了分数"）会走到这里 —— 必须如实报错，不得静默
        return {"ok": False, "v6": True,
                "error": f"{type(e).__name__}: {str(e)[:300]}"}


# ============================================================
# 评审插件化（V16.2，2026-10-11）
# ------------------------------------------------------------
# 用户要求：「把这个系统模块化，里面的工具要做成插头即插即用，也方便我们后面的维护」
#          「v3 的保留，就是给用户一个选择的机会，老版本就是快，新版本需要更多时间，让用户自己选」
#
# 做法：所有评审器都是 `app/review_plugins/` 下的**独立插件**，实现同一个契约
#       （PLUGIN 元数据 + available() + run()）。加一个新评审器 = 丢一个文件，
#       **不改本文件任何一行**。本层只负责把插件清单与调用结果转成 HTTP，
#       评审逻辑一行都不在这里 —— 这正是"即插即用"要的边界。
# 旧端点 `/api/review/evaluate`（v3）与 `/api/review/v6` **逐字保留**：
#       ① 有回归测试守着；② 老用户/老脚本仍在调；③ 插件层是新增，不是替换。
# ============================================================

@app.get("/api/review/plugins")
def review_plugins_list():
    """评审插件清单（含**耗时/成本/产出/可用性**，供用户自己权衡"快"与"准"）。"""
    try:
        import review_plugins as RP
        return {"ok": True, "plugins": RP.list_plugins(),
                "load_errors": RP.load_errors()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


class ReviewRunRequest(BaseModel):
    report: str
    topic: str = ""
    baseline: str = ""


@app.post("/api/review/run/{plugin_id}")
def review_run_plugin(plugin_id: str, req: ReviewRunRequest):
    """按插件 id 跑评审（统一入口）。失败一律返回结构化错误，不抛 500。"""
    try:
        import review_plugins as RP
    except Exception as e:
        return {"ok": False, "error": f"review_plugins 不可导入：{type(e).__name__}: {str(e)[:200]}"}
    return RP.run_plugin(plugin_id, req.report, topic=req.topic, baseline=req.baseline or "")


# ============================================================
# 统一插件接口（V16.3，2026-10-11）
# ------------------------------------------------------------
# 用户要求：「我希望不只是评审系统模块化，而是能模块化的都可以这样」
# 所以插件不再只有"评审器"一类，而是按 kind 分组（review / source / …），
# 统一由 `app/plugin_registry.py` 发现与调度。
# **加一类插件**（例如以后把"报告生成器""深批者"也插件化）= 在 KINDS 里加一行 + 建目录，
# 本文件只需再加一个可选入口；**加同类插件**则连这里都不用改。
# ============================================================

@app.get("/api/plugins")
def plugins_list(kind: Optional[str] = None):
    """全部插件（可按 kind 过滤）：评审器 / 检索源 / …，各带耗时成本与可用性。"""
    try:
        import plugin_registry as PR
        return {"ok": True, "kinds": PR.KINDS, "plugins": PR.list_plugins(kind=kind),
                "load_errors": PR.load_errors(kind=kind)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/plugins/sources")
def plugins_sources(kind_free: bool = True):
    """本局实际会跑的检索源（按插件自报的 default_on / applies_when / order 计算）。

    这是"路由也是插件自报"的可核查证据：把源清单与顺序直接暴露出来，
    而不是让路由规则埋在函数里没人看得见。
    """
    try:
        import plugin_registry as PR
        demo = PR.active_sources("示例课题", {"cn_terms": True, "med": False, "effective": False})
        return {"ok": True,
                "default_route_cn": [{"id": a["id"], "name": a["name"], "order": a["order"]}
                                     for a in demo],
                "all_sources": [{k: v for k, v in p.items()
                                 if k in ("id", "name", "desc", "lang", "free", "web",
                                          "eta_sec", "default_on", "order", "applies_when",
                                          "available", "unavailable_reason")}
                                for p in PR.list_plugins(kind="source")]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

# ==================== 课堂模拟器（V15 改造） ====================

# 课堂模拟器实例（复用 classroom_engine）
try:
    from classroom_engine import ClassroomSimulator, TeachingPhase, InstructionRequest
    _classroom = ClassroomSimulator()
    CLASSROOM_OK = True
    print("[课堂模拟器] 引擎加载成功", flush=True)
except Exception as e:
    CLASSROOM_OK = False
    print(f"[课堂模拟器] 引擎加载失败: {e}", flush=True)


@app.get("/classroom")
def classroom_page():
    """课堂模拟器前端"""
    page = os.path.join(V15_ROOT, "app", "templates", "classroom.html")
    if os.path.exists(page):
        return FileResponse(page)
    return {"ok": CLASSROOM_OK, "hint": "classroom.html 未找到"}


@app.post("/api/classroom/teach")
def classroom_teach(req: dict):
    """老师输入教学指令 → 学生响应 + 课堂反馈"""
    if not CLASSROOM_OK:
        raise HTTPException(status_code=500, detail="课堂引擎未加载")
    try:
        instruction = req.get("instruction", "")
        phase_str = req.get("phase", "导入")
        phase = TeachingPhase(phase_str) if phase_str in [p.value for p in TeachingPhase] else TeachingPhase.INTRODUCTION
        result = _classroom.process_instruction(instruction, phase)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/classroom/students")
def classroom_students():
    if not CLASSROOM_OK:
        return {"ok": False, "students": []}
    return {"ok": True, "students": _classroom.get_student_states()}


@app.post("/api/classroom/reset")
def classroom_reset():
    if CLASSROOM_OK:
        _classroom.reset()
        return {"ok": True}
    return {"ok": False}


# ==================== 用户反馈（比赛实证材料） ====================

class FeedbackRequest(BaseModel):
    topic: str = ""
    rating: int = 5
    comment: str = ""
    session_id: str = ""

@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest):
    """同学体验后提交反馈（打分+留言）；对取值范围与长度做校验，防脏数据污染答辩统计"""
    try:
        rating = int(req.rating)
    except Exception:
        raise HTTPException(status_code=400, detail="评分必须是 1-5 的整数")
    if not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="评分必须在 1-5 之间")
    comment = (req.comment or "").strip()
    if len(comment) > 500:
        comment = comment[:500]
    topic = (req.topic or "").strip()[:200]
    try:
        from feedback import save_feedback
        result = save_feedback(topic, rating, comment, (req.session_id or "")[:64])
        return {"ok": True, "stats": result.get("stats", {})}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/feedback/stats")
def feedback_stats():
    """反馈统计（答辩用）"""
    try:
        from feedback import get_feedback
        d = get_feedback()
        return {"ok": True, "stats": d.get("stats", {}), "total": d.get("stats", {}).get("total", 0),
                "list": d.get("feedback", [])[-20:]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==================== 健康检查 ====================

@app.get("/health")
def health():
    """健康检查：反映各模块真实加载状态与配置情况（便于排查"为什么跑不起来"）"""
    engine_ok = engine is not None
    try:
        from cnki_search import COOKIE_PATH
        cnki_cookie = os.path.exists(COOKIE_PATH)
    except Exception:
        cnki_cookie = False
    cfg = {}
    try:
        cfg["deepseek_key"] = _valid_key_label(engine.DEEPSEEK_API_KEY_1) if engine_ok else False
        cfg["glm_key"] = bool(getattr(engine, "GLM_API_KEY", ""))
        cfg["qwen_key"] = bool(getattr(engine, "QWEN_API_KEY", ""))
        cfg["bocha_key"] = bool(os.environ.get("BOCHA_API_KEY", ""))
    except Exception:
        pass
    return {
        "status": "ok",
        "service": "群智 V15",
        "modules": {
            "engine(讨论引擎)": engine_ok,
            "coach(主教练联网)": True,
            "review(v3五角色评审)": True,
            "classroom(课堂模拟器)": bool(CLASSROOM_OK),
            "cnki_cookie(知网凭据)": cnki_cookie,
        },
        "keys_configured": cfg,
        "warning": getattr(engine, "_KEY_WARNING", "") if engine_ok else "引擎未加载",
    }


def _valid_key_label(k: str) -> bool:
    """判断 key 是否为有效形态（非占位符）——与 sandbox/app.py 的 _valid_key 同口径"""
    try:
        k.encode("ascii")
    except Exception:
        return False
    low = (k or "").lower()
    return len(k or "") >= 12 and not any(b in low for b in ("your", "replace", "placeholder", "xxx"))



# ==================== 构造判据集盲评（2026-10-10）====================
# 用途：给「干预信号尺子替换」实验的构造判据集做**独立双人盲评**。
# 页面本身**不含构造脚本、也不含任何尺子读数**——只有「组别 + 标签 + 定义 + 文本」，
# 顺序由固定种子打乱，两位核验人看到同一顺序，便于配对算 Cohen's κ。
_BLIND_DIR = os.path.join(V15_ROOT, "data", "blind")
os.makedirs(_BLIND_DIR, exist_ok=True)


@app.get("/blind")
async def blind_page():
    """盲评页（挂在 /static 下，裸路径也给一个入口）"""
    p = os.path.join(_static_dir, "blind_review.html")
    if not os.path.exists(p):
        return HTMLResponse(u"<h1>盲评页未部署</h1><p>缺少 app/static/blind_review.html</p>",
                            status_code=404)
    return FileResponse(p)


@app.post("/api/blind/submit")
async def blind_submit(request: Request):
    """接收一份标注结果并落盘（标注人不用回传文件）"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    raw = str(body.get("verifier") or "anon")
    ver = re.sub(r"[^\w\u4e00-\u9fa5-]", "", raw)[:40] or "anon"
    body["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    body["_ip"] = (request.client.host if request.client else "")
    fname = "%s_%s.json" % (ver, time.strftime("%Y%m%d_%H%M%S"))
    path = os.path.join(_BLIND_DIR, fname)
    if os.path.exists(path):                       # 同一秒重复提交不覆盖
        fname = "%s_%s_%s.json" % (ver, time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4])
        path = os.path.join(_BLIND_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    return {"ok": True, "saved": fname, "n": len(body.get("answers") or {}),
            "expected": body.get("items")}


@app.get("/api/blind/list")
async def blind_list():
    """看当前收到了几份、每份标了多少条"""
    out = []
    for fn in sorted(os.listdir(_BLIND_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(_BLIND_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        out.append({"file": fn, "verifier": d.get("verifier"),
                    "n": len(d.get("answers") or {}), "items": d.get("items"),
                    "at": d.get("_saved_at")})
    return {"count": len(out), "dir": _BLIND_DIR, "submissions": out}


@app.get("/")
def index():
    """前端控制台"""
    console_path = os.path.join(V15_ROOT, "app", "templates", "console_new.html")
    if os.path.exists(console_path):
        return FileResponse(console_path)
    return {"service": "群智 · 科研决策预演智能体 V15", "docs": "/docs", "health": "/health"}

if __name__ == "__main__":
    import uvicorn
    print("🚀 群智 V15 网关启动：http://127.0.0.1:%s" % os.getenv("QZ_PORT", "5020"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("QZ_PORT", "5020")))
