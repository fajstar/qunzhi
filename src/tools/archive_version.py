# tools/archive_version.py
# 一键归档当前版本
# 用法：
#   python archive_version.py --version V14 --note "200组基础实验完成"
#   python archive_version.py --version V14 --note "验证实验完成，准备升级V15"

import os
import sys
import json
import shutil
import argparse
from datetime import datetime


# ============================================================
#  配置
# ============================================================

# 项目根目录（tools/ 的上级目录）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 需要归档的目录和文件
ITEMS_TO_ARCHIVE = [
    "sandbox",          # 核心沙盘
    "engine",           # 自动化引擎
    "codex_sm",         # SM Codex（基于 SM 前后端）
    "tools",            # 工具脚本
    "data",             # 实验数据
    ".env",             # API Key 配置
    "start_all.bat",    # 一键启动脚本
]

# 归档根目录
ARCHIVE_ROOT = os.path.join(PROJECT_ROOT, "archive")


# ============================================================
#  核心逻辑
# ============================================================

def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}")


def get_experiment_summary():
    """读取当前实验数据，生成摘要"""
    db_path = os.path.join(PROJECT_ROOT, "data", "experiments_db.json")
    if not os.path.exists(db_path):
        return None
    
    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)
    
    exps = db.get("experiments", [])
    summary = db.get("summary", {})
    
    return {
        "total_experiments": len(exps),
        "valid_experiments": len([e for e in exps if e.get("scores", {}).get("综合评分", 0) > 0]),
        "by_personality": summary.get("by_personality", {}),
        "by_info_mode": summary.get("by_info_mode", {}),
        "by_intervention": summary.get("by_intervention", {}),
    }


def generate_readme(version, note, exp_summary):
    """生成版本说明文档"""
    lines = [
        f"# {version} 版本归档",
        "",
        f"- **归档时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **备注**：{note}",
        "",
        "---",
        "",
        "## 实验数据摘要",
        "",
    ]
    
    if exp_summary:
        lines.append(f"- **总实验数**：{exp_summary['total_experiments']}")
        lines.append(f"- **有效实验数**：{exp_summary['valid_experiments']}")
        lines.append("")
        lines.append("### 按人格模式")
        for mode, stats in exp_summary.get("by_personality", {}).items():
            lines.append(f"- **{mode}**：{stats.get('count', 0)}次，均分 {stats.get('avg_score', 0)}")
        lines.append("")
        lines.append("### 按信息源")
        for info, stats in exp_summary.get("by_info_mode", {}).items():
            lines.append(f"- **{info}**：{stats.get('count', 0)}次，均分 {stats.get('avg_score', 0)}")
        lines.append("")
        lines.append("### 按干预有无")
        for interv, stats in exp_summary.get("by_intervention", {}).items():
            lines.append(f"- **{interv}**：{stats.get('count', 0)}次，均分 {stats.get('avg_score', 0)}")
    else:
        lines.append("（实验数据库为空或不存在）")
    
    lines.extend([
        "",
        "---",
        "",
        "## 目录结构",
        "",
        "```",
        f"{version}/",
        "├── sandbox/        # 核心沙盘系统",
        "├── engine/         # 自动化引擎",
        "├── codex/          # 命令行工具与工作台",
        "├── tools/          # 独立工具脚本",
        "├── data/           # 实验数据",
        "├── memory.json     # 工作台长期记忆",
        "├── .env            # API Key 配置",
        "├── start_all.bat   # 一键启动脚本",
        "└── README.md       # 本说明文件",
        "```",
        "",
        "---",
        "",
        "## 恢复方式",
        "",
        f"将 `archive/{version}/` 下的所有内容复制到项目根目录，覆盖同名文件即可。",
        "",
        "```bash",
        f"xcopy archive\\{version}\\* . /E /Y",
        "```",
    ])
    
    return "\n".join(lines)


def archive_version(version, note):
    """执行归档"""
    # 目标目录
    archive_dir = os.path.join(ARCHIVE_ROOT, version)
    
    # 检查是否已存在同名归档
    if os.path.exists(archive_dir):
        log(f"归档版本 {version} 已存在，将覆盖", "WARN")
        shutil.rmtree(archive_dir)
    
    os.makedirs(archive_dir, exist_ok=True)
    
    # 复制项目文件
    copied = 0
    failed = 0
    
    for item in ITEMS_TO_ARCHIVE:
        src = os.path.join(PROJECT_ROOT, item)
        dst = os.path.join(archive_dir, item)
        
        if not os.path.exists(src):
            log(f"跳过（不存在）: {item}", "WARN")
            continue
        
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
            else:
                shutil.copy2(src, dst)
            log(f"✅ {item}")
            copied += 1
        except Exception as e:
            log(f"❌ {item}: {e}", "ERROR")
            failed += 1
    
    # 生成 README
    exp_summary = get_experiment_summary()
    readme_content = generate_readme(version, note, exp_summary)
    readme_path = os.path.join(archive_dir, "README.md")
    
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)
    
    log(f"📝 README.md 已生成")
    
    # 输出总结
    print("\n" + "=" * 60)
    print(f"  归档完成：{version}")
    print("=" * 60)
    print(f"  位置：{archive_dir}")
    print(f"  成功：{copied} 项")
    print(f"  失败：{failed} 项")
    if exp_summary:
        print(f"  实验数：{exp_summary['total_experiments']}（有效 {exp_summary['valid_experiments']}）")
    print("=" * 60 + "\n")


def list_archives():
    """列出所有已归档的版本"""
    if not os.path.exists(ARCHIVE_ROOT):
        log("归档目录不存在，尚未进行过归档", "INFO")
        return
    
    versions = os.listdir(ARCHIVE_ROOT)
    if not versions:
        log("暂无归档版本", "INFO")
        return
    
    print("\n已归档的版本：")
    print("-" * 40)
    for v in sorted(versions):
        readme_path = os.path.join(ARCHIVE_ROOT, v, "README.md")
        if os.path.exists(readme_path):
            with open(readme_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip().lstrip("# ")
            print(f"  📦 {v} — {first_line}")
        else:
            print(f"  📦 {v}")
    print("-" * 40 + "\n")


# ============================================================
#  主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Sandbox 版本归档工具 —— 一键保存当前版本的全部代码、数据和记忆"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # archive 子命令
    parser_archive = subparsers.add_parser("archive", help="归档当前版本")
    parser_archive.add_argument("--version", "-v", required=True, help="版本号，如 V14")
    parser_archive.add_argument("--note", "-n", default="", help="归档备注")

    # list 子命令
    subparsers.add_parser("list", help="列出所有已归档版本")

    args = parser.parse_args()

    if args.command == "archive":
        archive_version(args.version, args.note)
    elif args.command == "list":
        list_archives()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()