"""查重规则的对抗性回归测试。

用户最初的抱怨就是这一类："不同的版本（伴奏/重制/现场…）不该被合并"。
本文件把这个判据**系统性**地锁住：

  · 19 种"听起来像但不是同一首"的情况必须**不**被合并
  · 真正的重复（重复下载、不同格式、带音轨号、标签差异）必须**能**被合并

★ 踩过的坑：写这个测试时 fixture 一开始只有 8 个字段，
而 `group_duplicates` 需要的是**真实索引行的形状**。字段不全时
它会一个组都不返回 —— 于是"不同作者也被合并了"这种**假通过**
就冒出来了（其实是因为两行都成了孤立行）。所以 fixture 必须
用真实的字段集，并且**先断言"相同行确实能成组"**作为自检。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import matching as M  # noqa: E402

# 真实索引行的字段集（见 db.all_tracks 的返回）
BASE = {
    "id": 0, "source_id": "s1", "path": "D:/M/Song.mp3", "rel_path": "Song.mp3",
    "name": "Song.mp3", "parent": "D:/M", "size": 5_000_000, "mtime": 0.0,
    "ext": ".mp3", "codec": "mp3", "bitrate": 320, "sample_rate": 44100,
    "bits_per_sample": 0, "channels": 2, "duration": 200.0,
    "title": "", "artist": "A", "album": "Alb", "album_artist": "",
    "genre": "", "year": "", "track_no": 0, "is_lossless": 0,
    "quality": 50, "has_cover": 0, "cover_hash": "", "cover_mime": "",
    "hash_quick": "", "hash_full": "", "hash_state": "", "name_key": "",
    "dup_key": "", "meta_status": "ok", "meta_error": "", "scanned_at": 0.0,
}


def T(name, **kw):
    r = dict(BASE)
    r.update({"name": name, "path": f"D:/M/{name}", "rel_path": name})
    r.update(kw)
    r["ext"] = os.path.splitext(name)[1].lower()
    return r


def merged(a, b) -> bool:
    """两行会不会落进同一个重复组。"""
    return any(len(g) > 1 for g in M.group_duplicates([a, b], tolerance=2.0))


class DedupSelfCheckTest(unittest.TestCase):
    """先证明这个 harness 本身是有效的。

    没有这一步，上面那个"字段不全 → 一个组都不返回"的坑会让
    所有 `assertFalse` 都假通过。
    """

    def test_identical_rows_do_group(self):
        a = T("Song.mp3")
        b = T("Song.mp3", path="D:/N/Song.mp3", rel_path="Other/Song.mp3")
        self.assertTrue(merged(a, b),
                        "相同行都没能成组 → fixture 字段不全，后面的断言都不可信")

    def test_version_variant_does_not_group(self):
        self.assertFalse(merged(T("Song.mp3"), T("Song (Live).mp3")),
                         "自检失败：版本差异都没被拦住")


class NotDuplicatesTest(unittest.TestCase):
    """★ 19 种"听起来像但不是同一首"—— 一律不许合并。"""

    VARIANTS = [
        "Song (Instrumental).mp3", "Song (Remastered).mp3", "Song (Remaster).mp3",
        "Song (Live).mp3", "Song (Acoustic).mp3", "Song (Remix).mp3",
        "Song (Radio Edit).mp3", "Song (Extended Mix).mp3", "Song (Karaoke).mp3",
        "Song (Demo).mp3", "Song (8-bit).mp3", "Song (Nightcore).mp3",
        "Song (Sped Up).mp3", "Song (Slowed).mp3", "Song (piano ver.).mp3",
        "Song (TV Size).mp3", "Song (Cover by B).mp3",
        "Song (Instrumental).flac", "Song [Instrumental].mp3",
    ]

    def test_each_variant_stays_separate(self):
        base = T("Song.mp3")
        for v in self.VARIANTS:
            with self.subTest(variant=v):
                self.assertFalse(merged(base, T(v)),
                                 f"{v} 被误判成重复了")

    def test_same_title_different_artist(self):
        """同名不同作者 → 不合并（用户明确要求过）。"""
        self.assertFalse(merged(T("Song.mp3", artist="A"),
                                T("Song.mp3", artist="B")))

    def test_duration_gap_beyond_tolerance(self):
        """时长差明显的不同录音不能合并。

        ★ 边界随 `duration_tolerance_sec` 走（门禁 = 2×容差）。
        默认容差 2.0 → 门禁 4.0 秒，所以 4.0 秒**收**、4.5 秒**不收**。

        原来这里断言 4.0 秒不收 —— 那是旧实现"窗口恰好 ~3.5 秒"的
        意外产物（桶宽写死 3 秒 + 邻居 ±1），并不是有意的规则；
        它的代价是"用户在界面上调时长容差**完全没用**"。
        """
        self.assertFalse(merged(T("Song.mp3", duration=200.0),
                                T("Song.mp3", duration=380.0)))
        self.assertFalse(merged(T("Song.mp3", duration=200.0),
                                T("Song.mp3", duration=204.5)))
        # 门禁之内仍然要收（同一份文件的标签误差）
        self.assertTrue(merged(T("Song.mp3", duration=200.0),
                               T("Song.mp3", duration=203.5)))

    def test_duration_inside_tolerance(self):
        """±2 秒内是同曲（编码器对时长的估计有误差）。"""
        self.assertTrue(merged(T("Song.mp3", duration=200.0),
                               T("Song.mp3", duration=201.0)))

class RealDuplicatesTest(unittest.TestCase):
    """真正的重复必须能被找到 —— 否则这个功能是坏的。"""

    def test_same_file_different_folder(self):
        self.assertTrue(merged(T("Song.mp3"),
                               T("Song.mp3", path="D:/N/Song.mp3")))

    def test_duplicate_download_suffix(self):
        """`Song (1).mp3` 是浏览器重复下载的典型产物。"""
        self.assertTrue(merged(T("Song.mp3"), T("Song (1).mp3")))

    def test_different_format_same_song(self):
        self.assertTrue(merged(T("Song.mp3"),
                               T("Song.flac", is_lossless=1, codec="flac")))

    def test_track_number_prefix(self):
        self.assertTrue(merged(T("Song.mp3"), T("01 - Song.mp3")))

    def test_tag_only_differences_are_still_duplicates(self):
        """只有标签不同（语言/流派/年份）→ 仍是同一首。"""
        self.assertTrue(merged(T("Song.mp3"),
                               T("Song.mp3", **{"genre": "Pop", "year": "2020"})))
        self.assertTrue(merged(T("Song.mp3"),
                               T("Song.mp3", album="Other")))


class VersionMismatchApiTest(unittest.TestCase):
    """`version_mismatch` 是上面判定的基础，单独锁住。"""

    def test_detects_common_markers(self):
        for m in ("Instrumental", "Remastered", "Live", "Acoustic", "Remix",
                  "Karaoke", "Demo", "Radio Edit", "Extended Mix",
                  "Sped Up", "Slowed", "Nightcore", "TV Size"):
            with self.subTest(marker=m):
                self.assertTrue(
                    M.version_mismatch(T("Song.mp3"), T(f"Song ({m}).mp3")),
                    f"{m} 没被识别为版本差异")

    def test_chinese_markers(self):
        for tag in ("伴奏", "纯音乐", "重制", "混音", "翻唱", "试听"):
            with self.subTest(tag=tag):
                self.assertTrue(
                    M.version_mismatch(T("歌.mp3"), T(f"歌（{tag}）.mp3")),
                    f"{tag} 没被识别为版本差异")

    def test_plain_pair_is_not_mismatch(self):
        self.assertFalse(M.version_mismatch(T("Song.mp3"), T("Song.mp3")))

    def test_missing_keys_do_not_raise(self):
        """`dup_key` 对 None / 0 / 空输入不能抛异常。"""
        for bad in ({}, {"name": None}, {"name": ""}, {"name": 0},
                    {"path": None, "name": None}):
            with self.subTest(row=bad):
                M.dup_key(bad)
                M.song_key(bad)
                M.group_duplicates([bad], tolerance=2.0)


class ToleranceContractTest(unittest.TestCase):
    """★ `duration_tolerance_sec` 必须真的管用。

    这是一条真实存在过的规则 bug：时长分桶写死 `int(d // 3)` + 邻居 ±1，
    等效窗口是 ±6 秒的**硬上限**，而且**完全不理会 tolerance** ——
    实测把容差从 2 调到 10，能发现的时长差**一点没变**。
    界面上那个"时长容差"滑块等于摆设。

    修法：桶宽、分桶窗口、合并门禁、`duration_similarity` 的门禁
    **全部由 tolerance 推导**，四个数保持同一个契约（门禁 = 2×tolerance）。
    这样"调大容差"才真的能把那些标签时长有出入的同一首歌收进来。
    """

    def _m(self, dur_a, dur_b, tol):
        from app.core import matching as M
        return len(M.group_duplicates(
            [T("Song.mp3", duration=dur_a), T("Song.mp3", duration=dur_b)],
            tolerance=tol)) > 0

    def test_tolerance_actually_changes_the_window(self):
        """★ 核心断言：同一个时长差，容差小时不收、容差大时收。"""
        gap = 8.0
        self.assertFalse(self._m(200.0, 200.0 + gap, 2.0),
                         "容差 2.0（门禁 4.0s）时不该收 8 秒的差")
        self.assertTrue(self._m(200.0, 200.0 + gap, 6.0),
                        "★ 容差调到 6.0（门禁 12s）还是收不进来 —— "
                        "容差设置没生效")

    def test_boundary_scales_with_tolerance(self):
        """边界 = 2×tolerance，四个容差档都要对得上。"""
        for tol in (2.0, 5.0, 10.0, 20.0):
            lim = 2.0 * tol
            with self.subTest(tol=tol):
                # 门禁内（留一点余量避开浮点边界）→ 合并
                self.assertTrue(self._m(200.0, 200.0 + lim * 0.95, tol),
                                f"tol={tol} 门禁内竟然不合并")
                # 门禁外 → 不合并
                self.assertFalse(self._m(200.0, 200.0 + lim * 1.5, tol),
                                 f"tol={tol} 门禁外竟然合并了")

    def test_default_tolerance_keeps_old_safety(self):
        """默认容差下仍然不许把"时长差明显的不同录音"并起来。"""
        self.assertFalse(self._m(200.0, 204.5, 2.0))
        self.assertFalse(self._m(200.0, 380.0, 2.0))

    def test_no_over_merge_at_large_tolerance(self):
        """容差调大也不能把不相干的歌并起来（不能修过头）。"""
        from app.core import matching as M
        for a, b in ((T("Song A.mp3", duration=200.0), T("Song B.mp3", duration=200.0)),
                     (T("Track 01.mp3", duration=200.0), T("Track 02.mp3", duration=200.0)),
                     (T("Song.mp3", duration=200.0), T("Song (伴奏).mp3", duration=200.0)),
                     (T("Song.mp3", duration=200.0, artist="A"),
                      T("Song.mp3", duration=200.0, artist="B"))):
            with self.subTest(a=a["name"], b=b["name"]):
                self.assertEqual([], M.group_duplicates([a, b], tolerance=10.0))

    def test_duration_similarity_gate_matches_grouping_gate(self):
        """`duration_similarity` 的门禁要和合并门禁是**同一个数**。

        两处不一致就会出现"门禁放行、分数却扣死"的怪现象
        （真实踩过：门禁 3×tolerance 而分数尾巴写死 28 秒，
        结果 tolerance=10 时 28.5 秒的差永远合不了）。
        """
        from app.core import matching as M
        for tol in (2.0, 5.0, 10.0):
            with self.subTest(tol=tol):
                inside = 2.0 * tol * 0.95
                s_in, _ = M.duration_similarity(
                    T("Song.mp3", duration=200.0),
                    T("Song.mp3", duration=200.0 + inside), tol)
                self.assertEqual(1.0, s_in,
                                 f"tol={tol} 门禁内的时长分不是满分")


class CrossLibraryPairingTest(unittest.TestCase):
    """★ 跨库配对的规则（对比页的核心）。

    这里修过两条**误配**（把不相干的两首歌配成一对）：

    1. 兜底阶段（阶段 3）**不看分数下限**。只要进了候选就配，
       而候选门槛只有 `max(threshold+0.06, 0.7)` = 0.76。
       实测 `Bohemian Rhapsody.flac` ↔ `Smells Like Teen Spirit.flac`
       （同艺术家、时长相同、体积相同）score 0.7336 **配上了**。
    2. 光有分数下限还不够：`size_similarity` 同体积给 1.0、
       `duration_similarity` 时长短差给 1.0，两项占 0.46 权重 ——
       于是"同艺术家 + 时长几乎相同 + 体积相同"就能把总分顶过 0.86，
       哪怕名字不相关。实测 `Alpha Song.flac` ↔ `Beta Song.flac`
       名字相似度只有 70%，score 0.8608，**配上了**。

    误配的代价比漏配大得多：漏配的表现是"显示成两库独有"，用户一眼看得出；
    误配的表现是这两首从"独有"列表里消失 —— **用户根本发现不了**，
    对比页会告诉他"两边都有这首歌"。
    """

    def _pair(self, L, R, tol=2.0):
        from app.core import matching as M
        return M.pair_sources(L, R, tolerance=tol)

    # ------------------------------------------------------------ 不该配
    def test_unrelated_songs_same_artist_do_not_pair(self):
        """同艺术家、时长相同、体积相同，但歌完全不同 → 不许配。"""
        r = self._pair([T("Bohemian Rhapsody.flac", duration=200.0, artist="Queen")],
                       [T("Smells Like Teen Spirit.flac", duration=200.0, artist="Queen")])
        self.assertEqual([], r["matches"], "两首不相干的歌被配成一对")
        self.assertEqual(1, len(r["only_left"]), "左库独有应保留")
        self.assertEqual(1, len(r["only_right"]), "右库独有应保留")

    def test_weak_name_similarity_does_not_pair(self):
        """名字只有 ~70% 像（共享通用后缀）→ 不许配。"""
        r = self._pair([T("Alpha Song.flac", duration=200.0, artist="X")],
                       [T("Beta Song.flac", duration=200.3, artist="X")])
        self.assertEqual([], r["matches"], "名字不相关的两首歌被配成一对")

    def test_instrumental_does_not_pair_with_original(self):
        r = self._pair([T("Song.mp3")], [T("Song (伴奏).mp3")])
        self.assertEqual([], r["matches"])

    def test_same_name_different_artist_does_not_pair(self):
        r = self._pair([T("Song.mp3", artist="A")], [T("Song.mp3", artist="B")])
        self.assertEqual([], r["matches"])

    # ------------------------------------------------------------ 该配（不能修过头）
    def test_same_name_different_folder_pairs(self):
        r = self._pair([T("Song.mp3", path="L/a/Song.mp3")],
                       [T("Song.mp3", path="R/b/Song.mp3")])
        self.assertEqual(1, len(r["matches"]))

    def test_word_order_difference_pairs(self):
        """`Daft Punk - Get Lucky` ↔ `Get Lucky - Daft Punk`。"""
        r = self._pair([T("Daft Punk - Get Lucky.mp3")],
                       [T("Get Lucky - Daft Punk.mp3")])
        self.assertEqual(1, len(r["matches"]),
                         "词序不同的同一首歌没配上")

    def test_same_name_large_duration_gap_still_pairs(self):
        """同名是铁证 —— 时长差 8 秒也要配（标签器估算误差）。"""
        r = self._pair([T("Song.mp3", duration=200.0, path="L/S.mp3")],
                       [T("Song.mp3", duration=208.0, path="R/S.mp3")])
        self.assertEqual(1, len(r["matches"]))

    def test_common_name_variations_pair(self):
        """日常会遇到的几种同曲异名都要能配上。"""
        for a, b in (("Song - Artist.flac", "Song - Artist (1).flac"),
                     ("01 Song.flac", "Song.flac"),
                     ("Song [320].mp3", "Song.mp3")):
            with self.subTest(a=a, b=b):
                r = self._pair([T(a)], [T(b)])
                self.assertEqual(1, len(r["matches"]), f"{a} ↔ {b} 没配上")

    def test_one_to_one_pairing(self):
        """一侧两份同名、另一侧一份 → 只配一次，另一份算独有。"""
        r = self._pair([T("Song.mp3", path="L/1/S.mp3"),
                        T("Song.mp3", path="L/2/S.mp3")],
                       [T("Song.mp3", path="R/S.mp3")])
        self.assertEqual(1, len(r["matches"]))
        self.assertEqual(1, len(r["only_left"]))


class DupKeyBucketStepTest(unittest.TestCase):
    """`dup_key` 的时长桶要能由调用方控制。

    它是**精确字符串键**：桶号不同就永远进不了同一个候选池。
    跨库配对的第 2 阶段（标题+艺术家，补"文件名被改写"的场景）完全靠它，
    而第 1 阶段（文件名键）是有 `tolerance` 判断的 —— 于是出现不一致：

        文件名相同 → 时长差 3 秒**能配**（走第 1 阶段）
        文件名不同 → 时长差 3 秒**配不上**，而且**调大容差也没用**

    实测 `Get Lucky (Live Version).flac` ↔ `daft punk - get lucky.flac`：
    两边键是 `get lucky|daft punk|200|live` 和 `get lucky|daft punk|204|`
    —— 桶号 200 vs 204，永远不进同一个池子。
    """

    def test_default_key_is_unchanged(self):
        """索引里存的就是默认键 —— 默认行为一个字不能变。"""
        from app.core import matching as M
        r = {"name": "Song.mp3", "title": "Song", "artist": "A",
             "rel_path": "Song.mp3", "duration": 200.0}
        self.assertEqual(M.dup_key(r), M.dup_key(r, bucket_step=2.0))
        self.assertEqual("song|a|200|", M.dup_key(r))

    def test_larger_bucket_step_widens_the_pool(self):
        from app.core import matching as M
        r = {"name": "Song.mp3", "title": "Song", "artist": "A",
             "rel_path": "Song.mp3", "duration": 203.0}
        # 2 秒桶 → 204；12 秒桶 → 204（12*17=204）… 换个数看差异
        r2 = dict(r, duration=200.0)
        self.assertNotEqual(M.dup_key(r, bucket_step=2.0),
                            M.dup_key(r2, bucket_step=2.0),
                            "2 秒桶下 200 与 203 应落在不同桶")
        self.assertEqual(M.dup_key(r, bucket_step=12.0),
                         M.dup_key(r2, bucket_step=12.0),
                         "12 秒桶下 200 与 203 应落在同一个桶")

    def test_invalid_bucket_step_falls_back(self):
        from app.core import matching as M
        r = {"name": "Song.mp3", "title": "Song", "artist": "A",
             "rel_path": "Song.mp3", "duration": 200.0}
        for bad in (0, -5, "abc", None):
            with self.subTest(v=bad):
                self.assertEqual("song|a|200|", M.dup_key(r, bucket_step=bad))


class PlanHashStalenessTest(unittest.TestCase):
    """整理方案的指纹要能发现"源文件被换掉了"。

    原来 `_plan_hash` 只把 `(path, dst_dir)` 拼进指纹，于是这种改动检测不到：

        同一路径的文件**被替换**了（重新下载、换了质量、损坏后修复）
        → path 没变、dst_dir 没变 → 指纹一模一样
        → 用户点"执行"时，搬走的是**他确认方案时没看过的那个文件**

    带上 `size` 之后，文件换了就会 409 要求重新生成方案。
    （不用 mtime：重新扫描/同步工具碰一下就会变，会误报。）
    """

    def _h(self, moves):
        from app.search_api import _plan_hash
        return _plan_hash({"moves": moves})

    def test_size_change_changes_hash(self):
        a = [{"path": "D:/M/a.flac", "dst_dir": "D:/M/Rock", "size": 1000}]
        b = [{"path": "D:/M/a.flac", "dst_dir": "D:/M/Rock", "size": 999999}]
        self.assertNotEqual(self._h(a), self._h(b),
                            "★ 源文件大小变了但指纹没变 —— 会搬走用户没确认过的文件")

    def test_identical_plan_keeps_hash(self):
        a = [{"path": "D:/M/a.flac", "dst_dir": "D:/M/Rock", "size": 1000}]
        self.assertEqual(self._h(a), self._h(list(a)),
                         "同一份方案重算指纹变了 → 会误报'方案已变化'")

    def test_dst_change_changes_hash(self):
        a = [{"path": "D:/M/a.flac", "dst_dir": "D:/M/Rock", "size": 1000}]
        b = [{"path": "D:/M/a.flac", "dst_dir": "D:/M/Jazz", "size": 1000}]
        self.assertNotEqual(self._h(a), self._h(b))

    def test_missing_plan_is_safe(self):
        self.assertEqual(self._h([]), self._h([]))
        from app.search_api import _plan_hash
        _plan_hash({})


if __name__ == "__main__":
    unittest.main()
