"""回收站还原：删除说"可恢复"，就必须真的能恢复。

## 为什么补这一块

界面和文档一直写着"删除 = 移入 `<库根>/.music-sync-trash/日期/`，**可恢复**"，
但应用里**没有任何还原入口** —— 用户得自己打开资源管理器、翻日期目录、
还得记得它原来在哪个文件夹。承诺了可恢复却没给恢复的办法，这是安全闭环上
唯一的缺口。

现在的做法：
  · 删除时把"原相对路径"记进回收站目录里的 `_origin.json`
    （跟着回收站走：DB 重建、回收站被整体搬走，都还能还原）
  · 还原默认回到**原位置**，默认**跳过同名**，绝不覆盖
  · 老回收站没有 `_origin.json` → 仍能列出，还原时落回库根目录
  · 彻底删除需要显式 confirm（界面上还要输入短语）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))


class TrashRestoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-trash-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        from app.core.config import SourceConfig
        from app.sources.base import build_source
        cls.root = Path(cls.tmp.name) / "lib"
        (cls.root / "Album").mkdir(parents=True)
        cls.src = build_source(SourceConfig(
            id="s1", name="L", type="local",
            root=str(cls.root).replace("\\", "/")))

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def setUp(self):
        # 每个用例都从"干净库"开始：清掉回收站与残留
        import shutil
        trash = self.root / ".music-sync-trash"
        if trash.exists():
            shutil.rmtree(trash, ignore_errors=True)
        for p in list((self.root / "Album").glob("*")):
            p.unlink()
        for p in list(self.root.glob("*.mp3")):
            p.unlink()

    # ------------------------------------------------------------ 基础
    def test_delete_records_origin(self):
        f = self.root / "Album" / "a.mp3"
        f.write_bytes(b"ID3" + b"\x01" * 100)
        self.src.remove("Album/a.mp3")
        self.assertFalse(f.exists())
        items = self.src.trash_list()
        self.assertEqual(1, len(items), items)
        self.assertEqual("a.mp3", items[0]["name"])
        self.assertEqual("Album/a.mp3", items[0]["origin"],
                         "删除时没记下原路径，之后就还原不回去了")

    def test_restore_goes_back_to_original_path(self):
        f = self.root / "Album" / "a.mp3"
        f.write_bytes(b"ID3" + b"\x02" * 100)
        self.src.remove("Album/a.mp3")
        rel = self.src.trash_list()[0]["rel"]
        r = self.src.trash_restore(rel)
        self.assertEqual("done", r["state"], r)
        self.assertEqual("Album/a.mp3", r["target"])
        self.assertTrue(f.exists())
        self.assertEqual(b"ID3" + b"\x02" * 100, f.read_bytes())
        self.assertEqual([], self.src.trash_list(), "还原后回收站里不该还有它")

    def test_origin_file_survives_without_db(self):
        """`_origin.json` 是跟着回收站目录走的，不依赖索引库。"""
        f = self.root / "Album" / "a.mp3"
        f.write_bytes(b"ID3" + b"\x03" * 100)
        self.src.remove("Album/a.mp3")
        origin_file = self.root / ".music-sync-trash" 
        found = list(origin_file.rglob("_origin.json"))
        self.assertTrue(found, "没有写出 _origin.json")
        data = json.loads(found[0].read_text(encoding="utf-8"))
        self.assertTrue(any(v.get("rel") == "Album/a.mp3" for v in data.values()), data)

    # ------------------------------------------------------------ 冲突
    def test_restore_skips_when_target_exists(self):
        f = self.root / "Album" / "a.mp3"
        f.write_bytes(b"OLD")
        self.src.remove("Album/a.mp3")
        rel = self.src.trash_list()[0]["rel"]
        f.write_bytes(b"NEW")                      # 原位置又被占用
        r = self.src.trash_restore(rel, policy="skip")
        self.assertEqual("conflict", r["state"], r)
        self.assertEqual(b"NEW", f.read_bytes(), "覆盖了已有文件")
        self.assertEqual(1, len(self.src.trash_list()), "冲突时不该动回收站")

    def test_restore_renames_when_asked(self):
        f = self.root / "Album" / "a.mp3"
        f.write_bytes(b"OLD")
        self.src.remove("Album/a.mp3")
        rel = self.src.trash_list()[0]["rel"]
        f.write_bytes(b"NEW")
        r = self.src.trash_restore(rel, policy="rename")
        self.assertEqual("done", r["state"], r)
        self.assertEqual(b"NEW", f.read_bytes())
        self.assertTrue((self.root / "Album" / "a (2).mp3").exists(),
                        f"没有改名落下: {r}")

    # ------------------------------------------------------------ 目录
    def test_trash_and_restore_directory(self):
        d = self.root / "Album2"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "x.mp3").write_bytes(b"X")
        self.src.trash_dir("Album2")
        self.assertFalse(d.exists())
        items = self.src.trash_list()
        self.assertTrue(items and items[0]["is_dir"], items)
        self.assertEqual("Album2", items[0]["origin"])
        r = self.src.trash_restore(items[0]["rel"])
        self.assertEqual("done", r["state"], r)
        self.assertTrue((self.root / "Album2" / "sub" / "x.mp3").exists())

    # ------------------------------------------------------------ 老回收站
    def test_old_trash_without_origin_still_lists_and_restores(self):
        """升级前删的文件没有 `_origin.json`，也要能列出、能还原。"""
        day = self.root / ".music-sync-trash" / "20260101"
        day.mkdir(parents=True)
        (day / "legacy.mp3").write_bytes(b"L")
        items = self.src.trash_list()
        self.assertEqual(1, len(items), items)
        self.assertEqual("", items[0]["origin"])
        r = self.src.trash_restore(items[0]["rel"])
        self.assertEqual("done", r["state"], r)
        self.assertTrue((self.root / "legacy.mp3").exists())

    # ------------------------------------------------------------ 彻底删除
    def test_purge_removes_permanently(self):
        f = self.root / "Album" / "a.mp3"
        f.write_bytes(b"ID3" + b"\x04" * 100)
        self.src.remove("Album/a.mp3")
        rel = self.src.trash_list()[0]["rel"]
        self.src.trash_purge(rel)
        self.assertEqual([], self.src.trash_list())
        self.assertFalse(f.exists())


if __name__ == "__main__":
    unittest.main()

class TrashApiTest(unittest.TestCase):
    """走 HTTP 的那一层：权限、404、索引刷新。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-trashapi-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        from app.core.config import AppConfig, SourceConfig, save_config
        cls.root = Path(cls.tmp.name) / "lib"
        (cls.root / "Album").mkdir(parents=True)
        (cls.root / "Album" / "a.mp3").write_bytes(b"ID3" + b"\x09" * 120)
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
        import time
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
        from app.core.db import Database
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

    def test_list_restore_roundtrip_via_http(self):
        f = self.root / "Album" / "a.mp3"
        self.assertTrue(f.exists())
        # 用"移入回收站"删掉它
        r = self.client.post("/api/execute", json={
            "steps": [{"id": "s1", "op": "trash", "src": "s1",
                       "src_path": "Album/a.mp3"}],
            "execute": True, "confirm": True})
        self.assertEqual(200, r.status_code, r.text[:200])
        self._idle()
        self.assertFalse(f.exists(), "文件没被移走")

        data = self.client.get("/api/trash", params={"sid": "s1"}).json()
        self.assertEqual(1, data["total"], data)
        item = data["items"][0]
        self.assertEqual("Album/a.mp3", item["origin"])

        r2 = self.client.post("/api/trash/restore", json={
            "source": "s1", "items": [{"rel": item["rel"]}], "policy": "skip"})
        self.assertEqual(200, r2.status_code, r2.text[:200])
        self.assertEqual(1, r2.json()["done"])
        self.assertTrue(f.exists(), "还原后文件没回来")
        # 还原出来的要回到索引里
        lib = self.client.get("/api/library/s1", params={"limit": 10}).json()
        self.assertGreaterEqual(lib["total"], 1)

    def test_other_source_404(self):
        r = self.client.get("/api/trash", params={"sid": "nope"})
        self.assertEqual(404, r.status_code)

    def test_purge_requires_confirm(self):
        r = self.client.post("/api/trash/purge", json={
            "source": "s1", "items": [{"rel": "20260101/x.mp3"}]})
        self.assertEqual(400, r.status_code, r.text[:120])

    def test_readonly_source_blocked(self):
        from app.core.config import SourceConfig, load_config, save_config
        cfg = load_config()
        cfg.sources.append(SourceConfig(
            id="ro", name="RO", type="local",
            root=str(self.root).replace("\\", "/"), read_only=True))
        save_config(cfg)
        try:
            r = self.client.post("/api/trash/restore", json={
                "source": "ro", "items": [{"rel": "x.mp3"}]})
            self.assertEqual(403, r.status_code, r.text[:160])
        finally:
            cfg = load_config(force=True)
            cfg.sources = [s for s in cfg.sources if s.id != "ro"]
            save_config(cfg)

