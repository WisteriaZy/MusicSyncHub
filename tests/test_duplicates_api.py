"""查重接口的响应契约测试。

这些字段是前端用来做"保留谁"选择的唯一依据，一旦命名/格式改了，
界面就会出现"每行都写将移入回收站、radio 全没选中"这种看起来像 bug 的现象。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient


class DuplicatesApiContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-dup-api-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        # Windows 上 SQLite 连接没关掉就删不掉临时目录。
        try:
            from app import main as main_mod
            inst = main_mod.DB
            if inst is not None:
                inst.close_all()
                main_mod.DB = None
        except Exception:
            pass
        os.environ.pop("MSH_DATA_DIR", None)
        os.environ.pop("MSH_DB", None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def _mk(self, sid, name, title, artist, dur, size, hash_):
        return {
            "id": abs(hash(name)) % 100000, "source_id": sid,
            "path": f"D:/{sid}/{name}", "rel_path": name, "name": name,
            "parent": f"D:/{sid}", "ext": ".flac", "size": size, "mtime": 1.0,
            "title": title, "artist": artist, "album": "A", "duration": dur,
            "bitrate": 900000, "sample_rate": 44100, "channels": 2,
            "bits_per_sample": 16, "codec": "FLAC", "is_lossless": 1,
            "has_cover": 0, "quality": 100.0, "hash_quick": hash_,
            "hash_full": None, "hash_state": "quick",
        }

    def test_group_identity_fields_are_consistent(self):
        """key / base_key 要带源前缀，suggested_keep 与 items[].did 必须是同一种写法。"""
        from app.core import matching as M
        from app.main import db

        rows = []
        # 两个"库"里各有同一首歌的重复副本 —— 组键必须靠 source_id 区分开
        for sid in ("srcA", "srcB"):
            rows.append(self._mk(sid, "Entropy.flac", "Entropy", "Distrion", 205.0,
                                 43_400_000, "samehash"))
            rows.append(self._mk(sid, "Entropy (2).flac", "Entropy", "Distrion", 205.0,
                                 43_400_000, "samehash"))
        for r in rows:
            r["dup_key"] = M.dup_key(r)
        db().upsert_tracks([{k: r.get(k) for k in (
            "source_id", "path", "rel_path", "name", "parent", "ext", "size", "mtime",
            "title", "artist", "album", "duration", "bitrate", "sample_rate", "channels",
            "bits_per_sample", "codec", "is_lossless", "has_cover", "quality",
            "hash_quick", "hash_full", "hash_state", "dup_key")} for r in rows])
        for sid in ("srcA", "srcB"):
            db().upsert_source(sid, sid, "local", f"D:/{sid}")

        # 查重现在是**后台任务**（大库上几十秒，同步请求会让界面像卡死）。
        # 测试用 wait=true 走同步分支，直接拿结果。
        r = self.client.post("/api/duplicates",
                             json={"sources": ["srcA", "srcB"], "within": "same",
                                   "hash_mode": "none", "wait": True})
        self.assertEqual(200, r.status_code, r.text)
        data = r.json()
        groups = data["groups"]
        self.assertEqual(2, len(groups), "两个库各一组，不能并成一组")

        keys = [g["key"] for g in groups]
        self.assertEqual(len(keys), len(set(keys)), "组键必须唯一（要带 source_id 前缀）")
        for g in groups:
            self.assertTrue(g["key"].startswith(g["source_id"] + "::"),
                            f"key 缺少源前缀: {g['key']}")
            self.assertTrue(g["suggested_keep"], "必须有默认保留项")
            ids = [it["did"] for it in g["items"]]
            self.assertEqual(len(ids), len(set(ids)), "did 必须唯一")
            self.assertIn(g["suggested_keep"], ids,
                          "suggested_keep 必须与 items[].did 同格式，否则前端选不中任何一行")
            for it in g["items"]:
                self.assertTrue(it["did"].startswith(it["source_id"] + "::"))

    def test_version_tags_present_on_items(self):
        """每条记录都要带 version_tags，界面才能显示"伴奏/重制版"徽标。"""
        from app.core import matching as M
        from app.main import db

        rows = [
            self._mk("srcC", "晴天.flac", "晴天", "周杰伦", 240.0, 10_000_000, "h1"),
            self._mk("srcC", "晴天 (伴奏).flac", "晴天", "周杰伦", 240.0, 9_000_000, "h2"),
        ]
        for r in rows:
            r["dup_key"] = M.dup_key(r)
        db().upsert_tracks([{k: r.get(k) for k in (
            "source_id", "path", "rel_path", "name", "parent", "ext", "size", "mtime",
            "title", "artist", "album", "duration", "bitrate", "sample_rate", "channels",
            "bits_per_sample", "codec", "is_lossless", "has_cover", "quality",
            "hash_quick", "hash_full", "hash_state", "dup_key")} for r in rows])
        db().upsert_source("srcC", "srcC", "local", "D:/srcC")

        data = self.client.post("/api/duplicates",
                                json={"sources": ["srcC"], "within": "same",
                                      "hash_mode": "none", "wait": True}).json()
        for g in data["groups"]:
            for it in g["items"]:
                self.assertIn("version_tags", it, "缺少 version_tags 字段")
        self.assertEqual(0, len(data["groups"]),
                         "原曲与伴奏不该被判为重复")


if __name__ == "__main__":
    unittest.main()
