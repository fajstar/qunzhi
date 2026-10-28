# -*- coding: utf-8 -*-
"""
知网检索模块 cnki_search.py
============================
合规边界：仅检索题录+摘要，不下载全文；限速+抖动+每日配额防封。
"""
import os, sys, re, json, time, urllib.parse
import html as html_mod

try:
    import requests
except ImportError:
    raise SystemExit("需要 requests：pip install requests")

BASE = os.path.dirname(os.path.abspath(__file__))


def _find_cookie_path() -> str:
    """cookie 路径收敛到 v15 项目内（禁止读取项目外副本）"""
    candidates = [
        os.path.join(BASE, "..", "cookies", "cnki.json"),   # sandbox_v15/cookies（正式）
        os.path.join(BASE, "cookies", "cnki.json"),          # 模块同级（备用）
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.normpath(p)
    return os.path.join(BASE, "..", "cookies", "cnki.json")


COOKIE_PATH = _find_cookie_path()
GRID_URL = "https://kns.cnki.net/kns8s/brief/grid"
INDEX_URL = "https://kns.cnki.net/kns8s/defaultresult/index"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0")

_MIN_INTERVAL = 1.2   # 合规限速：模拟人操作（±40%抖动）
_JITTER = 0.4
_last_call = 0.0
_limit_lock = None


def _throttle():
    global _last_call, _limit_lock
    import random as _r
    now = time.time()
    # 基础间隔 × (1 ± 40% 抖动)，模拟人操作节奏
    wait = _MIN_INTERVAL * (1 + _r.uniform(-_JITTER, _JITTER)) - (now - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _load_cookies():
    if not os.path.exists(COOKIE_PATH):
        return None
    with open(COOKIE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _cookie_str(cookies):
    parts = []
    for k, v in cookies.items():
        v = str(v)
        if any(ord(c) > 127 for c in v):      # 中文 cookie 值百分号编码（latin-1 限制）
            v = urllib.parse.quote(v, safe="")
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def _extract_checked_db(sess, cookies) -> str:
    """从检索页提取 CheckedDB（跨库数据库 ID 列表，KuaKuCode 必需）"""
    _throttle()
    r = sess.get(INDEX_URL + "?kw=" + urllib.parse.quote("人工智能") + "&korder=SU",
                 headers={"User-Agent": UA, "Cookie": _cookie_str(cookies),
                          "Referer": "https://kns.cnki.net/"}, timeout=15)
    m = re.search(r'id="CheckedDB"\s+value="([^"]*)"', r.text)
    return m.group(1) if m else ""


def _build_query_json(kw: str, checked_db: str) -> str:
    """构造 SearchQueryState（QNode 树，SIMPLE 检索，KuaKuCode=CheckedDB 跨库）"""
    state = {
        "QNode": {"QGroup": [{
            "Key": "Subject", "Title": "", "Logic": 0,
            "Items": [{"Key": "", "Title": "", "Logic": 0,
                       "Field": "SU", "Operator": 1,
                       "Value": kw, "Value2": ""}],
            "ChildItems": []
        }]},
        "ExScope": "0", "SimpTrad": "0", "SearchType": "2",
        "Rlang": "CHINESE", "KuaKuCode": checked_db, "Expands": {},
        "Resource": "CROSSDB", "Classid": "WD0FTY92", "Products": ""
    }
    return json.dumps(state, ensure_ascii=False)


def _strip_tags(s):
    return html_mod.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _parse_papers(html_text: str, max_n: int = 10) -> list:
    """解析 grid 返回的表格 HTML → 论文列表"""
    papers = []
    # 只取 tbody 内的数据行（跳过 thead）
    tbody = re.search(r"<tbody>(.*?)</tbody>", html_text, re.S)
    body = tbody.group(1) if tbody else html_text
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
    for row in rows:
        if "class=\"seq\"" not in row and "class='seq'" not in row:
            continue
        tm = re.search(r'class=["\']name["\'][^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', row, re.S)
        if not tm:
            continue
        title = _strip_tags(tm.group(2))
        href = tm.group(1).replace("&amp;", "&")
        # 作者（多个 <a>）
        am = re.search(r"class=['\"]author['\"][^>]*>(.*?)</td>", row, re.S)
        authors = []
        if am:
            authors = [_strip_tags(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", am.group(1), re.S)]
        # 来源（期刊/会议）
        sm = re.search(r"class=['\"]source['\"][^>]*>(.*?)</td>", row, re.S)
        source = _strip_tags(sm.group(1)) if sm else ""
        # 日期
        dm = re.search(r"class=['\"]date['\"][^>]*>(.*?)</td>", row, re.S)
        date = _strip_tags(dm.group(1)) if dm else ""
        # 被引
        cm = re.search(r"class=['\"]citation['\"][^>]*>(.*?)</td>", row, re.S)
        cite = _strip_tags(cm.group(1)) if cm else ""
        papers.append({
            "title": title,
            "authors": authors,
            "source": source,
            "date": date,
            "cite": cite,
            "url": href,
        })
        if len(papers) >= max_n:
            break
    return papers


def _extract_core_keywords(keyword: str) -> list:
    """长句切核心词，返回多级候选（按优先级）：
    知网 SU 分词 AND 对长句过严，长句易 0 命中。
    拆分：标点/连词（与和或及）→ 去停用词 → 保留完整词。
    返回候选列表，如 ["A B", "A", "A短前缀"]，调用方逐个尝试。
    """
    STOP = set(
        "的了与和或及在是就都而则把被对从向为以于之吗呢吧啊呀"
        "将可权使用进行应该如何怎样能否通过利用实现设计一个我们你们他们这些那些"
        "如何什么怎么为什么哪里是否能否怎样哪些该还"
    )
    # 1. 按标点/连词拆段
    segments = re.split(r"[，。？！；：、,;:!?]|\s+|与|和|或|及", keyword)
    segs = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        cleaned = "".join(ch for ch in seg if ch not in STOP)
        if len(cleaned) >= 2:
            segs.append(cleaned)
    if not segs:
        cleaned = "".join(ch for ch in keyword if ch not in STOP)
        segs = [cleaned] if cleaned else [keyword]

    candidates = []
    # 候选1：最多 2 段空格连接（每段截 8 字符）
    two = " ".join(s[:8] for s in segs[:2])
    if two:
        candidates.append(two)
    # 候选2：只取第一段
    if segs:
        candidates.append(segs[0][:8])
    # 候选3：第一段更短前缀（6 字符）
    if segs and len(segs[0]) > 6:
        candidates.append(segs[0][:6])
    # 去重保序
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _do_search(sess, cookies, keyword, checked_db) -> tuple:
    """执行一次检索，返回 (papers, error)"""
    query_json = _build_query_json(keyword, checked_db)
    params = {
        "boolSearch": "false",
        "QueryJson": query_json,
        "pageNum": "1", "pageSize": "20",
        "sortField": "", "sortType": "",
        "dstyle": "list",
        "boolSortSearch": "false", "sentenceSearch": "false",
        "productStr": "", "aside": keyword, "searchFrom": keyword,
        "manageId": "", "subject": "", "turnpage": "",
    }
    headers = {
        "User-Agent": UA,
        "Cookie": _cookie_str(cookies),
        "Referer": f"{INDEX_URL}?kw={urllib.parse.quote(keyword)}&korder=SU",
        "Origin": "https://kns.cnki.net",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    _throttle()
    try:
        r = sess.post(GRID_URL, headers=headers, data=params, timeout=25)
    except Exception as e:
        return None, f"知网检索请求失败: {e}"
    if r.status_code != 200 or "暂无数据" in r.text:
        return None, "知网返回暂无数据"
    papers = _parse_papers(r.text, 20)
    if not papers:
        return None, "解析结果为空"
    return papers, None


def _filter_by_recency(papers: list, recent_years: int = 3) -> list:
    """时效过滤 + 标题去重 + 低质来源预筛（V15.5）

    规则：
      1. 保留最近 N 年文献；若不足 3 条，放宽到 5 年（保证可用性）
      2. 按标题去重（知网同一篇可能重复出现）
      3. 剔除明显低质来源（报纸/资讯类期刊）
    """
    import datetime as _dt
    cur_year = _dt.datetime.now().year
    min_year = cur_year - recent_years

    # 低质来源关键词（检索层预筛，入库层还有一道更严格的筛选）
    LOW_QUALITY = [
        "传播力研究", "汽车实用技术", "汽车文摘", "上海汽车", "科技风",
        "科技资讯", "才智", "现代商贸工业", "中国市场", "营销界",
        "纳税", "经济研究导刊", "科技经济导刊", "视界观", "魅力中国",
        "中国新通信", "数码世界", "信息记录材料", "写真地理", "电脑迷",
    ]
    # V15（2026-10-10）：**教辅/科普/生活/党刊类来源**——CNKI 索引大量这类刊物，
    # 它们不是研究文献。实测混入过：家庭教育、好作文、语文世界、小学生作文辅导、
    # 知识就是力量、妇女生活、共产党员、河北教育(德育版) 等。
    # 用户要求"只用论文、避免爆雷"，故这类来源在只收论文模式下被剔除。
    POPULAR_SRC = [
        "作文", "辅导", "语文世界", "知识就是力量", "妇女生活", "家庭教育", "共产党员",
        "读者", "育儿", "故事", "画报", "文摘", "科普", "百科", "天地", "博览",
        "之友", "生活", "健康", "时尚", "美食", "旅游", "娱乐", "体育",
        "中学生", "小学生", "幼儿", "少年", "家长",
    ]
    NEWS_LIKE = ["报", "日报", "晚报", "时报", "资讯", "新闻"]

    def _year_of(p):
        m = re.match(r"(\d{4})", str(p.get("date") or ""))
        return int(m.group(1)) if m else 0

    def _is_low_quality(p):
        src = (p.get("source") or "").strip()
        if any(k in src for k in LOW_QUALITY):
            return True
        # V15：教辅/科普/生活类（学术刊常含"学报/研究/教育"等，不会命中这些词）
        if any(k in src for k in POPULAR_SRC):
            return True
        # 明显的报纸/资讯类（但避免误伤"学报""导报"等学术刊）
        if any(src.endswith(k) or (k in src and "学报" not in src and "导报" not in src)
               for k in NEWS_LIKE):
            return True
        return False

    # 去重 + 预筛
    seen_titles, uniq = set(), []
    for p in papers:
        t = (p.get("title") or "").strip()
        key = t[:24]
        if not t or key in seen_titles:
            continue
        seen_titles.add(key)
        if _is_low_quality(p):
            continue
        uniq.append(p)

    # 时效过滤（近 N 年）
    recent = [p for p in uniq if _year_of(p) >= min_year]
    # 近 N 年不足 3 条 → 放宽到 5 年
    if len(recent) < 3:
        recent = [p for p in uniq if _year_of(p) >= cur_year - 5]
    # V15（2026-10-10）：**严格时效**——不再回退到"全部老文献"。
    # 旧行为：近 3 年不足 → 放宽 5 年 → 仍不足 → 返回全部（于是 2011/2013 年的老文献会进池）。
    # 用户要求"只用论文、宁可少也不要爆雷"，故此处宁可为空，由上层多源检索补齐。
    # 如需恢复旧行为：设环境变量 QUNZHI_STRICT_RECENCY=0。
    if not recent and os.getenv("QUNZHI_STRICT_RECENCY", "1") != "0":
        print(f"[知网·严格时效] 近 {recent_years} 年（已放宽至 5 年）无结果，按严格模式返回空", flush=True)
        return []

    # 仍不足 → 返回去重后的全部（仅在非严格模式下）
    if not recent:
        recent = uniq

    # 按年份降序（新的在前）
    recent.sort(key=_year_of, reverse=True)
    return recent


def cnki_search(keyword: str, max_results: int = 10,
                recent_years: int = 3, fetch_abstracts: bool = False,
                abstract_max: int = 6) -> dict:
    """知网检索入口（元数据级 + 可选摘要）：
    返回 {ok, keyword, count, papers, source: "cnki", error?}

    V15.5 增强：
      - recent_years：只保留最近 N 年文献（默认 3 年）；近 N 年不足 3 条时放宽到 5 年
      - 标题去重（知网同一篇可能在不同栏目重复出现）
      - 长句自动切核心词重试（知网 SU 分词 AND 对长句过严）
    全部失败时 ok=False，供上层降级到博查。

    V16（2026-10-11，修 1·证据链）：
      - **fetch_abstracts=True 时在模块内部抓详情页摘要**，每篇返回 `abstract` 纯文本字段。
        为什么放在这里而不是把 session 交给调用方：session/cookies 是登录凭据、且不可 JSON
        序列化，**不该出模块**（本文件原有的安全注释即此意）。而摘要必须在持有 session 时抓，
        所以由本函数自己完成。
      - 抓不到的条目 `abstract` 为空串，并置 `abstract_missing=True`，
        供上层如实披露「该条只有题录」——这是 B7 蕴含核验能否判定的前提。
        实证背景：线上注入池 8/8 条 `content` 只有「标题；作者；来源；发表时间」54–65 字，
        `abstract` 全空，导致报告里的 [Pn] 论断无从核对（B7 实测 18/18 not_checkable）。
    """
    cookies = _load_cookies()
    if not cookies:
        return {"ok": False, "error": "cookies/cnki.json 不存在", "papers": []}

    sess = requests.Session()
    # 预热：首页（拿会话）
    try:
        _throttle()
        sess.get("https://www.cnki.net/",
                 headers={"User-Agent": UA, "Cookie": _cookie_str(cookies)}, timeout=15)
    except Exception as e:
        return {"ok": False, "error": f"知网首页访问失败: {e}", "papers": []}

    # 1. 拿 CheckedDB
    try:
        checked_db = _extract_checked_db(sess, cookies)
    except Exception as e:
        return {"ok": False, "error": f"CheckedDB 提取失败: {e}", "papers": []}

    def _finalize(raw_papers, used_kw=None):
        """时效过滤（近 N 年）+ 标题去重 + **刊级优先排序**（+ 可选摘要抓取）"""
        got = _filter_by_recency(raw_papers, recent_years=recent_years)
        # V16（修 3）：**刊级优先**。
        # 背景（用户提问"为什么查到的全是非核心期刊"）：三个原因里
        #   ① NCPSSD 打不开（0/6 命中，未修）② 知网检索**不限刊级**
        #   ③ 近 3 年时效过滤偏向发文量大的普通期刊。
        # 关于②：知网服务端过滤走加密的 `productStr`，没有实值不敢猜（猜错会直接 0 命中）。
        # 因此改用**可保证生效**的做法：拿到结果后按刊级排序，核心/CSSCI 排前面优先占名额，
        # 并统计条数——**没找到核心期刊时如实报告**，而不是让用户以为"只有普通期刊"。
        _n_core, _n_thesis = 0, 0
        try:
            import citations as _c
            for p in got:
                _c.ensure_journal_fields(p)
                _t = int(p.get("journal_tier") or 0)
                if p.get("type") == "thesis":
                    _n_thesis += 1
                elif _t >= 1:
                    _n_core += 1
            got.sort(key=lambda x: -int(x.get("journal_tier") or 0))
        except Exception as _e:
            _n_core = -1
            print(f"[知网·刊级] 级别判定跳过（{str(_e)[:60]}）", flush=True)
        got = got[:max_results]
        # V16：在持有 session 时就地抓摘要（session 不出本模块）
        if fetch_abstracts and got:
            _n_abs = 0
            try:
                from abstract_fetcher import fetch_abstracts as _fa
                _urls = [p.get("url") for p in got[:abstract_max] if p.get("url")]
                if _urls:
                    _amap = _fa(_urls, max_workers=3, session=sess, cookies=cookies) or {}
                    for p in got:
                        _a = (_amap.get(p.get("url")) or "").strip()
                        p["abstract"] = _a
                        if _a:
                            _n_abs += 1
                        else:
                            p["abstract_missing"] = True
            except Exception as _e:
                for p in got:
                    p.setdefault("abstract", "")
                    p["abstract_missing"] = True
                print(f"[知网·摘要] 抓取失败({str(_e)[:60]})，本批仅有题录", flush=True)
            if _n_abs:
                print(f"[知网·摘要] 抓到 {_n_abs}/{len(got)} 条摘要", flush=True)
            else:
                print(f"[知网·摘要] 0/{len(got)} 条，本批仅有题录（下游须如实披露）", flush=True)
        out = {"ok": True, "keyword": keyword,
               "count": len(got), "papers": got, "source": "cnki",
               "recency_years": recent_years,
               "raw_count": len(raw_papers),
               "core_journal_count": _n_core,
               "thesis_count": _n_thesis,
               "abstracts_fetched": sum(1 for p in got if p.get("abstract"))}
        if _n_core == 0:
            print(f"[知网·刊级] ⚠ 本批 {len(got)} 条**无核心期刊**"
                  f"（学位论文 {_n_thesis} 条）——报告应如实说明来源层次", flush=True)
        elif _n_core > 0:
            print(f"[知网·刊级] 核心期刊 {_n_core} 条（已排前）｜学位论文 {_n_thesis} 条", flush=True)
        if used_kw:
            out["used_keyword"] = used_kw
        # 说明：不再把 requests.Session / cookies 放进返回值——
        # 它们不可 JSON 序列化，且属于登录凭据，外传有泄露风险（仅内部使用）。
        # V16 起摘要改由本函数内部抓取（见上），因此调用方无需 session。
        return out


    # 2. 先整句检索
    papers, err = _do_search(sess, cookies, keyword, checked_db)
    if papers:
        return _finalize(papers)

    # 3. 整句失败（分词 AND 过严）→ 多级切核心词重试
    for core in _extract_core_keywords(keyword):
        if core == keyword:
            continue
        papers2, err2 = _do_search(sess, cookies, core, checked_db)
        if papers2:
            return _finalize(papers2, used_kw=core)
        err = err2 or err

    return {"ok": False, "keyword": keyword, "error": err or "知网检索失败", "papers": []}


if __name__ == "__main__":
    kw = sys.argv[1] if len(sys.argv) > 1 else "人工智能 教育"
    result = cnki_search(kw, max_results=8)
    print(f"关键词: {kw}")
    print(f"ok: {result.get('ok')}, error: {result.get('error', '')}")
    for i, p in enumerate(result.get("papers", []), 1):
        print(f"[{i}] {p['title'][:55]}")
        print(f"    作者: {'、'.join(p['authors'])[:40]} | 来源: {p['source'][:30]} | {p['date']} | 被引: {p['cite']}")
