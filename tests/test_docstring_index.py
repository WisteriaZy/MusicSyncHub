"""守住 `app/main.py` 顶部的导航索引，别让它腐烂。

## 为什么要这条测试

后端拆成了 `app/deps.py` + `app/routes/*.py` + `app/main.py`。
接手的人（尤其 AI 助手）靠 `main.py` 顶部那份导航索引判断"该去哪个文件找"。
但"手写索引"有个通病：**加了个路由忘了更新索引**，
半年后索引和现实对不上，比没有索引更误导。

这条测试把"索引必须和实际路由一致"变成可执行的约束：

  · 每个 `@app.xxx()` / `@router.xxx()` 的路径都必须出现在导航头里
  · 导航头里写出的每个 `/api/...` 路径都必须真的存在（任一模块）
  · 导航头里提到的函数名必须在某个模块里真有定义
  · 每个模块内部：静态路径必须排在带参路由之前（FastAPI 按注册顺序匹配）

## 拆分之后怎么改

路由搬了文件，但**路径没变** —— 所以导航索引通常不用动。
只有新增/删除路由时才需要更新 `main.py` 顶部那段注释。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent
APP = _PROJ / "app"
MAIN = APP / "main.py"

# `@app.get(...)`（main.py）与 `@router.post(...)`（routes/*.py）都要认
DECOR = re.compile(r"@(?:app|router)\.(?:get|post|put|delete|patch)\(\s*['\"]([^'\"]+)")


def _modules() -> list[Path]:
    """所有可能定义路由/helper 的后端模块。"""
    out = [MAIN, APP / "deps.py"]
    routes_dir = APP / "routes"
    if routes_dir.is_dir():
        out += sorted(routes_dir.glob("*.py"))
    # 其他 *_api.py 里也有路由（search / upload / station / tags / mappings / folders）
    out += sorted(p for p in APP.glob("*_api.py"))
    out += sorted((APP / "sources").glob("*_api.py"))
    return [p for p in out if p.exists()]


def _split_head(text: str) -> str:
    """取出文件顶部的模块 docstring（导航索引就在里面）。"""
    lines = text.splitlines()
    if not lines or not lines[0].lstrip().startswith('"""'):
        return ""
    for i in range(1, len(lines)):
        if lines[i].strip() == '"""':
            return "\n".join(lines[:i])
    return ""


def _routes(text: str) -> list[str]:
    return DECOR.findall(text)


class DocstringIndexTest(unittest.TestCase):
    """导航索引必须和实际路由一致（跨所有模块）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = MAIN.read_text(encoding="utf-8")
        cls.head = _split_head(cls.src)
        cls.per_file = {p: p.read_text(encoding="utf-8") for p in _modules()}
        cls.all_src = "\n".join(cls.per_file.values())
        # 导航索引只覆盖 `main.py` + `app/routes/*`（这两处是拆分时建立的）。
        # 更早拆出去的 `*_api.py` 有自己的文件头说明，不在这份索引的职责范围内。
        cls.covered = [MAIN] + sorted((APP / "routes").glob("*.py"))
        cls.routes = [r for p in cls.covered
                      for r in _routes(cls.per_file[p])]

    def test_head_exists(self):
        self.assertTrue(self.head, "main.py 顶部应该有导航索引 docstring")
        self.assertIn("导航索引", self.head,
                      "导航索引那段注释不见了 —— 是删掉了还是改写了？")
        self.assertIn("deps.py", self.head,
                      "导航索引里应该说明共享 helper 在 deps.py")

    def test_sane_route_count(self):
        """路由数量本身要合理（防止正则写错导致假绿）。"""
        self.assertGreaterEqual(len(self.routes), 40,
                                f"只解析到 {len(self.routes)} 个路由，正则可能失效了")

    def test_every_route_listed(self):
        """每个路由都要在导航里出现。"""
        missing = [r for r in self.routes if r not in self.head]
        self.assertEqual(
            [], missing,
            "这些路由没写进 main.py 顶部的导航索引：\n  "
            + "\n  ".join(missing)
            + "\n\n修法：把它们的路径补进导航索引对应的分组里。")

    def test_no_phantom_route_in_index(self):
        """导航里写的路由必须真的存在（任一模块）。"""
        nav = set(re.findall(r"/api/[\w/{}\-]+", self.head))
        actual = set(self.routes)
        bogus = sorted(r for r in nav if r not in actual)
        self.assertEqual(
            [], bogus,
            "导航索引里写了这些路由，但代码里没有：\n  " + "\n  ".join(bogus))

    def test_function_names_exist(self):
        """导航里提到的函数名必须真有定义（任一模块）。"""
        defs = set(re.findall(r"^(?:async )?def (\w+)", self.all_src, re.M))
        mentioned = set(re.findall(r"\b([a-z_][a-z0-9_]{4,})\s*\(", self.head))
        known_external = {
            "load_config", "save_config", "get_jobs", "run_in_background",
            "build_plan", "make_thumbnail", "version_tags", "start_scan",
            "files_transfer", "require_writable", "ensure_free", "include_router",
            "add_middleware", "exception_handler", "include", "routes",
        }
        phantom = sorted(n for n in mentioned
                         if n not in defs and n not in known_external)
        self.assertEqual(
            [], phantom,
            "导航索引里提到这些名字，但后端代码里没有对应 def：\n  "
            + "\n  ".join(phantom))


class RouteOrderTest(unittest.TestCase):
    """静态路径必须排在带路径参数的同前缀路由之前。

    FastAPI 按**注册顺序**匹配。`/api/sources/status` 如果排在
    `/api/sources/{sid}` 之后，就会被当成 `sid="status"` 吃掉。
    这类 bug 表现为"接口突然 404 或报数据源不存在"，很难一眼看出来。
    """

    def test_static_before_param(self):
        problems = []
        for path, text in ((p, p.read_text(encoding="utf-8")) for p in _modules()):
            pos = {}
            for m in DECOR.finditer(text):
                pos.setdefault(m.group(1), m.start())
            for static, sp in pos.items():
                prefix = static.rsplit("/", 1)[0]
                for other, op in pos.items():
                    if other == static:
                        continue
                    if other.rsplit("/", 1)[0] != prefix:
                        continue
                    if not re.fullmatch(r"\{\w+\}", other.rsplit("/", 1)[1]):
                        continue
                    if sp > op:
                        problems.append(f"{path.name}: {static} 排在 {other} 之后")
        self.assertEqual(
            [], problems,
            "静态路径被带参路由抢先匹配了：\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    unittest.main()
