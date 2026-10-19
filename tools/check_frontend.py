"""前端静态自检：`python tools/check_frontend.py`

放在项目里而不是临时工作区 —— 它被用了很多次，属于长期工具。

做两件事，按可靠性从低到高：
  1) index.html 引用的静态资源是否都存在 —— 少一个文件就是白屏；
  2) 每个 JS 文件用 **真正的 `node --check`** 做语法检查。

括号计数只当粗筛：它会被字符串/注释里的括号干扰，而 `node --check`
报的就是浏览器会遇到的错。之所以需要它：曾经有一次模板字符串提前闭合，
括号数照样配平，但整个模块加载失败（`Organizer is undefined`），
靠肉眼根本发现不了。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"
STATIC = WEB / "static"

bad = 0
suspects: list[str] = []


def find_node() -> str | None:
    """优先用 DSH 自带的 node，其次 PATH 里的。"""
    # 优先环境变量（CI / 打包环境注入），其次 PATH。
    # 之前这里写死了某个 IDE 的运行目录 —— 别人机器上根本不存在。
    for key in ("MSH_NODE", "DSH_NODE", "NODE_EXE"):
        v = os.environ.get(key)
        if v and Path(v).is_file():
            return v
    w = shutil.which("node")
    if w:
        return w
    for cand in (r"C:\Program Files\nodejs\node.exe",
                 os.path.expanduser(r"~\scoop\apps\nodejs\current\node.exe")):
        if Path(cand).is_file():
            return cand
    return None


print("=== index.html 引用的静态资源 ===")
idx = (WEB / "index.html").read_text(encoding="utf-8")
refs = sorted(set(m.group(1) for m in
                  re.finditer(r"/static/(?:js|css)/([\w.]+)\?v=\d+", idx)))
for r in refs:
    sub = "css" if r.endswith(".css") else "js"
    ok = (STATIC / sub / r).exists()
    if not ok:
        bad += 1
    print(f"  {r:26} {'存在' if ok else '缺失 !!'}")

js_files = sorted((STATIC / "js").glob("*.js"))

print("=== JS 括号配平（粗筛，会被字面量干扰）===")
for p in js_files:
    t = p.read_text(encoding="utf-8")
    d_par = t.count("(") - t.count(")")
    d_cur = t.count("{") - t.count("}")
    d_brk = t.count("[") - t.count("]")
    # `{}` / `[]` 不配平一定是结构问题；`()` 会被中文文案里的半角括号干扰，
    # 所以单独的 () 不配平只标"疑似"，以 node --check 为准。
    hard = d_cur != 0 or d_brk != 0
    if hard:
        bad += 1
    elif d_par != 0:
        suspects.append(p.name)
    note = ("配平" if not hard and d_par == 0
            else ("<<< 不配平" if hard else "疑似（以语法检查为准）"))
    print(f"  {p.name:24} ()={d_par:+d} {{}}={d_cur:+d} []={d_brk:+d}  {note}")

print("=== JS 真·语法检查 ===")
node = find_node()
if node:
    print(f"  用 {node}")
    for p in js_files:
        try:
            r = subprocess.run([node, "--check", str(p)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                bad += 1
                lines = (r.stderr or "").strip().splitlines()
                # node 的错误信息里第一行往往只是堆栈，找带 SyntaxError 的那行
                msg = next((x for x in lines if "Error" in x),
                           lines[0] if lines else "?")
                print(f"  {p.name:24} <<< {msg.strip()[:90]}")
            else:
                print(f"  {p.name:24} 语法 OK")
        except Exception as exc:
            print(f"  {p.name:24} 检查失败: {exc}")
else:
    print("  !! 找不到 node，跳过（这一步最权威，强烈建议保留 node）")

print("=== CSS 括号配平 ===")
css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
d = css.count("{") - css.count("}")
if d != 0:
    bad += 1
print(f"  app.css {{}}={d:+d}  {'配平' if d == 0 else '<<< 不配平'}")

print()
print(f"问题数: {bad}")
if suspects:
    print(f"括号疑似（不算失败）: {', '.join(suspects)}")
sys.exit(1 if bad else 0)
