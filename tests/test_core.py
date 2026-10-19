"""核心算法测试：路径归一化、歌曲键、匹配打分、查重分组、计划生成、执行器。

运行：
    uv run --with mutagen --with Pillow python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import audio as A
from app.core import matching as M
from app.core import paths as P
from app.core import plan as PLAN
from app.core.config import AppConfig, Settings, SourceConfig
from app.core.db import Database
from app.core.ops import Executor
from app.sources.local import LocalSource
from app.sources import manager


def track(name, title="", artist="", album="", duration=None, bitrate=320000,
          size=8_000_000, lossless=False, codec="MP3", source_id="a", path=None,
          mtime=0.0):
    d = {
        "name": name, "title": title, "artist": artist, "album": album,
        "duration": duration, "bitrate": bitrate, "size": size,
        "is_lossless": 1 if lossless else 0, "codec": codec,
        "source_id": source_id, "path": path or f"/{source_id}/{name}",
        "mtime": mtime,
    }
    d["name_key"] = P.name_key(name)
    d["dup_key"] = M.dup_key(d)
    d["quality"] = A.quality_score(d)
    return d


class TestPaths(unittest.TestCase):
    def test_norm_windows_and_posix(self):
        self.assertEqual(P.norm("D:\\Music\\A\\b.mp3"), "D:/Music/A/b.mp3")
        self.assertEqual(P.norm("/mnt/music//a/"), "/mnt/music/a")
        self.assertEqual(P.join("D:/Music", "Rock/a.mp3"), "D:/Music/Rock/a.mp3")
        self.assertEqual(P.parent("/mnt/music/a/b.mp3"), "/mnt/music/a")
        self.assertEqual(P.norm("\\\\NAS\\share\\x"), "//NAS/share/x")  # UNC 前缀保留

    def test_name_key_strips_noise(self):
        cases = [
            ("01 - 周杰伦 - 晴天 (320k).mp3", "周杰伦 晴天"),
            ("周杰伦 - 晴天 [FLAC].flac", "周杰伦 晴天"),
            ("05. Artist - Song (HQ).mp3", "artist song"),
            ("Song_Name_320kbps.mp3", "song name"),
        ]
        for raw, want in cases:
            self.assertEqual(P.name_key(raw), want, raw)

    def test_similarity(self):
        self.assertGreater(P.similarity("周杰伦 晴天", "周杰伦 - 晴天"), 0.85)
        self.assertLess(P.similarity("Hello", "Goodbye"), 0.5)

    def test_audio_ext_detect(self):
        self.assertTrue(P.is_audio("a.FLAC"))
        self.assertFalse(P.is_audio("cover.jpg"))
        self.assertTrue(P.is_cover("folder.JPG"))
        self.assertFalse(P.is_cover("myfolder.jpg"))


class TestMatching(unittest.TestCase):
    def test_pair_by_filename_across_metadata(self):
        """核心场景：文件名相同，元数据不同（两边标签不一致）。"""
        left = [
            track("周杰伦 - 晴天.mp3", title="晴天", artist="周杰伦", album="叶惠美",
                  duration=269.0, source_id="win"),
            track("Adele - Hello.mp3", title="Hello", artist="Adele",
                  duration=295.0, source_id="win"),
        ]
        right = [
            # 右侧标签不同命名习惯，但文件名一致
            track("周杰伦 - 晴天.mp3", title="Qing Tian", artist="Jay Chou", album="",
                  duration=269.4, source_id="linux"),
            track("Adele - Hello.mp3", title="hello", artist="ADELE", album="25",
                  duration=295.6, source_id="linux"),
        ]
        res = M.pair_sources(left, right)
        self.assertEqual(res["stats"]["matched"], 2)
        self.assertEqual(res["stats"]["only_left"], 0)
        for m in res["matches"]:
            self.assertGreaterEqual(m.score, 0.8, m.reasons)

    def test_no_false_match_on_different_duration(self):
        """同名但是不同版本（现场 vs 录音室，时长差很多）不应自动配成一对。"""
        left = [track("Song.mp3", title="Song", duration=200.0, source_id="a")]
        right = [track("Song.mp3", title="Song", duration=380.0, source_id="b")]
        res = M.pair_sources(left, right)
        self.assertEqual(res["stats"]["matched"], 0)
        self.assertEqual(res["stats"]["only_left"], 1)
        self.assertEqual(res["stats"]["only_right"], 1)

    def test_only_left_detected(self):
        left = [track("x1.mp3", duration=100), track("x2.mp3", duration=120)]
        right = [track("x1.mp3", duration=100)]
        res = M.pair_sources(left, right)
        self.assertEqual(res["stats"]["matched"], 1)
        self.assertEqual(res["stats"]["only_left"], 1)
        self.assertEqual(res["only_left"][0]["name"], "x2.mp3")

    def test_duplicate_grouping_same_library(self):
        rows = [
            track("Song.mp3", title="Song", artist="A", duration=200, bitrate=128000, size=3_000_000),
            track("Song (1).mp3", title="Song", artist="A", duration=200.2, bitrate=320000, size=8_000_000),
            track("Song.flac", title="Song", artist="A", duration=199.8, lossless=True,
                  codec="FLAC", bitrate=1000000, size=30_000_000),
            track("Other.mp3", title="Other", artist="B", duration=150),
        ]
        groups = M.group_duplicates(rows)
        self.assertEqual(len(groups), 1, groups)
        g = M.group_to_dict(groups[0])
        self.assertEqual(g["count"], 3)
        # 建议保留的应该是无损那份
        self.assertTrue(g["suggested_keep"].endswith("Song.flac"))
        self.assertGreater(g["wasted_bytes"], 0)

    def test_quality_ranking(self):
        flac = track("a.flac", lossless=True, codec="FLAC", bitrate=1000000, size=30_000_000)
        mp3_320 = track("a.mp3", bitrate=320000, size=8_000_000)
        mp3_128 = track("a_128.mp3", bitrate=128000, size=3_000_000)
        self.assertGreater(flac["quality"], mp3_320["quality"])
        self.assertGreater(mp3_320["quality"], mp3_128["quality"])

    def test_naming_conflicts(self):
        left = [track("01 - Song.mp3", title="Song", duration=200)]
        right = [track("Song.mp3", title="Song", duration=200)]
        conf = M.naming_conflicts(left, right)
        self.assertEqual(len(conf), 1)


class TestPlan(unittest.TestCase):
    def setUp(self):
        cfg = AppConfig(settings=Settings())
        self.left = {
            "id": "win", "label": "Windows",
            "tracks": [
                track("keep.mp3", duration=100, source_id="win", path="/win/keep.mp3"),
                track("onlywin.mp3", duration=120, source_id="win", path="/win/onlywin.mp3"),
            ],
        }
        self.right = {
            "id": "linux", "label": "Linux",
            "tracks": [
                track("keep.mp3", duration=100, source_id="linux", path="/linux/keep.mp3"),
                track("onlylinux.mp3", duration=130, source_id="linux", path="/linux/onlylinux.mp3"),
            ],
        }

    def test_missing_copy_plan(self):
        out = PLAN.build_plan(self.left, self.right, options={"copy_missing": True})
        ops = {(s["op"], P.basename(s["dst_path"])) for s in out["steps"]}
        self.assertIn(("copy", "onlywin.mp3"), ops)
        self.assertEqual(out["stats"]["only_left"], 1)
        self.assertEqual(out["stats"]["only_right"], 1)
        self.assertEqual(out["summary"]["by_op"]["copy"]["count"], 1)

    def test_mirror_delete_is_high_risk(self):
        out = PLAN.build_plan(self.left, self.right,
                              options={"copy_missing": False, "mirror_delete": True})
        trashes = [s for s in out["steps"] if s["op"] == "trash"]
        self.assertEqual(len(trashes), 1)
        self.assertEqual(trashes[0]["risk"], "high")
        self.assertTrue(out["warnings"])

    def test_duplicate_steps_respect_keep(self):
        groups = [{
            "key": "k", "title": "Song", "count": 2, "suggested_keep": "/a/best.flac",
            "best_quality": 130,
            "items": [
                {"path": "/a/best.flac", "source_id": "linux", "size": 30, "quality": 130},
                {"path": "/a/worse.mp3", "source_id": "linux", "size": 8, "quality": 32},
            ],
        }]
        steps = PLAN.duplicate_steps(groups)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].op, "trash")
        self.assertEqual(steps[0].src_path, "/a/worse.mp3")
        self.assertEqual(steps[0].risk, "high")
        # 用户改主意，保留差的那份
        steps2 = PLAN.duplicate_steps(groups, keep_map={"k": "/a/worse.mp3"})
        self.assertEqual(steps2[0].src_path, "/a/best.flac")


class TestScannerAndExecutor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msh-test-"))
        self.db = Database(self.tmp / "index.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, root: Path, name: str, size: int = 4096, fill: bytes = b"\x00"):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(fill * (size // max(1, len(fill))) + b"X" * (size % max(1, len(fill))))
        return p

    def test_scan_and_index_wav(self):
        """用真实 WAV 头部验证元数据解析链路（时长/采样率/声道）。"""
        import struct
        import wave

        root = self.tmp / "win-music"
        (root / "Album").mkdir(parents=True)
        wav_path = root / "Album" / "01 - Test Artist - Test Song.wav"
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x01" * 44100)  # 0.5 秒
        self._mk(root, "Album/cover.jpg", 2048, b"\xff\xd8\xff\xe0")
        self._mk(root, "Album/broken.mp3", 512, b"\x11")

        cfg = SourceConfig(id="win", name="Windows", type="local", root=str(root))
        src = LocalSource(cfg)
        from app.core import scanner as S

        res = S.scan_source("win", src, self.db, workers=2)
        self.assertEqual(res.seen, 2)
        rows = self.db.tracks("win")
        names = {r["name"] for r in rows}
        self.assertIn("01 - Test Artist - Test Song.wav", names)
        self.assertIn("broken.mp3", names)

        wav_row = self.db.track("win", P.norm(str(wav_path)))
        self.assertIsNotNone(wav_row)
        self.assertAlmostEqual(wav_row["duration"], 0.5, places=1)
        self.assertEqual(wav_row["sample_rate"], 44100)
        self.assertEqual(wav_row["channels"], 2)
        self.assertEqual(wav_row["artist"], "Test Artist")
        self.assertEqual(wav_row["title"], "Test Song")
        self.assertTrue(wav_row["is_lossless"])
        # 同目录 cover.jpg 被识别为封面
        self.assertEqual(wav_row["has_cover"], 1)
        broken = self.db.track("win", P.norm(str(root / "Album" / "broken.mp3")))
        self.assertIn(broken["meta_status"], ("error", "partial", "ok"))

        # 第二次扫描应该全部命中缓存
        res2 = S.scan_source("win", src, self.db, workers=2)
        self.assertGreaterEqual(res2.cached, 2)

        # 删除一个文件后再扫描，索引应清理
        os.remove(wav_path)
        res3 = S.scan_source("win", src, self.db, workers=2)
        self.assertEqual(res3.removed, 1)

    def test_executor_copy_verify_skip_overwrite(self):
        cfg_a = SourceConfig(id="a", name="A", type="local", root=str(self.tmp / "A"))
        cfg_b = SourceConfig(id="b", name="B", type="local", root=str(self.tmp / "B"))
        src_path = self._mk(self.tmp / "A", "Album/song.mp3", 8192, b"\xaa")
        sa, sb = LocalSource(cfg_a), LocalSource(cfg_b)
        ex = Executor({"a": sa, "b": sb}, self.db)

        step = {"id": "s1", "op": "copy", "src": "a", "src_path": P.norm(str(src_path)),
                "dst": "b", "dst_path": "Album/song.mp3", "size": 8192}
        dry = ex.run([step], execute=False)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["results"][0]["state"], "planned")
        self.assertFalse((self.tmp / "B" / "Album" / "song.mp3").exists())

        out = ex.run([step], execute=True, verify=True)
        self.assertEqual(out["results"][0]["state"], "done")
        self.assertEqual((self.tmp / "B" / "Album" / "song.mp3").read_bytes(), src_path.read_bytes())
        self.assertFalse((self.tmp / "B" / "Album" / "song.mp3.mshpart").exists())

        # skip 策略：目标已存在 → 状态是 `conflict`（**不是** error，也不是
        # 笼统的 skipped）。执行汇总必须能区分"冲突"和"出错" ——
        # 冲突是预期内的、不该催用户重试；出错才需要重试。
        again = ex.run([step], execute=True, policy="skip")
        self.assertEqual(again["results"][0]["state"], "conflict")
        self.assertEqual(again["results"][0].get("error_kind"), "conflict")
        # 冲突不该进"可重试"清单（重试也没用）
        self.assertNotIn("s1", (again["summary"].get("retryable_steps") or []))
        # rename 策略
        renamed = ex.run([step], execute=True, policy="rename")
        self.assertEqual(renamed["results"][0]["state"], "done")
        self.assertTrue((self.tmp / "B" / "Album" / "song (2).mp3").exists())

    def test_executor_trash_moves_to_recycle_bin(self):
        cfg_a = SourceConfig(id="a", name="A", type="local", root=str(self.tmp / "A"))
        victim = self._mk(self.tmp / "A", "junk.mp3", 1024, b"\x01")
        sa = LocalSource(cfg_a)
        ex = Executor({"a": sa}, self.db)
        step = {"id": "t1", "op": "trash", "src": "a", "src_path": P.norm(str(victim)), "size": 1024}
        out = ex.run([step], execute=True)
        self.assertEqual(out["results"][0]["state"], "done")
        self.assertFalse(victim.exists())
        self.assertTrue(Path(out["results"][0]["trash"]).exists())

    def test_readonly_source_blocks_write(self):
        cfg_b = SourceConfig(id="b", name="B", type="local", root=str(self.tmp / "B"), read_only=True)
        cfg_a = SourceConfig(id="a", name="A", type="local", root=str(self.tmp / "A"))
        self._mk(self.tmp / "A", "s.mp3", 512, b"\x02")
        sb, sa = LocalSource(cfg_b), LocalSource(cfg_a)
        ex = Executor({"a": sa, "b": sb}, self.db)
        step = {"id": "s", "op": "copy", "src": "a", "src_path": P.norm(str(self.tmp / "A" / "s.mp3")),
                "dst": "b", "dst_path": "s.mp3"}
        dry = ex.run([step], execute=False)
        self.assertEqual(dry["results"][0]["state"], "blocked")
        run = ex.run([step], execute=True)
        self.assertEqual(run["results"][0]["state"], "blocked")


class TestHash(unittest.TestCase):
    def test_quick_hash_discrimination(self):
        from app.core.scanner import quick_hash_bytes

        a = b"A" * 1_000_000
        b = b"A" * 999_999 + b"B"
        self.assertNotEqual(quick_hash_bytes(a), quick_hash_bytes(b))
        self.assertEqual(quick_hash_bytes(a), quick_hash_bytes(b"A" * 1_000_000))
        self.assertNotEqual(quick_hash_bytes(b"A" * 100), quick_hash_bytes(b"A" * 200))


if __name__ == "__main__":
    unittest.main(verbosity=2)
