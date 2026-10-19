"""端点层的"守卫"必须真的挂上 —— 用一个真 HTTP 客户端验证。

## 为什么单独测这个

守卫有两种写法，静态检查都容易漏：

  · `exclusive: None = Depends(exclusive_guard)`   写在**签名**里
  · `get_jobs().ensure_free()`                     写在**函数体**里

第一种尤其阴险：函数直接调用时 `exclusive` 只是默认值 `None`，
守卫根本不执行 —— 只有真正经过 FastAPI 的依赖注入才会触发。
所以必须用 `TestClient` 走一遍真实请求。

## 覆盖的两条

1. `POST /api/sources` —— 建数据源会写 config.toml + 改 source 表。
   和 `PUT /api/sources/{sid}`（一直有守卫）保持一致。
2. `PUT /api/settings` —— 改 `trash_dir` / `station_dir` / `staging_dir`
   会改变"文件最终落在哪个目录"。有任务在跑时改掉，同一个任务前后几步
   会落到不同目录 —— 文件还在，但用户按新路径找不到。
   其余设置随时可改，不该把整个设置页锁死。
"""
from __future__ import annotations

import os
import tempfile
import unittest

_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent


class GuardTest(unittest.TestCase):
    """独占位在 HTTP 层必须生效（409）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-guard-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        import sys
        sys.path.insert(0, str(_PROJ))
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            from app.core import jobs as J
            J.reset_for_tests()
        except Exception:
            pass
        # ★ 必须先关掉 main 里那个模块级 `DB`，再清临时目录。
        #   这些用例会真的打到 `/api/folders/create` 之类的写端点，
        #   它们会调 `db()` 在临时目录里建库；只调 `Database.close_all()`
        #   有时关不干净，Windows 上就报 WinError 32（文件被占用），
        #   测试本身全过、只在 tearDown 炸 —— 很误导。
        try:
            import app.deps as _M
            if getattr(_M, "DB", None) is not None:
                try:
                    _M.DB.close_all()
                except Exception:
                    pass
                _M.DB = None
        except Exception:
            pass
        try:
            from app.core.db import Database
            Database.close_all()
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls._tmp.cleanup()  # 若仍被占用，TemporaryDirectory 会在 GC 时重试

    def _occupy(self):
        """占住独占位，模拟"有任务在跑"。"""
        from app.core.jobs import get_jobs
        j = get_jobs().create("move", total=1, exclusive=True)
        self.assertTrue(j.exclusive)
        return j

    def _release(self):
        from app.core import jobs as J
        J.reset_for_tests()

    def test_create_source_blocked_while_busy(self):
        """有任务在跑时，建数据源必须 409 而不是悄悄改配置。"""
        self._occupy()
        try:
            r = self.client.post("/api/sources", json={
                "id": "zz-test", "name": "T", "type": "local",
                "root": str(_PROJ / "demo"),
            })
            self.assertEqual(409, r.status_code,
                             f"应拒绝，实际 {r.status_code}: {r.text[:200]}")
        finally:
            self._release()

    def test_update_source_blocked_while_busy(self):
        """对照组：改数据源一直是 409（说明守卫机制本身是好的）。"""
        self._occupy()
        try:
            r = self.client.put("/api/sources/nope", json={"name": "x"})
            self.assertEqual(409, r.status_code,
                             f"应拒绝，实际 {r.status_code}: {r.text[:200]}")
        finally:
            self._release()

    def test_path_settings_blocked_while_busy(self):
        """改「文件往哪去」的设置时，有任务在跑必须 409。"""
        self._occupy()
        try:
            for key in ("trash_dir", "station_dir", "staging_dir"):
                r = self.client.put("/api/settings", json={key: "/tmp/x"})
                self.assertEqual(
                    409, r.status_code,
                    f"{key} 在任务运行中不该允许修改，实际 {r.status_code}: "
                    f"{r.text[:160]}")
        finally:
            self._release()

    def test_other_settings_allowed_while_busy(self):
        """别的设置不该被连带锁死 —— 否则用户连线程数都改不了。"""
        self._occupy()
        try:
            r = self.client.put("/api/settings", json={"duration_tolerance_sec": 3.0})
            self.assertEqual(
                200, r.status_code,
                f"非路径类设置应允许修改，实际 {r.status_code}: {r.text[:200]}")
        finally:
            # 改回去了，别影响别的测试
            self._release()
            self.client.put("/api/settings", json={"duration_tolerance_sec": 2.0})

    def test_rejects_illegal_value(self):
        """非法值要 400 并说清哪个字段，不能默默存下来。"""
        self._release()
        r = self.client.put("/api/settings", json={"scan_workers": 99999999})
        self.assertIn(r.status_code, (200, 400),
                      f"异常状态：{r.status_code}")
        if r.status_code == 200:
            body = r.json()
            self.assertIn("errors", body,
                          "超大值必须被 report 出来（部分成功要带 errors）")


    # ---------------------------------------------------------- 真占位
    def test_write_endpoint_creates_and_finishes_job(self):
        """写端点在请求期间必须**真的建一个独占任务**，而不是只看一眼 busy()。

        这是"假守卫"的判别标准：原来 station/tags/folders 的写端点只挂
        `exclusive_guard`（进门检查），业务函数里从不 `create()`，
        于是整个写入期间 `busy()` 都是 None，别的写流程可以合法插进来。
        """
        from app.core.jobs import get_jobs
        self._release()
        before = len(get_jobs().list())
        r = self.client.post("/api/folders/create",
                             json={"source": "zz", "path": "nope"})
        self.assertNotEqual(500, r.status_code, r.text[:200])
        after = get_jobs().list()
        self.assertGreater(len(after), before,
                           "写端点没有创建任何任务 —— 那就是假守卫")
        # 请求结束后任务必须已收尾，不能留一个永远 running 的僵尸
        running = [j for j in after
                   if getattr(j, "state", "") == "running" and getattr(j, "exclusive", False)]
        self.assertEqual([], [(j.id, j.kind) for j in running[:3]],
                         f"请求结束后还留着独占任务：{[(j.id, j.kind) for j in running[:3]]}")

    def test_write_endpoint_blocked_by_real_slot(self):
        """已有一个独占任务在跑时，写端点必须 409（这次是真占位，不是只看一眼）。"""
        self._occupy()
        try:
            for path, body in (("/api/folders/create", {"source": "zz", "path": "nope"}),
                               ("/api/tags/write", {"source": "zz", "paths": ["a.mp3"]}),
                               ("/api/station/remove", {"files": ["a.mp3"]})):
                r = self.client.post(path, json=body)
                self.assertEqual(409, r.status_code,
                                 f"{path} 应 409，实际 {r.status_code}: {r.text[:160]}")
        finally:
            self._release()

    def test_read_only_endpoint_not_blocked(self):
        """只读端点不该被独占位拦住。"""
        self._occupy()
        try:
            r = self.client.get("/api/system")
            self.assertEqual(200, r.status_code)
        finally:
            self._release()


if __name__ == "__main__":
    unittest.main()
