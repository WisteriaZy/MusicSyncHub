"""按专辑整理（plan_by_album）的回归测试。

这是"智能整理"的正式逻辑：先选范围（选中的忽略）→ 按专辑名分组 →
找库里相似的现成文件夹 → 不够就 ≥N 首建目录 → 否则原地不动。
自定义映射优先级最高。

锁住的几个真实坑：
* 有专辑信息时应当归到 `作者/专辑`，而不是把歌堆进裸的作者目录
  （实测 10 首《よるつむぎ》被送进 `MIMI/`）；
* 但**只有够门槛**才为专辑多分一层 —— 用户本来是"一个作者一个目录"的平铺
  结构，给 3 首歌再套一层只会把层级搞乱（实测这一条把 1064 首的搬动降下来）；
* 不够门槛的零散小专辑默认**不动**，否则等于把用户的"精选集"重新洗一遍。
"""
from __future__ import annotations

import unittest

from app.core import organize as OG

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent



def _t(path, artist, album='', size=1000):
    n = path.rsplit('/', 1)[-1]
    return {
        'source_id': 's1', 'path': path, 'rel_path': path, 'name': n,
        'parent': path.rsplit('/', 1)[0], 'size': size, 'mtime': 1.0,
        'title': n.rsplit('.', 1)[0], 'artist': artist, 'album': album,
    }


