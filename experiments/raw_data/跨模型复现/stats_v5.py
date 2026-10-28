# -*- coding: utf-8 -*-
"""跨模型实验 v5 统计：进度 / 覆盖数 / E1vsE0 / 思考开关成本 / 失败情况"""
import sys, os, json, math, statistics, paramiko

sys.stdout.reconfigure(encoding="utf-8")
HOST, USER, PASS = "134.175.145.241", "ubuntu", "Qunzhi2026!"
C = "/home/ubuntu/qunzhi/experiments/2026-09-17_跨模型复现"
LOCAL = r"C:\比赛\重庆赛\跨模型实验\runs_v5"

LEX = {
    "计算机": ["技术", "架构", "算法", "系统", "数据", "实现", "工程", "部署", "性能", "接口", "模型", "缓存", "代码"],
    "教育学": ["教学", "学生", "学习", "课程", "教师", "课堂", "培养", "师范", "实训", "评价标准"],
    "心理学": ["动机", "情绪", "认知", "心理", "焦虑", "意愿", "安全感", "习惯", "感知"],
    "管理学": ["流程", "资源", "组织", "机制", "执行", "管理", "监督", "职责", "标准流程"],
    "社会学": ["群体", "文化", "规范", "关系", "公平", "传播", "阶层", "共同体", "同伴"],
    "经济学": ["成本", "收益", "激励", "投入", "效用", "价格", "性价比", "边际", "预算"],
}


def cov(t):
    return sum(1 for ws in LEX.values() if sum(t.count(w) for w in ws) > 0)


# ---- 拉取 ----
def pull():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=25)
    sftp = c.open_sftp()
    os.makedirs(LOCAL, exist_ok=True)
    n = 0
    try:
        for f in sftp.listdir(f"{C}/runs_v5"):
            if f.endswith(".json"):
                sftp.get(f"{C}/runs_v5/{f}", os.path.join(LOCAL, f))
                n += 1
    except Exception:
        pass
    # 日志
    try:
        with sftp.open(f"{C}/run_v5.log") as fh:
            log = fh.read().decode("utf-8", "replace")
        open(os.path.join(LOCAL, "_run_v5.log"), "w", encoding="utf-8").write(log)
    except Exception:
        log = ""
    # 进程
    _, o, _ = c.exec_command("ps -eo pid,etime,cmd | grep '[r]un_crossmodel_v5' | head -1", timeout=60)
    proc = o.read().decode("utf-8", "replace").strip()
    sftp.close()
    c.close()
    return n, log, proc


n, log, proc = pull()
print("=" * 96)
print(f"跨模型复现实验 v5 · 统计    已拉取 {n} 个 run 文件")
print("=" * 96)
print(f"  云端进程: {proc if proc else '❌ 未运行'}")
print(f"  进度: {n} / 96  ({n/96*100:.0f}%)")

runs = []
for f in sorted(os.listdir(LOCAL)):
    if not f.endswith(".json"):
        continue
    try:
        d = json.load(open(os.path.join(LOCAL, f), encoding="utf-8"))
        d["_cov"] = cov(d.get("report") or "")
        runs.append(d)
    except Exception:
        pass

if not runs:
    print("\n（还没有完成的运行）")
    print("\n日志尾部：")
    print(log[-1200:] if log else "（无）")
    sys.exit(0)

tok = sum(r.get("total_tokens", 0) for r in runs)
fail = sum(r.get("failed_calls", 0) for r in runs)
calls = sum(r.get("api_calls", 0) for r in runs)
print(f"  token 合计: {tok:,}   调用 {calls} 次，失败 {fail} 次")
print(f"  平均耗时: {statistics.mean(r.get('duration_s',0) for r in runs):.0f}s")

print("\n" + "=" * 96)
print("【一】按「模型 × 思考」汇总")
print("=" * 96)
print(f"{'组合':<16}{'次数':<6}{'平均覆盖':<10}{'平均字数':<10}{'平均token':<12}{'平均耗时':<10}{'失败':<7}{'思维tok'}")
combos = {}
for r in runs:
    k = f"{r['model_key']}_{r['think']}"
    combos.setdefault(k, []).append(r)
