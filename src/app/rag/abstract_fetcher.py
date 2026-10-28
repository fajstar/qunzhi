# -*- coding: utf-8 -*-
"""
知网论文摘要抓取模块 abstract_fetcher.py
========================================
合规边界：仅抓取【摘要】（kcms2/article/abstract 详情页），不下载全文。
- 3 路并发抓取 + 1.2s 合规限速
- 摘要缺失时返回 None（不硬造）
"""
import os, sys, re, json, time, random, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import html as html_mod

try:
    import requests
except ImportError:
    raise SystemExit("需要 requests")

BASE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0")

_MIN_INTERVAL = 1.5      # 基础间隔（合规限速，模拟人操作）
_JITTER = 0.4            # ±40% 随机抖动，避免固定节奏被识别
_DAILY_LIMIT = 300       # 每日请求上限（防封核心）
_DAILY_FILE = os.path.join(BASE, ".cnki_daily.json")

_last_call = 0.0
_lock = None


def _get_daily_count() -> int:
    """读取今日请求计数（跨进程共享）"""
    today = time.strftime("%Y-%m-%d")
    try:
        if os.path.exists(_DAILY_FILE):
            with open(_DAILY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today:
                return int(data.get("count", 0))
    except Exception:
        pass
    return 0


def _incr_daily_count(n: int = 1) -> None:
    today = time.strftime("%Y-%m-%d")
    try:
        cnt = _get_daily_count() + n
        with open(_DAILY_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today, "count": cnt}, f)
    except Exception:
        pass


def _over_daily_limit() -> bool:
    return _get_daily_count() >= _DAILY_LIMIT


def _throttle():
    """限速 + 随机抖动；超配额直接抛异常停止"""
    global _last_call, _lock
    if _over_daily_limit():
        raise RuntimeError(f"已达今日知网请求上限({_DAILY_LIMIT})，明日自动恢复")
    if _lock is None:
        import threading
        _lock = threading.Lock()
    with _lock:
        now = time.time()
        # 基础间隔 × (1 ± 抖动)
        wait = _MIN_INTERVAL * (1 + random.uniform(-_JITTER, _JITTER)) - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    _incr_daily_count()


def _find_cookie_path() -> str:
    candidates = [
        os.path.join(BASE, "..", "cookies", "cnki.json"),
        os.path.join(BASE, "cookies", "cnki.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.normpath(p)
    return os.path.join(BASE, "cookies", "cnki.json")


def _cookie_str(cookies: dict) -> str:
    parts = []
    for k, v in cookies.items():
        v = str(v)
        if any(ord(c) > 127 for c in v):
            v = urllib.parse.quote(v, safe="")
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def fetch_abstract(url: str, cookies: dict = None, session=None) -> str:
    """抓取单篇论文摘要（详情页），失败/无摘要返回 None。
    关键：需带 kns8s 检索页 Referer + 完整 cookie 会话（绕过安全验证，模拟浏览器正常行为）。
    session: 可选，复用已有 Session（含 kns8s 会话 cookie）
    防封：失败退避（429/验证码 → 指数退避，连续3次停止）
    """
    if not url or "abstract" not in url:
        return None
    if cookies is None:
        try:
            with open(_find_cookie_path(), "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception:
            return None
    # 失败退避
    backoff = 2.0
    consecutive_fail = 0
    for attempt in range(4):
        try:
            _throttle()
        except RuntimeError as qe:
            print(f"  [配额] {qe}", flush=True)
            return None
        try:
            sess = session if session is not None else requests.Session()
            r = sess.get(url, headers={
                "User-Agent": UA,
                "Cookie": _cookie_str(cookies),
                "Referer": "https://kns.cnki.net/kns8s/defaultresult/index",
            }, timeout=15)
            # 429/403/验证码 → 退避重试
            if r.status_code in (429, 403):
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    print("  [防封] 连续3次被拦，暂停知网抓取", flush=True)
                    return None
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status_code != 200:
                return None
            if "安全验证" in r.text or "/verify/" in r.text:
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    print("  [防封] 触发安全验证，暂停知网抓取", flush=True)
                    return None
                time.sleep(backoff)
                backoff *= 2
                continue
            consecutive_fail = 0
            text = r.text
            # 摘要通常在 <div class="abstract-text"> / <span class="abstract-text">
            m = re.search(r'class="abstract-text"[^>]*>(.*?)</(?:div|span|p)>', text, re.S)
            if not m:
                m = re.search(r'class="briefDIV"[^>]*>(.*?)</div>', text, re.S)
            if not m:
                m = re.search(r'<label[^>]*>摘要[^<]*</label>\s*<[^>]*>(.*?)</(?:div|p|span)>', text, re.S)
            if not m:
                m = re.search(r'<p[^>]*class="[^"]*abstract[^"]*"[^>]*>(.*?)</p>', text, re.S)
            if not m:
                return None
            content = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
            content = re.sub(r"\s+", " ", content).strip()
            content = re.sub(r"^【?摘要】?[:：]?\s*", "", content)
            return content if len(content) >= 10 else None
        except Exception:
            consecutive_fail += 1
            if consecutive_fail >= 3:
                return None
            time.sleep(backoff)
            backoff *= 2
    return None


def fetch_abstracts(urls: list, max_workers: int = 1, session=None, cookies=None) -> dict:
    """抓取多篇摘要：{url: abstract 或 None}
    session: 可选，复用已有 requests.Session（带 kns8s 会话 cookie 更稳）
    cookies: 可选，登录 cookie（默认自动读文件）
    防封：默认单线程（max_workers=1）+ 1.5s 抖动限速
    """
    if not urls:
        return {}
    if cookies is None:
        try:
            with open(_find_cookie_path(), "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception:
            pass
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_abstract, u, cookies, session): u for u in urls}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception:
                results[url] = None
    return results


if __name__ == "__main__":
    # 自测：用一篇真实论文 URL
    test_url = ("https://kns.cnki.net/kcms2/article/abstract?v=WP6tkxloIPsn9_2b9DpaNpy0OC9KTH-l6DthW0qSpBboca09n65JWi7VOyDVtsIFxRdaszDKY0rk9rr9dXG0sbMTUTwrlYPtZLaqoVDxFbTnM3gYbaieVM0sOUvh37kt2JyyuiTw9I6fdJ6aspU_pMqlNEUwki644CzUcpKQaHnjciUMeAc2fw==&uniplatform=NZKPT&language=CHS")
    print("抓取摘要中...")
    abstract = fetch_abstract(test_url)
    if abstract:
        print("[OK] 摘要:", abstract[:150])
    else:
        print("[FAIL] 未抓到摘要")