class AlbumPlanTest(unittest.TestCase):
    ROOT = 'D:/M'

    def _plan(self, rows, **kw):
        return OG.plan_by_album(rows, self.ROOT, **kw)

    def _dst_of(self, plan, path):
        for m in plan['moves']:
            if m['path'] == path:
                return m['dst_dir']
        return None

    # ---------------------------------------------------------- 范围
    def test_ignored_scope_is_untouched(self):
        """勾进忽略范围的目录整棵子树都不参与整理。"""
        rows = ([_t(f'D:/M/别动/歌{i}.flac', '周杰伦', '范特西') for i in range(6)]
                + [_t(f'D:/M/要整理/歌{i}.flac', '周杰伦', '范特西') for i in range(6)])
        plan = self._plan(rows, scopes=['D:/M/别动'], min_group=5)
        moved = {m['path'] for m in plan['moves']}
        self.assertFalse([p for p in moved if '/别动/' in p],
                         '忽略范围里的文件不该被移动')
        self.assertTrue([p for p in moved if '/要整理/' in p], '范围内的应该整理')
        self.assertIn('别动', plan['summary']['ignored'])

    def test_relative_scope_is_normalized(self):
        rows = [_t(f'D:/M/别动/歌{i}.flac', 'x', 'y') for i in range(3)]
        rel = self._plan(rows, scopes=['别动'])
        absolute = self._plan(rows, scopes=['D:/M/别动'])
        self.assertEqual(rel['summary']['ignored'], absolute['summary']['ignored'])
        self.assertEqual(['别动'], rel['summary']['ignored'])

    # ---------------------------------------------------------- 找现成目录
    def test_scattered_album_goes_into_existing_album_folder(self):
        """库里已有同名专辑目录 → 散在别处的歌并进去。"""
        rows = ([_t(f'D:/M/范特西/正主{i}.flac', '周杰伦', '范特西') for i in range(4)]
                + [_t(f'D:/M/散/a.flac', '周杰伦', '范特西'),
                   _t(f'D:/M/散/b.flac', '周杰伦', '范特西')])
        plan = self._plan(rows, min_group=5)
        self.assertEqual('范特西', self._dst_of(plan, 'D:/M/散/a.flac'))

    def test_similar_folder_is_chosen(self):
        """书名号/标点差异导致的"名字很像" → 建议并进现成目录。"""
        rows = ([_t(f'D:/M/《CITY RAT》 Original Soundtrack/a{i}.flac',
                    '某人', '《CITY RAT》 Original Soundtrack') for i in range(4)]
                + [_t('D:/M/散/x.flac', '某人', 'CITY RAT Original Soundtrack')])
        plan = self._plan(rows, min_group=5)
        dst = self._dst_of(plan, 'D:/M/散/x.flac')
        self.assertIsNotNone(dst, plan['summary'])
        self.assertIn('CITY RAT', dst)

    # ---------------------------------------------------------- 作者层级
    def test_big_album_gets_subfolder_under_artist(self):
        """有作者目录、专辑够门槛 → 归到 `作者/专辑`。"""
        rows = ([_t(f'D:/M/MIMI/别的.flac', 'MIMI', '别的专辑')]
                + [_t(f'D:/M/散/よるつむぎ{i}.flac', 'MIMI', 'よるつむぎ') for i in range(6)])
        plan = self._plan(rows, min_group=5)
        if not plan['moves']:
            import json
            print('\nDIAG rows=' + json.dumps(
                [(r['path'], r['artist'], r['album']) for r in rows], ensure_ascii=False))
            print('DIAG summary=' + json.dumps(plan['summary'], ensure_ascii=False))
            print('DIAG actions=' + json.dumps(
                [{k: a[k] for k in ('album', 'artist', 'key', 'dst', 'kind',
                                    'count', 'already', 'from_folders')}
                 for a in plan['actions']], ensure_ascii=False))
        self.assertEqual('MIMI/よるつむぎ', self._dst_of(plan, 'D:/M/散/よるつむぎ0.flac'))

    def test_small_album_stays_in_artist_folder(self):
        """专辑不够门槛 → 留在作者目录，不再多分一层（避免层级被搞乱）。"""
        rows = ([_t(f'D:/M/MIMI/别的.flac', 'MIMI', '别的专辑')]
                + [_t(f'D:/M/散/a.flac', 'MIMI', '小专辑'),
                   _t(f'D:/M/散/b.flac', 'MIMI', '小专辑')])
        plan = self._plan(rows, min_group=5)
        dst = self._dst_of(plan, 'D:/M/散/a.flac')
        self.assertEqual('MIMI', dst, plan['summary'])
        self.assertNotIn('/', dst)

    # ---------------------------------------------------------- 门槛
    def test_below_threshold_is_kept_by_default(self):
        """不够门槛、也没有现成目录 → 默认原地不动。"""
        rows = [_t(f'D:/M/散{i}.flac', f'作者{i}', f'专辑{i}') for i in range(3)]
        plan = self._plan(rows, min_group=5)
        self.assertEqual(0, plan['summary']['move'], plan['summary'])
        self.assertGreater(plan['summary']['small_kept'], 0)

    def test_below_threshold_can_go_to_sink(self):
        """收纳区必须是**真实存在**的目录 —— 只是嘴上说"用单曲01"而库里没有，
        它就不在可去处名单里，判定会落回"原地不动"。"""
        rows = ([_t(f'D:/M/散{i}.flac', f'作者{i}', f'专辑{i}') for i in range(3)]
                + [_t('D:/M/单曲01/已有的.flac', '甲', '乙')])
        plan = self._plan(rows, min_group=5, small_policy='sink',
                          sinks=['D:/M/单曲01'])
        moved = {m['dst_dir'] for m in plan['moves'] if '/散' in m['path']}
        self.assertEqual({'单曲01'}, moved, plan['summary'])

    def test_threshold_creates_new_folder(self):
        rows = [_t(f'D:/M/散{i}.flac', '某歌手', '某专辑') for i in range(6)]
        plan = self._plan(rows, min_group=5)
        dsts = {m['dst_dir'] for m in plan['moves']}
        self.assertEqual({'某歌手/某专辑'}, dsts, plan['summary'])

    def test_raising_threshold_reduces_moves(self):
        """门槛调高，需要移动的文件只会变少（单调性）。"""
        rows = ([_t(f'D:/M/散{i}.flac', '某歌手', '某专辑') for i in range(6)]
                + [_t(f'D:/M/散2/よる{i}.flac', 'MIMI', 'よるつむぎ') for i in range(5)])
        lo = self._plan(rows, min_group=3)['summary']['move']
        hi = self._plan(rows, min_group=10)['summary']['move']
        self.assertGreaterEqual(lo, hi, f'门槛调高后移动数反而增加: {lo} → {hi}')

    # ---------------------------------------------------------- 自定义映射
    def test_custom_mapping_wins(self):
        """自定义映射优先级最高，连现成的同名目录都不如它。"""
        rows = ([_t(f'D:/M/范特西/正主{i}.flac', '周杰伦', '范特西') for i in range(4)]
                + [_t('D:/M/散/a.flac', '周杰伦', '范特西')])
        plan = self._plan(rows, min_group=5,
                          custom={'范特西': '我的精选/周杰伦'})
        self.assertEqual('我的精选/周杰伦', self._dst_of(plan, 'D:/M/散/a.flac'))

    def test_custom_mapping_by_artist(self):
        rows = [_t(f'D:/M/散/{i}.flac', '周杰伦', f'专辑{i}') for i in range(3)]
        plan = self._plan(rows, min_group=99, custom={'周杰伦': '自建/周杰伦'})
        self.assertEqual({'自建/周杰伦'}, {m['dst_dir'] for m in plan['moves']})

    # ---------------------------------------------------------- 其他
    def test_untagged_tracks_are_ignored(self):
        """没有专辑也没有作者的歌不参与分组。"""
        rows = [_t('D:/M/散/无标签.flac', '', '')]
        plan = self._plan(rows, min_group=5)
        self.assertEqual(0, plan['summary']['move'])

    def test_nothing_to_do_for_well_organized_library(self):
        rows = [_t(f'D:/M/周杰伦/范特西/{i}.flac', '周杰伦', '范特西')
                for i in range(8)]
        plan = self._plan(rows, min_group=5)
        self.assertEqual(0, plan['summary']['move'], plan['summary'])

    def test_actions_carry_cover_and_artist_for_display(self):
        """actions 里要带上作者和封面地址，界面上不能只显示文件名。"""
        rows = [{'source_id': 's1', 'path': f'D:/M/散/{i}.flac',
                 'rel_path': f'散/{i}.flac', 'name': f'{i}.flac',
                 'parent': 'D:/M/散', 'size': 1000, 'mtime': 1.0,
                 'title': f'歌{i}', 'artist': '某歌手', 'album': '某专辑',
                 'id': 100 + i, 'cover_hash': 'abc'} for i in range(6)]
        plan = self._plan(rows, min_group=5)
        act = next(a for a in plan['actions'] if a['count'])
        self.assertTrue(act['items'], 'actions 里要带曲目明细')
        it = act['items'][0]
        self.assertEqual('某歌手', it['artist'])
        self.assertIn('/api/cover/', it['cover_url'])
        self.assertIn('artist', it)


