# -*- coding: utf-8 -*-
"""前端 JS 语法检查（V16.5，2026-10-11）
==================================================================
**为什么必须做这一步**（用事故换来的）：
  我用 Python 脚本改 `console_new.html` 时替换 `connectStream`，`t[j:]` 把原函数
  收尾的 `\\n}\\n` 也留着了，而新函数自己已有 `}` → **多一个右花括号**。
  后果：页面 HTTP 200 能打开，但**整个脚本语法错误 → 所有按钮失灵**。
  用户的原话是「服务器崩了，怎么用不了」—— 服务器其实好好的，是前端挂了。
  我当时只检查了"字符串在不在"（in 判断），**没有真正解析 JS**，所以没抓到。

  花括号配平也**不是**证明（配平仍可能有语法错，比如多一个 `)` 换少一个 `}`）。
  唯一可靠的办法：**用 JS 引擎真的解析一遍**。

用法：
    python tools/check_frontend_js.py            # 检查项目内模板
    python tools/check_frontend_js.py <file>     # 检查指定文件
退出码 0 = 全部通过；1 = 有语法错误（**部署脚本应据此中止**）。
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 不换对象，只改编码（被 import 也安全）

NODE_CANDIDATES = [

    r"C:\Program Files\nodejs\node.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
    "node",
]


def find_node():
    for c in NODE_CANDIDATES:
        try:
            r = subprocess.run([c, "-v"], capture_output=True, timeout=30)
            if r.returncode == 0:
                return c, (r.stdout or b"").decode("utf-8", "replace").strip()
        except Exception:
            continue
    return None, None


def extract_scripts(html):
    """取出所有 <script> 内联块（跳过带 src 的）。返回 [(序号, 代码)]。"""
    out = []
    for n, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                      html, re.S | re.I), 1):
        out.append((n, m.group(1)))
    return out


def check_js(node, code):
    """用 node --check 解析。返回 (ok, 错误信息)。"""
    fd, path = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        r = subprocess.run([node, "--check", path], capture_output=True, timeout=60)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or b"").decode("utf-8", "replace").strip()[:600]
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def main():
    args = sys.argv[1:]
    node, ver = find_node()
    if not node:
        print("!! 找不到 node（无法做 JS 语法检查）。请先安装 Node.js。")
        return 2
    print("用 %s（%s）检查前端 JS" % (node, ver))

    if args:
        files = args
    else:
        root = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(root) == "tools":
            root = os.path.dirname(root)
        d = os.path.join(root, "app", "templates")
        files = [os.path.join(d, f) for f in sorted(os.listdir(d))] if os.path.isdir(d) else []

    bad = 0
    for p in files:
        if not os.path.exists(p) or not p.endswith((".html", ".htm")):
            continue
        html = open(p, encoding="utf-8").read()
        blocks = extract_scripts(html)
        if not blocks:
            continue
        for n, code in blocks:
            if not code.strip():
                continue
            ok, err = check_js(node, code)
            tag = "%s <script#%d>（%d 字符）" % (os.path.basename(p), n, len(code))
            if ok:
                print("  OK   " + tag)
            else:
                bad += 1
                print("  FAIL " + tag)
                for line in err.split("\n")[:6]:
                    print("        " + line)
    print()
    if bad:
        print("✗ 有 %d 个 <script> 块语法错误 —— **不要部署**（页面会打开但功能全失灵）" % bad)
        return 1
    print("✓ 全部 <script> 块语法通过")
    return 0


if __name__ == "__main__":
    # 注意：**只在直接运行时**改 stdout 编码。
    # 之前写在模块顶层，导致被 import 时把调用方的 stdout 也换掉/关掉
    # （部署脚本 import 它之后 print 直接报 "I/O operation on closed file"）。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
