# -*- coding: utf-8 -*-
"""
公网访问防护中间件 guard.py
============================
公网暴露 v15 的防护：
1. 访问口令：需要 ?key=xxx 或 header X-Access-Key（防陌生人乱用）
2. IP 限流：每 IP 每 60s 最多 N 次启动预演（防刷爆 API 额度）
3. 全局限流：每 IP 总请求频率限制
"""
import os
import time, threading
from fastapi import Request, HTTPException

# 访问口令：部署时用环境变量 QUNZHI_ACCESS_KEY 设置
# 仓库里不放真值（原值见部署文档，或自己重新生成一个）
ACCESS_KEY = os.environ.get("QUNZHI_ACCESS_KEY", "CHANGE_ME_ACCESS_KEY")

# 启动预演限流：每 IP 60s 内最多 N 次
# 注：评审期间多名评委可能在同一间办公室/同一 NAT 出口 IP 下同时体验，
#     原来的 3 次/60s 会让第 4 个人直接被挡（429）——对评审体验是负面印象。
#     这里放宽到 8 次/60s，同时保留上限以防 API 额度被刷。
_START_WINDOW = 60
_START_MAX = 8
# 全局限流：每 IP 10s 内最多 N 次请求（页面轮询/SSE 重连较频繁，留足余量）
_GLOBAL_WINDOW = 10
_GLOBAL_MAX = 40

_lock = threading.Lock()
_start_hits = {}   # ip -> [timestamps]
_global_hits = {}  # ip -> [timestamps]


def _client_ip(request: Request) -> str:
    """取客户端 IP（穿透后取真实 IP：CF-Connecting-IP）"""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_access(request: Request):
    """访问校验：口令 + 限流。不通过抛 HTTPException"""
    ip = _client_ip(request)

    # 1. 口令校验（API 接口要求，静态页面放行）
    path = request.url.path
    if path.startswith("/api/"):
        key = request.headers.get("X-Access-Key") or request.query_params.get("key")
        if key != ACCESS_KEY:
            raise HTTPException(status_code=401, detail="需要访问口令（X-Access-Key 或 ?key=）")

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

        # 3. 启动预演限流（防刷 API）
        if path == "/api/sandbox/start":
            s = _start_hits.setdefault(ip, [])
            s[:] = [t for t in s if now - t < _START_WINDOW]
            if len(s) >= _START_MAX:
                raise HTTPException(
                    status_code=429,
                    detail=f"启动预演过于频繁（同一网络 {_START_WINDOW} 秒内最多 {_START_MAX} 次）。"
                           "若多位体验者共用同一出口网络，请稍等约 1 分钟再试。")
            s.append(now)


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
