"""文件浏览接口的回归测试。

重点是"每个文件夹有多少首"的口径：聚合是按曲目的 parent 分组的，
只有**直接存放文件**的目录才会出现在聚合结果里。像 `歌手/专辑/` 这种，
`歌手/` 自己不存文件，如果不补齐中间目录，它在列表里就会显示成 0 首
（这正是开发时踩到的 bug）。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient


class FilesApiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-files-api-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        # 数据源根目录必须是真实存在的目录：/api/files/list 会去读文件系统。
        # 注意数据源是记在 config.toml 里的（不是索引库），接口走 resolve_source()
        # 从配置里取，所以这里得真写一份配置。
        cls.root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(cls.root, exist_ok=True)
        from app.core.config import (AppConfig, SourceConfig, save_config,
                                     load_config)
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="测试库", type="local", root=cls.root)]))
        load_config(force=True)
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            from app import main as main_mod
            if main_mod.DB is not None:
                main_mod.DB.close_all()
                main_mod.DB = None
        except Exception:
            pass
        try:
            from app.core import config as cfg_mod
            cfg_mod._cache = None
        except Exception:
            pass
        os.environ.pop("MSH_DATA_DIR", None)
        os.environ.pop("MSH_DB", None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def setUp(self):
        from app.core import matching as M
        from app.main import db

        # 目录结构（root = 上面那个真实临时目录）：
        #   <root>/歌手A/专辑1/    2 首
        #   <root>/歌手A/专辑2/    1 首
        #   <root>/歌手B/          3 首（直接存在这一层）
        #   <root>/散曲/           1 首
        # 期望：歌手A = 3 首（自己 0 首，靠子目录汇总），歌手B = 3，散曲 = 1
        rows = []

        def add(rel, artist, album="", dur=100.0):
            path = f"{self.root}/{rel}"
            name = rel.rsplit("/", 1)[-1]
            rows.append({
                "source_id": "s1", "path": path, "rel_path": rel, "name": name,
                "parent": path.rsplit("/", 1)[0], "ext": ".flac", "size": 1000,
                "mtime": 1.0, "title": name.rsplit(".", 1)[0], "artist": artist,
                "album": album, "duration": dur, "bitrate": 900000,
                "sample_rate": 44100, "channels": 2, "bits_per_sample": 16,
                "codec": "FLAC", "is_lossless": 1, "has_cover": 0, "quality": 100.0,
                "hash_quick": None, "hash_full": None, "hash_state": "none",
            })

        add("歌手A/专辑1/a1.flac", "歌手A", "专辑1")
        add("歌手A/专辑1/a2.flac", "歌手A", "专辑1")
        add("歌手A/专辑2/a3.flac", "歌手A", "专辑2")
        add("歌手B/b1.flac", "歌手B")
        add("歌手B/b2.flac", "歌手B")
        add("歌手B/b3.flac", "歌手B")
        add("散曲/c1.flac", "歌手C")
        for r in rows:
            r["dup_key"] = M.dup_key(r)
            r["name_key"] = r["title"].lower()
        db().upsert_tracks([{k: r.get(k) for k in (
            "source_id", "path", "rel_path", "name", "parent", "ext", "size", "mtime",
            "title", "artist", "album", "duration", "bitrate", "sample_rate", "channels",
            "bits_per_sample", "codec", "is_lossless", "has_cover", "quality",
            "hash_quick", "hash_full", "hash_state", "dup_key", "name_key")} for r in rows])
        db().upsert_source("s1", "测试库", "local", self.root)

    def test_middle_directories_get_aggregated_counts(self):
        """中间目录（自己不存文件）必须汇总子目录的曲目数，不能是 0。"""
        r = self.client.get("/api/files/folders", params={"sid": "s1"})
        self.assertEqual(200, r.status_code, r.text)
        f = {x["rel"]: x for x in r.json()["folders"]}
        self.assertIn("歌手A", f, "中间目录必须出现在结果里")
        self.assertEqual(3, f["歌手A"]["count_all"], "歌手A 应汇总两个专辑的 3 首")
        self.assertEqual(0, f["歌手A"]["count"], "歌手A 自己没有直接存放文件")
        self.assertEqual(2, f["歌手A/专辑1"]["count_all"])
        self.assertEqual(1, f["歌手A/专辑2"]["count_all"])
        self.assertEqual(3, f["歌手B"]["count_all"])
        self.assertEqual(3, f["歌手B"]["count"])
        self.assertEqual(1, f["散曲"]["count_all"])

    def test_deep_nesting_accumulates_all_levels(self):
        """三层嵌套时，顶层目录要累加到最深处。"""
        f = {x["rel"]: x for x in
             self.client.get("/api/files/folders", params={"sid": "s1"}).json()["folders"]}
        # 歌手A 的 3 首分别来自 专辑1(2) + 专辑2(1)
        self.assertEqual(f["歌手A"]["count_all"],
                         f["歌手A/专辑1"]["count_all"] + f["歌手A/专辑2"]["count_all"])

    def test_facets_by_artist(self):
        """按作者统计：歌手A=3、歌手B=3、歌手C=1。"""
        r = self.client.get("/api/files/facets",
                            params={"sid": "s1", "field": "artist"})
        self.assertEqual(200, r.status_code, r.text)
        got = {x["value"]: x["count"] for x in r.json()["facets"]}
        self.assertEqual(3, got.get("歌手A"))
        self.assertEqual(3, got.get("歌手B"))
        self.assertEqual(1, got.get("歌手C"))

    def test_facets_rejects_unknown_field(self):
        r = self.client.get("/api/files/facets",
                            params={"sid": "s1", "field": "password"})
        self.assertEqual(400, r.status_code)

    def test_select_by_artist_returns_dids(self):
        """按作者集合：返回的 did 必须是 源::路径 的形式（前端靠它去重/勾选）。"""
        r = self.client.post("/api/files/select",
                             json={"source": "s1", "by": "artist", "value": "歌手A"})
        self.assertEqual(200, r.status_code, r.text)
        data = r.json()
        self.assertEqual(3, data["count"])
        for it in data["items"]:
            self.assertTrue(it["did"].startswith("s1::"), it["did"])
            self.assertTrue(it["did"].endswith(it["path"]))

    def test_select_unknown_artist_is_empty(self):
        r = self.client.post("/api/files/select",
                             json={"source": "s1", "by": "artist", "value": "不存在"})
        self.assertEqual(0, r.json()["count"])

    def test_move_endpoint_requires_confirm(self):
        """真要写入时必须带 confirm，否则 400。"""
        r = self.client.post("/api/files/move", json={
            "steps": [{"src": "s1", "src_path": "D:/M/a.flac", "dst_path": "D:/M/b.flac"}],
            "execute": True,
        })
        self.assertEqual(400, r.status_code)
        self.assertIn("confirm", r.text)

    def test_move_endpoint_dry_run_does_nothing(self):
        """预演必须不产生任何文件（用真实临时文件验证）。"""
        import pathlib
        src_dir = pathlib.Path(self._tmp.name) / "M"
        src_dir.mkdir(parents=True, exist_ok=True)
        f = src_dir / "dry.flac"
        f.write_bytes(b"x" * 64)
        internal = f"{self.root}/dry.flac"
        r = self.client.post("/api/files/move", json={
            "steps": [{"src": "s1", "src_path": internal,
                       "dst_path": f"{self.root}/sub/dry.flac"}],
            "execute": False,
        })
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["dry_run"])
        self.assertTrue(f.exists(), "预演不应动文件")
        self.assertFalse((src_dir / "sub").exists(), "预演不应建目录")


if __name__ == "__main__":
    unittest.main()
