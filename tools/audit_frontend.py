"""全面 debug 工具：静态扫描前端里"引用了但不存在"的东西。

它能抓到这几类"静默坏掉"的问题（界面上点了没反应，控制台却不一定报错）：
  · `querySelector('#xxx')` 拿不到元素 → 后面 `.onclick` 直接 TypeError
  · `API.foo(...)` 在 api.js 里没有定义
  · `App.xxx` / `Staging.xxx` / `UI.xxx` 导出里没有
  · HTML 里重复的 id
  · `U.xxx` 工具函数不存在
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "web" / "static" / "js"

bad = 0


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """删掉 `//` 行注释和 `/* */` 块注释，再拿去做引用扫描。

    为什么要这一步：注释里出现 `App.views`、`API.foo` 这种字样时，
    扫描器会把它当成真实引用去报告"没有导出"——**假阳性**。
    第一次遇到就发生在本项目里（修 M13 时在注释里写了原来用的名字）。

    注意只删注释、不删字符串：字符串里的 `API.xxx` 是真实调用。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for line in text.splitlines():
        # 只处理"整行/行尾"注释，且不误伤 URL 里的 `//`
        if line.lstrip().startswith("//"):
            continue
        line = re.sub(r"(?<![:\"'])//(?!/).*$", "", line)
        out.append(line)
    return "\n".join(out)


def fail(msg: str) -> None:
    global bad
    bad += 1
    print("  [x] " + msg)


print("=== 1 重复 id（同一函数内才算冲突）===")
# 同一个 id 出现在两个**互斥分支**里是正常的（比如"有数据/无数据"两套模板），
# 真正的坑是同一个函数里两次渲染同一个 id —— 那才可能同时出现在 DOM 里。
DYNAMIC_IDS = {"stg-bulk", "st-selinfo", "job-pill", "toast-root", "modal-root"}


def dup_ids_in_scope(text: str, scope: str) -> list:
    """同一 id 重复**且标签写法完全一样**才算抄漏。

    同一个 id 出现在互斥分支（`a ? '<img id=x>' : '<div id=x>'`）里是正常的，
    两边不可能同时在 DOM 里。真正危险的是同一段模板被复制了两份、
    标签一模一样 —— 那才会出现两个同 id 节点。
    """
    tags = re.findall(r"<[a-zA-Z][^>]*\bid=\"([\w-]+)\"[^>]*>", scope)
    full = re.findall(r"<[a-zA-Z][^>]*\bid=\"[\w-]+\"[^>]*>", scope)
    out = []
    for tag in set(full):
        if full.count(tag) > 1:
            out.append(re.search(r'\bid="([\w-]+)"', tag).group(1))
    return out or [k for k, v in Counter(tags).items() if v > 1 and
                   len(set(re.findall(rf'<[a-zA-Z][^>]*id="{k}"[^>]*>', scope))) == 1
                   and scope.count(f'id="{k}"') > 2]


for p in sorted(JS.glob("*.js")) + [ROOT / "app" / "web" / "index.html"]:
    t = read(p)
    if p.suffix == ".html":
        continue
    # 按函数切块检查
    parts = re.split(r"\n  (?:async )?function\s+(\w+)", t)
    for i in range(1, len(parts), 2):
        name, body = parts[i], parts[i + 1]
        dup = [k for k in dup_ids_in_scope(t, body) if k not in DYNAMIC_IDS]
        if dup:
            fail(f"{p.name}::{name}() 同一个函数里重复 id {dup}")

print("=== 2 API.xxx 调用是否都有定义 ===")
api = read(JS / "api.js")
defined = set(re.findall(r"^\s{4}(\w+):\s*\(", api, re.M))
defined |= set(re.findall(r"^\s{4}(\w+):\s*function", api, re.M))
used: Counter = Counter()
for p in sorted(JS.glob("*.js")):
    if p.name == "api.js":
        continue
    for m in re.finditer(r"\bAPI\.(\w+)\(", read(p)):
        used[m.group(1)] += 1
missing = sorted(k for k in used if k not in defined)
if missing:
    fail(f"调用了未定义的 API 方法: {missing}")
else:
    print(f"  ok（用到 {len(used)} 个，全部有定义）")

print("=== 3 App.xxx 是否在导出里 ===")
app = read(JS / "app.js")
# app.js 的导出是 `return { a, b, c }` 也可能写成 window.App = {...}
exported: set = set()
for mm in re.finditer(r"(?:return|window\.App\s*=)\s*\{([^}]*)\}", app, re.S):
    exported |= set(re.findall(r"[\w$]+", mm.group(1)))
