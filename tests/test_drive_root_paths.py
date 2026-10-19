"""盘符根（`Y:\\` 这种）的路径处理。

## 真实 bug（第 1 轮排查抓到）

用户把网络位置映射成盘符（例如 `Z:\\`）当数据源。`P.norm("Y:\\\\")` 会把尾斜杠也剥掉
→ `"Y:"`，而 **`Z:` 在 Windows 上是「盘符相对路径」**（相对 Y: 盘自己的当前目录），
不是根目录。于是：

    os.path.join("Y:", "x.mp3")  →  "Y:x.mp3"      ← 少一个分隔符

后果（都在真实库里复现过）：

  · 索引里存成 `Z:节奏医生/…`，`rel_path` 也带着盘符
    → 与另一个库的键格式不一致（查重/整理对不上）
  · `inside_root("Y:", "Y:节奏医生/…")` 永远 `False`
    → 一旦该源可写，**所有写操作都会被判"越界"拒绝**
  · 扫描**漏文件**：老索引 2301 条，磁盘实际 2308 条，少了 7 个

界面之所以"看着正常"，是因为 Windows 恰好把裸盘符解析成根目录 —— 靠运气。

修法在 `LocalSource._native`：裸盘符补上分隔符，让盘符根变成真正的根。

## 这些测试**不碰磁盘**

只是验证字符串变换，所以用不存在的盘符（`Z:`、`Q:`）是安全的。
"""
from __future__ import annotations

import os
import sys
import unittest

_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "nt",
                     "盘符根是 Windows 独有的概念；POSIX 上 /mnt/x 这类根没有'裸盘符'问题")
class DriveRootTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core.config import SourceConfig
        from app.sources.base import build_source
        cls.SourceConfig = SourceConfig
        # ★ 必须 staticmethod：把普通函数赋成类属性会变成**方法**，
        #   调用时 self 会被当成第一个参数传进去（踩过）。
        cls.build_source = staticmethod(build_source)

    def _src(self, root: str):
        return self.build_source(self.SourceConfig(
            id="z", name="Z", type="local", root=root))

    # ---------------------------------------------------------- native 转换
    def test_bare_drive_gets_separator(self):
        """根是 `Z:` 时，native 路径必须以分隔符结尾 —— 否则它是盘符相对路径。"""
        for root in ("Z:", "Z:/", "Z:\\"):
            with self.subTest(root=root):
                src = self._src(root)
                native = src._native("")
                self.assertTrue(native.endswith(("\\", "/")),
                                f"root={root!r} → _native('')={native!r} 缺分隔符")

    def test_join_from_drive_root(self):
        """从盘符根拼出来的绝对路径必须是 `Z:/x`，不能是 `Z:x`。"""
        for root in ("Z:", "Z:/", "Z:\\"):
            with self.subTest(root=root):
                src = self._src(root)
                got = src.abs_path("a/b.mp3")
                self.assertEqual("Z:/a/b.mp3", got)

    def test_rel_path_strips_drive(self):
        """相对路径里**不能**带盘符。"""
        src = self._src("Z:")
        self.assertEqual("a/b.mp3", src.rel_path("Z:/a/b.mp3"))

    def test_inside_root_agrees_with_abs_path(self):
        """`abs_path` 产出的路径必须被判为"在根内"。

        这一条断了就意味着：该源上任何写操作都会报"超出数据源根目录"。
        """
        from app.core import paths as P
        for root in ("Z:", "Z:/", "Z:\\"):
            with self.subTest(root=root):
                src = self._src(root)
                self.assertTrue(
                    P.inside_root(src.root, src.abs_path("a")),
                    f"root={root!r} 下 abs_path('a') 被判越界")

    def test_normal_subdir_root_unaffected(self):
        """普通子目录根（`D:/Music`）不能被上面的改动带歪。"""
        src = self._src("D:/Music")
        self.assertEqual("D:\\Music", src._native(""))
        self.assertEqual("D:/Music/a/b.mp3", src.abs_path("a/b.mp3"))
        self.assertEqual("a/b.mp3", src.rel_path("D:/Music/a/b.mp3"))


if __name__ == "__main__":
    unittest.main()
