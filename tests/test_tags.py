"""元数据（标签）读写的回归测试。

这次不碰用户的真实音乐库 —— 用 **stdlib 现造的 WAV 文件**当素材：
wave 模块能写出合法的容器，mutagen 的 ID3 分支正好支持 .wav，
这样测试完全自给自足，也不依赖任何第三方素材。

锁住的坑：
* 原子写用的是 `xxx.flac.mshtag` 这种临时名，光看扩展名认不出格式，
  必须把 kind 显式传下去（否则报"不支持改标签: .mshtag"）；
* 批量只改**勾选的字段**，没勾的一律不能动
  （想给 20 首歌补个专辑，不能顺手把标题抹掉）；
* 写坏了要能还原 —— 所以每次写入前都留一份 JSON 备份。
"""
from __future__ import annotations

import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path


def _make_wav(path: Path, seconds: float = 0.05, rate: int = 8000) -> None:
    """造一个能被 mutagen 认出来的最小 WAV。"""
    n = int(rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<" + "h" * n, *([0] * n)))


class TagsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-tags-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        from app.core import tags
        cls.T = tags
        cls.dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("MSH_DATA_DIR", None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def setUp(self):
        self.f = self.dir / f"t{id(self)}.wav"
        _make_wav(self.f)

    def test_can_edit_by_extension(self):
        self.assertTrue(self.T.can_edit("a.flac"))
        self.assertTrue(self.T.can_edit("a.mp3"))
        self.assertTrue(self.T.can_edit("a.m4a"))
        self.assertTrue(self.T.can_edit("a.ogg"))
        self.assertTrue(self.T.can_edit("a.opus"))
        self.assertTrue(self.T.can_edit("a.wav"))
        self.assertTrue(self.T.can_edit("a.aiff"))
        self.assertFalse(self.T.can_edit("a.txt"))
        self.assertFalse(self.T.can_edit("a.jpg"))

    def test_temp_name_still_resolves_kind(self):
        """原子写的临时名 `x.flac.mshtag` 也要能认出格式。"""
        self.assertEqual("flac", self.T._kind("/m/a.flac.mshtag"))
        self.assertEqual("id3", self.T._kind("/m/a.mp3.mshtag"))
        self.assertEqual("mp4", self.T._kind("/m/a.m4a.mshtag"))

    def test_wav_uses_its_own_container_class(self):
        """WAV/AIFF 必须走 WAVE/AIFF 分支，不能当普通 ID3 处理。

        直接对 .wav 用 `ID3(path).save(path)` 会把 RIFF 头覆盖成 ID3 头，
        文件当场损坏（实测踩过：`wave.Error: file does not start with RIFF id`）。
        """
        self.assertEqual("wav", self.T._kind("/m/a.wav"))
        self.assertEqual("aiff", self.T._kind("/m/a.aiff"))
        _make_wav(self.f)
        self.assertEqual(b"RIFF", self.f.read_bytes()[:4])
        self.T.write_tags(str(self.f), {"title": "波形测试"})
        self.assertEqual(b"RIFF", self.f.read_bytes()[:4], "RIFF 头必须还在")

    def test_write_then_read_roundtrip(self):
        res = self.T.write_tags(str(self.f), {
            "title": "新标题", "artist": "新作者", "album": "新专辑",
            "year": "2024", "genre": "Electronic", "track": "3",
        })
        self.assertTrue(res["ok"])
        self.assertTrue(res["backup"], "写入必须留备份")
        got = self.T.read_tags(str(self.f))
        self.assertEqual("新标题", got["title"])
        self.assertEqual("新作者", got["artist"])
        self.assertEqual("新专辑", got["album"])
        self.assertEqual("2024", got["year"])
        self.assertEqual("Electronic", got["genre"])
        self.assertEqual("3", got["track"])

    def test_partial_write_does_not_touch_other_fields(self):
        """只改一个字段，别的字段必须原样 —— 批量编辑最容易在这里出事。"""
        self.T.write_tags(str(self.f), {"title": "T1", "artist": "A1",
                                        "album": "AL1", "genre": "G1"})
        before = self.T.read_tags(str(self.f))
        self.T.write_tags(str(self.f), {"album": "AL2"})
        after = self.T.read_tags(str(self.f))
        self.assertEqual("AL2", after["album"])
        for k in ("title", "artist", "genre"):
            self.assertEqual(before[k], after[k], f"{k} 不该被改动")

    def test_empty_value_clears_field(self):
        self.T.write_tags(str(self.f), {"genre": "Rock"})
        self.assertEqual("Rock", self.T.read_tags(str(self.f))["genre"])
        self.T.write_tags(str(self.f), {"genre": ""})
        self.assertEqual("", self.T.read_tags(str(self.f))["genre"])

    def test_file_still_valid_after_write(self):
        """写完还得是个能正常解析的音频文件（原子写 + 回读校验的意义）。"""
        self.T.write_tags(str(self.f), {"title": "校验"})
        with wave.open(str(self.f), "rb") as w:
            self.assertEqual(1, w.getnchannels())
        self.assertFalse(list(self.dir.glob("*.mshtag")), "临时文件要清理干净")

    def test_backup_and_restore(self):
        self.T.write_tags(str(self.f), {"title": "原始标题", "artist": "原始作者"})
        origin = self.T.read_tags(str(self.f))
        self.T.write_tags(str(self.f), {"title": "改坏了", "album": "乱写的"})
        self.assertEqual("改坏了", self.T.read_tags(str(self.f))["title"])
        mine = [b for b in self.T.list_backups() if b.get("path") == str(self.f)]
        self.assertTrue(mine, "应该能找到这个文件的备份")
        self.T.restore_backup(mine[-1]["name"])
        back = self.T.read_tags(str(self.f))
        self.assertEqual(origin["title"], back["title"])
        self.assertEqual(origin["artist"], back["artist"])
        self.assertEqual(origin["album"], back["album"])

    def test_backup_name_traversal_is_rejected(self):
        for bad in ("../x.json", "a/b.json", "..\\x.json"):
            with self.assertRaises(Exception):
                self.T.restore_backup(bad)

    def test_unsupported_format_raises(self):
        p = self.dir / "x.txt"
        p.write_text("not audio", encoding="utf-8")
        self.assertFalse(self.T.can_edit(str(p)))
        with self.assertRaises(Exception):
            self.T.write_tags(str(p), {"title": "x"})

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.T.write_tags(str(self.dir / "nope.flac"), {"title": "x"})


class TagFieldTest(unittest.TestCase):
    """字段与文案的契约（界面直接拿它渲染）。"""

    def test_fields_and_labels_cover_each_other(self):
        from app.core import tags
        for k in tags.FIELDS:
            self.assertIn(k, tags.FIELD_LABELS, f"{k} 缺中文标签")
        self.assertEqual(set(tags.FIELDS), set(tags.FIELD_LABELS))

    def test_no_duplicate_field_names(self):
        from app.core import tags
        self.assertEqual(len(tags.FIELDS), len(set(tags.FIELDS)))


if __name__ == "__main__":
    unittest.main()
