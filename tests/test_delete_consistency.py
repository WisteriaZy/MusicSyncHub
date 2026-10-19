"""删除后的一致性：不能有幽灵文件、不能误报失败、计数要当场变。

## 用户反馈

> 数据源的索引更新不及时（更新源也是）；删除文件后会在文件浏览变成幽灵文件
> （有时候看得见 有时候看不见，删除会提示失败（尽管已经删除））

三件事其实是一条链：

1. **幽灵文件**：`/api/files/list` 有 20 秒目录聚合缓存，而执行器删完
   **没有清缓存** —— 删掉的文件在列表里继续出现，刷新有时又消失。
2. **误报失败**：同一批里出现重复路径（列表勾选 + 目录树勾选会重复）时，
   第二条 `remove` 抛"文件不存在"，被记成 error → 界面显示"失败 N"。
3. **计数不更新**：删除后前端只 `load()`，没刷数据源曲目数。
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


class DeleteConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-del-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        from app.core.config import AppConfig, SourceConfig, save_config
        cls.root = Path(cls.tmp.name) / "lib"
        for sub in ("A", "B"):
            (cls.root / sub).mkdir(parents=True)
        for rel in ("A/a1.mp3", "A/a2.mp3", "B/b1.mp3"):
            (cls.root / rel).write_bytes(b"ID3" + rel.encode() * 20)
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="L", type="local",
                         root=str(cls.root).replace("\\", "/")),
        ]))
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.post("/api/scan/s1", json={})
        cls._idle()

    @classmethod
    def _idle(cls, timeout: float = 60.0):
        end = time.time() + timeout
        while time.time() < end:
            if not cls.client.get("/api/busy").json().get("busy"):
                return
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        try:
            import app.deps as D
            if getattr(D, "DB", None) is not None:
                D.DB.close_all()
                D.DB = None
        except Exception:
            pass
        try:
            from app.core import jobs as J
            J.reset_for_tests()
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def _names(self, path="A"):
        r = self.client.get("/api/files/list", params={"sid": "s1", "path": path})
        self.assertEqual(200, r.status_code, r.text[:200])
        return [f["name"] for f in r.json().get("files", [])]

    def test_no_ghost_right_after_delete(self):
        """删完立刻查列表：不能还看得见（缓存必须失效）。"""
        self.assertIn("a1.mp3", self._names())
        r = self.client.post("/api/execute", json={
            "steps": [{"op": "trash", "src": "s1", "src_path": "A/a1.mp3"}],
            "execute": True, "confirm": True, "policy": "skip"})
        self.assertEqual(200, r.status_code, r.text[:200])
        self._idle()
        self.assertNotIn("a1.mp3", self._names(),
                         "删完列表里还有它 —— 目录缓存没失效（幽灵文件）")

    def test_duplicate_steps_do_not_report_failure(self):
        """同一路径发两次：第一条 done、第二条 skipped，不能算失败。"""
        r = self.client.post("/api/execute", json={
            "steps": [
                {"op": "trash", "src": "s1", "src_path": "A/a2.mp3"},
                {"op": "trash", "src": "s1", "src_path": "A/a2.mp3"},
            ],
            "execute": True, "confirm": True, "policy": "skip"})
        body = r.json()
        states = [x["state"] for x in body["results"]]
        self.assertIn("done", states)
        self.assertEqual(0, body["summary"].get("error", 0),
                         f"重复路径被记成失败了：{body['results']}")
        self.assertEqual(1, body["summary"].get("skipped", 0), body["summary"])

    def test_counts_update_after_delete(self):
        """数据源里的曲目数必须当场减少（不是等下次扫描）。"""
        before = self.client.get("/api/sources").json()
        n0 = [s for s in before if s["id"] == "s1"][0]["indexed"]
        r = self.client.post("/api/execute", json={
            "steps": [{"op": "trash", "src": "s1", "src_path": "B/b1.mp3"}],
            "execute": True, "confirm": True, "policy": "skip"})
        self.assertEqual(200, r.status_code)
        self._idle()
        after = self.client.get("/api/sources").json()
        n1 = [s for s in after if s["id"] == "s1"][0]["indexed"]
        self.assertEqual(n0 - 1, n1, "删除后数据源计数没变")


if __name__ == "__main__":
    unittest.main()
