"""查重分组回归测试：不允许把不同的歌并成一组。

背景：`group_duplicates` 在按 dup_key 精确分组之后，还会对"键不同但看起来像"
的组做一次并查集合并。这一步原先是纯分数门禁，而分数会被"体积相同"抬到阈值：
`size_similarity` 同体积给 1.0，两侧标签都缺时 `meta_similarity` 给中性 0.5，
于是 "Song 01" 和 "Song 02" 这种只差一两个字符的不同歌也能拿到 0.90 分被并到
一起——整张专辑并成一个巨型组，界面上看起来就是"查重只有几组"。
"""
from __future__ import annotations

import unittest

from app.core import matching as M
from app.core import paths as P


def row(i, title, artist, dur, name=None, size=5_000_000, hash_=None):
    name = name or f"{title}.mp3"
    return {
        "id": i, "source_id": "s1", "path": f"D:/Music/{name}", "name": name,
        "parent": "D:/Music", "rel_path": name, "ext": ".mp3",
        "size": size, "mtime": 1.0,
        "title": title, "artist": artist, "album": "Album",
        "track_no": str(i), "duration": dur, "bitrate": 320000,
        "sample_rate": 44100, "channels": 2, "bits_per_sample": 16,
        "codec": "MP3", "is_lossless": 0, "has_cover": 0, "quality": 40.0,
        "name_key": P.name_key(name),
        "dup_key": M.dup_key({"title": title, "artist": artist, "duration": dur}),
        "hash_quick": hash_ or f"h{i:040d}", "hash_full": None, "hash_state": "quick",
    }


