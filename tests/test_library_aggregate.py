"""曲库浏览接口的统计口径回归测试。

两个真实踩过的坑：
1. `/api/library/{sid}` 的"总体积/平均码率"原来只按 source_id 统计，
   完全忽略 prefix 和筛选条件 —— 界面选了文件夹，列表变了但数字还是整库的，
   看起来就像"统计不跟着目录走"。
2. 分页参数（LIMIT/OFFSET）曾经被追加进同一个 params 列表，导致统计语句
   收到多余的绑定参数直接 500（sqlite3.ProgrammingError）。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient


class LibraryAggregateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-lib-agg-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        cls.root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(cls.root, exist_ok=True)
        from app.core.config import AppConfig, SourceConfig, save_config, load_config
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

        # 目录A：3 首无损（大）；目录B：2 首有损（小）
        rows = []

        def add(rel, size, lossless, bitrate, artist, codec):
            path = f"{self.root}/{rel}"
            rows.append({
                "source_id": "s1", "path": path, "rel_path": rel,
                "name": rel.rsplit("/", 1)[-1], "parent": path.rsplit("/", 1)[0],
                "ext": "." + codec.lower(), "size": size, "mtime": 1.0,
                "title": rel.rsplit("/", 1)[-1], "artist": artist, "album": "",
                "duration": 200.0, "bitrate": bitrate, "sample_rate": 44100,
                "channels": 2, "bits_per_sample": 16, "codec": codec,
                "is_lossless": 1 if lossless else 0, "has_cover": 0, "quality": 100.0,
                "hash_quick": None, "hash_full": None, "hash_state": "none",
            })

        add("A/a1.flac", 10_000_000, True, 900_000, "歌手甲", "FLAC")
        add("A/a2.flac", 20_000_000, True, 900_000, "歌手甲", "FLAC")
        add("A/a3.flac", 30_000_000, True, 900_000, "歌手乙", "FLAC")
        add("B/b1.mp3", 1_000_000, False, 128_000, "歌手丙", "MP3")
        add("B/b2.mp3", 2_000_000, False, 320_000, "歌手丙", "MP3")
        for r in rows:
            r["dup_key"] = M.dup_key(r)
        db().upsert_tracks([{k: r.get(k) for k in (
            "source_id", "path", "rel_path", "name", "parent", "ext", "size", "mtime",
            "title", "artist", "album", "duration", "bitrate", "sample_rate", "channels",
            "bits_per_sample", "codec", "is_lossless", "has_cover", "quality",
            "hash_quick", "hash_full", "hash_state", "dup_key")} for r in rows])
        db().upsert_source("s1", "测试库", "local", self.root)

    def _lib(self, **params):
        r = self.client.get("/api/library/s1", params=params)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def test_whole_library_aggregate(self):
        d = self._lib()
        self.assertEqual("all", d["scope"])
        self.assertEqual(5, d["aggregate"]["count"])
        self.assertEqual(63_000_000, d["aggregate"]["bytes"])
        self.assertEqual(3, d["aggregate"]["lossless"])
        self.assertEqual(5, d["aggregate_all"]["count"])

    def test_aggregate_follows_prefix(self):
        """选了目录，统计必须只算该目录（含子目录）。"""
        a = self._lib(prefix=f"{self.root}/A")
        self.assertEqual("folder", a["scope"])
        self.assertEqual(3, a["aggregate"]["count"], "目录A 只有 3 首")
        self.assertEqual(60_000_000, a["aggregate"]["bytes"])
        self.assertEqual(3, a["aggregate"]["lossless"])
        self.assertEqual(900_000, a["aggregate"]["avg_bitrate"])

        b = self._lib(prefix=f"{self.root}/B")
        self.assertEqual(2, b["aggregate"]["count"])
        self.assertEqual(3_000_000, b["aggregate"]["bytes"])
        self.assertEqual(0, b["aggregate"]["lossless"])
        self.assertEqual(224_000, b["aggregate"]["avg_bitrate"])
        # 整库对比值不受 prefix 影响
        self.assertEqual(5, b["aggregate_all"]["count"])

    def test_prefix_scopes_are_disjoint_and_sum_to_whole(self):
        a = self._lib(prefix=f"{self.root}/A")["aggregate"]
        b = self._lib(prefix=f"{self.root}/B")["aggregate"]
        whole = self._lib()["aggregate"]
        self.assertEqual(whole["count"], a["count"] + b["count"])
        self.assertEqual(whole["bytes"], a["bytes"] + b["bytes"])

    def test_aggregate_follows_filters(self):
        """搜索 / 仅无损 / 码率下限都要影响统计。"""
        lossless = self._lib(lossless=True)
        self.assertEqual("filtered", lossless["scope"])
        self.assertEqual(3, lossless["aggregate"]["count"])
        self.assertEqual(60_000_000, lossless["aggregate"]["bytes"])

        by_artist = self._lib(q="歌手丙")
        self.assertEqual(2, by_artist["aggregate"]["count"])
        self.assertEqual(3_000_000, by_artist["aggregate"]["bytes"])
        self.assertEqual(0, by_artist["aggregate"]["lossless"])

        minbr = self._lib(min_bitrate=500_000)
        self.assertEqual(3, minbr["aggregate"]["count"])

    def test_pagination_params_do_not_leak_into_aggregate(self):
        """分页参数只作用于列表，不能污染统计语句（曾经的 500）。"""
        p1 = self._lib(limit=1, offset=0)
        p2 = self._lib(limit=1, offset=3)
        self.assertEqual(1, len(p1["tracks"]))
        self.assertEqual(1, len(p2["tracks"]))
        # 统计与 offset 无关，永远是整个范围
        self.assertEqual(5, p1["aggregate"]["count"])
        self.assertEqual(5, p2["aggregate"]["count"])
        self.assertEqual(p1["aggregate"]["bytes"], p2["aggregate"]["bytes"])
        # 总数也不受 limit 影响
        self.assertEqual(5, p1["total"])
        self.assertEqual(5, p2["total"])

    def test_combined_prefix_and_pagination(self):
        """目录 + 分页 + 统计三者同时使用（最容易踩参数错位的组合）。"""
        d = self._lib(prefix=f"{self.root}/A", limit=2, offset=0)
        self.assertEqual(2, len(d["tracks"]))
        self.assertEqual(3, d["total"])
        self.assertEqual(3, d["aggregate"]["count"])
        self.assertEqual(60_000_000, d["aggregate"]["bytes"])