for p in sorted(JS.glob("*.js")):
    if p.name == "app.js":
        continue
    t = strip_comments(read(p))
    for mm in re.finditer(r"\bApp\.(\w+)\b", t):
        k = mm.group(1)
        if k in exported:
            continue
        # 有 `if (App.x)` 守卫的算安全（可选功能，缺失不报错）
        if re.search(rf"(?:if\s*\(\s*App\.{k}\b|App\.{k}\s*&&|App\.{k}\s*\?)", t):
            continue
        fail(f"{p.name}: App.{k} 没有导出，也没有守卫（会 TypeError）")

print("=== 4 Staging / UI / U 的导出 ===")


def _brace_block(text: str, start: int) -> str:
    """从 `start`（指向 `{`）取到配对的那个 `}`。

    不能用 `\\{([^}]*)\\}`：导出对象里有 getter（`get items() { … }`），
    简单的非贪婪匹配会在 getter 的 `}` 处提前截断，
    于是 `onChange`、`colorOf` 这些排在后面的成员会被误报成"没导出"。
    """
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return ""


def exports_of(src: str) -> set:
    exp: set = set()
    for mm in re.finditer(r"(?:return|window\.\w+\s*=|const\s+\w+\s*=)\s*\{", src):
        blk = _brace_block(src, src.index("{", mm.start()))
        exp |= set(re.findall(r"[\w$]+", blk))
    return exp


for mod, fname in (("Staging", "staging.js"), ("UI", "ui.js"), ("U", "api.js")):
    exp = exports_of(read(JS / fname))
    if not exp:
        print(f"  （{fname} 没解析出导出，跳过）")
        continue
    for p in sorted(JS.glob("*.js")):
        if p.name == fname:
            continue
        t = read(p)
        badk = None
        for u in re.finditer(rf"\b{mod}\.(\w+)\b", t):
            k = u.group(1)
            if k in exp:
                continue
            # 有守卫的可选调用不算错
            if re.search(rf"(?:if\s*\(\s*{mod}\.{k}\b|{mod}\.{k}\s*&&"
                         rf"|typeof\s+{mod}\.{k}\s*===)", t):
                continue
            badk = k
            break
        if badk:
            fail(f"{p.name}: {mod}.{badk} 没有导出，也没有守卫")

print("=== 5 querySelector('#id') / getElementById 的目标是否存在 ===")
# id 可能来自：JS 模板、index.html、或运行时 createElement
alljs = "\n".join(read(p) for p in sorted(JS.glob("*.js")))
allids = set(re.findall(r'\bid="([\w-]+)"', alljs))
allids |= set(re.findall(r"\bid='([\w-]+)'", alljs))
allids |= set(re.findall(r"\bid\s*=\s*['\"]([\w-]+)['\"]", alljs))
allids |= set(re.findall(r'id="([\w-]+)"',
                         read(ROOT / "app" / "web" / "index.html")))
missing_ids = []
for p in sorted(JS.glob("*.js")):
    t = read(p)
    for mm in re.finditer(r"""querySelector\(\s*['"]#([\w-]+)['"]\s*\)""", t):
        if mm.group(1) not in allids:
            missing_ids.append(f"{p.name}: querySelector('#{mm.group(1)}')")
    for mm in re.finditer(r"""getElementById\(\s*['"]([\w-]+)['"]\s*\)""", t):
        if mm.group(1) not in allids:
            missing_ids.append(f"{p.name}: getElementById('{mm.group(1)}')")
for x in sorted(set(missing_ids)):
    fail(x + " 找不到对应元素")

print("=== 6 checkbox 联动：写死 checked 的检查 ===")
vf = read(JS / "views.files.js")
for name in ("paintTreeFiles", "paintList"):
    i = vf.find(f"function {name}")
    if i < 0:
        continue
    seg = vf[i:i + 4000]
    for mm in re.finditer(r'data-check="[^"]*"\s+checked>', seg):
        fail(f"{name}: 勾选框写死 checked（取消勾选后重绘又被勾上）")

print("=== 7 半选（indeterminate）是否都设了 ===")
for fname, need in (("views.files.js", 2), ("staging.js", 1)):
    t = read(JS / fname)
    n = t.count("indeterminate")
    if n < need:
        fail(f"{fname}: indeterminate 只出现 {n} 次（至少需要 {need}）")
    else:
        print(f"  {fname}: indeterminate x{n} ok")

print()
print(f"问题数: {bad}")
sys.exit(1 if bad else 0)
