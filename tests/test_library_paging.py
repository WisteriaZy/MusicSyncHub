"""曲库浏览的分页与列选择的回归测试。

两件被真实使用逼出来的事：

1. **分页必须稳定**。「加载更多」以前是 `pageSize += 500` 再整表重载，
   现在改成按 offset 追加新页 —— 这要求后端的排序是**全序**的。
   库里大量曲目的排序键相同（按"作者"排时有几百首作者为空），
   SQLite 对同键行的顺序不保证稳定，两次查询（offset 0 与 offset N）
   可能给出不同排列，于是分页就会重复或漏行。修法是追加 `id` 当决胜键。

2. **列太多横着看很累**。11 列全铺开超出容器宽度，做成可勾选 + 记住选择，
   封面和文件名是固定列（关掉就不知道在看什么了）。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent



def _rec(i, artist, album, name=None, bitrate=320000):
    n = name or f"{i:04d}.flac"
    return {
        'source_id': 's1', 'path': f'D:/M/{artist}/{n}', 'rel_path': f'{artist}/{n}',
        'name': n, 'parent': f'D:/M/{artist}', 'ext': 'flac', 'size': 1000 + i,
        'mtime': 1.0 + i, 'title': n.rsplit('.', 1)[0], 'artist': artist,
        'album': album, 'duration': 60.0 + i, 'bitrate': bitrate,
        'sample_rate': 44100, 'channels': 2, 'bits_per_sample': 16,
        'codec': 'FLAC', 'is_lossless': 1, 'has_cover': 0, 'quality': 50.0,
        'hash_quick': None, 'hash_full': None, 'hash_state': 'none',
    }


class LibraryPagingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='msh-paging-')
        os.environ['MSH_DATA_DIR'] = cls._tmp.name
        os.environ['MSH_DB'] = os.path.join(cls._tmp.name, 'idx.sqlite3')
        cls.root = 'D:/M'
        from app.core.config import AppConfig, SourceConfig, save_config, load_config
        save_config(AppConfig(sources=[
            SourceConfig(id='s1', name='测试库', type='local', root=cls.root)]))
        load_config(force=True)
        from app.main import app, db
        db().upsert_source('s1', '测试库', 'local', cls.root)
        cls.client = TestClient(app)

        # 刻意制造大量同键行：只有 3 个不同的作者，几百首歌平分
        from app.core import matching as M
        rows = []
        for i in range(400):
            artist = ['', '', 'A', 'B'][i % 4]      # 一半是空作者，同键最多
            rec = _rec(i, artist, f'专辑{i % 7}')
            rec['dup_key'] = M.dup_key(rec)
            rec['name_key'] = str(rec.get('title') or '').lower()
            rows.append(rec)
        db().upsert_tracks(rows)

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
        os.environ.pop('MSH_DATA_DIR', None)
        os.environ.pop('MSH_DB', None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def _lib(self, **params):
        r = self.client.get('/api/library/s1', params=params)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _walk(self, page=100, **params):
        """按页取完全部，返回按序拼起来的 id 列表。"""
        ids, off = [], 0
        while True:
            d = self._lib(limit=page, offset=off, **params)
            got = [t['id'] for t in d['tracks']]
            if not got:
                break
            ids += got
            off += page
            if off > 10000:            # 防御：别把测试跑死
                break
        return ids

    def test_paging_has_no_duplicates_or_gaps(self):
        """分页拿全量，必须不重不漏。"""
        for sort in ('path', 'name', 'artist', 'album', 'bitrate', 'size',
                     'duration', 'mtime', 'title', 'quality'):
            with self.subTest(sort=sort):
                ids = self._walk(page=50, sort=sort)
                self.assertEqual(len(ids), len(set(ids)), f'{sort}: 有重复行')
                self.assertEqual(400, len(ids), f'{sort}: 有漏行')

    def test_paging_order_matches_single_shot(self):
        """分页拼起来的顺序要和一次取全量完全一致。

        这条是最关键的：只要排序不是全序，两次查询就会给出不同排列。
        """
        for sort in ('artist', 'album', 'bitrate', 'name'):
            with self.subTest(sort=sort):
                full = [t['id'] for t in self._lib(limit=5000, sort=sort)['tracks']]
                paged = self._walk(page=37, sort=sort)   # 用非整除的页大小
                self.assertEqual(full, paged)

    def test_paging_works_with_desc(self):
        full = [t['id'] for t in self._lib(limit=5000, sort='artist',
                                           order='desc')['tracks']]
        paged = self._walk(page=64, sort='artist', order='desc')
        self.assertEqual(full, paged)

    def test_paging_works_with_fuzzy_search(self):
        """模糊搜索分支是 Python 里排序的，同样要有决胜键。"""
        full = [t['id'] for t in self._lib(q='0', fuzzy=1, limit=5000)['tracks']]
        self.assertTrue(full)
        paged, off = [], 0
        while off < len(full):
            d = self._lib(q='0', fuzzy=1, limit=25, offset=off)
            got = [t['id'] for t in d['tracks']]
            if not got:
                break
            paged += got
            off += 25
        self.assertEqual(full, paged)
        self.assertEqual(len(paged), len(set(paged)))

    def test_offset_beyond_end_is_empty_not_error(self):
        d = self._lib(limit=10, offset=999999)
        self.assertEqual([], d['tracks'])
        self.assertEqual(400, d['total'])

    def test_first_page_metadata_is_intact(self):
        """追加分页不能影响第一页给出的总数/聚合。"""
        d = self._lib(limit=50, offset=0)
        self.assertEqual(400, d['total'])
        self.assertEqual(50, d['returned'])
        self.assertEqual(400, d['aggregate']['count'])


class LibraryColumnsTest(unittest.TestCase):
    """列选择的契约（界面直接拿它渲染）。

    这些都是从源码里解析出来校验的 —— 列显示是纯前端行为，没有接口可测，
    但契约本身很重要，写错了用户就会看到"勾了没反应"或者"恢复默认后没恢复"。
    """

    JS = None

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.JS = _PROJ.joinpath('app', 'web', 'static', 'js', 'views.library.js').read_text(encoding='utf-8')

    def _all_cols(self):
        import re
        return re.findall(r"\{ key: '(\w+)'", self.JS)

    def _default_cols(self):
        import re
        m = re.search(r'DEFAULT_COLS = \[([^\]]+)\]', self.JS)
        self.assertTrue(m, '应该有默认列定义')
        return re.findall(r"'(\w+)'", m.group(1))

    def test_visible_defaults_are_narrower(self):
        """默认列要明显少于全部列 —— 否则"太宽"的问题又回来了。"""
        all_cols = self._all_cols()
        keys = self._default_cols()
        self.assertTrue(all_cols)
        self.assertLess(len(keys), len(all_cols), '默认列数应少于全部列数')
        for k in keys:
            self.assertIn(k, all_cols, f'默认列 {k} 不在列定义里')

    def test_all_default_cols_are_unique(self):
        keys = self._default_cols()
        self.assertEqual(len(keys), len(set(keys)), '默认列里有重复项')

    def test_always_columns_are_marked(self):
        """必须有不许关闭的列（封面/文件名），否则关光了就不知道在看什么。"""
        import re
        always = re.findall(r"\{ key: '(\w+)'[^}]*always: true", self.JS)
        self.assertIn('cover', always)
        self.assertIn('name', always)

    def test_always_columns_are_in_the_defaults(self):
        """固定列必须在默认里，否则一进来就少一列、还会被当成"隐藏了"。"""
        import re
        always = re.findall(r"\{ key: '(\w+)'[^}]*always: true", self.JS)
        keys = self._default_cols()
        for k in always:
            self.assertIn(k, keys, f'固定列 {k} 必须在默认列里')

    def test_reset_refreshes_checkboxes_before_applying(self):
        """「恢复默认」必须先把新状态刷回复选框，再 apply。

        `apply()` 是从 DOM 的 `checked` 读值的；光改内存里的 colSet，
        复选框还是旧状态，apply 就会把旧值又存回去 ——
        表现是"表格看着恢复了，但下次打开又是自定义那套"。

        这里查的是**性质**而不是执行顺序：重置那段里必须出现
        "把 colSet 写回 cb.checked" 这个动作。用源码文本断言执行顺序太脆
        （`apply()` 这个词在定义、调用两处都出现，容易误判）。
        """
        start = self.JS.index("#col-reset')")
        end = self.JS.index('\n    };', start)
        body = self.JS[start:end]
        import re
        self.assertRegex(
            body, r'cb\.checked\s*=\s*colSet\.has',
            '重置时必须把新的 colSet 写回复选框，否则 apply() 会读回旧状态')
        self.assertIn('loadCols()', body, '重置应先重新加载默认列')
        self.assertIn('apply()', body, '重置后要应用并重绘')

    def test_default_set_is_not_persisted(self):
        """选的正好是默认那套时不落盘 —— 让"默认"始终跟着代码走。"""
        start = self.JS.index('function saveCols()')
        end = self.JS.index('\n  }', start)
        body = self.JS[start:end]
        self.assertIn('removeItem', body, '默认组合应移除 localStorage 项')
        self.assertIn('isDefault', body)


class CrossViewJumpTest(unittest.TestCase):
    """「曲库浏览 → 文件浏览定位」与「多选目录」的契约。

    这两件事跨越了多个前端模块，靠接口测不到，但接口**必须**提供足够的信息：
      · 文件列表要给出每行的绝对路径，否则定位时只能按文件名猜（会串行）；
      · 曲目详情要给出 source_id + path，才能切源并跳转；
      · 文件浏览必须把 revealFile 暴露出来，供其它视图调用。
    """

    WEB = None

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.WEB = _PROJ.joinpath('app', 'web', 'static', 'js')

    def _read(self, name):
        return (self.WEB / name).read_text(encoding='utf-8')

    def test_file_rows_expose_absolute_path(self):
        """文件行要带 data-path —— 定位靠它，不能只靠文件名。"""
        js = self._read('views.files.js')
        self.assertRegex(js, r'data-path="\$\{U\.esc\(t\.path\)\}"',
                         '文件行必须带 data-path 属性')

    def test_files_view_exposes_reveal(self):
        js = self._read('views.files.js')
        self.assertIn('revealFile', js, '文件浏览要导出 revealFile')
        self.assertRegex(js, r'registerView\(\s*[\'"]files[\'"]',
                         '应注册 files 视图')
        self.assertIn('revealFile', js[js.index('registerView'):],
                      'revealFile 要放进导出的方法里')

    def test_library_detail_jumps_to_files(self):
        """曲目详情要有跳转入口，且用 source_id + path（能跨源跳）。"""
        js = self._read('views.library.js')
        self.assertIn("data-act=\"files\"", js, '详情里要有"在文件浏览中打开"')
        self.assertIn('revealFile', js, '应调用文件浏览的 revealFile')
        self.assertIn('t.source_id', js, '跳转要带上 source_id（可能跨数据源）')

    def test_app_exposes_view_api(self):
        """app.js 要能取到别的视图的方法，跨视图调用才成立。"""
        js = self._read('app.js')
        self.assertIn('function viewApi(', js)
        self.assertIn('viewApi', js[js.index('return {'):],
                      'viewApi 要被导出')

    def test_folder_picker_supports_multiple(self):
        """文件夹选择器要支持多选，并提供全选/全不选/反选。"""
        js = self._read('folderpicker.js')
        self.assertIn('opt.multiple', js, '要支持 multiple 选项')
        for bid in ('fp-all', 'fp-none', 'fp-invert'):
            self.assertIn(bid, js, f'缺少 {bid}')
        # 全选的作用范围说明：只影响当前列表，不能清掉别处已勾选的
        self.assertIn('listedPaths', js, '全选应只作用于当前列表')
        # 多选返回数组
        self.assertRegex(js, r'finish\(Array\.from\(picked\)\)',
                         '多选应返回路径数组')

    def test_staging_has_no_target_limit(self):
        """候选区不能再有"最多 7 个去向"的限制，改为列组显示。"""
        js = self._read('staging.js')
        self.assertNotIn('MAX_TARGETS', js,
                         '不应再有去向数量上限（颜色循环复用即可）')
        self.assertIn('stg-col', js, '应使用列组布局')
        self.assertIn('stg-col-pending', js, '应有"未分配去向"列')


if __name__ == '__main__':
    unittest.main()
