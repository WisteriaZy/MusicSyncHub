"""拆分后的专项审计：找出"只在特定调用路径才炸"的失效模块。

## 为什么需要这个

拆分 `app/main.py` 时踩过两个坑，**启动不报错、测试也不一定覆盖到**：

  · `from .core import X`（单点相对导入）在 `app/routes/` 下会解析成
    `app.routes.core` —— ModuleNotFoundError 只在端点被调用时抛
  · `FUZZY_MIN_SCORE` 是 `deps` 的模块级常量，路由模块没导入它 ——
    NameError 只在 `/api/library` 被调用时抛

还有一个**自己写出来的**：整段重写 `set_keep` 时把 `out = []` 弄丢了，
`POST /api/duplicates/keep` 必然 500 —— 而它当时**没有任何测试覆盖**，
758 项单测 + 90 个冒烟全绿。问题是靠本工具的 symtable 检查抓到的。

## 四类检查

1. **逐模块导入** —— 语法 / 循环导入 / 顶层 import 失败
2. **未定义名字**（`symtable`）—— 函数里用到既不是局部、也不在模块命名空间、
   也不是内置的名字。上面两个坑的通用检测器。
3. **路由集合差分** —— 和拆解前的备份比，路由必须**不丢**
4. **前后端一致性** —— `api.js` 里的路径必须在后端注册表里存在

## 用法

    python tools/audit_split.py
    python tools/audit_split.py --baseline <拆解前备份目录>

`--baseline` 指向备份目录；里面有 `app/` 就直接读，只有 zip 就解压到临时目录再读。

**建议把它加进改动后的检查清单**（和 `check_frontend.py` / `audit_frontend.py` 并列）。
"""
from __future__ import annotations

import argparse
import builtins
import importlib
import os
import pathlib
import re
import symtable
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
DECOR = re.compile(r"@(?:app|router)\.(?:get|post|put|delete|patch)\(\s*['\"]([^'\"]+)")


def modules() -> list[str]:
    out = []
    for f in sorted(APP.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        mod = ".".join(f.relative_to(ROOT).with_suffix("").parts)
        if mod.endswith(".__init__"):
            mod = mod[:-len(".__init__")]
        out.append(mod)
    return out


# ---------------------------------------------------------------- 1) 导入

def check_imports() -> list[str]:
    bad = []
    for m in modules():
        try:
            importlib.import_module(m)
        except Exception as exc:                    # noqa: BLE001
            bad.append(f"{m}: {type(exc).__name__}: {exc}")
    return bad


# ---------------------------------------------------------------- 2) 未定义名字

def _walk_tables(t: symtable.SymbolTable):
    yield t
    for child in t.get_children():
        yield from _walk_tables(child)


def check_undefined(modname: str) -> list[str]:
    mod = sys.modules.get(modname)
    path = getattr(mod, "__file__", None) if mod else None
    if not path or not path.endswith(".py"):
        return []
    try:
        src = pathlib.Path(path).read_text(encoding="utf-8")
        top = symtable.symtable(src, path, "exec")
    except Exception:                               # noqa: BLE001
        return []
    ns = (set(dir(mod)) | set(dir(builtins))
          | {"__file__", "__name__", "__doc__", "__package__", "__loader__",
             "__spec__", "__builtins__", "__annotations__", "__cached__"})
    out: list[str] = []
    for table in _walk_tables(top):
        if table.get_type() != "function":
            continue
        for sym in table.get_symbols():
            nm = sym.get_name()
            if not sym.is_global():
                continue
            if sym.is_assigned() or sym.is_imported():
                continue
            if nm in ns:
                continue
            out.append(f"{modname}: {table.get_name()}() 用到未定义的名字 `{nm}`")
    return out


# ---------------------------------------------------------------- 3) 路由

def routes_in_tree(base: pathlib.Path) -> set[str]:
    found: set[str] = set()
    if not base.exists():
        return found
    for f in base.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        found |= {m.group(1) for m in DECOR.finditer(f.read_text(encoding="utf-8"))}
    return found


def baseline_routes(dir_or_zip: str) -> set[str]:
    p = pathlib.Path(dir_or_zip)
    if (p / "app").is_dir():
        return routes_in_tree(p / "app")
    zp = p if p.suffix == ".zip" else next(iter(p.glob("1-项目源码.zip")), None)
    if zp is None or not zp.exists():
        return set()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="msh-base-"))
    with zipfile.ZipFile(zp) as z:
        z.extractall(tmp)
    return routes_in_tree(tmp / "app")


