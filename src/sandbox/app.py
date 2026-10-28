# app.py
# 群智 · 讨论引擎（SandboxSession / Coach 等核心类）
# 注意：本模块**不是** Web 入口，不再独立监听端口。
#       Web 层是 FastAPI 网关 app/main_gateway.py（默认 http://127.0.0.1:5020），
#       由它通过 importlib 加载本模块并复用其中的引擎类。

import json, os, random, re, sys, io, time, threading
from datetime import datetime
from queue import Queue
# 说明：本文件已不再依赖 flask / openai SDK——
#   · Web 层由 FastAPI 网关（app/main_gateway.py）承担，本模块只做讨论引擎；
#   · 模型调用走 requests 直连（硬超时），避免 openai SDK 的 ssl read 无限阻塞。
from prompts import (SPEAK_PROMPT, COACH_AUDIT_PROMPT, AUDIT_CONCERN_MAP,
                     INTERVENTION_TEMPLATES, INTERVENTION_DECISION_PROMPT,
                     CITATION_RULE_ON, CITATION_RULE_OFF)


# ---------- 引用模块（V15 优化 2026-09-25）----------
# 引擎与网关共用 app/citations.py；网关会把 app/ 加进 sys.path，
# 独立运行时（如单跑引擎或测试）这里兜底补一次。
def _citations():
    try:
        import citations as _c
        return _c
    except Exception:
        _appdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
        if _appdir not in sys.path:
            sys.path.insert(0, _appdir)
        import citations as _c
        return _c


# ---------- V15 优化（2026-09-25）：主教练逐轮干预的客观信号 ----------
# 设计原则：**是否触发干预由本地阈值判定**，模型只在白名单指令里做选择并给出依据，
# 不允许模型自由决定“要不要干预、干预什么”。全部本地计算，无 API 调用、可复现。
#
# V16 校准（2026-10-11，修 4）——依据是对**全量 27 条真实轮次信号**的分布检定：
#   ① `repeat_rate` 旧阈值 0.55：实测 max **0.206** → **0% 触发**，是**死阈值**。
#      降到 0.15（实测 p75=0.108 之上、max 0.206 之下）——注意 0.25 仍会高于实测 max，还是死阈值。
#   ② `unsupported_claims` 旧阈值 3：实测 max **1** → **0% 触发**，同样是死阈值。
#      降到 1（即"出现 1 条未补证断言即提示"）。
#   ③ `off_topic` 旧阈值 0.65：实测分布 p50=0.50 / p75=0.667，阈值**落在分布中部**，
#      触发与否近乎随机；且 2026-10-10 那局轮2 = 0.636，**差 0.014 没触发**。
#      降到 0.55，使其在"明显跑题"时稳定响。
#      ⚠ **但必须同时披露：该度量本身的构造成效未通过检验**（见 IV_RULER_UNVALIDATED）——
#      它量的是"措辞重合"而非"主题偏离"（换说法讲同一件事 → 1.000；复读课题词 → 0.182）。
#      阈值校准只解决"能不能响"，**不解决"量得对不对"**。修法（尺子替换）的预注册已写好、
#      但**尚未开跑**，所以线上仍是这把未经验证的尺子。
IV_REPEAT_TH = 0.15       # 观点重复率阈值（原 0.55，实测 max 0.206；0.25 仍是死阈值）
IV_OFFTOPIC_TH = 0.55     # 跑题度阈值（原 0.65，落在分布中部）
IV_TENSION_TH = 0.15      # 张力下限（批判性发言占比）
IV_UNSUPPORTED_TH = 1     # 未补证断言条数阈值（原 3，实测 max 1 → 死阈值）
IV_MIN_GAP = 1            # 两次干预之间至少间隔的轮数
IV_MAX_RATIO = 0.5        # 单局干预次数上限 = max(1, int(max_rounds * 该比例))

# 尺子是否经过构造成效验证。False = **未验证**，读数须如实标注、不得作为唯一依据。
# 依据：_ruler_audit / _ruler_ablation 的实测（构造效度 + 按信号子集 AUC）。
# 开跑"尺子替换"实验并通过入选条件后，才应改为 True。
IV_RULER_UNVALIDATED = {
    "off_topic": True,     # 构造效度不成立：换说法=1.000、复读=0.182；子集 AUC 0.500（≈随机）
    "tension": True,       # 构造效度不成立：真批判=0.000、标记词堆砌=1.000；子集 AUC 0.350（低于随机）
    "repeat_rate": False,  # 构造效度成立（相同文本→1.0），仅刻度偏低（已校准）
    "unsupported_claims": False,  # 构造效度成立，仅刻度偏低（已校准）
}
IV_RULER_NOTE = u"⚠ 本读数的度量成效**未经验证**：它量的是措辞重合而非语义偏离，" \
                u"请勿仅凭该读数断定讨论跑题。"

CRITIC_MARKERS = (u"但是", u"不过", u"然而", u"问题", u"风险", u"相反", u"质疑",
                  u"漏洞", u"不成立", u"未必", u"局限", u"失败", u"反对", u"担忧")


def _bigrams(text):
    t = re.sub(u"[\\s\\W_]+", "", str(text or ""))
    return set(t[i:i + 2] for i in range(len(t) - 1)) if len(t) >= 2 else set()


def _jaccard(a, b):
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


def compute_round_signals(topic, round_speeches, history_speeches=None,
                          valid_refs=None, round_num=0):
    """五个客观信号（分歧/张力、未补证断言、观点重复率、跑题度、引用情况）。

    返回 dict，其中 triggers 列出被触发的信号名，should_consider 表示是否值得让主教练决策。
    """
    speeches = [s for s in (round_speeches or []) if s and s.strip()]
    hist = [s for s in (history_speeches or []) if s and s.strip()]
    n = len(speeches)

    # ① 观点重复率：本轮发言与历史发言的最大相似度
    repeat = max((_jaccard(s, h) for s in speeches for h in hist), default=0.0) if hist else 0.0

    # ② 跑题度：1 - 本轮发言对“课题关键词”的覆盖率
    #    注意：不能用 课题↔单条发言 的 Jaccard——课题短、发言长，相似度天然很低（实测恒为 0.9+，
    #    会每轮误报）。改用“课题 bigram 被本轮发言并集覆盖的比例”，才是真正的跑题度量。
    if speeches:
        union = set()
        for s in speeches:
            union |= _bigrams(s)
        tb = _bigrams(topic)
        cover = (len(tb & union) / float(len(tb))) if tb else 0.0
        offtopic = max(0.0, min(1.0, 1.0 - cover))
    else:
        offtopic = 0.0

    # ③ 张力水平：含批判标记的发言占比
    tension = (sum(1 for s in speeches if any(m in s for m in CRITIC_MARKERS)) / float(n)) if n else 0.0

    # ④ 未补证断言数：含数字/年份/研究表述但没有 [P#]、也没写“待验证”的句子
    unsupported = 0
    for s in speeches:
        for sent in re.split(u"[。！？\\n]", s):
            if len(sent) < 10 or u"[P" in sent or u"待验证" in sent:
                continue
            if re.search(u"\\d+%|\\d{4}\\s*年|研究表明|数据显示|据统计|文献", sent):
                unsupported += 1

    # ⑤ 引用情况：本轮出现的合法编号数
    cited = set()
    for s in speeches:
        cited.update(re.findall(r"\[P\d+\]", s))
    if valid_refs:
        cited = set(c for c in cited if c.strip(u"[]") in valid_refs)

    triggers = []
    if repeat >= IV_REPEAT_TH:
        triggers.append(u"观点重复率偏高")
    if offtopic >= IV_OFFTOPIC_TH:
        triggers.append(u"跑题度偏高")
    if tension <= IV_TENSION_TH and n >= 3:
        triggers.append(u"张力不足（缺批判性发言）")
    if unsupported >= IV_UNSUPPORTED_TH:
        triggers.append(u"未补证断言偏多")
    if n >= 3 and not cited:
        triggers.append(u"本轮完全没有引用编号")

    return {
        "round": round_num,
        "speech_count": n,
        "repeat_rate": round(repeat, 3),
        "off_topic": round(offtopic, 3),
        "tension": round(tension, 3),
        "unsupported_claims": unsupported,
        "cited_count": len(cited),
        "cited": sorted(cited),
        "triggers": triggers,
        "should_consider": bool(triggers),
        "thresholds": {"repeat": IV_REPEAT_TH, "off_topic": IV_OFFTOPIC_TH,
                       "tension": IV_TENSION_TH, "unsupported": IV_UNSUPPORTED_TH},
        # V16（修 4）：如实标注哪些度量**未经成效验证**——读数可以用，但不得当成结论。
        "ruler_unvalidated": dict(IV_RULER_UNVALIDATED),
        "ruler_note": IV_RULER_NOTE,
    }


def format_signals_text(sig):
    """把信号整理成给模型看的文本（模型只据此选指令，不再自行判断是否干预）"""
    _txt = (u"观点重复率 %.3f（阈值 %.2f）\n跑题度 %.3f（阈值 %.2f）\n张力水平 %.3f（下限 %.2f）\n"
            u"未补证断言 %d 条（阈值 %d）\n本轮引用编号 %d 个\n"
            u"已触发：%s") % (
        sig.get("repeat_rate", 0), IV_REPEAT_TH, sig.get("off_topic", 0), IV_OFFTOPIC_TH,
        sig.get("tension", 0), IV_TENSION_TH, sig.get("unsupported_claims", 0),
        IV_UNSUPPORTED_TH, sig.get("cited_count", 0),
        u"、".join(sig.get("triggers") or []) or u"无")
    # V16（修 4）：把"未验证"如实告诉模型，避免它把跑题度当成事实依据
    _uv = [k for k, v in (sig.get("ruler_unvalidated") or IV_RULER_UNVALIDATED).items() if v]
    if _uv:
        _txt += u"\n" + IV_RULER_NOTE + u"（未验证的度量：%s）" % u"、".join(_uv)
    return _txt



# ---------- UTF-8 配置 ----------
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
elif hasattr(sys.stdout, 'buffer'):
    try:
        sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
    except:
        pass

# ---------- API 配置（提交版：从环境变量/.env 读取，不硬编码密钥）----------
# 使用前：复制 .env.example 为 .env 并填入你的 Key（填 DEEPSEEK_API_KEY 即可，
# 会自动用于双客户端负载均衡；也可分别设 DEEPSEEK_API_KEY_1/_2 覆盖）。

