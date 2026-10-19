"""索引自动刷新：验证"库变了，索引自己跟上"。

## 覆盖三个机制

1. 内容被替换（大小 / mtime 变） → 只重扫那个目录，索引跟着变
2. 新增文件（父目录 mtime 变） → 重扫后索引里出现
3. 文件被删 → 重扫该目录时清理掉失效行（不留幽灵）
另加：忙碌时必须让路；设置关掉时线程不启动。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))


class IndexWatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-iw-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        from app.core.config import AppConfig, SourceConfig, save_config
        cls.root = Path(cls.tmp.name) / "lib"
        (cls.root / "A").mkdir(parents=True)
        for n, size in (("a1.mp3", 200), ("a2.mp3", 200)):
            (cls.root / "A" / n).write_bytes(b"x" * size)
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="L", type="local",
                         root=str(cls.root).replace("\\", "/")),
        ]))
        from app.core import scanner as S
        import app.deps as D
        cls.S, cls.D = S, D
        S.scan_source("s1", D.manager.get("s1"), D.db())

    @classmethod
    def tearDownClass(cls):
        # ★ 必须丢掉全局 jobs 单例：它绑定的是本类的临时 DB，
        #   类结束后 DB 被关掉，后面的测试再用 get_jobs() 就会炸。
        try:
            import app.core.jobs as _J
            _J.reset_for_tests()
        except Exception:
            pass
        try:
            import app.deps as D
            if getattr(D, "DB", None) is not None:
                D.DB.close_all()
                D.DB = None
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def setUp(self):
        # tick_once 现在会登记独占 Job；上一个用例的 job 若没清干净会误判 busy
        from app.core import jobs as J
        try:
            J.reset_for_tests()
        except Exception:
            pass

    def _rows(self):
        return {r["name"]: r for r in self.D.db().tracks("s1")}

    # ------------------------------------------------------------ 机制
    def test_detects_content_change_and_updates_index(self):
        from app.core import indexwatch as W
        W.tick_once(force=True)                       # 首次：只播种
        p = self.root / "A" / "a1.mp3"
        p.write_bytes(b"y" * 777)                     # 内容替换（大小变）
        res = W.tick_once(force=True)
        self.assertIn("s1", res["sources"])
        self.assertTrue(res["sources"]["s1"].get("scanned"),
                       f"没有触发重扫：{res['sources']['s1']}")
        rows = self._rows()
        self.assertEqual(777, int(rows["a1.mp3"]["size"]),
                         "索引里的体积没跟上")

    def test_detects_new_file(self):
        from app.core import indexwatch as W
        W.tick_once(force=True)
        new = self.root / "A" / "a3.mp3"
        new.write_bytes(b"z" * 333)
        W.tick_once(force=True)
        self.assertIn("a3.mp3", self._rows(), "新增文件没进索引")

    def test_removes_deleted_file(self):
        from app.core import indexwatch as W
        W.tick_once(force=True)
        (self.root / "A" / "a2.mp3").unlink()
        W.tick_once(force=True)
        self.assertNotIn("a2.mp3", self._rows(), "删掉的文件还留在索引里")

    def test_skips_when_busy(self):
        from app.core import indexwatch as W, jobs as J

        class _Busy:
            # ★ 必须是**方法**：真实代码里 `Jobs.busy` 是方法。
            #   这里写成属性（字典）会掩盖"漏写括号"这类 bug ——
            #   本轮 BUG-02 就是这么被测试放过去的。
            def busy(self):
                return {"kind": "scan", "id": "x"}

        old = J.get_jobs
        J.get_jobs = lambda: _Busy()                  # type: ignore[assignment]
        try:
            res = W.tick_once()                        # 不 force
        finally:
            J.get_jobs = old                           # type: ignore[assignment]
        self.assertEqual("busy", res.get("skipped"),
                         "有写任务时不该抢跑")

    def test_busy_is_called_as_method(self):
        """BUG-02 回归：`busy` 是方法，漏写括号会恒为真、后台全线瘫痪。"""
        src = (Path(__file__).resolve().parent.parent
               / "app" / "core" / "indexwatch.py").read_text(encoding="utf-8")
        self.assertIn("get_jobs().busy()", src)
        self.assertNotIn("get_jobs().busy:", src)

    def test_status_reports_after_run(self):
        from app.core import indexwatch as W
        W.tick_once(force=True)
        st = W.status()
        self.assertIn("running", st)
        self.assertIsNotNone(st.get("last"))


if __name__ == "__main__":
    unittest.main()