class GroupByModeTest(unittest.TestCase):
    """分组依据（group_by）必须真正决定分组方式与目标目录形态。

    这组测试来自一个真实反馈：用户想按作者归类，但界面里根本没有这个选择，
    代码写死"专辑优先、没专辑才退回作者"，结果"想按作者分却总被按专辑拆开"。
    更糟的是按作者分组时若还去匹配专辑目录，一组的身份就错了 ——
    实测 290 首 HOYO-MiX 被整组塞进「崩坏星穹铁道-何者 Nameless Faces」。
    """
    ROOT = 'D:/M'

    def _rows(self):
        """一个作者三张专辑，另加一个不同作者，用来观察分组粒度。"""
        rows = []
        for ai, alb in enumerate(['专辑一', '专辑二', '专辑三']):
            for i in range(6):
                rows.append(_t(f'D:/M/散/ho{ai}{i}.flac', 'HOYO-MiX', alb))
        for i in range(6):
            rows.append(_t(f'D:/M/散/zz{i}.flac', 'Zris', 'Groundbreaking'))
        return rows

    def _plan(self, **kw):
        return OG.plan_by_album(self._rows(), self.ROOT, min_group=5, **kw)

    # ---------------------------------------------------------- 默认
    def test_default_modes_are_validated(self):
        with self.assertRaises(ValueError):
            self._plan(group_by='乱写的')

    def test_artist_album_splits_per_album(self):
        """作者+专辑：同一作者的不同专辑各自成组。"""
        p = self._plan(group_by='artist_album')
        keys = [a['key'] for a in p['actions']]
        self.assertIn('HOYO-MiX\x1f专辑一', keys)
        self.assertIn('HOYO-MiX\x1f专辑二', keys)
        # 两层结构 → 目标是 作者/专辑
        dsts = {m['dst_dir'] for m in p['moves'] if 'HOYO' in m['path'] or 'ho' in m['path']}
        self.assertTrue(any('/' in d for d in dsts),
                        f'两层结构目标应含作者与专辑两层，实际 {dsts}')

    def test_artist_mode_groups_all_albums_together(self):
        """仅作者：同一作者的**全部专辑**归为一组，目标就是作者目录。"""
        p = self._plan(group_by='artist')
        keys = [a['key'] for a in p['actions']]
        self.assertIn('HOYO-MiX', keys)
        self.assertFalse([k for k in keys if '\x1f' in k],
                         '仅作者模式下不该出现"作者+专辑"的组合键')
        dsts = {m['dst_dir'] for m in p['moves']}
        self.assertIn('HOYO-MiX', dsts,
                      f'HOYO-MiX 的曲目应全部归入 HOYO-MiX/，实际 {sorted(dsts)}')
        # 关键：不该把不同专辑塞进某个专辑目录
        self.assertFalse([d for d in dsts if d.startswith('HOYO-MiX/')],
                         '仅作者模式下不该再分专辑层')

    def test_artist_mode_does_not_hijack_album_folder(self):
        """仅作者模式绝不能把整组塞进一个名字沾边的专辑目录。

        这正是实测踩到的坑：290 首 HOYO-MiX 全被送进了
        「崩坏星穹铁道-何者 Nameless Faces」——因为拿作者名去匹配了专辑目录。
        """
        rows = self._rows() + [
            _t(f'D:/M/崩坏星穹铁道-何者 Nameless Faces/x{i}.flac', 'HOYO-MiX', '')
            for i in range(3)
        ]
        p = OG.plan_by_album(rows, self.ROOT, min_group=5, group_by='artist')
        bad = [m for m in p['moves'] if m['dst_dir'].endswith('Nameless Faces')]
        self.assertFalse(bad, f'不该有曲目被塞进那个专辑目录：{bad[:2]}')
        dsts = {m['dst_dir'] for m in p['moves']}
        self.assertIn('HOYO-MiX', dsts)

    def test_album_mode_ignores_artist_folders(self):
        """仅专辑：有专辑信息时不该退到作者目录（那会把不同专辑混在一起）。"""
        rows = self._rows() + [
            _t(f'D:/M/HOYO-MiX/existing{i}.flac', 'HOYO-MiX', '') for i in range(3)
        ]
        p = OG.plan_by_album(rows, self.ROOT, min_group=5, group_by='album')
        keys = [a['key'] for a in p['actions']]
        self.assertFalse([k for k in keys if '\x1f' in k],
                         '仅专辑模式下不该出现组合键')
        for m in p['moves']:
            if 'ho' in m['path'] or 'HOYO' in m['path']:
                self.assertNotEqual('HOYO-MiX', m['dst_dir'],
                                    '有专辑信息时不该把曲目丢进作者目录')

    def test_modes_produce_different_group_counts(self):
        """三种模式必须给出**不同**的分组粒度，否则这个选项等于没做。"""
        counts = {}
        for gb in ('artist', 'album', 'artist_album'):
            p = self._plan(group_by=gb)
            counts[gb] = len([a for a in p['actions'] if a['count']])
        self.assertGreater(counts['artist_album'], counts['artist'],
                           '作者+专辑应该比仅作者分得更细')
        self.assertGreater(counts['album'], counts['artist'],
                           '按专辑分应该比按作者分更细（本例一作者多专辑）')

    def test_summary_reports_grouping(self):
        p = self._plan(group_by='artist')
        self.assertEqual('artist', p['summary']['group_by'])

    def test_small_group_keeps_place_by_default(self):
        """未达门槛且库内无对应目录 → 保留原位（不改动既有合集目录）。"""
        rows = [_t(f'D:/M/散/a{i}.flac', '小作者', '小专辑') for i in range(2)]
        p = OG.plan_by_album(rows, self.ROOT, min_group=5, group_by='artist')
        self.assertEqual([], p['moves'], '只有 2 首，不该移动')
        self.assertEqual(2, p['summary']['keep'])

    def test_sink_policy_collects_small_groups(self):
        rows = [_t(f'D:/M/散/a{i}.flac', '小作者', '小专辑') for i in range(2)]
        p = OG.plan_by_album(rows, self.ROOT, min_group=5, group_by='artist',
                             small_policy='sink', sinks=['D:/M/单曲'])
        self.assertTrue(p['moves'], 'sink 策略下应该收进收纳区')
        self.assertTrue(all(m['dst_dir'] == '单曲' for m in p['moves']),
                        f"实际 {[m['dst_dir'] for m in p['moves']]}")


