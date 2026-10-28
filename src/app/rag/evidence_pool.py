# -*- coding: utf-8 -*-
"""证据池标准化 / 去重合并 / 完整性过滤（检索链路 B1–B3）
==========================================================
把「注入前必须做的三件事」收在一处，供两条建池路径共用：

  B1 normalize     —— DOI 归一、标题归一、快照日期、结构化字段保留（渲染只读不算）
  B2 dedupe/merge  —— DOI 分桶 → 标题归一精确 → 3-gram 近似；预印本↔正式版合并
  B3 integrity     —— 出版类型过滤（社论/读者来信/**勘误**…）+ 撤稿一票否决

设计约束（见《检索链路诊断与重构方案》第七节）：
  * 纯函数优先；除 B3 的撤稿查询外不触网，导入本模块不产生任何网络调用；
  * **失败一律 fail-open**：撤稿查询失败时保留条目，只把状态写进审计，绝不静默丢数据；
  * 每一次丢弃都进审计计数，任何一局都能回放"检索→去重→过滤→注入"的漏斗。

为什么需要 B3 的出版类型过滤（实证）：2026-10-10 01:34 的真实会话里，
注入池第 8 条是 `Correction: Perceptions vs pr…`（Frontiers in Psychology 的**勘误通告**），
它被当成一条正式文献注入了讨论——勘误不是研究结论，不能作为论据。
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PIPELINE_VERSION = "evidence_pool/1.0 (2026-10-10)"

# 归一 DOI 的形状校验：10.<4-9 位>/<后缀>
_DOI_SHAPE = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_PREFIX = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.I)
_DOI_LABEL = re.compile(r"^doi\s*:\s*", re.I)
_WS = re.compile(r"\s+")
# 标题归一：去掉空白/标点/下划线（CJK 属 \w，会被保留）
_NORM_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)
# 出版类型白名单式校验：只认纯 ASCII 小写类型名，中文/异常值一律不判（宁可放过不可错杀）
_ASCII_TYPE = re.compile(r"^[a-z][a-z\- ]{2,30}$")

# B3：这些出版类型不是研究文献，不应进入决策论据。
# 注意：**不包含** posted-content（预印本）、article、review、book-chapter、proceedings-article、dataset。
DROP_PUB_TYPES = frozenset([
    "editorial", "letter", "erratum", "correction", "correction-addendum",
    "addendum", "retraction", "component", "peer-review", "abstract",
    "grant", "paratext", "editorial-material", "unknown-component",
])

NEAR_DUP_THRESHOLD = 0.72      # 3-gram Jaccard 阈值（沿用 V15 实测值）
MIN_GRAMS_FOR_NEAR = 4         # 短标题不做近似判定，避免误杀

# B3 撤稿查询（OpenAlex，免 Key；is_retracted 数据源含 Retraction Watch）
OPENALEX_URL = "https://api.openalex.org/works"
MAILTO = os.environ.get("OPENALEX_MAILTO", "qunzhi@example.edu.cn")
RETRACTION_ENABLED = os.environ.get("QUNZHI_RETRACTION_CHECK", "1") == "1"
RETRACTION_TIMEOUT = float(os.environ.get("QUNZHI_RETRACTION_TIMEOUT", "8"))
_RETRACT_TTL = 6 * 3600
_RETRACT_CACHE = {}


# ---------------------------------------------------------------- B1 归一
def normalize_doi(value):
    """DOI 归一：去 doi: 标签/去 URL 前缀/去空白/去尾标点/转小写。形状不合法返回 ""。"""
    s = str(value or "").strip()
    if not s:
        return ""
    s = _DOI_LABEL.sub("", s)
    s = _DOI_PREFIX.sub("", s).strip()
    s = _WS.sub("", s)
    s = s.rstrip(".,;)]}>").strip()
    if not _DOI_SHAPE.match(s):
        return ""
    return s.lower()


def norm_title(value):
    """标题归一：NFKC + 小写 + 去空白/标点/下划线。"""
    return _NORM_STRIP.sub("", unicodedata.normalize("NFKC", str(value or "")).lower())


def title_grams(text, n=3):
    """字符 n-gram 集合（输入可为已归一标题或原始标题）。"""
    t = text if re.match(r"^[\w]+$", str(text or "")) else norm_title(text)
    t = str(t or "")
    if len(t) < n:
        return set()
    return set(t[i:i + n] for i in range(len(t) - n + 1))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


# ------------------------------------------------------- 期刊级别（单一来源）
_TIER_FNS = None


def journal_lookup():
    """返回 (tier_of, describe)。名录缺失时退化为安全兜底（tier 0 / 「—」）。

    这里是**全项目唯一的期刊名录入口**——此前 coach_search 与 citations 各自
    维护一份 try/except 导入链，属于"同一 concern 实现 2–3 处"的典型。
    """
    global _TIER_FNS
    if _TIER_FNS is not None:
        return _TIER_FNS
    try:
        from core_journals import tier_of, describe
        _TIER_FNS = (tier_of, describe)
        return _TIER_FNS
    except Exception:
        pass
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from core_journals import tier_of, describe
        _TIER_FNS = (tier_of, describe)
    except Exception:
        _TIER_FNS = (lambda _n: 0, lambda _n: u"—")
    return _TIER_FNS


def lookup_journal(name):
    """期刊名 → (tier, level_label)。

    tier: 1=CSSCI 2=北大核心 11=CSSCI扩展 3=国际 Q1/Q2 4=国际 Q3/Q4
          5=国际期刊（SJR 未收录） 0=未入任何名录
    label 区分三种情况（**不再用同一个「—」糊住**）：
          * 有刊名但不在名录里 → 「非核心期刊」（已识别，不是核心）
          * 拉丁刊名但 SJR 未收录 → 「国际期刊」
          * 无刊名 / 查不到 → 「—」（未识别）
    """
    n = str(name or "").strip()
    if not n:
        return 0, u"—"
    tier_of, describe = journal_lookup()
    try:
        t = int(tier_of(n) or 0)
    except Exception:
        t = 0
    if t == 0 and re.search(r"[A-Za-z]{4,}", n):
        t = 5
    try:
        label = describe(n) if t else u"—"
    except Exception:
        label = u"—"
    if t == 5 and (not label or label == u"—"):
        label = u"国际期刊"
    if t == 0 and re.search(u"[\u4e00-\u9fff]", n):
        # 中文刊名且有刊名实体，但不在 CSSCI/北大核心/扩展版名录 → 普通期刊
        label = u"非核心期刊"
    return t, label


def level_present(item):
    """级别字段是否有效（「—」/空 都算未覆盖）。"""
    lv = str(item.get("journal_level") or "").strip()
    return lv not in ("", u"—", "-", "None")


# ---------------------------------------------------------------- B2 去重合并
def _meta_score(rec):
    """元数据可信度打分：DOI/刊名/核心级别 越高越可信（用于决定谁当"主记录"）。"""
    s = 0
    if normalize_doi(rec.get("doi")):
        s += 3
    if str(rec.get("journal") or rec.get("venue") or "").strip():
        s += 3
    try:
        tier = int(rec.get("journal_tier") or 0)
    except Exception:
        tier = 0
    if tier in (1, 2, 11):
        s += 2
    elif tier in (3, 4, 5):
        s += 1
    return s


def _longer(a, b):
    a, b = str(a or ""), str(b or "")
    return a if len(a) >= len(b) else b


def _as_int(v):
    try:
        return int(str(v).strip() or 0)
    except Exception:
        return 0


def _merge_into(primary, other):
    """把 other 的信息并入 primary（primary 是元数据更可信的一条），原地修改。"""
    for k in ("title", "authors", "journal", "venue", "year", "doi", "url",
              "abstract", "source", "source_type", "kind", "type"):
        if not str(primary.get(k) or "").strip() and str(other.get(k) or "").strip():
            primary[k] = other[k]
    primary["content"] = _longer(primary.get("content"), other.get("content"))
    primary["abstract"] = _longer(primary.get("abstract"), other.get("abstract"))
    # 被引数取大：同一文献在不同源的被引数不一致，取大不取小
    if _as_int(other.get("cite")) > _as_int(primary.get("cite")):
        primary["cite"] = other.get("cite")
    # 版本合并留痕（预印本↔正式版↔会议-期刊版）
    tag = str(other.get("title") or other.get("doi") or "").strip()[:60]
    if tag:
        seen = primary.setdefault("merged_from", [])
        if tag not in seen:
            seen.append(tag)
    primary["versions"] = _as_int(primary.get("versions") or 1) + _as_int(other.get("versions") or 1)
    return primary


def dedupe_and_merge(items, near_threshold=NEAR_DUP_THRESHOLD):
    """DOI 分桶 → 标题归一精确 → 3-gram 近似；命中即**合并**（不是丢弃）。

    返回 (out, audit)。合并而非丢弃是关键：8 槽位下重复条目直接损失 12.5% 信息容量，
    但两条各自的元数据（DOI/刊名/被引）都要保住。
    """
    src = [x for x in (items or []) if isinstance(x, dict)]
    out, by_doi, by_title, grams = [], {}, {}, []
    merged = 0
    for it in src:
        doi = normalize_doi(it.get("doi"))
        tn = norm_title(it.get("title"))
        g = title_grams(tn) if len(tn) >= 3 else set()

        idx = by_doi.get(doi) if doi else None
        if idx is None and tn:
            idx = by_title.get(tn)
        if idx is None and len(g) >= MIN_GRAMS_FOR_NEAR:
            for j, pg in enumerate(grams):
                if len(pg) >= MIN_GRAMS_FOR_NEAR and jaccard(g, pg) >= near_threshold:
                    idx = j
                    break

        if idx is not None:
            cur = out[idx]
            if _meta_score(it) > _meta_score(cur):      # 新条目元数据更可信 → 换主
                cur = out[idx] = _merge_into(it, cur)
            else:
                cur = _merge_into(cur, it)
            merged += 1
            if doi:
                by_doi[doi] = idx
            if tn:
                by_title[tn] = idx
            if g:
                grams[idx] = grams[idx] | g
            continue

        it.setdefault("versions", 1)
        out.append(it)
        j = len(out) - 1
        if doi:
            by_doi[doi] = j
        if tn:
            by_title[tn] = j
        grams.append(g)

    audit = {"dedupe_in": len(src), "after_dedupe": len(out),
             "merged": merged, "dup_dropped": len(src) - len(out)}
    return out, audit


# ---------------------------------------------------------------- B3 完整性
def publication_type_ok(item):
    """出版类型是否可作论据。无类型信息 → 放行（fail-open）。"""
    raw = item.get("cr_type") or item.get("publication_type") or item.get("type") or ""
    t = str(raw).strip().lower()
    if not _ASCII_TYPE.match(t):
        return True
    return t not in DROP_PUB_TYPES


def _cache_get(doi):
    hit = _RETRACT_CACHE.get(doi)
    if not hit:
        return None
    val, ts = hit
    if time.time() - ts > _RETRACT_TTL:
        _RETRACT_CACHE.pop(doi, None)
        return None
    return val


def _cache_put(doi, flag):
    _RETRACT_CACHE[doi] = (bool(flag), time.time())


def check_retractions(items, timeout=None, batch=50):
    """批量查撤稿状态。返回 ({归一 DOI: True}, status)。status 说明查询是否可用。

    只在**查得动**时返回 True 的条目；查询失败返回空 map + status 文本，
    调用方必须 fail-open（保留条目），把 status 写进审计。
    """
    timeout = RETRACTION_TIMEOUT if timeout is None else timeout
    found, dois, todo = {}, set(), []
    if not RETRACTION_ENABLED:
        return found, "disabled"
    for it in items or []:
        d = normalize_doi((it or {}).get("doi"))
        if not d:
            continue
        dois.add(d)
        cached = _cache_get(d)
        if cached is None:
            todo.append(d)
        elif cached:
            found[d] = True
    if not dois:
        return found, "no_doi"
    if not todo:
        return found, "cache_only"

    errs = []
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        q = "|".join(chunk)
        url = "%s?filter=doi:%s&per-page=%d&select=doi,is_retracted" % (
            OPENALEX_URL, urllib.parse.quote(q, safe="|"), batch)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "QunZhi/1.0 (mailto:%s)" % MAILTO,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            errs.append(str(e)[:80])
            continue
        got = set()
        for w in (data.get("results") or []):
            d = normalize_doi(w.get("doi"))
            if not d:
                continue
            got.add(d)
            flag = bool(w.get("is_retracted"))
            _cache_put(d, flag)
            if flag:
                found[d] = True
        for d in chunk:
            if d not in got:
                _cache_put(d, False)     # OpenAlex 不认识 → 视为未撤稿（fail-open）
    status = "ok" if not errs else ("unavailable: " + "; ".join(errs[:2]))
    return found, status


# ------------------------------------------------------- B9 网络来源可信过滤
_TS = None


def _trusted():
    """懒加载 trusted_sources（B9 单一实现）。导入失败 → None，调用方 fail-open。"""
    global _TS
    if _TS is not None:
        return _TS if _TS is not False else None
    try:
        import trusted_sources as _t
    except Exception:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            import trusted_sources as _t
        except Exception:
            _TS = False
            return None
    _TS = _t
    return _t


def web_source_filter(items, policy=None):
    """B9：按白名单给网络来源分级，只放行 academic / trusted。

    policy:
      * None 或 "paper_only" —— **不改变任何东西**（现状：网络源在 coach_search
        侧已按"三无"剔除）。返回 (items, audit)，audit.web_policy="paper_only"。
      * "whitelist"          —— 对**网络来源**逐条判定，blocked/unknown 剔除，
        academic/trusted 放行并打上 source_tier 三件套（tier/label/reason）。

    fail-open：trusted_sources 导入失败时原样返回，只把状态写进审计。
    """
    src = [x for x in (items or []) if isinstance(x, dict)]
    pol = policy or "paper_only"
    audit = {"web_policy": pol, "web_checked": 0, "web_dropped": 0,
             "web_dropped_items": [], "web_kept": {"academic": 0, "trusted": 0},
             "web_filter_status": "skipped"}
    if pol != "whitelist":
        return src, audit

    ts = _trusted()
    if ts is None:
        audit["web_filter_status"] = "unavailable"      # fail-open
        return src, audit
    audit["web_filter_status"] = "ok"

    kept, dropped = [], []
    for it in src:
        if not ts.is_web_source(it):
            # 学术源的条目不走这张网，但同样打标，前端才能有一列统一的"来源类型"
            if not str(it.get("source_tier") or "").strip():
                it["source_tier"] = "academic"
                it["source_tier_label"] = u"学术来源"
                it["source_tier_reason"] = u"非网络检索来源"
            kept.append(it)
            continue
        audit["web_checked"] += 1
        v = ts.classify(it)
        it["source_tier"] = v["tier"]
        it["source_tier_label"] = v["label"]
        it["source_tier_reason"] = v["reason"]
        if v["tier"] in ("academic", "trusted"):
            audit["web_kept"][v["tier"]] = audit["web_kept"].get(v["tier"], 0) + 1
            kept.append(it)
        else:
            audit["web_dropped"] += 1
            if len(audit["web_dropped_items"]) < 8:
                audit["web_dropped_items"].append({
                    "title": str(it.get("title") or "")[:60],
                    "host": v["host"], "tier": v["tier"], "reason": v["reason"],
                })
            dropped.append(it)
    return kept, audit


def integrity_filter(items, timeout=None, web_policy=None):
    """出版类型过滤 + 撤稿一票否决 + B9 网络来源可信过滤。返回 (kept, audit)。"""
    kept, dropped_types = [], []
    for it in items or []:
        if publication_type_ok(it):
            kept.append(it)
        else:
            dropped_types.append({
                "title": str(it.get("title") or "")[:60],
                "type": str(it.get("cr_type") or it.get("type") or ""),
            })
    audit = {
        "type_dropped": len(dropped_types),
        "type_dropped_items": dropped_types[:5],
        "retracted_dropped": 0,
        "retracted_items": [],
        "retraction_check": "",
    }
    # B9：网络来源分级（默认 paper_only = 不动）
    kept, w_audit = web_source_filter(kept, policy=web_policy)
    audit.update(w_audit)
    rmap, status = check_retractions(kept, timeout=timeout)
    audit["retraction_check"] = status
    if rmap:
        rest = []
        for it in kept:
            d = normalize_doi(it.get("doi"))
            if d and rmap.get(d):
                it["retracted"] = True
                audit["retracted_items"].append({"title": str(it.get("title") or "")[:60], "doi": d})
                audit["retracted_dropped"] += 1
                continue
            rest.append(it)
        kept = rest
    return kept, audit


# ---------------------------------------------------------------- 对外主入口
def finalize_pool(items, snapshot_date=None, do_integrity=True, web_policy=None):
    """B1+B2+B3+B9 一次做完。返回 (pool, audit)。原地补齐字段，审计可回放。

    调用方：coach_search.quality_gate（两条建池路径的唯一收口）。
    web_policy 默认 None → "paper_only"（现状，行为零变化）；
    传 "whitelist" 才启用 B9 的网络来源白名单。
    """
    snap = snapshot_date or time.strftime("%Y-%m-%d")
    src = [x for x in (items or []) if isinstance(x, dict)]

    # B1：归一 + 快照日期 + 结构化字段补齐（渲染只读不算）
    for it in src:
        _d = normalize_doi(it.get("doi"))
        it["doi_norm"] = _d
        # 参考文献表/前端显示的 DOI 统一成归一形式（原始写法留在 doi_raw，便于回溯）
        _raw = str(it.get("doi") or "").strip()
        if _d and _raw and _raw != _d:
            it.setdefault("doi_raw", _raw)
            it["doi"] = _d
        it["title_norm"] = norm_title(it.get("title"))
        it["snapshot_date"] = snap

    # B2
    deduped, audit = dedupe_and_merge(src)
    audit["pipeline"] = PIPELINE_VERSION
    audit["snapshot_date"] = snap
    audit["raw"] = len(src)

    # B3（+B9）
    kept = deduped
    if do_integrity:
        kept, i_audit = integrity_filter(kept, web_policy=web_policy)
        audit.update(i_audit)

    covered = sum(1 for x in kept if level_present(x))
    audit["final"] = len(kept)
    audit["level_covered"] = covered
    audit["level_coverage"] = round(covered / float(len(kept)), 3) if kept else 0.0
    return kept, audit


def funnel_text(audit):
    """一行漏斗（日志/报告用）：检索 → 去重 → 三无 → 类型过滤 → 撤稿 → 注入。"""
    a = audit or {}
    base = (u"检索 %s → 去重后 %s（合并 %s）→ 三无 -%s → 类型过滤 -%s → 撤稿 -%s → 注入 %s；"
            u"级别覆盖 %s/%s（%.0f%%）｜快照 %s｜撤稿查询 %s") % (
        a.get("raw", "?"), a.get("after_dedupe", "?"), a.get("merged", 0),
        a.get("venue_dropped", 0), a.get("type_dropped", 0), a.get("retracted_dropped", 0),
        a.get("final", "?"),
        a.get("level_covered", 0), a.get("final", 0),
        100.0 * float(a.get("level_coverage") or 0.0),
        a.get("snapshot_date", "?"), a.get("retraction_check") or "未查",
    )
    # B9：只在真的开了白名单网时才追加这一段，避免"只用论文"模式下日志变噪
    if a.get("web_policy") == "whitelist":
        kept = a.get("web_kept") or {}
        base += (u"｜网络源 %s 条：放行 可信%s/学术%s，剔除 %s（%s）") % (
            a.get("web_checked", 0),
            kept.get("trusted", 0), kept.get("academic", 0),
            a.get("web_dropped", 0), a.get("web_filter_status", "?"))
    # V16（修 1）：**摘要覆盖率**——证据链是否完整，一眼可见。
    # 为什么必须显式展示：注入池只有题录时，报告里的 [Pn] 标注看着像"有出处"，
    # 但实际无从核对（B7 实测：题录下 18/18 判不了）。这个数字是"证据链是否成立"的直接指标。
    if a.get("abstract_total"):
        _t = int(a.get("abstract_total") or 0)
        _w = int(a.get("abstract_with") or 0)
        base += (u"｜证据链 摘要 %d/%d（%.0f%%）") % (_w, _t, 100.0 * _w / max(1, _t))
        if _w == 0:
            base += u" ⚠ **全无摘要，[Pn] 无法核对**"
    return base
