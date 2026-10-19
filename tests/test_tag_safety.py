"""标签写入的安全性与格式覆盖（回归测试）。

每一条都是踩过的坑或做过实测的结论：

  1. **只改一个字段时其他字段不能丢** —— "批量给一张专辑补专辑名"是最常见
     用法，不能把标题/作者抹掉。
  2. **写完之后音频必须还能解码** —— 只看 mutagen 能不能解析是不够的，
     历史上 WAV 就被写坏过（ID3 头把 RIFF 结构顶掉）。
  3. **批量写同一张专辑时备份不能互相覆盖** —— 备份的全部意义就在于此。
  4. **`.wv` / `.tta` 必须可编辑** —— 它们能被扫描、能整理、能播放，
     标签编辑原来却直接报"暂不支持"，用户会在曲库里撞墙。
  5. **`.wma` / `.mka` / `.caf` 给清楚的提示** —— mutagen 没有合适的
     写入路径，宁可明确拒绝（实测 ASF 的标签写入会解不开流），
     但错误信息必须让人看懂。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FF_DIR = (r"D:\ffmpeg-8.0.1\ffmpeg-2026-01-07-git-af6a1dd0b2-essentials_build"
          r"\bin")
FFMPEG = os.path.join(FF_DIR, "ffmpeg.exe")
HAVE_FF = os.path.isfile(FFMPEG)

# 有写入路径的格式（kind 能识别）
EDITABLE = [("mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]),
            ("flac", ["-c:a", "flac"]),
            ("m4a", ["-c:a", "aac", "-b:a", "128k"]),
            ("ogg", ["-c:a", "libvorbis", "-q:a", "3"]),
            ("opus", ["-c:a", "libopus", "-b:a", "96k"]),
            ("wav", ["-c:a", "pcm_s16le"]),
            ("aiff", ["-c:a", "pcm_s16be"]),
            ("wv", ["-c:a", "wavpack"]),
            ("tta", ["-c:a", "tta"])]

# mutagen 没有可靠写入路径的格式：必须明确拒绝
NOT_EDITABLE = [("wma", ["-c:a", "wmav2", "-b:a", "128k"]),
                ("mka", ["-c:a", "libvorbis", "-q:a", "3"]),
                ("caf", ["-c:a", "pcm_s16le"])]


def _decode_ok(p: str) -> bool:
    r = subprocess.run([FFMPEG, "-v", "error", "-i", p, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=180)
    return r.returncode == 0


@unittest.skipUnless(HAVE_FF, "需要 ffmpeg 造测试音频")
class TagWriteSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="msh-tags-", ignore_cleanup_errors=True)
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        from app.core.config import (AppConfig, Settings, SourceConfig,
                                     load_config, save_config)
        save_config(AppConfig(sources=[SourceConfig(
            id="s1", name="t", type="local", root=cls._tmp.name)],
            settings=Settings(station_dir=os.path.join(cls._tmp.name, "st"),
                              staging_dir=os.path.join(cls._tmp.name, "up"))))
        load_config(force=True)
        cls.src = os.path.join(cls._tmp.name, "_src.wav")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=3", cls.src],
                       check=True, timeout=180)
        cls.jpg = os.path.join(cls._tmp.name, "_c.jpg")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "color=c=red:s=120x120:d=1", "-frames:v", "1",
                        cls.jpg], check=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _mk(self, ext, enc, name=None):
        p = os.path.join(self._tmp.name, name or f"song.{ext}")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", self.src, *enc, p],
                       check=True, capture_output=True, timeout=180)
        return p

    def test_partial_write_keeps_other_fields(self):
        """★ 只改专辑 → 标题/作者/流派必须原样保留。"""
        from app.core import tags as T
        for ext, enc in EDITABLE:
            with self.subTest(fmt=ext):
                p = self._mk(ext, enc, f"keep.{ext}")
                T.write_tags(p, {"title": "OrigT", "artist": "OrigA",
                                 "genre": "OrigG", "album": "OrigAlb"})
                before = T.read_tags(p)
                T.write_tags(p, {"album": "NewAlb"})
                after = T.read_tags(p)
                self.assertEqual("NewAlb", after.get("album"), ext)
                for k in ("title", "artist", "genre"):
                    self.assertEqual(before.get(k) or "", after.get(k) or "",
                                     f"{ext} 的 {k} 被抹掉了")

    def test_audio_still_decodes_after_write(self):
        """★ 写完音频必须还能解码（不是只看 mutagen 能否解析）。"""
        from app.core import tags as T
        for ext, enc in EDITABLE:
            with self.subTest(fmt=ext):
                p = self._mk(ext, enc, f"dec.{ext}")
                T.write_tags(p, {"title": "T1", "artist": "A1", "album": "AL1"},
                             cover_data=open(self.jpg, "rb").read(),
                             cover_mime="image/jpeg")
                self.assertTrue(_decode_ok(p), f"{ext} 写完解不开")
                a = T.read_tags(p)
                self.assertEqual("T1", a.get("title"), ext)
                self.assertFalse(a.get("error"), f"{ext}: {a.get('error')}")

    def test_backups_do_not_collide_in_batch(self):
        """★ 批量写同一张专辑：8 个文件要有 8 份备份，不能互相截断。"""
        from app.core import tags as T
        base = len(T.list_backups(limit=1000))
        for i in range(8):
            p = self._mk("mp3", ["-c:a", "libmp3lame", "-b:a", "128k"],
                         f"Alb{i}/t.mp3")
            T.write_tags(p, {"album": "BATCH"}, make_backup=True)
        after = len(T.list_backups(limit=1000))
        self.assertGreaterEqual(after - base, 8,
                                f"批量写 8 个只留了 {after - base} 份备份")

    def test_apev2_formats_are_editable(self):
        """★ `.wv` / `.tta` 必须能改标签（它们能被扫描/整理/播放）。"""
        from app.core import tags as T
        for ext in ("wv", "tta"):
            with self.subTest(fmt=ext):
                self.assertTrue(T.can_edit(f"x.{ext}"),
                                f"{ext} 明明能被扫描却不让改标签")

    def test_apev2_cover_roundtrip(self):
        """APEv2 封面要能写进去也能读回来。

        踩过：`g[key]` 返回的是 `APEBinaryValue` **包装对象**而不是裸 bytes，
        直接 `isinstance(raw, bytes)` 永远为假 —— 封面写在磁盘上、读回来
        却显示"无"。
        """
        from app.core import tags as T
        p = self._mk("wv", ["-c:a", "wavpack"], "cov.wv")
        T.write_tags(p, {"title": "T", "artist": "A", "album": "AL"},
                     cover_data=open(self.jpg, "rb").read(),
                     cover_mime="image/jpeg")
        a = T.read_tags(p)
        self.assertTrue(a.get("cover"), "封面写进去了但读不回来")
        self.assertEqual(1, a.get("cover_count"))
        # 也能删掉
        T.write_tags(p, {}, remove_cover=True)
        self.assertFalse(T.read_tags(p).get("cover"), "封面删不掉")

    def test_unsupported_formats_are_rejected_clearly(self):
        """不支持的格式要明确拒绝，且错误信息能让人看懂。"""
        from app.core import tags as T
        for ext, enc in NOT_EDITABLE:
            with self.subTest(fmt=ext):
                p = self._mk(ext, enc, f"no.{ext}")
                self.assertFalse(T.can_edit(p), f"{ext} 不该被判为可编辑")
                with self.assertRaises(Exception) as cm:
                    T.write_tags(p, {"title": "X"})
                self.assertIn("格式", str(cm.exception))
                # 拒绝之后文件必须原样可解码
                self.assertTrue(_decode_ok(p), f"{ext} 被拒绝时却弄坏了文件")

    def test_missing_file_and_bad_format_raise(self):
        from app.core import tags as T
        with self.assertRaises(FileNotFoundError):
            T.write_tags(os.path.join(self._tmp.name, "nope.mp3"),
                         {"album": "x"})
        bad = os.path.join(self._tmp.name, "a.xyz")
        with open(bad, "wb") as fh:
            fh.write(b"not audio")
        with self.assertRaises(Exception):
            T.write_tags(bad, {"album": "x"})

    def test_delete_empty_guard(self):
        """写完标题和作者都空 → 放弃（防止把文件写成没有身份的垃圾）。"""
        from app.core import tags as T
        p = self._mk("mp3", ["-c:a", "libmp3lame", "-b:a", "128k"], "empty.mp3")
        T.write_tags(p, {"title": "T", "artist": "A"})
        with self.assertRaises(Exception):
            T.write_tags(p, {"title": "", "artist": ""}, delete_empty=True)
        # 原文件必须没被动过
        a = T.read_tags(p)
        self.assertEqual("T", a.get("title"))
        self.assertTrue(_decode_ok(p))


if __name__ == "__main__":
    unittest.main()
