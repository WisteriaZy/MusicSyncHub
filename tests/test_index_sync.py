"""执行完操作后，索引必须跟着变 —— 否则"删了但删不掉"。

## 真实事故（用户反馈原话）

> 回收站功能不能正常工作（移动到回收站却删不了文件）

排查下来是三处叠加：

1. 前端 `trashDialog` 构造的步骤**没有 `id`**；而 `Executor._sync_index`
   是按 `results[].step` 回查原始步骤的：

       st = by_id.get(r.get("step") or "")
       if not st: continue          # ← 没有 id 就整段跳过

   于是磁盘上的文件进了回收站，**索引里还留着**：曲库照样显示这首歌、
   数字不变。用户看到的就是"删了但没删掉"。

2. 就算有 id，`remove_track(sid, st["src_path"])` 也是按**原样**匹配，
   而索引里存的是绝对路径；调用方传相对路径时 DELETE 命中 0 行。

3. 目录删除走的是另一个接口，它倒是按前缀清索引 —— 所以只有"删单个文件"
   这条路径有问题，更难被发现。

修法：
  · `Executor.run` 给每条步骤补默认 id（任何调用方都不会再踩）；
  · `_drop_track()` 依次尝试绝对 / 相对 / 原样路径，再按目录前缀兜底。
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


class IndexSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-idx-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        from app.core.config import AppConfig, SourceConfig, save_config
        cls.root = Path(cls.tmp.name) / "lib"
        for sub in ("A", "B", "C"):
            (cls.root / sub).mkdir(parents=True)
        for rel in ("A/a1.mp3", "A/a2.mp3", "B/b1.mp3", "C/c1.mp3"):
            (cls.root / rel).write_bytes(b"ID3" + rel.encode() * 8)
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

    def _total(self) -> int:
        return self.client.get("/api/library/s1",
                              params={"limit": 50}).json()["total"]

    def _trash(self, path, with_id=False):
        st = {"op": "trash", "src": "s1", "src_path": path}
        if with_id:
            st["id"] = "x1"
        r = self.client.post("/api/execute", json={
            "steps": [st], "execute": True, "confirm": True, "policy": "skip"})
        self.assertEqual(200, r.status_code, r.text[:220])
        self._idle()
        return r.json()

    # ------------------------------------------------------------ 核心
    def test_step_without_id_still_updates_index(self):
        """★ 就是这条 bug：前端不传 id，索引就永远不更新。"""
        before = self._total()
        res = self._trash("A/a1.mp3")                  # 不带 id
        self.assertEqual("done", res["results"][0]["state"], res)
        self.assertIsNotNone(res["results"][0].get("step"),
                             "执行器没有补默认 id，_sync_index 会整段跳过")
        self.assertEqual(before - 1, self._total(),
                         "磁盘删了但索引没删 —— 曲库里还会显示这首歌")

    def test_relative_path_updates_index(self):
        before = self._total()
        self._trash("A/a2.mp3", with_id=True)          # 相对路径
        self.assertEqual(before - 1, self._total())

    def test_absolute_path_updates_index(self):
        before = self._total()
        abs_path = str(self.root / "B" / "b1.mp3").replace("\\", "/")
        self._trash(abs_path, with_id=True)            # 绝对路径
        self.assertEqual(before - 1, self._total())

    def test_folder_delete_clears_whole_subtree(self):
        """目录删除按前缀清索引（这条本来就对，防止以后改坏）。"""
        before = self._total()
        r = self.client.post("/api/folders/delete", json={
            "source": "s1", "path": "C"})
        self.assertEqual(200, r.status_code, r.text[:200])
        after = self._total()
        self.assertEqual(before - 1, after)

    def test_transfer_removes_source_row(self):
        """跨库传输成功后源侧索引行也要删（否则搜索得到、播放失败）。"""
        from app.core.config import SourceConfig, load_config, save_config
        other = Path(self.tmp.name) / "other"
        other.mkdir(exist_ok=True)
        cfg = load_config()
        cfg.sources.append(SourceConfig(
            id="s2", name="O", type="local",
            root=str(other).replace("\\", "/")))
        save_config(cfg)
        self.client.post("/api/reload", json={})
        # 前面的用例把文件都删光了 —— 自己造一个，别依赖用例顺序
        (self.root / "D").mkdir(exist_ok=True)
        (self.root / "D" / "d1.mp3").write_bytes(b"ID3" + b"d1" * 40)
        self.client.post("/api/scan/s1", json={})
        self._idle()
        before = self._total()
        self.assertGreaterEqual(before, 1, "扫描后索引里应该有文件")
        abs_src = str(self.root / "D" / "d1.mp3").replace("\\", "/")
        r = self.client.post("/api/files/transfer", json={
            "from": "s1", "to": "s2", "items": [abs_src],
            "policy": "skip", "verify": True,
            "execute": True, "confirm": True})
        self.assertEqual(200, r.status_code, r.text[:200])
        self._idle()
        self.assertEqual(before - 1, self._total())


if __name__ == "__main__":
    unittest.main()