class WizardUiContractTest(unittest.TestCase):
    """智能整理界面的契约（纯前端，没有接口可测，但错了用户就会困惑）。

    三条都是用户直接反馈出来的：
      1. 收纳区原来是"toggle 小圆片"，和忽略范围的复选列表不是一套操作，
         看着别扭 —— 统一成同样的 label+checkbox 列表；
      2. 选了"保留原位"时，收纳区那一整块其实用不上，却还亮着可点，
         让人以为设了不生效 —— 必须灰掉；
      3. 曲目明细原来是藏在「曲目」按钮的二级弹窗里，看不到每组有什么歌 ——
         改成在分组行下方就地展开。
    """

    JS = None

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.JS = _PROJ.joinpath('app', 'web', 'static', 'js', 'organize.js').read_text(encoding='utf-8')

    def _fn(self, name):
        """截出某个函数的函数体（到下一个顶层 `function`/`}` 收尾为止）。"""
        i = self.JS.index(name)
        j = self.JS.find('\n    }', i)
        return self.JS[i:j if j > 0 else len(self.JS)]

    # ---------------------------------------------------------- 收纳区
    def test_sink_uses_checkbox_list_like_scope(self):
        """收纳区要和忽略范围一样，用 label + checkbox 的列表。"""
        seg = self._fn('async function loadScope')
        self.assertIn('data-sink', seg)
        self.assertRegex(seg, r'<label[^>]*>\s*<input type="checkbox" data-sink',
                         '收纳区应是 label 包 checkbox 的列表项')
        # 不许再用 chip 那种 toggle 小圆片
        self.assertNotIn('class="chip ${sinks.has', seg)
        self.assertNotIn('og-chips', seg,
                         '收纳区不该再用 chips 容器')

    def test_sink_list_shows_counts(self):
        """列表里要显示曲目数 —— 光看名字没法判断该选哪个。"""
        seg = self._fn('async function loadScope')
        self.assertIn('sink_options', seg)
        self.assertIn('o.count', seg)

    def test_sink_field_dims_when_keep_policy(self):
        """选了"保留原位"，收纳区那块要灰掉并禁用。"""
        seg = self._fn('function paintSinkHint')
        self.assertIn("smallPolicy !== 'sink'", seg)
        self.assertIn('dim', seg, '要加 dim 类把失效的那块灰掉')
        self.assertIn('cb.disabled', seg, '复选框要真的禁用，不只是变暗')

    def test_policy_radios_are_never_dimmed(self):
        """★ 灰的只能是**下面那块**列表，单选按钮本身必须一直可点。

        踩过的坑：把 `dim` 加在包住单选按钮的整个 field 上，于是选了
        "保留原位"之后它自己也被灰、还点不动 —— 就再也切不回
        "归入收纳区"了，只能关掉重开。
        """
        seg = self._fn('function paintSinkHint')
        self.assertNotIn("querySelector('#aw-sink-field')", seg,
                         '不该把整个 field 灰掉（那会连单选按钮一起灰）')
        self.assertIn("querySelector('#aw-sink-body')", seg,
                      '要灰的是收纳区列表那一块')
        # 结构上：单选按钮必须在被灰的那块**外面**
        html = self.JS[self.JS.index("name=\"aw-small\""):]
        html = html[:html.index('</details>')]
        radio_at = html.index('name="aw-small"')
        body_at = html.index('id="aw-sink-body"')
        self.assertLess(radio_at, body_at,
                        '单选按钮要排在 aw-sink-body 之前（即不在被灰的块里）')
        # 并且 dim 的 CSS 不能再绑在 .field 上
        from pathlib import Path
        css = _PROJ.joinpath('app', 'web', 'static', 'css', 'app.css').read_text(encoding='utf-8')
        self.assertNotIn('.field.dim', css,
                         'dim 不该绑在 .field 上，否则整块（含单选）都灰')

    def test_sink_hint_explains_why_disabled(self):
        """灰掉的同时要说明原因，否则用户只会觉得"坏了"。"""
        seg = self._fn('function paintSinkHint')
        self.assertIn('不参与整理', seg)

    def test_effective_sink_is_reported(self):
        """勾了多个收纳区时，要告诉用户实际会归到哪一个。"""
        seg = self._fn('function effectiveSink')
        self.assertIn('split', seg)          # 按层级深度挑
        hint = self._fn('function paintSinkHint')
        self.assertIn('effectiveSink', hint)

    def test_sink_hint_refreshed_on_policy_change(self):
        seg = self.JS[self.JS.index('input[name=aw-small]'):]
        seg = seg[:400]
        self.assertIn('paintSinkHint', seg,
                      '切换保留原位/收纳区时要刷新灰显状态')

    # ---------------------------------------------------------- 曲目明细
    def test_track_details_are_inline_not_a_second_dialog(self):
        """曲目明细要就地展开，不再走二级弹窗。"""
        self.assertIn('trackListHtml', self.JS)
        self.assertIn('data-items-for', self.JS, '要有可就地展开的明细行')
        self.assertNotIn('data-aitems', self.JS,
                         '不该再有"曲目"按钮打开二级弹窗')
        self.assertNotIn('function itemsDialog', self.JS,
                         'itemsDialog 已废弃，应删除')

    def test_inline_details_show_cover_artist_album_and_folder(self):
        """明细要够直观：封面、作者、专辑、体积，以及"现在放在哪"。"""
        seg = self._fn('function trackListHtml')
        self.assertIn('UI.coverHtml', seg, '要显示封面缩略图')
        for field in ('t.artist', 't.album', 't.path', 't.size', 't.name'):
            self.assertIn(field, seg, f'明细里应显示 {field}')
        self.assertIn('当前目录', seg, '要标出这首歌当前所在目录')

    def test_details_mark_what_will_move(self):
        seg = self._fn('function trackListHtml')
        self.assertIn('将移动', seg)
        self.assertIn('已在目标目录', seg)

    def test_expand_toggles_hidden(self):
        """展开按钮要真的切换显隐，并且能同时展开多组对比。

        ★ 契约已更新：箭头**不再是"给 SVG 加 class 让 CSS 旋转"**，
        而是**直接替换文本字符**（▸ / ▾）。

        为什么改：用户反复反馈那个箭头会"从左往右滑过来"、而且**失焦时出现**。
        前几轮把过渡一条条去掉都没根治（`.btn` 的 transform 过渡、
        `.og-exp .ico` 的 transition:none、`!important` 钉死……）。
        旋转这条路一直有残留，索性换成纯文本箭头 —— 没有 transform、
        没有 transition，动画在物理上就不可能存在。
        """
        seg = self.JS[self.JS.index("planBox.querySelectorAll('[data-aexp]')"):]
        seg = seg[:900]
        self.assertIn("tr.hidden = open", seg, "展开/收起的显隐切换不能丢")
        self.assertIn(".oga", seg, "箭头应该是文本字符 .oga")
        self.assertIn("textContent", seg, "箭头要直接换字符")
        self.assertNotIn("classList.toggle('open'", seg,
                         "不要再回到'加 class 让 CSS 旋转'那条路（那个会带动画）")

    def test_no_unconditional_transform_transition(self):
        """基础规则里**不许**出现无条件的 `transform` 过渡。

        这个坑踩过三次（`.btn`、`.tile .t-actions .btn`、`.stat`/`.tile`）：
        写 `transition: transform .Xs` 时只想给 `:hover` / `:active` 的位移服务，
        但浏览器会对该元素**任何** transform 变化做动画 ——
        包括"被父级布局带着走"。表现就是**箭头/按钮会自己滑过去**。

        正当用法：写在 `:hover` / `:active` 之类的伪类里；
        或确实需要整体位移的容器（这里只放行 `.sidebar` 的窄屏滑入）。
        """
        import pathlib
        import re
        # 注意：本模块里的 `ROOT` 是**库根路径**常量（'D:/M'），不是项目根，
        # 所以这里从 __file__ 自己算。
        proj = pathlib.Path(__file__).resolve().parent.parent
        css = (proj / "app" / "web" / "static" / "css" / "app.css").read_text(
            encoding="utf-8")
        offenders = []
        for m in re.finditer(r"^([^\n{}]+)\{([^}]*)\}", css, re.M):
            sel, body = m.group(1).strip(), m.group(2)
            if not re.search(r"transition:[^;]*\btransform\b", body):
                continue
            if re.search(r":(hover|active|focus|disabled|open)", sel):
                continue
            if sel in (".sidebar",):
                continue
            offenders.append(sel)
        self.assertEqual([], offenders,
                         f"基础规则里的 transform 过渡会让被动移动也播动画：{offenders}")


    def test_is_chosen_does_not_use_outer_local(self):
        """`isChosen` 不许引用 paintPlan 里的局部变量。

        踩过：写成 `keysOf(a).some((k) => keys.has(k))`，而 `keys` 是
        paintPlan 内部的 const —— 直接报 `keys is not defined`，
        整个方案区渲染不出来（浏览器实测抓到的）。
        """
        seg = self._fn('const isChosen')
        self.assertNotIn('keys.has(', seg,
                         'isChosen 里不能用外层的 keys 变量')
        self.assertIn('only', seg, '应基于 only 集合判断')

    def test_expand_selector_escapes_key(self):
        """组键里可能带引号/反斜杠，属性选择器必须转义，否则会抛异常。"""
        self.assertIn('function cssEsc', self.JS)
        seg = self._fn('function cssEsc')
        self.assertIn('replace', seg)
        self.assertIn('cssEsc', self.JS[self.JS.index('[data-aexp]'):])

    def test_items_derive_folder_from_path(self):
        """plan 的 items 里没有 folder 字段，当前目录必须从 path 截出来。

        （这条踩过：一开始直接读 `t.folder`，结果整个"当前目录"列是空的。）
        """
        seg = self._fn('function trackListHtml')
        self.assertIn('folderOf', seg)
        self.assertNotIn('t.folder', seg,
                         'items 里没有 folder 字段，不能直接读')


