"""测试公用工具。

存在的理由：项目里大量测试用"从函数开头切 N 个字符"来断言源码内容，
例如 `src[i:i + 2600]`。这个写法**天生脆弱** —— 函数里加几行，
被断言的字符串就滑出窗口，测试莫名其妙地红。

踩过：给 `upload_batches` 加了"非音频单独列出"的逻辑之后，
`test_lists_stray_files_in_root` 就失败了，而代码完全正确。

`fn_body(src, name)` 按**缩进**取完整函数体，与行数无关。
"""
from __future__ import annotations

import re


def fn_body(src: str, name: str) -> str:
    """取出名为 `name` 的函数/箭头函数的**完整函数体**（含定义行）。

    支持三种写法（本项目都用到了）：
      · `def name(...)`            —— Python
      · `function name(...)`       —— 老式 JS
      · `const name = (...) => {`  —— 本项目大量使用的箭头函数

    按缩进判断边界：下一个同级（或更浅）缩进的 `def`/`class`/`@`/
    `function`/`const NAME =` 即为结束。
    比"切 N 个字符"稳得多 —— 函数里加多少行都不影响。
    """
    pats = [
        rf"^([ \t]*)(?:async\s+)?def\s+{re.escape(name)}\s*\(",
        rf"^([ \t]*)(?:async\s+)?function\s+{re.escape(name)}\s*\(",
        rf"^([ \t]*)(?:const|let|var)\s+{re.escape(name)}\s*=",
    ]
    m = None
    for p in pats:
        m = re.search(p, src, re.M)
        if m:
            break
    if not m:
        return ""
    indent = m.group(1)
    sibling = re.compile(
        rf"^[ \t]{{0,{len(indent)}}}(?:def |class |@|(?:async )?function |"
        rf"(?:const|let|var) [A-Za-z_$][\w$]*\s*=)")
    lines = src[m.start():].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        s = ln.rstrip("\n")
        if not s.strip():
            out.append(ln)
            continue
        cur = re.match(r"[ \t]*", s).group(0)
        if len(cur) < len(indent):
            break
        if len(cur) == len(indent):
            st = s.lstrip()
            # 收尾括号 / 续行不算新定义
            if sibling.match(s) and not st.startswith((")", "]", "}", ".", "+", "?")):
                break
        out.append(ln)
    return "".join(out)


def around(src: str, needle: str, before: int = 300, after: int = 600) -> str:
    """取 `needle` 前后一段（用于断言"附近有某内容"）。"""
    i = src.find(needle)
    if i < 0:
        return ""
    return src[max(0, i - before):i + len(needle) + after]


def app_src() -> str:
    """把 `app/` 下所有 .py 拼成一段源码。

    ★ 为什么需要：`app/main.py` 已经拆成 `deps.py` + `routes/*.py`。
    测试里原先大量 `py("app/main.py")` 的断言其实是"这段代码在不在"
    —— 那是**行为契约**，不该绑死在某个文件名上。否则每拆一次文件就要改一批测试，
    久而久之大家会把测试改了事，契约就名存实亡了。
    """
    import pathlib as _pl
    base = _pl.Path(__file__).resolve().parent.parent / "app"
    parts = []
    for f in sorted(base.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)
