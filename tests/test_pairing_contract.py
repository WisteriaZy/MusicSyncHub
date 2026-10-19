"""配对阶段的"一对一"与"确定性"契约。

## 这两条为什么必须锁住

审计实测出来的两个缺陷（都有复现数据）：

1. **阶段 1 复用了阶段 0 已经配上的左记录** → 同一个左文件进两个 Match。
   `left_total=1` 却 `matched=2`，界面上"已匹配 + 仅左"大于总数；
   跨库查重时这两条配对会被 union 成"一组 3 条"，其中两条来自**同一个库的
   两个不同文件**，界面却提示"保留一份、清理其它" —— 直接导致删错。

2. **阶段 0 按输入顺序贪心且没有时长门禁** → 同一份数据把左侧顺序反过来，
   配对结果会变（实测同一对从 0.57 变成 1.00）；255 秒差的文件也能配上。

## 注意：两条**不要**改掉的刻意决定

  · 阶段 3 不管时长 ——「**同名是铁证**，差 8 秒也要配」
    （`tests/test_dedup_rules.py::test_same_name_large_duration_gap_still_pairs`）
  · 阶段 0 不管艺术家 —— 词序颠倒时"首词当艺术家"必然误判
    （`tests/test_dedup_rules.py::test_word_order_difference_pairs`）
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent


def T(name, title="", artist="", duration=200.0, size=1000,
      source_id="s1", path=None):
    from app.core import paths as P
    path = path or name
    return {
        "source_id": source_id, "path": f"/{source_id}/{path}",
        "rel_path": path, "name": name, "title": title, "artist": artist,
        "duration": duration, "size": size, "name_key": P.name_key(name),
        "quality": 50, "dup_key": "",
    }


class PairingContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("MSH_DATA_DIR",
                              tempfile.mkdtemp(prefix="msh-pair-"))
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core import matching as M
        cls.M = M

    def _pair(self, left, right, tolerance=2.0):
        return self.M.pair_sources(left, right, tolerance=tolerance)

    # ---------------------------------------------------------- 一对一
    def test_left_never_paired_twice(self):
        """一个左文件只能进一个 Match。"""
        left = [T("Artist - Song.flac", "Song", "Artist", 200)]
        right = [T("Song - Artist.flac", "Song", "Artist", 200, source_id="s2"),
                 T("Artist - Song.flac", "Song", "Artist", 200, source_id="s2")]
        r = self._pair(left, right)
        lefts = [id(m.left) for m in r["matches"]]
        self.assertEqual(len(lefts), len(set(lefts)),
                         f"同一个左文件被配了多次：{[m.left['path'] for m in r['matches']]}")
        self.assertLessEqual(len(r["matches"]), len(left))

    def test_stats_never_exceed_totals(self):
        """`matched + only_left` 不能超过 `left_total`（否则界面数字自相矛盾）。"""
        left = [T(f"Song{i}.mp3", f"Song{i}", "A", 200) for i in range(5)]
        right = [T(f"Song{i}.mp3", f"Song{i}", "A", 200, source_id="s2")
                 for i in range(3)]
        st = self._pair(left, right)["stats"]
        self.assertLessEqual(st["matched"] + st["only_left"], st["left_total"],
                             f"左侧统计超了：{st}")

    # ---------------------------------------------------------- 确定性
    def test_order_independent(self):
        """同一份数据换个行序，配对结果必须一致。"""
        left = [T("Zed - Song.flac", "Song", "Zed", 180),
                T("Song - Zed.flac", "Song", "Zed", 180)]
        right = [T("Song - Zed.flac", "Song", "Zed", 180, source_id="s2")]

        def sig(rows):
            r = self._pair(rows, right)
            return sorted((m.left["path"], round(m.score, 4)) for m in r["matches"])

        self.assertEqual(sig(left), sig(list(reversed(left))),
                         "配对结果依赖输入顺序")

    # ---------------------------------------------------------- 空键
    def test_empty_name_key_is_not_a_match(self):
        """两侧 `name_key` 都归一化成空串时**不是**"完全一致"。"""
        from app.core import paths as P
        self.assertEqual(0.0, P.similarity("", ""),
                         "空 ↔ 空必须表示'没有证据'，不是'完全一致'")
        left = [T("320k.flac", "", "", 200)]
        right = [T("wusun.flac", "", "", 200, source_id="s2")]
        self.assertEqual([], self._pair(left, right)["matches"],
                         "两个毫无关系的文件被配成了一对")

    # ---------------------------------------------------------- 时长门禁
    def test_huge_duration_gap_does_not_pair(self):
        """时长差远超容差、且名字只是"词序颠倒" → 不该配。"""
        left = [T("Adele - Hello.flac", "Hello", "Adele", 40)]
        right = [T("Hello - Adele.flac", "Hello", "Adele", 295, source_id="s2")]
        self.assertEqual([], self._pair(left, right)["matches"],
                         "40 秒片段被当成了 5 分钟原曲的副本")

    # ---------------------------------------------------------- 刻意决定
    def test_same_name_ignores_duration_gap(self):
        """刻意决定：同名是铁证，差 8 秒仍要配（标签器估算误差）。"""
        left = [T("Song.mp3", "Song", "", 200)]
        right = [T("Song.mp3", "Song", "", 208, source_id="s2")]
        self.assertEqual(1, len(self._pair(left, right)["matches"]),
                         "同名文件差 8 秒没配上 —— 这条是刻意保留的行为")


if __name__ == "__main__":
    unittest.main()