class SameDestinationMergeTest(unittest.TestCase):
    """同一目标目录的组必须合并成一行。

    真实反馈引出的问题：`artist_album` 的分组键是"作者+专辑"，但一首曲目的
    `artist` 标签经常不是唯一作者 —— `Riya`、`fizzd; Riya`、`celesti whispers`
    都在同一张 Rhythm Doctor OST 里，`Minecraft_ Lena Raine` 和
    `Minecraft_ Aaron Cherof` 都在同一张 Tricky Trials 里。

    于是同一张专辑被拆成十几个组，界面上同一个目标目录出现十几行、每行一两首，
    看着像算法坏了。实测最夸张的一个目标目录被 **107 个组**指向。
    """
    ROOT = 'D:/M'

    def _plan(self, rows, **kw):
        kw.setdefault('min_group', 5)
        return OG.plan_by_album(rows, self.ROOT, **kw)

    def test_same_destination_collapses_to_one_row(self):
        """同一作者目录下、专辑各不相同的曲目 → 只应有一行。"""
        rows = []
        for ai, alb in enumerate(['专辑一', '专辑二', '专辑三']):
            rows.append(_t(f'D:/M/散/{alb}/a{ai}.flac', 'C418', alb))
        p = self._plan(rows, group_by='artist_album')
        dsts = [a['dst'] for a in p['actions']]
        self.assertEqual(len(dsts), len(set(dsts)),
                         f'同一目标目录不该出现多行：{dsts}')

    def test_varying_artist_tags_do_not_fragment_album(self):
        """同一张专辑、作者标签写法不一 → 必须合并成一行。

        这是实测最典型的情况：一张 OST 有很多作曲者。
        """
        rows = []
        for i, art in enumerate(['fizzd', 'fizzd; Riya', 'Riya', 'celesti whispers']):
            for j in range(2):
                rows.append(_t(f'D:/M/散/x{i}{j}.flac', art,
                               'Rhythm Doctor OST'))
        rows.append(_t('D:/M/Rhythm Doctor OST/FLAC/keep.flac', 'fizzd',
                       'Rhythm Doctor OST'))
        p = self._plan(rows, group_by='artist_album')
        targets = [a for a in p['actions'] if a['dst']]
        self.assertEqual(1, len(targets),
                         f'一张专辑只该有一行，实际 {[(a["dst"], a["album"]) for a in targets]}')
        a = targets[0]
        # 8 首散落 + 1 首已在目标目录 = 9（already 是共享的，也算在 total 里）
        self.assertEqual(9, a['total'], '合并后应合计 9 首（含已在位的 1 首）')
        self.assertEqual(8, a['count'], '其中 8 首需要移动')
        self.assertEqual(1, a['already'])

    def test_merged_row_keeps_all_source_keys(self):
        """合并行要记住**全部**来源键。

        否则"只执行勾选的组"会漏搬 —— 因为按单个 key 过滤时，
        合并进来的其他键的曲目会被丢掉。
        """
        rows = [_t(f'D:/M/散/{i}.flac', art, '同一张专辑')
                for i, art in enumerate(['A', 'B'])]
        # 让目标目录存在，才会真的产生移动
        rows.append(_t('D:/M/同一张专辑/keep.flac', 'A', '同一张专辑'))
        p = self._plan(rows, group_by='artist_album')
        acts = [a for a in p['actions'] if a['dst']]
        self.assertEqual(1, len(acts))
        keys = acts[0].get('keys') or []
        self.assertEqual(2, len(keys), f'应保留两个来源键，实际 {keys}')
        self.assertEqual(2, acts[0]['count'])

    def test_merged_row_sums_counts_and_reports_origin(self):
        rows = []
        for i, art in enumerate(['A', 'B', 'C']):
            for j in range(2):
                rows.append(_t(f'D:/M/散/{i}{j}.flac', art, '同一张专辑'))
        rows.append(_t('D:/M/同一张专辑/keep.flac', 'A', '同一张专辑'))
        p = self._plan(rows, group_by='artist_album')
        a = [x for x in p['actions'] if x['dst']][0]
        self.assertEqual(6, a['count'])
        self.assertEqual(7, a['total'])
        self.assertIn('merged_from', a)
        self.assertIn('归并', a['why'],
                      '合并行要说明是多个分组归并来的，不能只留一条原始依据')

    def test_scattered_tracks_are_collected_into_existing_folder(self):
        """合并之后仍要能把散落的曲目收进已存在的目标目录。

        （这是这次改动的**核心行为**，别为了合并显示把它弄丢了。）
        """
        rows = [_t(f'D:/M/散/{i}.flac', art, '专辑甲')
                for i, art in enumerate(['A', 'B', 'C'])]
        rows.append(_t('D:/M/专辑甲/keep.flac', 'A', '专辑甲'))
        p = self._plan(rows, group_by='artist_album')
        self.assertEqual(3, p['summary']['move'])
        for m in p['moves']:
            self.assertEqual('专辑甲', m['dst_dir'])

    def test_no_duplicate_destinations_in_summary(self):
        """汇总层面也不能出现重复目标目录。"""
        rows = []
        for i, art in enumerate(['A', 'B', 'C', 'D']):
            rows.append(_t(f'D:/M/散/{i}.flac', art, '同一张专辑'))
        p = self._plan(rows, group_by='artist_album')
        dsts = [a['dst'] for a in p['actions']]
        self.assertEqual(len(dsts), len(set(dsts)))

    def test_merge_preserves_move_count(self):
        """合并只是换显示，**不能改变实际要搬的曲目数**。"""
        rows = []
        for i, art in enumerate(['A', 'B', 'C']):
            for j in range(3):
                rows.append(_t(f'D:/M/散/{i}{j}.flac', art, f'专辑{i}'))
        rows.append(_t('D:/M/专辑0/keep.flac', 'A', '专辑0'))
        p = self._plan(rows, group_by='artist_album')
        self.assertEqual(3, p['summary']['move'],
                         '只有"专辑0"有现成目录，那 3 首散落的应被收进去')

    def test_merge_keeps_distinct_destinations_separate(self):
        """不同目标目录**不能**被误合并。"""
        rows = ([_t(f'D:/M/散/a{i}.flac', '作者甲', '专辑甲') for i in range(6)]
                + [_t(f'D:/M/散/b{i}.flac', '作者乙', '专辑乙') for i in range(6)])
        p = self._plan(rows, group_by='artist_album')
        dsts = {a['dst'] for a in p['actions'] if a['count']}
        self.assertEqual(2, len(dsts), f'两个作者应各自成目录，实际 {dsts}')

    def test_below_threshold_group_is_left_alone(self):
        """没达到门槛、库里又没有对应目录 → 一律不动。

        刻意锁住：避免以后为了让"合并显示"好看而放宽门槛，
        把用户的合集目录重新洗一遍。
        """
        rows = []
        for i, art in enumerate(['A', 'B', 'C']):
            for j in range(3):
                rows.append(_t(f'D:/M/散/{i}{j}.flac', art, f'专辑{i}'))
        p = self._plan(rows, group_by='artist_album', min_group=5)
        self.assertEqual(0, p['summary']['move'],
                         '每组只有 3 首、库里也没有对应目录，不该动')
        self.assertEqual(9, p['summary']['keep'])

    def test_single_group_is_left_alone(self):
        """没得合并时不要画蛇添足加 merged_from。"""
        rows = [_t(f'D:/M/散/a{i}.flac', '甲', '专辑甲') for i in range(6)]
        p = self._plan(rows, group_by='artist_album')
        a = [x for x in p['actions'] if x['dst']][0]
        self.assertNotIn('merged_from', a)
        self.assertNotIn('归并', a['why'])