class DuplicateGroupingTest(unittest.TestCase):

    def test_many_distinct_songs_stay_separate(self):
        """40 首不同的歌（体积相同、时长相邻）必须得到 40 组，而不是被并成一组。"""
        rows = []
        n = 0
        for i in range(1, 41):
            for _c in range(2):
                n += 1
                rows.append(row(n, f"Song {i:02d}", f"Artist {i:02d}", 100.0 + i))
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(40, len(groups), f"应为 40 组，实际 {len(groups)}")
        for g in groups:
            self.assertEqual(2, len(g))

    def test_same_song_same_bucket_merges(self):
        """同一首歌、时长落在同一个桶、只是文件名多了音轨号 → 应当合成一组。"""
        rows = [
            row(1, "Get Lucky", "Daft Punk", 7.0, name="Get Lucky.mp3"),
            row(2, "Get Lucky", "Daft Punk", 7.0, name="03 - Daft Punk - Get Lucky.mp3"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(1, len(groups))
        self.assertEqual(2, len(groups[0]))

    def test_same_song_different_bucket_merges_by_tokens(self):
        """同一首歌、时长跨桶、文件名只是词序不同 → 仍应合成一组。

        difflib 对这种词序差异只给 0.76，靠普通的相似度阈值拦不住也放不过，
        所以走的是"词元集合一致"这条证据。
        """
        rows = [
            row(1, "", "", 7.0, name="Daft Punk - Get Lucky.mp3"),
            row(2, "", "", 9.5, name="Get Lucky - Daft Punk.mp3"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(1, len(groups), "同一首歌换个词序不应被拆开")

    def test_no_duration_files_do_not_crash(self):
        """时长缺失（损坏文件）时不能报错，也不能并成一组。"""
        rows = [
            row(1, "", "", None, name="aaa.mp3"),
            row(2, "", "", None, name="bbb.mp3"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(0, len(groups))

    def test_different_versions_are_not_merged(self):
        """原曲 vs remix：时长不同、文件名词元也不同 → 不能并成一组。"""
        rows = [
            row(1, "Get Lucky", "Daft Punk", 7.0),
            row(2, "Get Lucky Remix", "Daft Punk", 11.0),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(0, len(groups), "不同版本不应被并成一组")

    def test_artist_conflict_blocks_merge(self):
        """标题相同、时长相近，但艺术家明显不同 → 不能并成一组。"""
        rows = [
            row(1, "Exclusive Track", "Windows Only", 200.0),
            row(2, "Exclusive Track", "Linux Only", 200.0, name="Linux Exclusive Track.mp3"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(0, len(groups))

    def test_numbered_album_tracks_stay_separate(self):
        """同一专辑里连续编号的曲目不能被并到一起（这是最容易踩的坑）。"""
        rows = []
        n = 0
        for i in range(1, 13):
            for _c in range(2):
                n += 1
                rows.append(row(n, f"Track {i:02d}", "Various", 180.0,
                                 name=f"{i:02d} - Track {i:02d}.mp3", size=8_000_000))
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(12, len(groups), f"应为 12 组，实际 {len(groups)}")


class VersionSeparationTest(unittest.TestCase):
    """伴奏 / 重制版 / 现场版 / 翻唱… 是**不同的录音**，不能和原曲并成一组。

    背景：归一化会故意把"伴奏""remaster"这些词当噪声删掉（跨库同名匹配需要），
    于是原曲和伴奏的归一化文件名一模一样、时长也相同，分数直接顶到 1.0。
    因此版本标记必须单独参与分组键与配对门禁。
    """

    def test_version_tags_extraction(self):
        cases = {
            "晴天.wav": "",
            "晴天（伴奏）.wav": "instrumental",
            "晴天 (Karaoke).mp3": "instrumental",
            "Time (Remastered).flac": "remaster",
            "Get Lucky (Remix).mp3": "remix",
            "Hello (Live).mp3": "live",
            "Hello (Acoustic).mp3": "acoustic",
            "Hello - 翻唱.mp3": "cover",
            "晴天 - 国语版.mp3": "language|v:国语|version",
            # 音质/格式标记不算版本，否则同一份文件会被无谓拆开
            "01 - 周杰伦 - 晴天 (320k).mp3": "",
            "01 - 周杰伦 - 晴天 [FLAC].flac": "",
            "Daft Punk - Get Lucky (低音质).wav": "",
        }
        for name, want in cases.items():
            self.assertEqual(want, P.version_tags(name), f"版本标记错误: {name}")

    def test_original_and_instrumental_are_separate_groups(self):
        rows = [
            row(1, "晴天", "周杰伦", 240.0, name="晴天.flac", hash_="h1"),
            row(2, "晴天", "周杰伦", 240.0, name="晴天 (伴奏).flac", hash_="h2"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(0, len(groups), "原曲与伴奏不是重复，不应成组")

    def test_original_and_remaster_are_separate_groups(self):
        rows = [
            row(1, "Camping！", "Shiroi-Ice", 200.0, name="Camping！.flac", hash_="h1"),
            row(2, "Camping！", "Shiroi-Ice", 200.0,
                name="Camping！(Remaster).flac", hash_="h2"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(0, len(groups))

    def test_same_version_still_groups(self):
        """同一版本的两份（同名不同副本）必须照常成组。"""
        rows = [
            row(1, "晴天", "周杰伦", 240.0, name="晴天 (伴奏).flac", hash_="h1"),
            row(2, "晴天", "周杰伦", 240.0, name="01 - 晴天（伴奏）.flac", hash_="h1"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(1, len(groups))
        self.assertEqual(2, len(groups[0]))

    def test_cross_library_versions_do_not_pair(self):
        """跨库对比也不能把原曲和伴奏配成一对。"""
        left = [row(1, "晴天", "周杰伦", 240.0, name="晴天.flac")]
        right = [row(2, "晴天", "周杰伦", 240.0, name="晴天（伴奏）.flac")]
        r = M.pair_sources(left, right)
        self.assertEqual(0, r["stats"]["matched"])

    def test_cross_library_trackno_difference_still_pairs(self):
        """只是多了音轨号前缀的同一份文件，仍要能配上。"""
        left = [row(1, "晴天", "周杰伦", 240.0, name="01 - 周杰伦 - 晴天.flac")]
        right = [row(2, "晴天", "周杰伦", 240.0, name="周杰伦 - 晴天.flac")]
        r = M.pair_sources(left, right)
        self.assertEqual(1, r["stats"]["matched"])

    def test_word_order_variant_pairs(self):
        """词序不同的同一首歌应能配上，但带 Remix 的不能。"""
        left = [row(1, "", "", 240.0, name="Daft Punk - Get Lucky.flac")]
        right = [row(2, "", "", 240.0, name="Get Lucky - Daft Punk.flac")]
        self.assertEqual(1, M.pair_sources(left, right)["stats"]["matched"])
        right2 = [row(3, "", "", 240.0, name="Get Lucky - Daft Punk Remix.flac")]
        self.assertEqual(0, M.pair_sources(left, right2)["stats"]["matched"])

    def test_dup_key_carries_version(self):
        """版本标记必须体现在分组键上，否则增量扫描会用旧键把版本混起来。"""
        a = row(1, "晴天", "周杰伦", 240.0, name="晴天.flac")
        b = row(2, "晴天", "周杰伦", 240.0, name="晴天（伴奏）.flac")
        self.assertNotEqual(M.dup_key(a), M.dup_key(b))

    def test_group_dict_exposes_base_key(self):
        """界面"不区分版本"视图靠 base_key 把各版本重新并起来。"""
        rows = [
            row(1, "晴天", "周杰伦", 240.0, name="晴天.flac", hash_="h1"),
            row(2, "晴天", "周杰伦", 240.0, name="晴天2.flac", hash_="h1"),
            row(3, "晴天", "周杰伦", 240.0, name="晴天（伴奏）.flac", hash_="h2"),
            row(4, "晴天", "周杰伦", 240.0, name="晴天（伴奏）2.flac", hash_="h2"),
        ]
        groups = M.group_duplicates(rows, tolerance=2.0, threshold=0.9)
        self.assertEqual(2, len(groups), "原曲组 + 伴奏组")
        dicts = [M.group_to_dict(g) for g in groups]
        base = {d["base_key"] for d in dicts}
        self.assertEqual(1, len(base), "两组的 base_key 应相同，才能合并显示")
        self.assertEqual(2, len({d["key"] for d in dicts}), "key 必须不同")


if __name__ == "__main__":
    unittest.main()
