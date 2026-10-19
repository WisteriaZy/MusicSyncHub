"""入库查重的回归测试。

用户的原话是"防止入库重复歌曲，但记得给用户选择到底入不入库，用户的选择权最大"。
所以这里要锁住两件事：

1. **能认出来**：库内已有的曲目（内容相同）必须被判为 `exact`；
   文件名与体积都一致的判为 `likely`。
2. **不能误报、也不能替用户决定**：不相干的文件不许报重复；
   查重只产出 `dup` 字段，**绝不改动任何"入库/不入库"的状态**。

踩过的坑：索引里存的是 `quick_hash_bytes(整份文件内容)`，
一开始我按"只读头尾抽样"去算本地哈希，结果永远匹配不上 ——
而且不会报错，是最安静的那种 bug。所以有一条专门盯这个。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# 项目根（不要写死本机路径）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent


def _row(path, name, size, h, artist='A', album='AL'):
    return {'id': 1, 'path': path, 'name': name, 'size': size,
            'hash_quick': h, 'artist': artist, 'album': album}


class DupCheckTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from app import upload_api as U
        cls.U = U
        cls.tmp = tempfile.TemporaryDirectory(prefix='msh-dup-')
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.tmp.cleanup()
        except Exception:
            pass

    def _index(self, rows):
        return self.U._index_rows(rows)

    def _file(self, name, data: bytes):
        p = self.dir / name
        p.write_bytes(data)
        return {'name': name, 'size': p.stat().st_size, 'rel': name,
                '_abs': str(p)}

    # ---------------------------------------------------------- 索引
    def test_index_keys_by_hash_and_lowercased_name(self):
        idx = self._index([_row('D:/M/a.flac', 'A.FLAC', 10, 'hh')])
        self.assertIn('hh', idx['by_hash'])
        self.assertIn('a.flac', idx['by_name'],
                      '文件名索引要统一小写，否则大小写不同就查不到')
        self.assertNotIn('A.FLAC', idx['by_name'])

    def test_index_tolerates_missing_hash(self):
        idx = self._index([_row('D:/M/a.flac', 'a.flac', 10, None)])
        self.assertEqual({}, idx['by_hash'])
        self.assertTrue(idx['by_name'])

    # ---------------------------------------------------------- 内容相同
    def test_exact_match_by_content(self):
        from app.core.scanner import quick_hash_local
        f = self._file('same.flac', b'x' * 5000)
        h = quick_hash_local(f['_abs'])
        idx = self._index([_row('D:/M/old.flac', 'old.flac', 5000, h)])
        dup = self.U._dup_check(f, idx)
        self.assertIsNotNone(dup)
        self.assertEqual('exact', dup['level'])
        self.assertIn('old.flac', dup['why'])

    def test_hash_matches_scanner_convention(self):
        """本地哈希必须等于索引里那套算法（整份内容的 quick_hash_bytes）。

        用"只读头尾"的抽样版会永远匹配不上，而且不报错 —— 必须锁死。
        """
        from app.core.scanner import quick_hash_local, quick_hash_bytes
        data = os.urandom(30000)
        f = self._file('conv.flac', data)
        self.assertEqual(quick_hash_bytes(data), quick_hash_local(f['_abs']))

    def test_exact_beats_likely(self):
        """既有内容相同、又有同名同体积时，要报可信度更高的 exact。"""
        from app.core.scanner import quick_hash_local
        f = self._file('both.flac', b'y' * 4096)
        h = quick_hash_local(f['_abs'])
        idx = self._index([
            _row('D:/M/1.flac', 'both.flac', 4096, 'other-hash'),
            _row('D:/M/2.flac', 'both.flac', 4096, h),
        ])
        dup = self.U._dup_check(f, idx)
        self.assertEqual('exact', dup['level'])

    # ---------------------------------------------------------- 疑似
    def test_likely_by_name_and_size(self):
        f = self._file('x.flac', b'z' * 777)
        # 索引里那条哈希跟这个文件**不一样**（甚至没有），只有名字+体积一致
        idx = self._index([_row('D:/M/x.flac', 'x.flac', 777, 'nope')])
        dup = self.U._dup_check(f, idx)
        self.assertIsNotNone(dup)
        self.assertEqual('likely', dup['level'])

    def test_name_match_with_different_size_is_not_dup(self):
        """同名但体积不同 → 是另一版（不同音质），不算重复。"""
        f = self._file('y.flac', b'z' * 777)
        idx = self._index([_row('D:/M/y.flac', 'y.flac', 888, 'nope')])
        self.assertIsNone(self.U._dup_check(f, idx))

    def test_unrelated_file_is_not_flagged(self):
        """不相干的文件一律不许报重复 —— 误报会让人不敢入库。"""
        f = self._file('brand-new.flac', b'q' * 1234)
        idx = self._index([_row('D:/M/other.flac', 'other.flac', 999, 'zzz')])
        self.assertIsNone(self.U._dup_check(f, idx))

    def test_empty_index_reports_nothing(self):
        f = self._file('e.flac', b'e' * 100)
        self.assertIsNone(self.U._dup_check(f, self._index([])))

    def test_missing_file_does_not_raise(self):
        """暂存文件被删了也不能把整个分析搞崩。"""
        f = {'name': 'gone.flac', 'size': 10, 'rel': 'gone.flac',
             '_abs': str(self.dir / 'not-there.flac')}
        idx = self._index([_row('D:/M/gone.flac', 'gone.flac', 10, 'h')])
        # 哈希读不到 → 退回名字+体积判定
        dup = self.U._dup_check(f, idx)
        self.assertEqual('likely', dup['level'])


class DupDoesNotDecideTest(unittest.TestCase):
    """查重是**提示**，不是拦截：它不许自己决定入不入库。

    用户明确要求"给用户选择到底入不入库，用户的选择权最大"。
    所以后端只产出 `dup` 字段；前端负责把选择摆在用户面前。
    """

    JS = None

    @classmethod
    def setUpClass(cls):
        cls.JS = _PROJ.joinpath('app', 'web', 'static', 'js', 'upload.js').read_text(encoding='utf-8')

    def test_ui_offers_both_choices(self):
        """必须同时提供"取消勾选重复项"和"仍然全部入库"两个入口。"""
        self.assertIn('up-dup-uncheck', self.JS)
        self.assertIn('up-dup-keep', self.JS)

    def test_ui_shows_what_was_matched(self):
        """要告诉用户跟库里的哪个文件重了，不能只说"重复"。"""
        self.assertIn('dup.match', self.JS)
        self.assertIn('dup.why', self.JS)

    def test_does_not_silently_drop_duplicates(self):
        """任何阶段都不许自动把重复项从列表里剔除。"""
        self.assertNotIn('items = items.filter((it) => !it.dup)', self.JS)
        self.assertNotIn('.filter((it) => !it.dup)', self.JS)

    def test_backend_never_skips_on_dup(self):
        """后端也不能因为查重就跳过文件 —— 只标记，不动。"""
        src = _PROJ.joinpath('app', 'upload_api.py').read_text(encoding='utf-8')
        seg = src[src.index('def _dup_check'):]
        seg = seg[:seg.index('\ndef ', 10)] if '\ndef ' in seg[10:] else seg
        for bad in ('continue', 'skipped.append', 'raise '):
            self.assertNotIn(bad, seg,
                             f'_dup_check 里不该出现 {bad}（它只负责判定）')


class DedupIsBackgroundTest(unittest.TestCase):
    """查重必须是**按需的后台任务**，不能挂在"分析"里同步跑。

    原因：精确判定要把每个文件读一遍算内容哈希，几百首就是几 GB 的磁盘 IO。
    原先放在 /api/upload/inspect 里，点一下"分析"要等到全部读完才出结果。
    现在：分析阶段只做零成本粗筛（同名同体积），精确查重走
    POST /api/upload/dedup 起的后台任务，有进度、可轮询。
    """

    API = None
    JS = None

    @classmethod
    def setUpClass(cls):
        base = _PROJ.joinpath('app')
        cls.API = (base / 'upload_api.py').read_text(encoding='utf-8')
        cls.JS = (base / 'web' / 'static' / 'js' / 'upload.js').read_text(encoding='utf-8')

    def test_inspect_uses_cheap_check_only(self):
        """分析阶段只能调零成本粗筛，不许调读内容的 _dup_check。"""
        # 取 _suggest_for 的函数体
        seg = self.API[self.API.index('def _suggest_for'):]
        seg = seg[:seg.index('\ndef ', 10)] if '\ndef ' in seg[10:] else seg
        self.assertIn('_cheap_dup(', seg, '分析阶段应使用零成本粗筛')
        self.assertNotIn('_dup_check(', seg,
                         '分析阶段不该读文件内容算哈希（会很慢）')

    def test_cheap_check_does_not_read_content(self):
        """粗筛只比名字和体积，不许打开文件。"""
        seg = self.API[self.API.index('def _cheap_dup'):]
        seg = seg[:seg.index('\ndef ', 10)] if '\ndef ' in seg[10:] else seg
        for bad in ('open(', 'quick_hash', 'read('):
            self.assertNotIn(bad, seg, f'粗筛里不该出现 {bad}')

    def test_dedup_endpoint_exists_and_is_a_job(self):
        self.assertIn('@router.post("/api/upload/dedup")', self.API)
        self.assertIn('@router.get("/api/upload/dedup")', self.API)
        self.assertIn('@router.get("/api/upload/dups")', self.API)
        seg = self.API[self.API.index('def upload_dedup('):]
        seg = seg[:seg.index('\n@router', 10)] if '\n@router' in seg[10:] else seg
        self.assertIn('threading.Thread', seg, '查重要在后台线程里跑')
        self.assertIn('daemon=True', seg, '守护线程，别挡住进程退出')
        self.assertIn('job.id', seg, '要把任务号返回给界面用于轮询')

    def test_dedup_worker_reports_progress(self):
        """进度是在干活的那个函数里推进的（界面靠它画进度条）。"""
        seg = self.API[self.API.index('def _dedup_files'):]
        seg = seg[:seg.index('\ndef ', 10)] if '\ndef ' in seg[10:] else seg
        self.assertIn('job.total', seg)
        self.assertIn('job.done', seg)

    def test_dedup_respects_cancel(self):
        """取消要真的生效（用户可能传了几百首才发现不需要）。"""
        seg = self.API[self.API.index('def _dedup_files'):]
        seg = seg[:seg.index('\ndef ', 10)] if '\ndef ' in seg[10:] else seg
        self.assertIn('job.cancel', seg)

    def test_single_file_error_does_not_abort_batch(self):
        """个别文件读不了（被删/权限）不能把整批查重搞崩。"""
        seg = self.API[self.API.index('def _dedup_files'):]
        seg = seg[:seg.index('\ndef ', 10)] if '\ndef ' in seg[10:] else seg
        self.assertIn('except Exception', seg)

    def test_ui_has_dedup_button_and_progress(self):
        self.assertIn('up-dedup', self.JS, '要有独立的查重按钮')
        self.assertIn('up-dup-prog', self.JS, '要有进度区域')
        self.assertIn('uploadDedupStart', self.JS)
        self.assertIn('uploadDedupStatus', self.JS)

    def test_ui_merges_saved_results_on_reinspect(self):
        """重新分析时要带上已保存的查重结果，不必重跑一遍。"""
        self.assertIn('uploadDups', self.JS)
        self.assertIn('dedupDone', self.JS)


if __name__ == '__main__':
    unittest.main()