def _load_env_file():
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(_env_path):
        try:
            with open(_env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _, _v = _line.partition("=")
                        if _k and not os.environ.get(_k):
                            os.environ[_k] = _v
        except Exception:
            pass

_load_env_file()


def _valid_key(k: str) -> bool:
    """判断是否是一把"看起来可用"的 key：
    - 非空、纯 ASCII（中文占位符放进 HTTP header 会触发 latin-1 编码错误）
    - 不含示例占位符特征（sk-your / your- / 请填入 / xxx）"""
    if not k or not isinstance(k, str):
        return False
    try:
        k.encode("ascii")
    except UnicodeEncodeError:
        return False
    low = k.lower()
    for bad in ("sk-your", "your-", "your_", "请填入", "xxx", "placeholder", "填入"):
        if bad in low:
            return False
    return len(k) >= 12


_key = os.environ.get("DEEPSEEK_API_KEY", "")
_ph = "sk-REPLACE_WITH_YOUR_DEEPSEEK_KEY"      # 纯 ASCII 占位符，避免 Header 编码异常
DEEPSEEK_API_KEY_1 = os.environ.get("DEEPSEEK_API_KEY_1", _key) or _ph
DEEPSEEK_API_KEY_2 = os.environ.get("DEEPSEEK_API_KEY_2", _key) or _ph
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 启动自检：未配置或无可用 key 时给出醒目的中文提示（不中断启动，页面仍可打开，
# 但第一次调用会立刻暴露原因，而不是抛 UnicodeEncodeError 让人看不懂）
_KEY_WARNING = ""
if not _valid_key(DEEPSEEK_API_KEY_1) and not _valid_key(DEEPSEEK_API_KEY_2):
    _KEY_WARNING = ("[配置缺失] 未检测到有效的 DEEPSEEK_API_KEY。"
                    "请把 .env.example 复制为 .env 并填入你的 Key（目录：项目根目录）。")
    print("=" * 68, flush=True)
    print(_KEY_WARNING, flush=True)
    print("=" * 68, flush=True)

import requests as _requests

_api_counter = 0
_api_lock = threading.Lock()


def get_api_client():
    """兼容保留：旧版返回 openai 客户端对象，现已改为 requests 直连。
    统一返回当前轮询到的 API Key（历史调用方若仍引用本函数也不会报错）。"""
    return _get_api_key()


def _get_api_key():
    global _api_counter
    with _api_lock:
        _api_counter += 1
        if not _valid_key(DEEPSEEK_API_KEY_1):
            return DEEPSEEK_API_KEY_2
        if not _valid_key(DEEPSEEK_API_KEY_2):
            return DEEPSEEK_API_KEY_1
        return DEEPSEEK_API_KEY_1 if (_api_counter - 1) % 6 < 3 else DEEPSEEK_API_KEY_2

# ---------- 多模型支持（V15.4：外围环节分流，核心讨论仍用 DeepSeek）----------
# 设计原则：6 人讨论/报告生成等核心环节保持 DeepSeek（与 798 轮实验一致）；
# 人话化、深批者、轻量抽取等外围环节分流到 Qwen / GLM，降低单点依赖与成本。
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")
GLM_BASE_URL = os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
# 注意：glm-5.3-flash 是推理模型（输出去 reasoning_content，content 可能为空），
# 用于本场景需把 max_tokens 提到 4000+；默认用非推理的 glm-4-flash（快、稳、直接出结果）。
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-4-flash")

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-flash")

def call_llm(prompt, provider="deepseek", max_tokens=400, temperature=0.75, timeout=30, retries=2):
    """统一多模型调用入口（requests 直连 + 硬超时 + 失败降级到 DeepSeek）。

    provider: deepseek（默认，双 key 轮询）| glm | qwen
    任一 provider 未配置或调用失败时，自动回落到 DeepSeek，保证系统不中断。
    """
    if provider == "glm" and _valid_key(GLM_API_KEY):
        url, key, model = f"{GLM_BASE_URL}/chat/completions", GLM_API_KEY, GLM_MODEL
    elif provider == "qwen" and _valid_key(QWEN_API_KEY):
        url, key, model = f"{QWEN_BASE_URL}/chat/completions", QWEN_API_KEY, QWEN_MODEL
    else:
        if provider in ("glm", "qwen"):
            print(f"ℹ️ [{provider}] 未配置有效 Key，直接使用 DeepSeek", flush=True)
        return call_with_retry(prompt, max_tokens=max_tokens, temperature=temperature,
                               timeout=timeout, retries=retries)

    call_timeout = max(timeout, 5) + 5
    for attempt in range(retries + 1):
        try:
            resp = _requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=call_timeout,
            )
            if resp.status_code == 200:
                d = resp.json()
                ch = (d.get("choices") or [{}])[0]
                msg = ch.get("message", {}) or {}
                text = (msg.get("content") or "").strip()
                if text:
                    return text
                # 诊断：推理型模型把 token 花在 reasoning_content，content 为空
                if not text and msg.get("reasoning_content"):
                    print(f"⚠️ [{provider}] 返回空 content（该模型为推理型，"
                          f"finish_reason={ch.get('finish_reason')}，需提高 max_tokens）", flush=True)
                if attempt < retries:
                    time.sleep(1)
            else:
                print(f"⚠️ [{provider}] HTTP {resp.status_code} (尝试 {attempt+1}): {resp.text[:100]}")
                if attempt < retries:
                    time.sleep(1.5)
        except Exception as e:
            print(f"⚠️ [{provider}] 失败 (尝试 {attempt+1}): {str(e)[:100]}")
            if attempt < retries:
                time.sleep(1.5)
    # 兜底：分流模型全失败 → 回落 DeepSeek，保证功能不中断
    print(f"⚠️ [{provider}] 全部失败，回落 DeepSeek", flush=True)
    return call_with_retry(prompt, max_tokens=max_tokens, temperature=temperature,
                           timeout=timeout, retries=retries)

