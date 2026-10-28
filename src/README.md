# 代码结构

> 60 个文件，约 940 KB。**不是框架，是自研编排。**
> 不用 LangChain、不用 OpenAI SDK —— 用 `requests` 直连，因为要控制超时和重试。

---

## 一、目录鸟瞰

```
src/
├── app/                面向外部的服务层（52 个文件）
│   ├── main_gateway.py      FastAPI 入口，所有 HTTP 路由在这
│   ├── guard.py             访问口令 + IP 限流
│   ├── input_intent.py      意图体检（判断课题够不够具体）
│   │
│   ├── rag/                 检索装置
│   │   ├── arxiv_search.py      arXiv
│   │   ├── crossref_search.py   Crossref
│   │   ├── pubmed_search.py     PubMed
│   │   ├── openalex_search.py    OpenAlex
│   │   ├── semanticscholar_search.py
│   │   ├── doaj_search.py       DOAJ
│   │   ├── cnki_search.py       知网
│   │   ├── chinaxiv_search.py   ChinaXiv
│   │   ├── ncpssd_search.py     国家哲学社会科学文献中心
│   │   ├── nstl_search.py       NSTL
│   │   └── bocha_search.py      博查联网搜索（非学术库）
│   │
│   ├── source_plugins/      11 个检索源插件（统一接口，加一个 = 丢一个文件）
│   ├── citations.py         引用编号与期刊分级
│   ├── review_v6.py         三档风险评审（v6）
│   ├── coach_search.py      主教练联网搜索
│   ├── classroom_engine.py  课堂模拟器
│   └── templates/           前端页面（原生 HTML + JS）
│
├── sandbox/            讨论引擎（核心，2 个文件）
│   ├── app.py               ← 最重要的文件：角色、发言、干预、报告
│   └── prompts.py           8 类干预指令模板 + 各角色 prompt
│
├── tools/              工具脚本（3 个）
│
└── requirements.txt / .env.example / start_all.bat
```

---

## 二、一次预演走哪条路

```
① 前端 POST /api/sandbox/start
        ↓
② main_gateway.py 接到请求
        ├─ guard.py        口令 + 限流
        ├─ input_intent.py 意图体检（课题太模糊就反问）
        └─ 复杂度分级      决定 3 轮还是更少
        ↓
③ 检索装置（11 源并行）
        ├─ source_plugins/  各源插件
        ├─ 前置 4 道过滤    低质源 / 时效 / 相关性裁判 / 来源分级
        └─ 5 道质量闸门     去重 / 配额 / 期刊分级 / 排序剔三无 / 完整性
        ↓
④ sandbox/app.py 起 6 个角色
        ├─ 分配信息片段（每人 40–80 字，刻意互补与矛盾）
        ├─ 第 1–3 轮讨论（每轮 6 人发言）
        ├─ 每轮结束：compute_round_signals() 算 5 个本地信号
        └─ 信号触发 → prompts.py 里挑一条干预指令 → 主教练介入
        ↓
⑤ 生成报告
        ├─ 一句话结论 / 三步行动 / 认知盲区 / 参考文献
        └─ citations.py 编号并分级
        ↓
⑥ 前端 SSE 实时推送（/api/sandbox/stream）
```

---

## 三、三个核心机制在哪

| 机制 | 文件 | 关键位置 |
|---|---|---|
| **团队异构** | `sandbox/app.py` | `COGNITIVE_PROTOTYPES`（7 种）、`PROFESSIONAL_BACKGROUNDS`（8 种）、`VALUE_ORIENTATIONS`（6 种）|
| **信息不对称** | `sandbox/app.py` | 角色初始化时分配不同的 40–80 字片段 |
| **主教练干预** | `sandbox/app.py` + `prompts.py` | 信号计算 → `Coach.decide_intervention()` → `INTERVENTION_TEMPLATES` |

**`compute_round_signals()` 算的 5 个信号：**

```python
repeat_rate         观点重复率
off_topic           跑题度
tension             张力（反向指标：低张力才异常）
unsupported_claims  未补证断言数
cited_count         引用数
```

**8 类干预指令**（`prompts.py` 的 `INTERVENTION_TEMPLATES`）：
概念冻结 / 可验证性声明 / 红队测试 / 机制预算 / 伦理审查 / 视角切换 / 降级运行 / 反事实推演

> ⚠️ **边界**：这 8 类指令**有定义**，但实际只用到 5 类。
> 且「干预有效」**没有对照实验支撑** —— 只能证明机制在跑。见 [LIMITATIONS](../LIMITATIONS.md) 第 9 条。

---

## 四、为什么不用 LangChain

尝试过，放弃了。原因写在 [journey/](../journey/) 里。简单说：

**编排逻辑被框架的抽象吃掉了。** 我们需要在「每轮结束、下一轮开始之前」插入信号计算和干预决策，
而框架的 agent/chain 抽象把这一步藏起来了 —— 想控制就得 hack，hack 完还不如自己写。

**现在的方式**：`requests.post` 直连 + 自己实现的 `call_with_retry()`（硬超时、指数退避、`finish_reason` 检查）。

---

## 五、如果你只想看几处

| 想看什么 | 打开哪 |
|---|---|
| **三个机制怎么实现** | `sandbox/app.py` 搜 `COGNITIVE_PROTOTYPES`、`intervention_records` |
| **干预指令长什么样** | `sandbox/prompts.py` 搜 `INTERVENTION_TEMPLATES` |
| **检索怎么去重分级** | `app/source_plugins/` 任选一个，看 `search()` 的返回结构 |
| **API 有哪些** | `app/main_gateway.py` 搜 `@app.post` |
| **怎么防滥用** | `app/guard.py` |

---

## 六、跑起来

```bash
cd src
pip install -r requirements.txt
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
python -m uvicorn app.main_gateway:app --port 5020
```

**访问口令**：环境变量 `QUNZHI_ACCESS_KEY`（仓库里不含真值）。

**依赖**：Python 3.10+、FastAPI、Uvicorn、Pydantic、Requests。
可选：chromadb / jieba / sentence-transformers（语义检索，不装也能跑）。

---

## 七、代码质量：如实的评价

| 项 | 状态 |
|---|---|
| 结构 | 一般。`sandbox/app.py` 是 100 KB 单文件（1506 行），职责偏多 |
| 测试 | **很少**。只有 `test_v6_code.py` 一组，覆盖率未知 |
| 注释 | 较好。关键的坑都写进注释了（如 ChinaXiv 的 `pageSize`） |
| 类型标注 | **几乎没有** |
| 错误处理 | 较好。全链路有熔断（超时不出分，如实标注「未执行」）|

**这是一个人三个月做出来的项目，不是团队产品。请按这个预期看。**