def registered_routes() -> set[str]:
    from fastapi.routing import APIRoute
    importlib.import_module("app.main")
    main = sys.modules["app.main"]

    def walk(rs, seen=None):
        seen = seen or set()
        for r in rs:
            if id(r) in seen:
                continue
            seen.add(id(r))
            if isinstance(r, APIRoute):
                yield r
                continue
            inner = getattr(r, "original_router", None) or getattr(r, "router", None)
            if inner is not None and hasattr(inner, "routes"):
                yield from walk(inner.routes, seen)

    return {r.path for r in walk(main.app.routes)}


# ---------------------------------------------------------------- 4) 前后端

def check_frontend_paths(known: set[str]) -> list[str]:
    js = APP / "web" / "static" / "js" / "api.js"
    if not js.exists():
        return []
    text = js.read_text(encoding="utf-8")
    out = []
    for raw in sorted(set(re.findall(r"['\"`](/api/[^'\"`]*?)['\"`]", text))):
        norm = re.sub(r"\$\{[^}]*\}", "{p}", raw).split("?")[0].rstrip("/")
        if not norm.startswith("/api/"):
            continue
        if "{p}" in norm:
            pre, _, post = norm.partition("{p}")
            ok = any(k.startswith(pre) and k.endswith(post) for k in known)
            ok = ok or any(k.startswith(pre.rstrip("/")) for k in known)
        else:
            ok = norm in known
        if not ok:
            out.append(f"api.js 用了 {raw}，但后端没有对应路由")
    return out


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="拆分后专项审计")
    ap.add_argument("--baseline", default=None,
                    help="拆解前备份目录（含 app/ 或 1-项目源码.zip）")
    args = ap.parse_args(argv)

    os.environ.setdefault("MSH_DATA_DIR", tempfile.mkdtemp(prefix="msh-audit-"))
    sys.path.insert(0, str(ROOT))

    problems = 0
    print("=== 1) 逐模块导入 ===")
    bad = check_imports()
    print(f"  {len(modules())} 个模块，失败 {len(bad)}")
    for b in bad:
        print("   X ", b)
    problems += len(bad)

    print("\n=== 2) 未定义名字（symtable）===")
    undef: list[str] = []
    for m in modules():
        undef += check_undefined(m)
    print(f"  可疑 {len(undef)} 处")
    for u in undef:
        print("   X ", u)
    problems += len(undef)

    print("\n=== 3) 路由集合 ===")
    known = registered_routes()
    api = sorted(p for p in known if p.startswith("/api/"))
    print(f"  唯一路径 {len(known)} 条（/api {len(api)} 条，其余 {len(known) - len(api)} 条）")
    if args.baseline:
        old = baseline_routes(args.baseline)
        old_api = {p for p in old if p.startswith("/api/")}
        lost = sorted(old_api - set(api))
        add = sorted(set(api) - old_api)
        print(f"  基线 /api {len(old_api)} 条 -> 现在 {len(api)} 条")
        print(f"  丢失: {lost or '无'}")
        print(f"  新增: {add or '无'}")
        problems += len(lost)

    print("\n=== 4) 前后端路径一致性 ===")
    fe = check_frontend_paths(known)
    print(f"  问题 {len(fe)} 处")
    for f in fe:
        print("   X ", f)
    problems += len(fe)

    print(f"\n=== 合计 {problems} 处问题 ===")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