def call_with_retry(prompt, max_tokens=400, temperature=0.75, timeout=15, retries=3):
    # 用 requests 直连（硬超时），避免 openai SDK 的 ssl read 无限阻塞
    key = _get_api_key()
    call_timeout = max(timeout, 5) + 5
    for attempt in range(retries + 1):
        try:
            resp = _requests.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [{"role": "system", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=call_timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                _ch = (data.get("choices") or [{}])[0]
                text = (_ch.get("message", {}).get("content") or "").strip()
                # 非空即视为有效：此前用 len(text) > 5，
                # 会把"1,3,5""NONE""复杂"这类短而正确的答案误判为失败并重试（实测踩坑）
                if text:
                    return text
                # 2026-10-24：空内容时说明原因 —— 推理型模型（如 GLM-5.x）会把
                #   max_tokens 全花在 reasoning_tokens 上，content 为空、
                #   finish_reason=length。此前静默重试 3 次后返回 None，
                #   日志里看不出根因，只能人工重跑。现在打明确告警。
                _fr = _ch.get("finish_reason")
                if _fr == "length":
                    _rt = (_ch.get("message", {}).get("reasoning_content") or "")
                    print(f"⚠️ 输出被截断：max_tokens={max_tokens} 不足，"
                          f"finish_reason=length（推理内容 {len(_rt)} 字）。"
                          f"推理型模型建议 max_tokens≥8000。", flush=True)
                if attempt < retries: time.sleep(1)
            else:
                print(f"⚠️ API HTTP {resp.status_code} (尝试 {attempt+1})")
                if attempt < retries: time.sleep(1.5)
        except Exception as e:
            print(f"⚠️ API 失败 (尝试 {attempt+1}): {e}")
            if attempt < retries: time.sleep(1.5)
    return None

def translate_plain(text):
    if not text or len(text) < 15: return text
    prompt = f"将以下发言翻译成一句大白话，20字以内，直接输出：\n{text[:200]}"
    return call_with_retry(prompt, max_tokens=40, temperature=0.5, timeout=5, retries=0) or text[:30] + "..."

# ---------- 性格常量 ----------
COGNITIVE_PROTOTYPES = {
    "hunter": {"name": "推进者", "style": "倾向于推动讨论进程，提出具体可行的下一步。相信行动比完美计划更重要。", "color": "#4a4a4a"},
    "trader": {"name": "联结者", "style": "善于在看似对立的观点之间找到共通点。相信合作比对抗更有价值。", "color": "#5a7a5a"},
    "ghost": {"name": "洞察者", "style": "习惯于先倾听，然后在关键时刻指出被集体忽略的盲点。", "color": "#6a5a7a"},
    "gambler": {"name": "探索者", "style": "乐于跳出既有框架，提出尚未被考虑的替代方案。", "color": "#7a6a5a"},
    "diplomat": {"name": "调和者", "style": "关注讨论的氛围和参与感，适时总结共识并引导分歧。", "color": "#5a6a8a"},
    "skeptic": {"name": "检验者", "style": "从反面推演方案的可行性，追问边界条件和潜在风险。", "color": "#6a6a6a"},
    "critic": {"name": "批判者", "style": "主动攻击方案的逻辑漏洞，追问伦理边界和失败场景。你不是为了否定，而是为了让方案经得起最坏的考验。", "color": "#c0392b"}
}

PROFESSIONAL_BACKGROUNDS = [
    {"name": "计算机科学", "icon": "💻", "perspective": "从技术可行性、系统架构和数据安全的角度分析问题。"},
    {"name": "教育学", "icon": "📖", "perspective": "从学习效果、学生发展和教学规律的角度分析问题。"},
    {"name": "心理学", "icon": "🧠", "perspective": "从动机、认知偏差和行为模式的角度分析问题。"},
    {"name": "管理学", "icon": "📊", "perspective": "从资源配置、流程效率和组织行为的角度分析问题。"},
    {"name": "社会学", "icon": "🌐", "perspective": "从群体行为、文化规范和社会影响的角度分析问题。"},
    {"name": "经济学", "icon": "💰", "perspective": "从激励结构、资源分配和边际效用的角度分析问题。"},
    {"name": "法学", "icon": "⚖️", "perspective": "从合规性、权利义务、伦理边界和制度约束的角度分析问题。"},
    {"name": "医学", "icon": "🩺", "perspective": "从健康风险、身心状态和干预安全性的角度分析问题。"},
]

VALUE_ORIENTATIONS = [
    {"name": "效率优先", "icon": "⚡", "belief": "最优方案用最小成本获得最大效果，推动快速决策和迭代优化。"},
    {"name": "公平优先", "icon": "⚖️", "belief": "公平是合作基础，好方案必须让所有人感到被公正对待。"},
    {"name": "创新优先", "icon": "💡", "belief": "真正的价值来自打破常规，倾向于支持有新意的方案。"},
    {"name": "人文关怀", "icon": "🤲", "belief": "任何方案最终都要服务于人，关注感受、尊严和成长。"},
    {"name": "实用主义", "icon": "🔧", "belief": "最好的方案能真正用起来，关注现实约束条件。"},
    {"name": "长远主义", "icon": "🔭", "belief": "短期效果不是衡量标准，倾向于支持具有长期价值的方案。"}
]

PLAYER_NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]

PRESET_INFO_LIBRARY = {
    "通用科研": [
        "你了解该问题领域的核心概念和主要争议点，但缺乏针对本场景的一手数据。",
        "你掌握类似场景下其他组织或地区的解决方案，但不确定这些方案在本场景的适用性。",
        "你关注不同利益相关者之间可能存在的目标冲突，但对各方真实的优先级排序缺乏了解。",
        "你了解技术方案在类似问题中的应用案例，但对技术落地的人力和资金成本存在疑虑。",
        "你关注方案实施后可能产生的非预期副作用，尤其是对弱势群体的潜在影响。",
        "你掌握相关政策的总体方向，但缺乏具体执行层面的操作指南和成功标准。"
    ],
    "小组评分": [
        "你是一名成绩优异但被搭便车困扰的学生，主张用技术手段量化个人贡献。",
        "你是一名认为评分应该照顾弱势同学的辅导员，担心技术监控会伤害学生自尊。",
        "你是一名主张用匿名互评解决搭便车问题的老师，但担心恶意打分。",
        "你是一名计算机专业学生，认为可以用Git提交记录和代码贡献度来客观评分。",
        "你关注评分的公平性，但更担心过度监控会破坏团队信任。",
        "你了解其他高校用AI追踪贡献度的案例，但担心本校学生反感被监视。"
    ],
    "AI与就业": [
        "你了解教育部AI进校园的政策导向，但对具体实施细则不清楚。",
        "你掌握了学校近三年各专业就业率数据，但无法归因于任何单一因素。",
        "你有一份关于AI对就业市场影响的行业报告，但数据来源偏宏观。",
        "你知道其他高校用AI职业导航工具提升了就业率，但担心本校预算不足。",
        "你关注AI对低技能岗位的冲击，但更担心课程更新速度跟不上企业需求。",
        "你认为AI不是威胁而是机遇，但需要找到校企合作的具体落地方式。"
    ],
    "小组评分_真实": [
        "你了解形成性评价与终结性评价的理论差异（来源：Black & Wiliam, 1998），但缺乏本校学生互评信效度的实证数据。",
        "你掌握国内高校评分改革的部分案例（来源：教育部2021年高校教学改革项目成果汇编），但不知道学生主观公平感知的关键影响因素。",
        "你关注匿名互评与实名互评的差异研究（来源：Panadero et al., 2013, Studies in Higher Education），但对本校学生的匿名评分行为特征缺乏一手数据。",
        "你了解软件开发中用Git提交记录量化个人贡献的方法（来源：软件工程领域敏捷开发最佳实践白皮书），但担心该方法在人文学科小组作业中的适用性。",
        "你关注过度监控可能破坏团队信任的风险（来源：组织行为学中关于电子监控与员工信任的元分析研究），但缺乏高等教育场景下的直接验证。",
        "你掌握部分高校用AI追踪学生贡献度的案例（来源：2023年教育部AI教育应用优秀案例集），但担心本校学生对该技术的接受度。"
    ],
    "课堂参与度_真实": [
        "你了解主动学习教学法的理论框架（来源：Freeman et al., 2014, PNAS），但缺乏本校课堂参与度的基线数据。",
        "你掌握中国大学生学习与发展追踪研究的部分数据（来源：CCSS项目年度报告），但不知道影响本校学生课堂沉默的核心因素。",
        "你关注课堂应答系统对参与度的影响研究（来源：Caldwell, 2007, CBE—Life Sciences Education），但对实施成本和技术门槛存在顾虑。",
        "你了解同伴教学法的实践效果（来源：Mazur, 1997, Peer Instruction），但担心该方法在本校大班教学中的可操作性。",
        "你关注学生课堂参与的心理障碍因素（来源：教育心理学中关于社交焦虑与课堂沉默的研究），但缺乏针对师范生群体的专门分析。",
        "你掌握翻转课堂的国内外实施案例（来源：教育部2020年混合式教学改革项目成果汇编），但不确定本校学生的课外学习投入能否支撑翻转模式。"
    ],
    "师范生上讲台_真实": [
        "你了解教学焦虑的理论框架（来源：Horwitz et al., 1986, The Modern Language Journal），但其在师范生群体中的适用性有待验证。",
        "你掌握教师资格证考试中教学演示环节的评分标准（来源：教育部教师资格考试大纲），但不知道评分标准与真实课堂控场能力之间的相关性。",
        "你关注微格教学对师范生教学技能培养的效果（来源：Allen & Ryan, 1969, Microteaching），但对微格教学向真实课堂的迁移效果存疑。",
        "你了解模拟教学在医学教育中的成功应用（来源：医学教育中标准化病人研究综述），但不确定该方法在师范教育中的适用性和成本。",
        "你关注教师自我效能感的培养途径（来源：Tschannen-Moran & Hoy, 2001, Teaching and Teacher Education），但缺乏针对师范生首次上讲台的纵向追踪数据。",
        "你掌握部分高校师范生教学技能训练的改革方案（来源：教育部师范类专业认证标准），但担心方案落地过程中师资和课时资源的瓶颈。"
    ],
    "知识传授与核心素养_真实": [
        "你了解核心素养框架的国际比较研究（来源：OECD, 2018, Education 2030），但不清楚国内教师对该框架的实际理解程度。",
        "你掌握义务教育课程标准（2022年版）中对核心素养的要求（来源：教育部义务教育课程方案和课程标准），但缺少一线课堂落实情况的实证数据。",
        "你关注PISA测试中中国学生表现的优势与短板（来源：OECD PISA 2018 Results），但不知道PISA表现与日常课堂教学之间的直接关联。",
        "你了解项目式学习在核心素养培养中的作用（来源：Buck Institute for Education, PBL研究综述），但担心该方法在应试压力下的推广阻力。",
        "你关注教师专业发展与课程改革的协同问题（来源：Fullan, 2007, The New Meaning of Educational Change），但缺乏本校教师对新课标适应性的调查数据。",
        "你掌握部分学校在核心素养评价方面的探索（来源：教育部基础教育课程教材发展中心案例集），但担心评价体系的复杂性和教师负担问题。"
    ],
    "师范生实习反馈_真实": [
        "你了解教育实习评价的理论框架（来源：Darling-Hammond, 2006, Journal of Teacher Education），但缺乏本校实习评价体系的效度分析。",
        "你掌握教育部师范生教育实践的相关规定（来源：教育部关于加强师范生教育实践的意见），但不知道各高校在执行层面的差异性。",
        "你关注电子档案袋在教师成长追踪中的应用（来源：Barrett, 2007, Journal of Adolescent & Adult Literacy），但担心技术平台建设和维护成本。",
        "你了解同伴互评在教师专业发展中的作用（来源：Wenger, 1998, Communities of Practice），但不确定师范生群体中同伴互评的接受度和信效度。",
        "你关注实习指导教师的能力建设问题（来源：教师教育者专业标准相关研究），但缺乏本校实习指导教师培训需求的系统调查。",
        "你掌握部分高校师范生实习改革的试点经验（来源：教育部师范类专业认证优秀案例），但担心改革的可持续性和推广成本。"
    ],
    "AI英语口语_真实": [
        "你了解AI语音评测技术的原理和局限（来源：自动语音识别技术综述），但缺乏本校学生对该技术的使用偏好和接受度数据。",
        "你掌握大学英语教学指南中对口语能力的要求（来源：教育部大学英语教学指南），但不知道本校新生的口语能力基线水平和主要瓶颈。",
        "你关注AI口语工具对学习动机的影响研究（来源：计算机辅助语言学习领域动机研究综述），但缺乏长期使用效果的追踪数据。",
        "你了解自适应学习技术在语言教学中的应用（来源：个性化学习系统在二语习得中的研究综述），但担心技术成本与教学效益的平衡。",
        "你关注学生课外口语练习的投入问题（来源：二语习得中自主学习研究），但不确定AI工具能否有效提升课外练习的频次和质量。",
        "你掌握部分高校AI辅助英语教学的实施案例（来源：教育部教育信息化2.0行动计划成果汇编），但担心技术依赖可能削弱人际交流能力的培养。"
    ],
    "AI与就业_真实": [
        "你了解AI对就业市场影响的宏观研究（来源：Frey & Osborne, 2017, Technological Forecasting and Social Change），但缺乏针对中国大学生群体的专门分析。",
        "你掌握教育部高校毕业生就业质量年度报告的核心数据（来源：教育部高校学生司就业质量报告），但不知道AI相关岗位的需求增长趋势和技能要求变化。",
        "你关注技能错配与结构性失业的研究（来源：ILO, 2020, Global Employment Trends for Youth），但缺乏本校专业设置与市场需求匹配度的分析。",
        "你了解AI职业导航工具的应用案例（来源：部分高校AI就业指导平台实施报告），但担心算法的公平性和数据隐私问题。",
        "你关注大学生AI素养的现状调查（来源：国内外AI素养调查研究综述），但不知道本校学生的AI知识储备和技能水平。",
        "你掌握高校校企合作培养AI人才的案例（来源：教育部产学合作协同育人项目成果汇编），但担心合作质量参差不齐和持续性不足。"
    ],
    "校园垃圾分类_真实": [
        "你了解住建部垃圾分类政策要求（来源：住建部关于在全国地级及以上城市全面开展生活垃圾分类工作的通知），但缺乏高校场景下的实施效果评估数据。",
        "你掌握部分高校垃圾分类的试点经验（来源：教育部绿色校园创建优秀案例集），但不知道本校学生的分类意识和行为现状。",
        "你关注行为经济学在垃圾分类激励中的应用（来源：Nudge理论在环保行为中的应用研究），但担心激励措施的持续性和成本。",
        "你了解智能垃圾分类设备的技术方案（来源：智能垃圾分类技术综述），但担心设备采购和维护的经费来源。",
        "你关注学生环保行为的心理驱动因素（来源：环境心理学中环保行为影响因素研究），但缺乏针对本校学生的专项调查。",
        "你掌握部分城市垃圾分类的实施效果数据（来源：住建部垃圾分类考核通报），但不知道校园场景与社区场景的关键差异。"
    ],
    "社团与学习平衡_真实": [
        "你了解大学生时间管理的理论模型（来源：Britton & Tesser, 1991, Journal of Educational Psychology），但缺乏本校学生时间分配的实证数据。",
        "你掌握课外活动与学业表现关系的部分研究（来源：Astin, 1984, Journal of College Student Personnel），但不知道本校的具体情况。",
        "你关注社团参与对就业能力的影响（来源：大学生课外活动与就业竞争力相关研究），但缺乏对过度参与的负面效应的分析。",
        "你了解高校社团管理制度的相关规定（来源：团中央高校社团管理指导意见），但不知道本校制度的执行情况和学生满意度。",
        "你关注学生自我调节学习能力的培养（来源：Zimmerman, 2002, Theory Into Practice），但不确定社团活动是否能促进或抑制这种能力。",
        "你掌握部分高校时间管理课程的实施案例（来源：大学生学习指导课程改革项目成果），但担心学分压力下学生的选课意愿。"
    ],
    "校园数字化_真实": [
        "你了解教育部教育信息化2.0行动计划的政策框架（来源：教育部教育信息化2.0行动计划），但缺乏本校信息化建设的现状评估数据。",
        "你掌握智慧校园建设的技术标准和规范（来源：GB/T 36342-2018 智慧校园总体框架），但不知道本校师生对现有系统的满意度和核心痛点。",
        "你关注高校数据治理的挑战与对策（来源：高校数据治理研究综述），但担心数据孤岛和系统互通的技术障碍。",
        "你了解数字校园的用户体验研究（来源：教育信息化用户体验评估框架），但缺乏本校师生使用习惯和需求的系统调查。",
        "你关注教育数据安全与隐私保护（来源：网络安全法及个人信息保护法相关要求），但不确定本校现有的数据安全措施是否满足合规要求。",
        "你掌握部分高校数字化转型的案例（来源：教育部智慧教育示范区建设成果），但担心经费投入和技术人才的瓶颈。"
    ],
    "AI代写作业_真实": [
        "你了解国内外高校对AI代写的政策差异（来源：Turnitin, 2023, AI Writing Detection Report），但缺乏本校学生的实际AI使用频率和场景数据。",
        "你掌握本校近两年课程论文的查重率数据（来源：教务处内部统计），但无法区分哪些重复来自AI生成、哪些来自传统抄袭。",
        "你关注形成性评价在防止AI代写中的作用（来源：Boud & Falchikov, 2007, Assessment & Evaluation in Higher Education），但对大规模课堂的实施成本存疑。",
        "你了解过程性评价工具（如Portfolio、学习日志）的技术实现方案（来源：JISC, 2022, Digital Assessment Review），但担心教师的培训和接受度。",
        "你关注AI检测工具的公平性问题（来源：Sadasivan et al., 2023, Patterns），尤其是对非母语写作者可能存在的误判风险。",
        "你掌握部分高校“AI辅助声明+过程答辩”制度的试点经验（来源：教育部2024年AI教育应用规范试点报告），但不确定该制度在本校的推广可行性和学生配合度。"
    ],
    "在线课程vs传统课堂_真实": [
        "你了解在线学习效果的元分析结论（来源：Means et al., 2013, Teachers College Record），但缺乏本校线上与线下课程学习效果的直接对比数据。",
        "你掌握本校在线课程平台的登录时长和完成率数据（来源：教务处LMS后台统计），但不知道学生的实际学习投入度和屏幕背后的行为模式。",
        "你关注自我调节学习能力在在线课程中的关键作用（来源：Broadbent & Poon, 2015, Internet and Higher Education），但对如何测量和培养这种能力缺乏操作方案。",
        "你了解混合式学习的最佳实践模型（来源：Graham, 2006, Handbook of Blended Learning），但不确定本校教师的混合教学设计能力和意愿。",
        "你关注在线课程中的社交临场感缺失问题（来源：Garrison et al., 2000, Internet and Higher Education），但缺乏用低成本技术手段弥补该缺失的具体方案。",
        "你掌握部分高校线上线下等效性认证的标准（来源：Quality Matters Higher Education Rubric, 2020），但担心认证流程的复杂性和持续性投入。"
    ],
    "社区充电桩_真实": [
        "你了解国内新能源汽车充电桩的保有量和政策目标（来源：国务院办公厅关于加快电动汽车充电基础设施建设的指导意见），但缺乏老旧小区电容改造的实际成本数据。",
        "你掌握本社区居民电动车保有量的问卷调查结果（来源：社区居委会2025年摸底数据），但不知道居民的真实充电行为和峰谷时段分布。",
        "你关注共享经济模式在充电桩运营中的应用（来源：共享经济在公共服务领域的研究综述），但担心共享模式下的设备损耗和维护责任归属问题。",
        "你了解智能充电桩的预约和负载均衡技术方案（来源：国家电网智慧能源服务平台技术白皮书），但对老旧小区的电网改造难度和审批流程存疑。",
        "你关注充电桩选址的公平性问题（来源：城市公共服务设施空间公平性研究），尤其是无车居民对公共空间被占用的反对声音。",
        "你掌握部分城市“统建统营”模式的试点经验（来源：住建部2024年完整社区建设试点案例），但担心该模式在本社区的民意基础和物业配合度。"
    ],
    "分布式缓存系统_真实": [
        "你了解Redis Cluster的哈希槽机制和Gossip协议（来源：Redis官方文档），但缺乏在实际业务负载下进行节点动态扩缩容的测试数据。",
        "你掌握Memcached的分段LRU淘汰算法细节（来源：Memcached源码分析），但不知道在冷热数据分层场景下与Redis的混合架构如何设计。",
        "你关注缓存雪崩与缓存穿透的预防策略（来源：《高可用架构设计实践》白皮书），但对不同策略（随机过期、熔断、布隆过滤器）的复杂性和运维成本缺乏量化比较。",
        "你了解一致性哈希算法和虚拟节点的设计（来源：Karger et al., 1997, STOC），但担心在节点故障时，数据迁移的瞬时延迟对核心业务的影响。",
        "你关注缓存与数据库双写一致性的问题（来源：分布式系统设计模式综述），但缺乏在高并发场景下验证不同一致性策略（先删缓存、延迟双删）效果的一手数据。",
        "你掌握部分大型互联网公司缓存架构的公开案例（来源：大型网站技术架构演进），但不确定这些方案在中等规模业务和有限运维资源下的适用性。"
    ],
    "并行计算优化_真实": [
        "你了解OpenMP的多种调度策略（static, dynamic, guided）（来源：OpenMP官方规范），但缺乏在非均匀任务场景下对这些策略的实际性能对比数据。",
        "你掌握GPU线程束（Warp）发散对性能的影响（来源：NVIDIA CUDA编程指南），但不知道如何针对特定算法模式（如稀疏矩阵运算）重组数据布局以减少发散。",
        "你关注MPI通信与计算重叠的优化技术（来源：MPI标准文档与最佳实践），但担心在异构网络环境下，重叠策略的实际收益和调试复杂性。",
        "你了解共享内存编程中的伪共享（False Sharing）问题（来源：Hennessy & Patterson, 计算机体系结构），但对如何在复杂数据结构中检测和修复伪共享缺乏实操经验。",
        "你关注Intel TBB的工作窃取（Work Stealing）调度机制（来源：Intel TBB文档），但不确定在任务粒度差异极大的场景下，它是否优于静态划分。",
        "你掌握一些在超算上优化Linpack基准测试的技巧（来源：TOP500技术报告），但担心这些针对特定硬件的极致优化是否适用于通用的中小规模GPU集群。"
    ],
    "嵌入式内存管理_真实": [
        "你了解FreeRTOS提供的多种堆管理方案（heap_1到heap_5）（来源：FreeRTOS官方文档），但缺乏在长期运行、频繁分配/释放场景下，不同方案内存碎片化速率的对比数据。",
        "你掌握内存池（Memory Pool）固定大小分配的O(1)时间优势（来源：嵌入式系统内存管理实践），但担心内部碎片带来的平均约25%的空间浪费在资源极度受限的设备上是否可接受。",
        "你关注栈溢出检测的软硬件方法（来源：ARM Cortex-M3技术参考手册），但对不同检测机制（MPU vs 哨兵值）的性能开销和实现复杂度缺乏量化评估。",
        "你了解TLSF（Two-Level Segregated Fit）算法的实时性优势（来源：Masmano et al., 2004），但缺乏在MCU上与经典buddy system算法的对比测试数据。",
        "你关注Rust语言所有权模型在避免内存泄漏方面的潜力（来源：Rust编程语言官方文档），但担心其在嵌入式开发中的编译工具链成熟度和生态支持。",
        "你掌握静态内存分配在安全关键系统（如MISRA C规范）中的优势（来源：MISRA C编码规范），但不确定在中等复杂度的应用中，静态分配对开发效率和灵活性的实际影响。"
    ],
    "去中心化身份认证_真实": [
        "你了解W3C的DID（去中心化标识符）核心架构（来源：W3C DID Core Specification），但对不同DID方法（如did:web, did:ethr）的性能和安全性假设缺乏深入比较。",
        "你掌握零知识证明（ZKP）的基本原理（来源：Goldwasser, Micali & Rackoff, 1985），但担心在移动端身份认证场景中，生成ZKP证明的计算开销和耗时是否满足实时性要求。",
        "你关注DID生态系统中的凭证撤销难题（来源：ACM CCS 2023相关论文），但不确定累加器方案、链上注册表等不同撤销机制的效率、成本和去中心化程度的平衡点。",
        "你了解OAuth 2.0和OpenID Connect的授权流程（来源：OAuth 2.0 RFC 6749），但不确定如何在DID架构下兼容现有的授权码流程，以降低应用方的接入门槛。",
        "你关注用户私钥管理和恢复的社会工程学挑战（来源：加密货币钱包安全研究综述），但缺乏在非技术用户群体中，去中心化身份方案的可用性测试数据。",
        "你掌握欧盟eIDAS 2.0对数字身份钱包的监管框架（来源：欧盟eIDAS 2.0提案），但不确定合规要求（如可追溯性）是否会限制纯去中心化方案的设计灵活性。"
    ],
    "深度神经网络推理优化_真实": [
        "你了解知识蒸馏（Knowledge Distillation）的基本框架（来源：Hinton et al., 2015），但缺乏在Transformer类大模型上应用不同蒸馏策略（logit, feature, relation）的效果对比。",
        "你掌握训练后量化（PTQ）和量化感知训练（QAT）的差异（来源：Jacob et al., 2018, CVPR），但不确定在4-bit量化下，模型推理精度的损失是否在可接受范围内。",
        "你关注模型剪枝中结构化与非结构化方法的差异（来源：Han et al., 2015, NIPS），但对剪枝后模型在实际硬件（如GPU）上的推理加速比缺乏量化评估，担心理论加速与实际不符。",
        "你了解TensorRT和ONNX Runtime的图优化与算子融合技术（来源：NVIDIA TensorRT文档与ONNX Runtime文档），但担心过度依赖特定推理框架会带来供应商锁定风险。",
        "你关注大模型推理阶段的能耗与碳排放问题（来源：Strubell et al., 2019），但缺乏对不同优化策略（剪枝、量化、蒸馏）在全生命周期碳足迹上的影响评估。",
        "你掌握部分云厂商提供的模型推理服务（MaaS）部署案例（来源：各云厂商MaaS白皮书），但不确定自建推理集群与使用云服务在总拥有成本（TCO）上的长期差异。"
    ],
}

# ---------- 记忆系统 ----------
class ExperimentMemory:
    def __init__(self):
        self.data = self._load()
    def _load(self):
        db_path = os.path.join("..", "data", "experiments_db.json")
        if os.path.exists(db_path):
            try:
                with open(db_path, 'r', encoding='utf-8') as f: return json.load(f)
            except: pass
        return {"experiments": [], "summary": {}}
    def save(self):
        db_path = os.path.join("..", "data", "experiments_db.json")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with open(db_path, 'w', encoding='utf-8') as f: json.dump(self.data, f, ensure_ascii=False, indent=2)
    def add_experiment(self, record):
        self.data["experiments"].append(record); self._update_summary(); self.save()
    def _update_summary(self):
        exps = self.data["experiments"]
        if not exps: return
        default_scores = [e["scores"]["综合评分"] for e in exps if e.get("personality_mode") == "默认人格"]
        optimal_scores = [e["scores"]["综合评分"] for e in exps if e.get("personality_mode") == "最优人格"]
        random_scores = [e["scores"]["综合评分"] for e in exps if e.get("personality_mode") == "随机人格"]
        closed_scores = [e["scores"]["综合评分"] for e in exps if e.get("personality_mode") == "关闭人格"]
        real_scores = [e["scores"]["综合评分"] for e in exps if e.get("info_mode") == "真实信息"]
        inferred_scores = [e["scores"]["综合评分"] for e in exps if e.get("info_mode") == "推断信息"]
        mixed_scores = [e["scores"]["综合评分"] for e in exps if e.get("info_mode") == "混合信息"]
        with_intervention = [e["scores"]["综合评分"] for e in exps if e.get("intervention") == "有"]
        without_intervention = [e["scores"]["综合评分"] for e in exps if e.get("intervention") == "无"]
        # V15 优化（2026-09-25）：干预时机是新的自变量维度。
        # 旧数据为预设一次性注入（predefined_once），新数据为逐轮决策（per_round），
        # **两者不可合并统计**（同“跨批次素材不同不合并”的规矩）。
        by_timing = {}
        for t in ("predefined_once", "per_round"):
            vals = [e["scores"]["综合评分"] for e in exps if e.get("intervention_timing") == t]
            by_timing[t] = {"count": len(vals),
                            "avg_score": round(sum(vals) / len(vals), 2) if vals else 0}
        self.data["summary"] = {
            "total_experiments": len(exps),
            "by_personality": {
                "默认人格": {"count": len(default_scores), "avg_score": round(sum(default_scores)/len(default_scores),2) if default_scores else 0},
                "最优人格": {"count": len(optimal_scores), "avg_score": round(sum(optimal_scores)/len(optimal_scores),2) if optimal_scores else 0},
                "随机人格": {"count": len(random_scores), "avg_score": round(sum(random_scores)/len(random_scores),2) if random_scores else 0},
                "关闭人格": {"count": len(closed_scores), "avg_score": round(sum(closed_scores)/len(closed_scores),2) if closed_scores else 0}
            },
            "by_info_mode": {
                "真实信息": {"count": len(real_scores), "avg_score": round(sum(real_scores)/len(real_scores),2) if real_scores else 0},
                "推断信息": {"count": len(inferred_scores), "avg_score": round(sum(inferred_scores)/len(inferred_scores),2) if inferred_scores else 0},
                "混合信息": {"count": len(mixed_scores), "avg_score": round(sum(mixed_scores)/len(mixed_scores),2) if mixed_scores else 0}
            },
            "by_intervention": {
                "有干预": {"count": len(with_intervention), "avg_score": round(sum(with_intervention)/len(with_intervention),2) if with_intervention else 0},
                "无干预": {"count": len(without_intervention), "avg_score": round(sum(without_intervention)/len(without_intervention),2) if without_intervention else 0}
            },
            "by_intervention_timing": by_timing,
            "last_updated": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    def get_summary(self): return self.data.get("summary", {})
    def get_recent(self, n=10): return self.data["experiments"][-n:]
    def get_count(self): return len(self.data["experiments"])
    def get_recent_configs(self, n=10):
        return [{"personality_mode": e.get("personality_mode"), "info_mode": e.get("info_mode")} for e in self.get_recent(n)]

# ---------- 主教练类 ----------
def _parse_json_obj(text):
    """从模型输出里抠出第一个 JSON 对象（容错）。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            return None
    return None


class Coach:
    def __init__(self, api_caller):
        self.api_caller = api_caller

    # ---------- V15 优化（2026-09-25）：本局内逐轮干预决策 ----------
    def decide_intervention(self, topic, round_num, max_rounds, signals, recent_logs,
                            candidates=None):
        """当轮干预决策。返回 dict 或 None（**明确允许不干预**）。

        进入本方法前，是否值得干预已由本地阈值（compute_round_signals）判定；
        本方法只让模型做两件事：① 在白名单指令里选一个；② 给出触发依据与讨论原文证据。
        **模型不得自创指令**——不在白名单的返回值一律丢弃。
        """
        if not signals or not signals.get("should_consider"):
            return None
        allowed = list(INTERVENTION_TEMPLATES.keys())
        hint = ""
        if candidates:
            hint = u"\n（开局候选，仅供参考，不是必须选：%s）" % u"、".join(str(c) for c in candidates)
        prompt = INTERVENTION_DECISION_PROMPT.format(
            round_num=round_num,
            max_rounds=max_rounds,
            topic=topic,
            signals_text=format_signals_text(signals),
            recent_logs=(u"\n".join(recent_logs or [])[-1500:] or u"（本轮暂无发言）"),
            allowed_actions=u"、".join(allowed) + hint,
        )
        try:
            result = self.api_caller(prompt, max_tokens=400, temperature=0.2, timeout=20)
        except Exception as e:
            print(f"[主教练] 干预决策调用失败，本轮不干预: {e}")
            return None
        data = _parse_json_obj(result)
        if not data:
            return None
        action = data.get("action")
        if not action or str(action).strip().lower() in ("null", "none", "无", "不干预", "无干预"):
            return None
        action = str(action).strip()
        if action not in allowed:
            print(f"[主教练] 决策返回白名单外的指令（{action}），本轮不干预")
            return None
        return {
            "round": round_num,
            "action": action,
            "trigger": str(data.get("trigger") or "").strip(),
            "evidence": str(data.get("evidence") or "").strip(),
            "target": data.get("target") if isinstance(data.get("target"), list) else [],
            "signal_values": {k: signals.get(k) for k in
                              ("repeat_rate", "off_topic", "tension",
                               "unsupported_claims", "cited_count", "triggers")},
            "source": "in_round",
        }

    def next_session_suggestions(self, audit_opinions):
        """审计意见 → 下一局的干预建议。

        V15 优化（2026-09-25）：审计发生在讨论**结束之后**，其产出**不得覆盖本局的干预记录**
        （否则等于用事后信息改写本局设计）。此处只作为「留给下一局」的先验建议。
        """
        return [i["action"] for i in self.analyze_concerns(audit_opinions or [])]


    def audit_design(self, topic, experiment_config, report_summary, emergence_summary, memory=None):
        prompt = COACH_AUDIT_PROMPT.format(
            topic=topic,
            personality_mode=experiment_config.get('personality_mode', '未知'),
            info_mode=experiment_config.get('info_mode', '未知'),
            report_summary=report_summary[:800] if report_summary else '无',
            emergence_summary=emergence_summary[:500] if emergence_summary else '无'
        )
        result = self.api_caller(prompt, max_tokens=3000, temperature=0.7, timeout=30)
        opinions = []
        if result:
            opinions = [line.strip() for line in result.strip().split('\n') if line.strip()]
            if not opinions:
                opinions = [result.strip()]
        else:
            opinions = ["审计分析生成失败"]

        if memory:
            recent_configs = memory.get_recent_configs(10)
            if len(recent_configs) >= 5:
                mode_counts = {}
                for cfg in recent_configs:
                    key = f"{cfg['personality_mode']}+{cfg['info_mode']}"
                    mode_counts[key] = mode_counts.get(key, 0) + 1
                dominant_count = max(mode_counts.values()) if mode_counts else 0
                if dominant_count >= len(recent_configs) * 0.8:
                    opinions.append(f"【配置多样性警告】最近{len(recent_configs)}次实验中，单一配置占比超过80%。当前实验设计存在'配置单一性'风险，强烈建议在下一轮实验中切换人格模式或信息源，以覆盖配置空间。")

        return opinions

    def analyze_concerns(self, audit_opinions):
        interventions = []
        for opinion in audit_opinions:
            for concern, action in AUDIT_CONCERN_MAP.items():
                if concern in opinion:
                    interventions.append({"concern": concern, "action": action})
        return interventions

    def chat(self, user_message, memory_db=None):
        """主教练对话。
        提交版：无实验记忆库（memory_db=None），用产品定位上下文直接对话；
        若传入实验记忆库，仍走完整分层分析（兼容旧版）。
        """
        if memory_db is None:
            context = f"""你是群智系统的主教练，负责监督多智能体决策预演。
你的角色：审视线索、发现盲区、指导讨论方向。
【用户提问】{user_message}
请直接回答，给出有洞察的分析。"""
            response = self.api_caller(context, max_tokens=8000, temperature=0.7, timeout=35)
            return response if response else "我暂时无法回答。"

        summary = memory_db.get_summary()
        total = memory_db.get_count()
        all_exps = memory_db.data.get("experiments", [])

        # ============================================================
        #  第一层：始终加载的全局摘要（极低成本）
        # ============================================================
        layer1 = f"""总实验次数：{total}
按人格模式：{json.dumps(summary.get('by_personality',{}), ensure_ascii=False)}
按信息源：{json.dumps(summary.get('by_info_mode',{}), ensure_ascii=False)}
按干预有无：{json.dumps(summary.get('by_intervention',{}), ensure_ascii=False)}"""

        # ============================================================
        #  第二层：高频审计意见统计（中等成本）
        # ============================================================
        layer2_parts = []
        all_audits = []
        for exp in all_exps:
            audits = exp.get("coach_audit", [])
            if isinstance(audits, list):
                all_audits.extend(audits)

        concern_keywords = ["可验证性缺失", "共识幻觉", "技术乐观主义", "伦理缺位",
                           "人格同质化", "机制过载", "理想执行者假设", "配置多样性警告"]
        concern_counts = {}
        for audit in all_audits:
            for kw in concern_keywords:
                if kw in audit:
                    concern_counts[kw] = concern_counts.get(kw, 0) + 1

        top_concerns = sorted(concern_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        if top_concerns:
            layer2_parts.append("【高频审计意见】")
            for kw, count in top_concerns:
                example = next((a for a in all_audits if kw in a), "")
                layer2_parts.append(f"- {kw}（出现{count}次）示例：{example[:120]}...")

        layer2 = "\n".join(layer2_parts) if layer2_parts else "暂无审计数据"

        # ============================================================
        #  第三层：全局笔记（极低成本，但极有价值）
        # ============================================================
        global_notes = memory_db.data.get("global_notes", [])
        layer3 = ""
        if global_notes:
            recent_notes = global_notes[-10:]
            layer3 = "【主教练历史笔记（按时间倒序）】\n" + "\n".join(
                f"- [{n.get('timestamp','')}] {n.get('note','')}" for n in reversed(recent_notes)
            )

        # ============================================================
        #  拼接最终上下文
        # ============================================================
        context = f"""你是主教练，拥有全部 {total} 次实验的全局视野。

【第一层：全局统计摘要】
{layer1}

【第二层：高频审计意见与典型案例】
{layer2}

【第三层：历史全局笔记】
{layer3 if layer3 else "暂无历史笔记"}

【第四层：原始报告检索能力】
如果你需要查看某次实验的完整报告，请告诉我实验编号，我可以为你调取。

【用户提问】{user_message}

请基于上述全部数据进行分析。如果需要更细粒度的数据，请明确指出。"""

        response = self.api_caller(context, max_tokens=8000, temperature=0.7, timeout=35)
        return response if response else "我暂时无法回答。"

# ---------- 玩家类 ----------
class SandboxPlayer:
    def __init__(self, name, prototype_key, profession_idx, value_idx, value_weights=None):
        self.name = name; self.prototype_key = prototype_key
        self.prototype = COGNITIVE_PROTOTYPES[prototype_key]
        self.profession = PROFESSIONAL_BACKGROUNDS[profession_idx]
        self.value_idx = value_idx  # V15：保存主价值观索引（波动锚定用）
        self.value = VALUE_ORIENTATIONS[value_idx]
        # V15：多价值观权重（主价值观50% + 其余随机分配），支持讨论中波动
        self.base_value_weights = value_weights if value_weights else _default_value_weights(value_idx)
        self.value_weights = dict(self.base_value_weights)
        self.session_memory = []; self.known_info = ""; self.topic = ""
        self.speech_count = 0; self.speech_history = []
        self.temperature = 0.75  # V15：个体温度，讨论中波动

    def init_for_session(self, topic, info_fragment, user_thought=""):
        self.topic = topic; self.known_info = info_fragment
        self.speech_count = 0; self.speech_history = []
        self.session_memory = [
            f"课题：{topic}", f"已知信息：{info_fragment}",
            f"性格：{self.prototype['name']}——{self.prototype['style']}",
            f"专业：{self.profession['name']}{self.profession['icon']}——{self.profession['perspective']}",
            f"价值取向：{self.value['name']}{self.value['icon']}——{self.value['belief']}"
        ]
        if user_thought: self.session_memory.append(f"观察者想法：「{user_thought}」。")

    def pre_deliberate(self, rounds: int = 1):
        """V15.3：讨论前自我讨论（认知锚点）
        每个玩家先独自对课题形成初步立场（1轮，控制成本），
        再参与团队讨论——让第 1 轮发言更有深度。
        A/B 验证：2 轮 vs 0 轮 → 实用性 +1 分但耗时 +17%；
        改为 1 轮（砍掉自我质疑），保留大部分收益、成本减半。
        """
        if not self.topic:
            return
        identity = (
            f"你是讨论参与者{self.name}。\n"
            f"性格：{self.prototype['name']}——{self.prototype['style']}\n"
            f"专业：{self.profession['name']}——{self.profession['perspective']}\n"
            f"价值取向：{self.value['name']}——{self.value['belief']}\n"
            f"你已知的信息：{self.known_info or '（无外部信息，基于专业背景思考）'}\n"
            f"课题：{self.topic}"
        )
        thought = f"【初步立场】从你的专业和价值观出发，你认为这个课题最核心的问题是什么？你初步的立场是什么？给出 1 个你特有的观点和 1 个你认为大家最容易忽略的点。（150字内）"
        r1 = call_with_retry(f"{identity}\n\n{thought}", max_tokens=300, temperature=0.6, timeout=15)
        if not r1:
            return
        anchor = f"【我的认知锚点（讨论前思考）】{r1}"
        self.session_memory.append(anchor)
        self.cognitive_anchor = anchor

    def value_weights_text(self):
        """生成价值观权重描述（按权重降序，主价值观突出）"""
        sorted_w = sorted(self.value_weights.items(), key=lambda x: x[1], reverse=True)
        parts = []
        for idx, w in sorted_w:
            v = VALUE_ORIENTATIONS[idx]
            pct = int(round(w * 100))
            if pct >= 15:
                parts.append(f"{v['name']}{v['icon']}（权重{pct}%）：{v['belief']}")
        return "\n".join(parts) if parts else self.value['name']

    def fluctuate(self, round_num, max_rounds):
        """V15：每轮价值观权重 + 温度波动（主价值观保持50%锚定，其余小幅漂移）"""
        # 温度波动：±0.08，随轮次增加轻微升温（讨论深入更放开）
        drift = round(random.uniform(-0.08, 0.08), 3)
        self.temperature = max(0.5, min(0.95, 0.75 + drift + round_num * 0.02))
        # 价值观波动：主价值观保持 ~50%，其余权重小幅漂移后归一化
        weights = dict(self.base_value_weights)
        # 非主价值观小幅漂移（±0.04）
        for k in weights:
            if k != self.value_idx:
                weights[k] = max(0.02, weights[k] + random.uniform(-0.04, 0.04))
        # 归一化（主价值观锚定 0.5）
        others = [k for k in weights if k != self.value_idx]
        total_others = sum(weights[k] for k in others)
        if total_others > 0:
            for k in others:
                weights[k] = (weights[k] / total_others) * 0.5
        weights[self.value_idx] = 0.5
        self.value_weights = weights

    def speak(self, round_num, recent_logs, all_players):
        other_names = [p.name for p in all_players if p.name != self.name]
        log_context = "\n".join(recent_logs[-8:]) if recent_logs else "（讨论刚开始）"
        # 引用约束开关（A/B 对照用；默认开启）
        citation_rule = CITATION_RULE_ON if getattr(self, "citation_constraint", True) else CITATION_RULE_OFF
        prompt = SPEAK_PROMPT.format(
            name=self.name,
            prototype_name=self.prototype['name'],
            prototype_style=self.prototype['style'],
            profession_name=self.profession['name'],
            profession_perspective=self.profession['perspective'],
            value_name=self.value['name'],
            value_belief=self.value['belief'],
            topic=self.topic,
            known_info=self.known_info,
            round_num=round_num,
            other_names=', '.join(other_names),
            log_context=log_context,
            value_weights=self.value_weights_text(),
            temperature=self.temperature,
            citation_rule=citation_rule,
        )
        result = call_with_retry(prompt, max_tokens=400, temperature=self.temperature, timeout=15)
        if result:
            self.speech_count += 1
            self.speech_history.append({"round": round_num, "content": result})
            self.session_memory.append(f"我说：{result}")
            return result
        return "我需要更多信息来形成有深度的观点，希望先听听其他参与者的看法。"

    def hear(self, speaker_name, text): self.session_memory.append(f"{speaker_name}说：{text}")


def _default_value_weights(main_idx: int) -> dict:
    """默认多价值观权重：主价值观 50%，其余 50% 随机分配"""
    weights = {i: 0.0 for i in range(len(VALUE_ORIENTATIONS))}
    weights[main_idx] = 0.5
    remaining = 0.5
    others = [i for i in range(len(VALUE_ORIENTATIONS)) if i != main_idx]
    random.shuffle(others)
    # 随机分成5份（保证都>0）
    cuts = sorted(random.uniform(0, 1) for _ in range(len(others) - 1))
    cuts = [0.0] + cuts + [1.0]
    for i, o in enumerate(others):
        weights[o] = remaining * (cuts[i + 1] - cuts[i])
    return weights

# ---------- 会话类 ----------
class SandboxSession:
    def __init__(self, topic, config, max_rounds=3, user_thought="", custom_infos=None, preset_name=None, experiment_mode=None, force_dynamic_info=False, coach_interventions=None, lite_mode=False, info_pool=None):
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.topic = topic; self.max_rounds = max_rounds; self.user_thought = user_thought
        self.experiment_mode = experiment_mode or {}
        self.lite_mode = lite_mode  # 学校平台快速模式：只做讨论+报告，跳过锚点/涌现/审计等花活
        self.players = []; self.logs = []
        self.report = None; self.emergence_analysis = None; self.golden_insight = None
        self.cards = None; self.report_evaluation = None; self.coach_audit_opinions = []
        self.coach_interventions = coach_interventions or []
        self.running = False; self.current_round = 0; self.vote_history = []
        self.last_speaker_prof = None; self.single_agent_comparison = None

        # ---------- V15 优化（2026-09-25）：引用与干预记录 ----------
        # info_pool：主教练本轮实际注入的结构化信息池（含编号 P1..Pn），
        #            报告末尾的参考文献表与引用核验都由它生成，不由模型生成。
        self.info_pool = _citations().normalize_pool(info_pool or [])
        self.references = _citations().to_meta_list(self.info_pool)
        self.citation_check = None            # 报告生成后填充（check_citations 的结果）
        self.intervention_records = []        # 本局逐轮干预记录（含 trigger/evidence/signal_values）
        self.intervention_timing = (self.experiment_mode or {}).get("intervention_timing", "per_round")
        self.next_session_suggestions = []    # 审计产出，仅留给下一局，不覆盖本局
        # 2026-10-24（③）：6 段背景信息的真实来源，用于报告如实标注
        #   custom   = 检索到的真实片段（含 [P#] 编号）
        #   inferred = 检索失败后按课题现编（无外部依据）
        #   preset   = 退回通用预置库（与课题无关）
        self.info_source = "custom"

        personality_enabled = self.experiment_mode.get('personality_enabled', True)
        # V15 修复（2026-10-09）：槽位默认值不再退化。
        # 旧默认值统一是 {"proto":"trader","prof":0,"value":0} = 计算机科学/联结者/效率优先，
        # 一旦组队配置的键名与 PLAYER_NAMES 对不上，就会「6 人全是计算机」而且**静默无声**。
        _SLOT_DEFAULT = {"甲": ("hunter", 0, 0), "乙": ("trader", 1, 1), "丙": ("ghost", 2, 2),
                         "丁": ("gambler", 3, 3), "戊": ("skeptic", 4, 4), "己": ("critic", 5, 5)}
        _missing_slots = []
        for name in PLAYER_NAMES:
            if not personality_enabled:
                cfg = {"proto": "hunter", "prof": 0, "value": 0}
            else:
                cfg = config.get(name)
                if not isinstance(cfg, dict) or "proto" not in cfg:
                    _p, _pr, _v = _SLOT_DEFAULT.get(name, ("trader", 0, 0))
                    cfg = {"proto": _p, "prof": _pr, "value": _v}
                    _missing_slots.append(name)
            vw = cfg.get("value_weights") if isinstance(cfg, dict) else None
            self.players.append(SandboxPlayer(name, cfg["proto"], cfg["prof"], cfg["value"], value_weights=vw))
        if _missing_slots:
            # 显式告警：把"静默降级"变成可见事件（含具体槽位）
            self.add_log("system", "⚠️ 组队配置缺少 %d 个槽位（%s），已按槽位默认补齐；"
                                   "若参与者专业重复，请检查主教练组队输出。"
                         % (len(_missing_slots), "、".join(_missing_slots)))
            print(f"[V15] 组队配置缺槽位: {_missing_slots}（已按槽位默认补齐）", flush=True)

        
        info_mode = self.experiment_mode.get('info_mode', 'real')

        if info_mode == "shared_single":
            # F组专用：所有人拿到完全相同的一条信息
            if preset_name and preset_name in PRESET_INFO_LIBRARY:
                info_pool = PRESET_INFO_LIBRARY[preset_name][:]
            else:
                info_pool = PRESET_INFO_LIBRARY.get("通用科研", ["你对这个课题有一般性的了解。"])[:6]
            # 所有人共享同一条信息（取第一条）
            single_info = info_pool[0] if info_pool else "你对这个课题有一般性的了解。"
            infos = [single_info] * 6
        elif info_mode == "shared_real":
            # 6人共享相同的6条信息片段
            if preset_name and preset_name in PRESET_INFO_LIBRARY:
                shared_infos = PRESET_INFO_LIBRARY[preset_name][:]
            else:
                shared_infos = PRESET_INFO_LIBRARY.get("通用科研", ["你对这个课题有一般性的了解。"])[:6]
            while len(shared_infos) < 6:
                shared_infos.append("你对这个课题有一般性的了解。")
            # 将6条信息拼接成一段文本，所有玩家共享
            shared_info_text = "\n".join(shared_infos)
            infos = [shared_info_text] * 6
        elif info_mode == "inferred":
            dynamic = self._generate_dynamic_infos(topic, self.players)
            infos = dynamic if (dynamic and len(dynamic) == 6) else [f"你对课题「{topic}」有一般性的了解，需要基于你的专业背景进行推断性分析。" for _ in range(6)]
        elif info_mode == "mixed":
            if preset_name and preset_name in PRESET_INFO_LIBRARY:
                real_part = PRESET_INFO_LIBRARY[preset_name][:3]
            else:
                real_part = PRESET_INFO_LIBRARY.get("通用科研", ["你对这个课题有一般性的了解。"])[:3]
            inferred_players = self.players[3:] if len(self.players) >= 6 else self.players[3:]
            dynamic_part = self._generate_dynamic_infos(topic, inferred_players)
            if dynamic_part and len(dynamic_part) == 3:
                infos = real_part + dynamic_part
            else:
                fallback = [f"你对课题「{topic}」有一般性的了解，需要基于你的专业背景进行推断性分析。" for _ in range(3)]
                infos = real_part + fallback
        elif custom_infos and len(custom_infos) == 6 and any(i and i.strip() for i in custom_infos):
            infos = custom_infos
            self.info_source = "custom"          # 检索成功：6 段是真实片段（带 [P#] 编号）
        elif (preset_name and preset_name in PRESET_INFO_LIBRARY
              and sum(1 for _s in PRESET_INFO_LIBRARY[preset_name] if "来源" in str(_s)) >= 3):
            # 2026-10-24（③ 修正）：**专属预设优先于现编**。
            #   预置库里有 17 个「_真实」预设，6 段全带真实来源（Redis 官方文档 /
            #   Hinton et al. 2015 / 住建部通知 …），人工整理、可核验 ——
            #   比现编好（现编贴题但无出处）。只有「通用科研 / 小组评分」这类
            #   无来源的通用预设，才允许被现编替代。
            infos = PRESET_INFO_LIBRARY[preset_name][:]
            while len(infos) < 6: infos.append("你对这个课题有一般性的了解。")
            self.info_source = "preset"
        elif force_dynamic_info and not custom_infos:
            # 2026-10-24（③）修复：force_dynamic_info 此前是**死参数**
            #   （只在 __init__ 签名里出现，全文从未被使用），
            #   导致检索失败时直接落到「预置库」——退回与课题无关的通用片段。
            #   现在接上：检索失败且无专属预设 → 按课题现编，并在报告标注「推断生成」。
            dynamic = self._generate_dynamic_infos(topic, self.players)
            if dynamic and len(dynamic) == 6:
                infos = dynamic
                self.info_source = "inferred"
            else:
                infos = PRESET_INFO_LIBRARY.get(preset_name or "通用科研",
                                                ["你对这个课题有一般性的了解。"])[:6]
                self.info_source = "preset"
        elif preset_name and preset_name in PRESET_INFO_LIBRARY:
            infos = PRESET_INFO_LIBRARY[preset_name][:]
            while len(infos) < 6: infos.append("你对这个课题有一般性的了解。")
            self.info_source = "preset"
        else:
            dynamic = self._generate_dynamic_infos(topic, self.players)
            if dynamic and len(dynamic) == 6:
                infos = dynamic
                self.info_source = "inferred"
            else:
                infos = PRESET_INFO_LIBRARY.get("通用科研", ["你对这个课题有一般性的了解。"])[:6]
                self.info_source = "preset"

        # 初始化每个玩家（topic/known_info/session_memory）——V15.3 修复
        # 之前玩家从未 init_for_session，导致 self.topic 为空、预讨论无法工作
        # V15 优化（2026-09-25）：引用硬约束开关传入每位玩家（A/B 对照用）
        citation_constraint = (self.experiment_mode or {}).get("citation_constraint", True)
        for i, p in enumerate(self.players):
            info_frag = infos[i] if i < len(infos) else "你对这个课题有一般性的了解。"
            p.citation_constraint = citation_constraint
            p.init_for_session(topic, info_frag, user_thought)

    def _generate_dynamic_infos(self, topic, players):
        player_descriptions = [f"{p.name}：{p.profession['name']}背景，{p.value['name']}，{p.prototype['name']}" for p in players]
        prompt = f"为多学科讨论会分配背景信息。课题：{topic}\n参与者：{chr(10).join(player_descriptions)}\n为每位生成40-80字个性化信息，含具体片段，有互补性和矛盾。格式：甲：你了解…… 乙：你掌握…… 丙：你关注…… 丁：你有一份…… 戊：你知道…… 己：你担心……"
        result = call_with_retry(prompt, max_tokens=600, temperature=0.7, timeout=15)
        if result:
            infos = {}
            for line in result.strip().split('\n'):
                for name in PLAYER_NAMES:
                    if line.startswith(name + '：') or line.startswith(name + ':'):
                        info = line.split('：',1)[-1].split(':',1)[-1].strip()
                        if info: infos[name] = info
            if len(infos) == 6: return [infos[name] for name in PLAYER_NAMES]
        return None

    def run(self):
        self.running = True
        self.add_log("system", f"课题：{self.topic}")
        if self.user_thought: self.add_log("system", f"观察者想法：{self.user_thought}")
        self.add_log("system", "参与者：")
        for p in self.players:
            self.add_log("system", f"  {p.name}　{p.profession['icon']}{p.profession['name']}　{p.value['icon']}{p.value['name']}　{p.prototype['name']}")

        # V15.3：讨论前每个玩家自我讨论（形成认知锚点）—— lite 模式跳过（提速）
        if not self.lite_mode:
            try:
                self.add_log("system", "🧠 玩家进入自我思考，形成认知锚点...")
                for p in self.players:
                    p.pre_deliberate(rounds=1)
                    anchor = getattr(p, "cognitive_anchor", "")
                    if anchor:
                        self.add_log("system", f"  {p.name} 锚点形成：{anchor[:80]}...")
            except Exception as e:
                print(f"⚠️ 预讨论失败: {e}")

        # ---------- V15 优化（2026-09-25）：主教练改为“本局内逐轮决策” ----------
        # 旧实现：干预在开局前按课题预设，第 2 轮一次性全部注入，之后各轮无干预。
        # 新实现：每轮结束后按**本轮客观信号**决定下一轮是否干预、干预什么；
        #         开局候选（coach_interventions）只作为候选提示传入，不再强制注入。
        coach = Coach(call_with_retry)
        pre_candidates = [c.get("action") if isinstance(c, dict) else c
                          for c in (self.coach_interventions or [])]
        iv_limit = max(1, int(self.max_rounds * IV_MAX_RATIO))
        last_iv_round = -IV_MIN_GAP - 1
        all_speeches = []          # 历史发言（用于重复率）
        round_signal_log = []      # 每轮信号存档（复现用）

        for rnd in range(1, self.max_rounds + 1):
            self.current_round = rnd
            # V15：每轮所有玩家价值观权重 + 温度波动（讨论深入，观点动态演化）
            for p in self.players:
                p.fluctuate(rnd, self.max_rounds)
            self.add_log("round", f"第 {rnd} 轮")

            shuffled = self.players[:]; random.shuffle(shuffled)
            round_speeches = []
            for player in shuffled:
                speech = player.speak(rnd, self._get_recent_messages(), self.players)
                collision = False
                if self.last_speaker_prof is not None and player.profession['name'] != self.last_speaker_prof: collision = True
                self.last_speaker_prof = player.profession['name']
                # plain_text 前端不使用，不再逐条翻译（省 6 次/轮 API 调用）；
                # 大白话版仅在最终报告生成时输出一个
                self.add_log("speech", speech, player, "", collision)
                if speech:
                    round_speeches.append(speech)
                for other in self.players:
                    if other != player: other.hear(player.name, speech)

            # ---- 本轮结束：算信号 → 决定下一轮是否干预（本局内决策，非跨局）----
            sig = compute_round_signals(
                self.topic, round_speeches, all_speeches,
                valid_refs=set(r.get("ref_no") for r in (self.info_pool or [])),
                round_num=rnd,
            )
            round_signal_log.append(sig)
            all_speeches.extend(round_speeches)

            if rnd < self.max_rounds and len(self.intervention_records) < iv_limit \
                    and (rnd - last_iv_round) >= IV_MIN_GAP:
                try:
                    decision = coach.decide_intervention(
                        self.topic, rnd, self.max_rounds, sig,
                        self._get_recent_messages(), candidates=pre_candidates,
                    )
                except Exception as e:
                    self.add_log("system", f"⚠️ 主教练干预决策失败，本轮不干预: {str(e)[:80]}")
                    decision = None
                if decision:
                    self._apply_intervention(decision)
                    self.intervention_records.append(decision)
                    last_iv_round = rnd
                else:
                    # 明确记录“本轮不干预”及其依据——不干预本身也是可报告的行为
                    self.add_log("system",
                                 "🧠 [主教练] 本轮不干预（依据：%s）"
                                 % (u"、".join(sig.get("triggers") or []) or u"信号未触发"))

            if not self.lite_mode and rnd % 2 == 0 and rnd < self.max_rounds:
                vote_thread = threading.Thread(target=self._quick_vote, args=(rnd,), daemon=True)
                vote_thread.start()

        self.round_signals = round_signal_log
        if self.intervention_records:
            self.add_log("system", "🧠 [主教练] 本局共干预 %d 次，分布于第 %s 轮"
                         % (len(self.intervention_records),
                            u"、".join(str(r.get("round")) for r in self.intervention_records)))

        self.add_log("system", "讨论结束，正在生成分析……")

        if self.lite_mode:
            # 学校平台快速模式：只生成报告，跳过涌现/卡片/金句/单AI/评分/审计
            self._generate_report()
            self.running = False
            return

        import threading as thr
        non_core_done = thr.Event()

        def run_non_core():
            for func, name in [
                (self._extract_golden_insight, "金句提取"),
                (self._generate_cards, "洞察卡片"),
                (self._generate_single_agent_comparison, "单体AI对比")
            ]:
                try:
                    func()
                except Exception as e:
                    self.add_log("system", f"⚠️ {name}失败: {e}")
            non_core_done.set()

        non_core_thread = thr.Thread(target=run_non_core, daemon=True)
        non_core_thread.start()

        self._generate_report()
        self._generate_emergence_analysis()
        self._evaluate_report()

        non_core_done.wait(timeout=90)
        if not non_core_done.is_set():
            self.add_log("system", "⚠️ 部分非核心分析超时，不影响主报告")

        # 主教练审计（提交版：无实验记忆库，memory 传 None）
        self._run_coach_audit(None)
        self.running = False

    def _apply_intervention(self, decision):
        """把一条干预决策落到玩家身上（V15 优化 2026-09-25 抽出的公共路径）。

        - 干预文本取自 INTERVENTION_TEMPLATES 白名单；
        - target 为空则视为全体；
        - 「视角切换」额外把一名玩家临时切成批判者（沿用原有实现）。
        """
        action = decision.get("action") or ""
        display = INTERVENTION_TEMPLATES.get(action, action)
        trigger = decision.get("trigger") or ""
        evidence = decision.get("evidence") or ""
        targets = decision.get("target") or []

        log_msg = "🧠 [主教练干预·第%s轮] %s" % (decision.get("round", self.current_round), display)
        if trigger:
            log_msg += "（触发：%s）" % trigger
        if evidence:
            log_msg += "（依据原文：%s）" % evidence[:60]
        self.add_log("system", log_msg)

        mem = "[主教练干预] " + display
        if trigger:
            mem += "（触发：%s）" % trigger
        for player in self.players:
            if not targets or player.name in targets:
                player.session_memory.append(mem)

        if action == "视角切换":
            pool = [p for p in self.players if (not targets or p.name in targets)] or self.players
            target = random.choice(pool)
            target.prototype_key = "critic"
            target.prototype = COGNITIVE_PROTOTYPES["critic"]
            self.add_log("system", "🔀 [主教练视角切换] %s 临时切换为批判者" % target.name)
            target.session_memory.append(
                "[主教练指令] 你现在必须以批判者视角发言，质疑所有方案的逻辑漏洞和伦理边界。")

    def _get_recent_messages(self):
        msgs = []
        for e in self.logs[-15:]:
            if e.get("type") == "speech": msgs.append(f"[{e.get('player','?')}] {e.get('message','')}")
        return msgs

    def _quick_vote(self, rnd):
        questions = ["哪个方向最值得优先投入？", "团队最大分歧点是什么？", "若必须选一个方案，选哪个？"]
        q = questions[(rnd // 2 - 1) % len(questions)]
        self.add_log("vote", f"快速投票：{q}")
        results = []
        recent = self._get_recent_messages()
        for p in self.players:
            prompt = f"你是{p.name}。课题：{self.topic}\n近期：{chr(10).join(recent[-2:])}\n问题：{q}\n用一句话回答（不超过20字）。"
            ans = call_with_retry(prompt, max_tokens=30, temperature=0.7, timeout=3, retries=0)
            if not ans:
                ans = "无法在当前信息下决定"
            self.add_log("vote_result", ans, p)
            results.append({"player": p.name, "answer": ans})
        self.vote_history.append({"round": rnd, "question": q, "results": results})

    def _generate_report(self):
        full_log = self._format_log()
        _cit = _citations()
        has_pool = bool(self.info_pool)
        # V15 优化（2026-09-25）：报告必须标注出处；参考文献表由代码渲染，不让模型生成
        # V16（2026-10-11，修 6）：新增「七、实验方案」。
        # 动机（用户实测反馈）：原六节全是"讨论报告"结构，方案只停留在构想——
        # 无样本量、无测量工具、无时间表、无统计检验，读完没法直接开展实验。
        prompt = (f"你是科研协作分析师。课题：{self.topic}\n讨论：{full_log[-8000:]}\n"
                  f"生成报告：### 一、涌现视角 ### 二、主要分歧 ### 三、隐藏预设 "
                  f"### 四、建设性方向 ### 五、风险提示 ### 六、下一步行动建议 "
                  f"### 七、实验方案\n"
                  f"【第七节的硬要求】必须让读者**拿着就能开做**，逐项写全下列五项，缺一不可；"
                  f"确实写不出的项写「暂缺：<具体原因>」，**不得用泛泛表述顶替**：\n"
                  f"1. **样本**：对象是谁、多少人、从哪来（例：某师范院校大二 2 个班，约 80 人）；\n"
                  f"2. **测量**：测哪些指标、用什么工具（量表名称 / 行为计数的口径）；\n"
                  f"3. **时间表**：周期与节点（第几周做什么、总共几周）；\n"
                  f"4. **统计检验**：用什么方法、判定阈值（例：配对 t 检验，α=0.05）；\n"
                  f"5. **失效条件**：出现什么结果算这个方案不成立、下一步转做什么。\n"
                  f"第七节只写方案本身，不要重复前六节的论述。\n"
                  f"{_cit.citation_rules_text(has_pool)}\n直接输出Markdown，不要写参考文献列表。")
        report = call_with_retry(prompt, max_tokens=6000, temperature=0.5, timeout=120, retries=1)
        self.report = report or f"### ⚠️ 生成失败\n{full_log[-1500:]}"
        if self.user_thought:
            blind_prompt = f"用户想法：「{self.user_thought}」讨论：{full_log[-2000:]}\n列出2-3个用户未覆盖的角度。格式：- **角度名**：一句话说明"
            blindspots = call_with_retry(blind_prompt, max_tokens=400, temperature=0.6, timeout=15, retries=0)
            if blindspots: self.report += f"\n\n### 🎯 你的认知盲区\n你最初认为「{self.user_thought}」。讨论中浮现了以下你可能忽略的角度：\n{blindspots}"

        # 2026-10-24（③-C）：背景信息若为推断生成，报告里如实标注
        if getattr(self, "info_source", "") == "inferred":
            self.report += (
                "\n\n### ⚠️ 背景信息为推断生成（非检索结果）\n"
                "本次 6 位角色的背景资料由模型**按课题推断生成**，"
                "不是检索到的真实文献。\n"
                "- 文中**不带 [P#] 编号**的具体数字、报告名、机构案例，均需自行核实\n"
                "- 标注「待验证」的内容没有外部依据\n"
                "- 需要可核验来源时，请检查检索是否成功（source_status）\n"
            )

        # 引用核验（本地规则，不调模型）：有出处 / 无出处 / 引用了不存在的编号
        self.citation_check = _cit.check_citations(self.report, self.info_pool)
        if self.citation_check.get("phantom_count"):
            self.report += ("\n\n### ⚠️ 引用核验提示\n"
                            "本报告出现了信息池中不存在的文献编号：%s。"
                            "系统判定为疑似编造引用，请以参考文献表为准。"
                            % "、".join(self.citation_check["phantom"]))
        # V15 反幻觉（2026-10-09）：数字级核验——报告里的具体数字若在信息池中查无实据，明确标出
        if self.citation_check.get("unsourced_numbers_count"):
            self.report += ("\n\n### ⚠️ 无出处数字提示（疑似幻觉）\n"
                            "以下句含具体数字并声称有外部依据，但在本轮信息池中**查无实据**，"
                            "请勿直接采信：\n"
                            + "\n".join("- " + x for x in self.citation_check["unsourced_numbers"]))
        # V16（修 5）：Tier2 年份/中文量词单独提示，不与"幻觉数字"混计（避免误伤政策年份）
        if self.citation_check.get("unsourced_years_count"):
            self.report += ("\n\n### ℹ️ 年份/数量提示（非幻觉，仅提示核对）\n"
                            "以下句含年份或中文数量词但未标注出处。这类表述多用于时间标注，"
                            "**不计入幻觉数字**，仅建议核对：\n"
                            + "\n".join("- " + x for x in self.citation_check["unsourced_years"][:6]))
        # V16（修 1）：证据链披露——注入池里有多少条其实只有题录、没有摘要。
        # 不披露的话，读者会以为 [Pn] 的论断是被正文支持过的（B7 实测：题录下 18/18 判不了）。
        try:
            _no_abs = [r for r in (self.info_pool or []) if not (r.get("abstract") or "").strip()]
            if _no_abs and len(_no_abs) == len(self.info_pool or []):
                self.report += ("\n\n### ⚠️ 证据链提示\n"
                                "本轮信息池 **%d/%d 条只有题录（标题/作者/来源/日期）、没有摘要正文**。"
                                "因此报告里的 [Pn] 标注**只能说明编号存在，无法核对论断是否被原文支持**，"
                                "请以参考文献表为准、并自行查阅原文。"
                                % (len(_no_abs), len(self.info_pool)))
            elif _no_abs:
                self.report += ("\n\n> 证据链：信息池 %d 条中 %d 条仅有题录（无摘要），"
                                "涉及这些编号的论断无法自动核对。" % (len(self.info_pool), len(_no_abs)))
        except Exception:
            pass

        # 参考文献表（由代码生成，附在报告末尾；**只收学术来源**）
        ref_block = _cit.render_reference_list(self.info_pool)
        web_block = _cit.render_web_sources(self.info_pool)
        if ref_block or web_block:
            self.report += "\n\n---\n\n" + ref_block if ref_block else "\n\n---"
            if web_block:
                self.report += ("\n\n" + web_block if ref_block else "\n\n" + web_block)
            if self.citation_check:
                self.report += ("\n\n> 引用核验：有出处 %d 处（其中学术来源 %d 处、网络资料 %d 处）｜"
                                "引用了不存在的编号 %d 处｜未标注出处的断言 %d 处｜"
                                "**查无实据的数字 %d 处**。结论：%s"
                                % (self.citation_check.get("cited_count", 0),
                                   len(self.citation_check.get("cited_academic") or []),
                                   len(self.citation_check.get("cited_web") or []),
                                   self.citation_check.get("phantom_count", 0),
                                   self.citation_check.get("unsupported_count", 0),
                                   self.citation_check.get("unsourced_numbers_count", 0),
                                   self.citation_check.get("verdict", "")))
        self.add_log("system", "预演报告已生成（含引用核验、参考文献与网络资料分列）")

    def _generate_emergence_analysis(self):
        full_log = self._format_log()
        prompt = f"你是多智能体行为分析师。课题：{self.topic}。讨论：{full_log[-8000:]}\n分析涌现行为：### 🌀 协作模式 ### 🔍 视角互补 ### 🎭 角色演化 ### 💡 意外发现 ### 🧬 涌现证据\n直接输出Markdown，用>引用原句。"
        analysis = call_with_retry(prompt, max_tokens=4000, temperature=0.6, timeout=90, retries=1)
        self.emergence_analysis = analysis or "### ⚠️ 分析失败"
        self.add_log("system", "涌现分析已生成")

    def _extract_golden_insight(self):
        full_log = self._format_log()
        prompt = f"从讨论中提取一句最意外的观点。15字以内。直接输出句子。\n讨论：{full_log[-2000:]}"
        insight = call_with_retry(prompt, max_tokens=40, temperature=0.7, timeout=8, retries=0)
        self.golden_insight = insight or "讨论产生了值得深思的观点"

    def _generate_cards(self):
        full_log = self._format_log()
        prompt = f"根据讨论生成6张洞察卡片，每张一句话（20字以内），JSON格式：{{\"核心发现\":\"一句话\",\"主要分歧\":\"一句话\",\"认知盲区\":\"一句话\",\"潜在风险\":\"一句话\",\"建设方向\":\"一句话\",\"意外视角\":\"一句话\"}}\n讨论：{full_log[-2500:]}"
        result = call_with_retry(prompt, max_tokens=350, temperature=0.6, timeout=15, retries=0)
        try: self.cards = json.loads(result) if result else None
        except:
            if result:
                match = re.search(r'\{.*\}', result, re.DOTALL)
                if match:
                    try: self.cards = json.loads(match.group())
                    except: self.cards = None
        if not self.cards:
            self.cards = {"核心发现":"讨论完成，请查看深度报告","主要分歧":"参与者在多个维度上产生了丰富讨论","认知盲区":"请查看完整报告了解详情","潜在风险":"请查看完整报告了解详情","建设方向":"讨论收敛到了一个可执行的方向","意外视角":"出现了值得关注的意外观点"}

    def _generate_single_agent_comparison(self):
        questions = [f"1. 概括「{self.topic}」最核心的挑战或机遇（20字以内）",f"2. 最容易忽略的一个隐藏前提（30字以内）",f"3. 一个可能反直觉的替代视角（40字以内）",f"4. 一个务实建议（30字以内）",f"5. 建议的最大潜在风险（30字以内）",f"6. 用一句话总结核心判断（15字以内）"]
        questions_text = "\n".join(questions)
        prompt = f"你是一个通用大语言模型。独立分析以下课题。\n【课题】{self.topic}\n逐一回答问题，简洁直接。\n{questions_text}\n用数字编号对应问题。"
        comparison_text = call_with_retry(prompt, max_tokens=500, temperature=0.7, timeout=15, retries=0)
        if comparison_text:
            formatted = f'**单体AI对课题「{self.topic}」的独立分析**\n\n{comparison_text}\n\n---\n**对比要点**：以上是单体AI独立分析的结果。请切换到"涌现分析"和"预演报告"Tab，观察多智能体系统是否产出了**单体AI分析中完全没有出现**的视角或概念。如果出现了，那就是涌现行为的有力证据。'
            self.single_agent_comparison = formatted
        else: self.single_agent_comparison = "单体AI分析生成失败，请稍后重试。"

    def _evaluate_report(self):
        if not self.report: self.report_evaluation = None; return
        prompt = f"你是科研报告质量评估专家。请对以下报告评分。\n【课题】{self.topic}\n【报告】{self.report[:3000]}\n从以下五维度评分（1-10分），基于学术框架：深度、广度、涌现度、实用性、分歧质量。输出JSON：{{\"深度\":分数,\"广度\":分数,\"涌现度\":分数,\"实用性\":分数,\"分歧质量\":分数,\"综合评分\":分数,\"评语\":\"一句话\"}}"
        result = call_with_retry(prompt, max_tokens=400, temperature=0.5, timeout=15, retries=0)
        if result:
            try: self.report_evaluation = json.loads(result)
            except:
                match = re.search(r'\{.*\}', result, re.DOTALL)
                if match:
                    try: self.report_evaluation = json.loads(match.group())
                    except: self.report_evaluation = None
        if not self.report_evaluation: self.report_evaluation = {"深度":0,"广度":0,"涌现度":0,"实用性":0,"分歧质量":0,"综合评分":0,"评语":"评估生成失败"}

    def _run_coach_audit(self, memory=None):
        try:
            coach = Coach(call_with_retry)
            report_summary = self.report[:800] if self.report else ""
            emergence_summary = self.emergence_analysis[:500] if self.emergence_analysis else ""
            experiment_config = getattr(self, 'experiment_mode', {}) or {}
            opinions = coach.audit_design(self.topic, experiment_config, report_summary, emergence_summary, memory)
            self.coach_audit_opinions = opinions
            # V15 优化（2026-09-25）：审计发生在讨论**结束之后**，
            # 其产出只能作为「留给下一局」的先验建议，**不得覆盖本局的 coach_interventions**
            # （否则等于用事后信息改写本局设计，干预会永远晚一局）。
            self.next_session_suggestions = coach.next_session_suggestions(opinions)
        except Exception as e:
            print(f"⚠️ 主教练审计失败: {e}")
            self.coach_audit_opinions = ["审计分析生成失败"]

    def _format_log(self):
        lines = []
        for e in self.logs:
            if e.get("type") == "speech":
                lines.append(f"[{e.get('player','?')}·{e.get('prototype_name','?')}·{e.get('profession_name','?')}] {e.get('message','')}")
            elif e.get("type") == "vote": lines.append(f"[投票] {e.get('message','')}")
            elif e.get("type") == "vote_result": lines.append(f"  {e.get('player','?')}：{e.get('message','')}")
        return "\n".join(lines)

    def add_log(self, log_type, message, player=None, plain_text="", collision=False):
        entry = {"type": log_type, "message": message, "time": datetime.now().strftime("%H:%M:%S"), "plain_text": plain_text, "collision": collision}
        if player:
            entry["player"] = player.name; entry["prototype"] = player.prototype_key
            entry["prototype_name"] = player.prototype['name']; entry["profession_name"] = player.profession['name']
            entry["profession_icon"] = player.profession['icon']; entry["value_name"] = player.value['name']
            entry["value_icon"] = player.value['icon']; entry["color"] = player.prototype['color']
        self.logs.append(entry)


if __name__ == '__main__':
    print('群智 V15 引擎（请通过 app/main_gateway.py 启动）')