for k in sorted(combos):
    rs = combos[k]
    print(f"{k:<16}{len(rs):<6}{statistics.mean(x['_cov'] for x in rs):<10.2f}"
          f"{statistics.mean(len(x.get('report') or '') for x in rs):<10.0f}"
          f"{statistics.mean(x.get('total_tokens',0) for x in rs):<12,.0f}"
          f"{statistics.mean(x.get('duration_s',0) for x in rs):<10.0f}"
          f"{sum(x.get('failed_calls',0) for x in rs):<7}"
          f"{sum(x.get('reasoning_tokens_est',0) for x in rs):,}")

print("\n" + "=" * 96)
print("【二】按「臂」汇总（核心：E1 异构 vs E0 零异构 vs C 单次直答）")
print("=" * 96)
print(f"{'臂':<14}{'次数':<6}{'平均覆盖':<10}{'平均字数':<10}{'平均token':<12}")
arms = {}
for r in runs:
    arms.setdefault(r["arm"], []).append(r)
for a in ["E1_异构", "E0_零异构", "C_单次直答"]:
    if a in arms:
        rs = arms[a]
        print(f"{a:<14}{len(rs):<6}{statistics.mean(x['_cov'] for x in rs):<10.2f}"
              f"{statistics.mean(len(x.get('report') or '') for x in rs):<10.0f}"
              f"{statistics.mean(x.get('total_tokens',0) for x in rs):<12,.0f}")

print("\n" + "=" * 96)
print("【三】核心对比：每个「模型×思考」组合里，E1 是否高于 E0")
print("=" * 96)
print(f"{'组合':<16}{'E1覆盖':<10}{'E0覆盖':<10}{'差值':<10}{'方向':<12}{'E1字数':<10}{'E0字数'}")
verdicts = {}
for k in sorted(combos):
    rs = combos[k]
    e1 = [x["_cov"] for x in rs if x["arm"] == "E1_异构"]
    e0 = [x["_cov"] for x in rs if x["arm"] == "E0_零异构"]
    if not e1 or not e0:
        print(f"{k:<16}{'(数据不全)':<10}")
        continue
    m1, m0 = statistics.mean(e1), statistics.mean(e0)
    d = m1 - m0
    verdicts[k] = d
    arrow = "✅ E1 更高" if d > 0 else ("❌ E0 更高" if d < 0 else "= 持平")
    c1 = statistics.mean(len(x.get("report") or "") for x in rs if x["arm"] == "E1_异构")
    c0 = statistics.mean(len(x.get("report") or "") for x in rs if x["arm"] == "E0_零异构")
    print(f"{k:<16}{m1:<10.2f}{m0:<10.2f}{d:<+10.2f}{arrow:<12}{c1:<10.0f}{c0:.0f}")

print("\n" + "=" * 96)
print("【四】C 臂（单次直答）落在哪")
print("=" * 96)
for k in sorted(combos):
    rs = [x for x in combos[k] if x["arm"] == "C_单次直答"]
    if rs:
        print(f"  {k:<16} 单次直答覆盖 {statistics.mean(x['_cov'] for x in rs):.2f} "
              f"（E1 {statistics.mean(x['_cov'] for x in combos[k] if x['arm']=='E1_异构'):.2f} / "
              f"E0 {statistics.mean(x['_cov'] for x in combos[k] if x['arm']=='E0_零异构'):.2f}）")

print("\n" + "=" * 96)
print("【五】思考开关的代价（关 vs 开）")
print("=" * 96)
for mk in sorted({r["model_key"] for r in runs}):
    off = [r for r in runs if r["model_key"] == mk and r["think"] == "off"]
    on = [r for r in runs if r["model_key"] == mk and r["think"] == "on"]
    if off and on:
        print(f"  {mk}:")
        print(f"    off  覆盖 {statistics.mean(x['_cov'] for x in off):.2f} | "
              f"token {statistics.mean(x.get('total_tokens',0) for x in off):,.0f} | "
              f"耗时 {statistics.mean(x.get('duration_s',0) for x in off):.0f}s")
        print(f"    on   覆盖 {statistics.mean(x['_cov'] for x in on):.2f} | "
              f"token {statistics.mean(x.get('total_tokens',0) for x in on):,.0f} | "
              f"耗时 {statistics.mean(x.get('duration_s',0) for x in on):.0f}s")

print("\n" + "=" * 96)
print("【六】日志尾部")
print("=" * 96)
print(log[-800:] if log else "（无）")