class MetadataFirstTest(unittest.TestCase):
    """「按专辑分就以元数据为准」—— 这条必须贯彻到底。

    用户的原话："如果按专辑分 请你以元数据为准；歌曲为同一专辑的就处理，
    歌曲为同一作者的就整理"。真实库里出现的混乱：

      · 仅专辑模式下，`Alone` 被送进 `Marshmello`、
        `BedsideStories` 被送进 `Shiroi-ice`（名字相似度低到 0.09）——
        那是拿**专辑名**去匹配**作者目录**，等于把"按专辑整理"偷偷换成
        "按作者整理"。
      · `similar` 规则拿目录的 `top_album`（"这个目录里最常见的专辑标签"）
        去比，等于自问自答：作者目录 `Ahxello` 里装着 `Forever Away`，
        它的 top_album 就是 `Forever Away`，相似度 1.000，必然"命中"。
      · `City` → `Owl city`、`House` → `Snail's House`：短专辑名去认领
        更具体的目录。
    """
    ROOT = 'D:/M'

    def _plan(self, rows, **kw):
        kw.setdefault('min_group', 5)
        return OG.plan_by_album(rows, self.ROOT, **kw)

    # ---------------------------------------------------------- 名字匹配
    def test_album_mode_never_targets_artist_folder(self):
        """仅专辑模式：专辑名和作者目录名毫不相干时，绝不能送进去。"""
        rows = [_t(f'D:/M/散/a{i}.flac', 'Marshmello', 'Alone') for i in range(6)]
        rows.append(_t('D:/M/Marshmello/existing.flac', 'Marshmello', 'Alone'))
        p = self._plan(rows, group_by='album')
        for m in p['moves']:
            self.assertNotEqual('Marshmello', m['dst_dir'],
                                '按专辑分时不该把歌塞进作者目录')

    def test_album_mode_targets_same_named_album_folder(self):
        rows = [_t(f'D:/M/散/a{i}.flac', 'X', '真实专辑') for i in range(6)]
        rows.append(_t('D:/M/真实专辑/keep.flac', 'X', '真实专辑'))
        p = self._plan(rows, group_by='album')
        self.assertEqual(6, p['summary']['move'])
        for m in p['moves']:
            self.assertEqual('真实专辑', m['dst_dir'])

    def test_album_folder_beats_artist_folder(self):
        """同名专辑目录与同名作者目录同时存在时，必须选专辑目录。"""
        rows = [_t(f'D:/M/散/a{i}.flac', '作者', '专辑') for i in range(6)]
        rows.append(_t('D:/M/专辑/keep.flac', '作者', '专辑'))
        rows.append(_t('D:/M/作者/other.flac', '作者', '别的专辑'))
        p = self._plan(rows, group_by='album')
        for m in p['moves']:
            self.assertEqual('专辑', m['dst_dir'],
                             '按专辑分就该进专辑目录，不是作者目录')

    # ---------------------------------------------------------- 相似度门槛
    def test_alpha_beta_are_not_the_same_album(self):
        """`Volume Alpha` 和 `Volume Beta` 不许互相匹配。

        整体相似度 0.889，光看数字很容易并错。
        """
        self.assertFalse(OG._same_folder_name('Minecraft - Volume Beta',
                                              'Minecraft - Volume Alpha'))
        self.assertFalse(OG._same_folder_name('Minecraft - Volume Alpha',
                                              'Minecraft - Volume Beta'))

    def test_short_album_name_cannot_claim_specific_folder(self):
        """短而泛的专辑名不许认领更具体的目录。"""
        self.assertFalse(OG._same_folder_name('Owl city', 'City'))
        self.assertFalse(OG._same_folder_name("Snail's House", 'House'))

    def test_genuine_typo_still_matches(self):
        """真正的拼写/后缀差异仍要认（不能矫枉过正）。"""
        self.assertTrue(OG._same_folder_name('HOPE-T', 'Hope'))
        self.assertTrue(OG._same_folder_name('Oneshot - Solstice',
                                             'Oneshot_ Solstice'))

    def test_punctuation_differences_are_ignored(self):
        """下划线/连字符/波浪号差异不算不同。"""
        self.assertTrue(OG._same_folder_name('Oneshot - Solstice (OST)',
                                             'Oneshot_ Solstice (OST)'))

    def test_exact_match_always_works(self):
        self.assertTrue(OG._same_folder_name('某专辑', '某专辑'))
        self.assertTrue(OG._same_folder_name('A/B/某专辑', '某专辑'),
                        '要看每一层，不只叶子')

    def test_nested_format_subfolder_still_matches(self):
        """`专辑名/FLAC` 这种结构也要能认出来（只看叶子会拿 FLAC 去比）。"""
        self.assertTrue(OG._same_folder_name('Rhythm Doctor OST/FLAC',
                                             'Rhythm Doctor OST'))

    # ---------------------------------------------------------- 防"自问自答"
    def test_similar_does_not_probe_top_album(self):
        """similar 规则不许拿 `p["album"]` 去比。

        那是"目录里最常见的专辑标签"，拿它比等于自问自答，永远命中。
        """
        from pathlib import Path
        src = _PROJ.joinpath('app', 'core', 'organize.py').read_text(encoding='utf-8')
        i = src.index('def similar_folder')
        seg = src[i:i + 2600]
        # 只看代码，跳过解释性 docstring（里面会提到这件事本身）
        body = seg[seg.index('"""', seg.index('"""') + 3) + 3:]
        self.assertNotIn('p["album"]', body,
                         'similar_folder 不许探测 p["album"]（自问自答）')
        self.assertIn('segs[-1]', body, '应只比目录名本身')

    def test_artist_folder_with_matching_album_tag_is_not_a_target(self):
        """作者目录里的歌恰好都标着这张专辑时，不该就认这个作者目录。

        （这正是自问自答 bug 的真实形态：`Ahxello` 里装着 `Forever Away`。）
        """
        rows = [_t(f'D:/M/散/a{i}.flac', 'Ahxello', 'Forever Away')
                for i in range(6)]
        rows.append(_t('D:/M/Ahxello/one.flac', 'Ahxello', 'Forever Away'))
        p = self._plan(rows, group_by='album')
        for m in p['moves']:
            self.assertNotEqual('Ahxello', m['dst_dir'],
                                '不该因为标签相同就认作者目录')

    def test_existing_same_named_folder_is_never_missed(self):
        """库里**确实存在**同名专辑目录时，必须选它 —— 别把规则收得太紧。

        （收紧 `_same_folder_name` 和 `similar_folder` 之后专门加的一条：
          防止"挡住了误报、也挡住了正确结果"。）
        """
        rows = []
        # 一批专辑，磁盘上都有同名目录
        for n in ('专辑甲', '专辑乙', 'Summer Pockets Original SoundTrack',
                  'RHYTHM DOCTOR OST', 'Oneshot - Solstice'):
            for i in range(6):
                rows.append(_t(f'D:/M/散/{n}{i}.flac', '作者', n))
            rows.append(_t(f'D:/M/{n}/keep.flac', '作者', n))
        p = self._plan(rows, group_by='album')
        for a in p['actions']:
            if not a['count']:
                continue
            self.assertEqual('album_exact', a['kind'],
                             f"库里已有 {a['album']!r} 目录，却判成 {a['kind']}")
            self.assertTrue(OG._same_folder_name(a['dst'], a['album']))

    def test_typo_tolerance_still_finds_the_folder(self):
        """拼写有小差异时仍要能认出来（别把容错一起砍掉）。"""
        rows = [_t(f'D:/M/散/a{i}.flac', 'X', 'Hope') for i in range(6)]
        rows.append(_t('D:/M/HOPE-T/keep.flac', 'X', 'Hope'))
        p = self._plan(rows, group_by='album')
        dsts = {m['dst_dir'] for m in p['moves']}
        self.assertIn('HOPE-T', dsts, 'Hope 应该被认成 HOPE-T')

    def test_no_folder_name_conflicts_in_album_mode(self):
        """仅专辑模式下，所有目标目录名都必须与专辑名相符（除新建外）。"""
        rows = []
        pairs = [('Marshmello', 'Alone'), ('Little End', 'Everything'),
                 ('SmK', 'Memory'), ('plum', 'Dreams'), ('JJD', 'Adventure')]
        for art, alb in pairs:
            for i in range(3):
                rows.append(_t(f'D:/M/散/{art}{i}.flac', art, alb))
            rows.append(_t(f'D:/M/{art}/keep.flac', art, alb))
        p = self._plan(rows, group_by='album')
        for a in p['actions']:
            if not a['count'] or a['kind'] == 'new':
                continue
            want = a.get('album') or ''
            self.assertTrue(
                OG._same_folder_name(a['dst'], want),
                f"目标 {a['dst']!r} 与专辑 {want!r} 不符（{a['kind']}）")


