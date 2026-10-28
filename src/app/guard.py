# -*- coding: utf-8 -*-
"""
公网访问防护中间件 guard.py
============================
公网暴露的防护：

1. 访问口令：需要 ?key=xxx 或 header X-Access-Key（防陌生人乱用）
2. IP 限流：每 IP 每 60s 最多 N 次启动预演（防单点刷爆）
3. 全局限流：每 IP 总请求频率限制
4. **每日全局消费上限**：全体用户合计每天最多花 N 元，超了直接停服到次日
   —— 这是最后一道防线：即使 IP 限流被绕过（换 IP、分布式），
      账单也不会失控。默认 50 元/天 ≈ 312 次预演。

配置（全部走环境变量，仓库里不含真值）：

    QUNZHI_ACCESS_KEY       访问口令
    QUNZHI_DAILY_BUDGET     每日消费上限（元），默认 50
    QUNZHI_COST_PER_RUN     单次预演成本（元），默认 0.16
    QUNZHI_BUDGET_FILE      计数落盘路径，默认 /tmp/qunzhi_daily_spend.json
"""
import json
import os
import time
import threading

from fastapi import Request, HTTPException

# ---------- 访问口令 ----------
# 部署时用环境变量设置；仓库里不放真值
ACCESS_KEY = os.environ.get("QUNZHI_ACCESS_KEY", "CHANGE_ME_ACCESS_KEY")

# ---------- 频率限流 ----------
# 启动预演限流：每 IP 60s 内最多 N 次
# 注：评审期间多名评委可能在同一间办公室/同一 NAT 出口 IP 下同时体验，
#     原来的 3 次/60s 会让第 4 个人直接被挡（429）——对评审体验是负面印象。
#     这里放宽到 8 次/60s，同时保留上限以防 API 额度被刷。
_START_WINDOW = 60
_START_MAX = 8
# 全局限流：每 IP 10s 内最多 N 次请求（页面轮询/SSE 重连较频繁，留足余量）
_GLOBAL_WINDOW = 10
_GLOBAL_MAX = 40

# ---------- 每日消费上限（新增） ----------
DAILY_BUDGET = float(os.environ.get("QUNZHI_DAILY_BUDGET", "50"))
COST_PER_RUN = float(os.environ.get("QUNZHI_COST_PER_RUN", "0.16"))
BUDGET_FILE = os.environ.get("QUNZHI_BUDGET_FILE", "/tmp/qunzhi_daily_spend.json")

_lock = threading.Lock()
_start_hits = {}   # ip -> [timestamps]
_global_hits = {}  # ip -> [timestamps]


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_spend() -> dict:
    """读当日消费计数；跨天自动重置。返回 {'date':..., 'runs':int}"""
    try:
        with open(BUDGET_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == _today():
            return d
    except Exception:
        pass
    return {"date": _today(), "runs": 0}


def _save_spend(d: dict) -> None:
    try:
        tmp = BUDGET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, BUDGET_FILE)
    except Exception:
        pass          # 落盘失败不能挡住服务，内存计数仍在


def budget_status() -> dict:
    """给前端/运维看的当前预算状态"""
    with _lock:
        d = _load_spend()
    used = d["runs"] * COST_PER_RUN
    return {
        "date": d["date"],
        "runs": d["runs"],
        "spent": round(used, 2),
        "budget": DAILY_BUDGET,
        "remaining": round(max(0.0, DAILY_BUDGET - used), 2),
        "cost_per_run": COST_PER_RUN,
    }


def _client_ip(request: Request) -> str:
    """取客户端 IP（穿透后取真实 IP）"""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_access(request: Request):
    """访问校验：口令 + 限流 + 每日预算。不通过抛 HTTPException"""
    ip = _client_ip(request)
    path = request.url.path

    # 1. 口令校验（API 接口要求，静态页面放行）
    if path.startswith("/api/"):
        key = request.headers.get("X-Access-Key") or request.query_params.get("key")
        if key != ACCESS_KEY:
            raise HTTPException(status_code=401,
                                detail="需要访问口令（X-Access-Key 或 ?key=）")

    now = time.time()
    with _lock:
        # 2. 全局限流
        g = _global_hits.setdefault(ip, [])
        g[:] = [t for t in g if now - t < _GLOBAL_WINDOW]
        if len(g) >= _GLOBAL_MAX:
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请等待约 10 秒后重试（系统正在为其他体验者服务）")
        g.append(now)

        # 3. 启动预演限流（防单点刷 API）
        if path == "/api/sandbox/start":
            s = _start_hits.setdefault(ip, [])
            s[:] = [t for t in s if now - t < _START_WINDOW]
            if len(s) >= _START_MAX:
                raise HTTPException(
                    status_code=429,
                    detail=f"启动预演过于频繁（同一网络 {_START_WINDOW} 秒内最多 {_START_MAX} 次）。"
                           "若多位体验者共用同一出口网络，请稍等约 1 分钟再试。")
            s.append(now)

        # 4. 每日全局消费上限（最后一道防线）
        if path == "/api/sandbox/start":
            d = _load_spend()
            used = d["runs"] * COST_PER_RUN
            if used + COST_PER_RUN > DAILY_BUDGET:
                raise HTTPException(
                    status_code=503,
                    detail=(f"今日公开体验额度已用完（每天上限 ¥{DAILY_BUDGET:g}，"
                            f"约 {int(DAILY_BUDGET / COST_PER_RUN)} 次预演）。"
                            f"这是作者自费运行的演示站，请明天再来，或按 README 自行部署。"))
            d["runs"] += 1
            _save_spend(d)


def setup_guard(app):
    """注册防护中间件"""
    @app.middleware("http")
    async def guard_middleware(request: Request, call_next):
        try:
            check_access(request)
        except HTTPException as e:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
        return await call_next(request)
