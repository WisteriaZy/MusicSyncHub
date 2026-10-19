"""曲库浏览的模糊搜索接口（/api/library?fuzzy=1）回归测试。

关掉 fuzzy 时走 SQL LIKE（精确子串）；打开时按字段模糊打分，
容忍错别字、拼音缩写与词序差异。两者都要能和其他筛选条件共存。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient


class LibraryFuzzySearchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-libfuzzy-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        cls.root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(cls.root, exist_ok=True)
        from app.core.config import AppConfig, SourceConfig, save_config, load_config
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="测试库", type="local", root=cls.root)]))
        load_config(force=True)
        from app.main import app, db
        db().upsert_source("s1", "测试库", "local", cls.root)
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
        with db().write() as conn:
            conn.execute("DELETE FROM track WHERE source_id='s1'")
        rows = []
        spec = [
            ("Pop/01 - 周杰伦 - 晴天.flac", "晴天", "周杰伦", "", 320000, 1),
            ("Pop/02 - 周杰伦 - 青花瓷.flac", "青花瓷", "周杰伦", "", 320000, 1),
            ("Pop/03 - Adele - Hello.flac", "Hello", "Adele", "", 128000, 0),
            ("Rock/04 - Queen - Bohemian.flac", "Bohemian", "Queen", "", 900000, 1),
            ("Other/05 - 无题.flac", "", "", "", 128000, 0),
        ]
        for i, (rel, title, artist, album, br, ll) in enumerate(spec, 1):
            name = rel.rsplit("/", 1)[-1]
            rows.append({
                "source_id": "s1", "path": f"{self.root}/{rel}", "rel_path": rel,
                "name": name, "parent": f"{self.root}/{rel.rsplit('/', 1)[0]}",
                "ext": ".flac", "size": 1000 * i, "mtime": 1.0, "title": title,
                "artist": artist, "album": album, "duration": 100.0 * i,
                "bitrate": br, "sample_rate": 44100, "channels": 2,
                "bits_per_sample": 16, "codec": "FLAC", "is_lossless": ll,
                "has_cover": 0, "quality": 50.0, "hash_quick": None,
                "hash_full": None, "hash_state": "none",
            })
        for r in rows:
            r["dup_key"] = M.dup_key(r)
            r["name_key"] = r["title"].lower()
        db().upsert_tracks([{k: r.get(k) for k in (
            "source_id", "path", "rel_path", "name", "parent", "ext", "size", "mtime",
            "title", "artist", "album", "duration", "bitrate", "sample_rate", "channels",
            "bits_per_sample", "codec", "is_lossless", "has_cover", "quality",
            "hash_quick", "hash_full", "hash_state", "dup_key", "name_key")} for r in rows])

    def _lib(self, **params):
        r = self.client.get("/api/library/s1", params=params)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def test_fuzzy_finds_song_by_title(self):
        d = self._lib(q="晴天", fuzzy=1)
        self.assertEqual(1, d["total"], d["tracks"])
        self.assertTrue(d["fuzzy"])

    def test_fuzzy_finds_song_by_artist(self):
        d = self._lib(q="周杰伦", fuzzy=1)
        self.assertEqual(2, d["total"])

    def test_fuzzy_tolerates_typo(self):
        """错别字：青花词 → 青花瓷。LIKE 是搜不到的，模糊必须能搜到。"""
        exact = self._lib(q="青花词")            # 不开 fuzzy
        self.assertEqual(0, exact["total"], "精确匹配不该命中错别字")
        d = self._lib(q="青花词", fuzzy=1)
        self.assertEqual(1, d["total"], "模糊匹配应命中错别字")

    def test_fuzzy_tolerates_abbreviation(self):
        """作者名照样能搜到。"""
        d = self._lib(q="adele", fuzzy=1)
        self.assertEqual(1, d["total"], [t["name"] for t in d["tracks"]])

    def test_fuzzy_does_not_match_scattered_letters(self):
        """拉丁查询不能因为字母零散出现就命中（'adele' 不该匹配 'Bohemian'）。"""
        d = self._lib(q="adele", fuzzy=1)
        names = [t["name"] for t in d["tracks"]]
        self.assertFalse([n for n in names if "Bohemian" in n], names)
        self.assertFalse([n for n in names if "晴天" in n], names)

    def test_fuzzy_respects_other_filters(self):
        """模糊 + 无损筛选要一起生效。"""
        d = self._lib(q="周杰伦", fuzzy=1, lossless=True)
        self.assertEqual(2, d["total"])
        # 数据里 Hello 是有损的，加了无损筛选就该消失
        d2 = self._lib(q="Hello", fuzzy=1, lossless=True)
        self.assertEqual(0, d2["total"], "Hello 是有损的，应被筛掉")
        d3 = self._lib(q="Hello", fuzzy=1, lossless=False)
        self.assertEqual(1, d3["total"], "只看有损时 Hello 应在")

    def test_fuzzy_respects_prefix(self):
        d = self._lib(q="周杰伦", fuzzy=1, prefix=f"{self.root}/Rock")
        self.assertEqual(0, d["total"], "Rock 目录里没有周杰伦")

    def test_fuzzy_aggregate_matches_hits(self):
        d = self._lib(q="周杰伦", fuzzy=1)
        self.assertEqual(d["total"], d["aggregate"]["count"])
        # 晴天(1000) + 青花瓷(2000) = 3000 字节
        self.assertEqual(3000, d["aggregate"]["bytes"])
        self.assertEqual(2, d["aggregate"]["lossless"])

    def test_fuzzy_pagination_is_consistent(self):
        """分页只影响返回条数，不影响总数与统计。"""
        p1 = self._lib(q="周杰伦", fuzzy=1, limit=1, offset=0)
        p2 = self._lib(q="周杰伦", fuzzy=1, limit=1, offset=1)
        self.assertEqual(1, p1["returned"])
        self.assertEqual(1, p2["returned"])
        self.assertNotEqual(p1["tracks"][0]["name"], p2["tracks"][0]["name"])
        self.assertEqual(p1["total"], p2["total"])
        self.assertEqual(p1["aggregate"]["bytes"], p2["aggregate"]["bytes"])

    def test_no_match_returns_empty(self):
        d = self._lib(q="完全不相干的词xyz", fuzzy=1)
        self.assertEqual(0, d["total"])
        self.assertEqual(0, d["aggregate"]["count"])


if __name__ == "__main__":
    unittest.main()