class PlanByAlbumSignatureTest(unittest.TestCase):
    """`plan_by_album` 的签名契约 —— 这个坑踩了三次，必须锁住。

    项目里到处都想表达"两层/单层"这个选择，很容易顺手写成
    `structure="nested"`，但 `plan_by_album` 的参数叫 `keep_flat_below`。
    写错的后果是**运行时 TypeError**（编译期看不出来）：
    `search_api` 的 plan-paths、`station_api` 的 suggest 都栽过，
    表现是接口 500，而且只有真正调用到那行才暴露。
    """

    def test_only_known_keys_are_accepted(self):
        import inspect
        from app.core import organize as OG
        params = set(inspect.signature(OG.plan_by_album).parameters)
        for bad in ("structure", "flat", "nested"):
            self.assertNotIn(bad, params,
                             f"plan_by_album 没有 {bad} 参数")
        # 真正表达"层数"的参数
        self.assertIn("keep_flat_below", params)
        self.assertIn("group_by", params)
        self.assertIn("folder_pool_rows", params)

    def test_no_caller_passes_structure(self):
        """全项目扫一遍：不许有人再给 plan_by_album 传 structure=。

        这个坑踩了**三次**（search_api 的 plan-paths、station_api 的 suggest、
        upload_api 的 _suggest_for），每次都是运行时 TypeError → 接口 500，
        而且只有真正调用到那行才暴露。所以这里做成源码级守卫。

        只认"真正的关键字实参"（前面不是标识符字符）；
        `keep_flat_below=(structure == "nested")` 这种是合法的取值表达式，
        不能误判。
        """
        import re
        from pathlib import Path
        root = _PROJ.joinpath('app')
        bad = []
        for p in root.rglob("*.py"):
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"(?<![\w.])(?:_?plan_by_album(?:_impl)?)\(", src):
                i = m.end()
                depth = 1
                while i < len(src) and depth:
                    if src[i] == "(":
                        depth += 1
                    elif src[i] == ")":
                        depth -= 1
                    i += 1
                call = src[m.start():i]
                # 「真正的关键字实参」= structure= 前面不是标识符/点号
                if re.search(r"(?<![\w.])structure\s*=", call):
                    line = src[:m.start()].count("\n") + 1
                    bad.append(f"{p.name}:{line}")
        self.assertEqual([], bad,
                         f"这些地方给 plan_by_album 传了不存在的 structure=: {bad}")

    def test_plan_by_album_accepts_the_real_kwargs(self):
        """按真实调用方式跑一次，确认参数名对得上（防回归）。"""
        from app.core import organize as OG
        rows = [_t(f"D:/M/散/a{i}.flac", "作者", "专辑") for i in range(6)]
        p = OG.plan_by_album(rows, "D:/M", min_group=5,
                             group_by="artist_album",
                             keep_flat_below=True,
                             folder_pool_rows=rows)
        self.assertIn("actions", p)


if __name__ == '__main__':
    unittest.main()
