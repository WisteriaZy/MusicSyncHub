"""模糊搜索与智能整理的回归测试。

搜索那几个用例锁住的是实际踩过的坑：
* 用整个文件名去比短查询会被拉低（"青花词" vs "周杰伦 - 青花瓷.flac" 只有 0.27），
  所以必须按字段分别打分；
* 目录名会被整目录的歌共享，路径命中不能压过歌名/作者命中。

整理那几个用例锁住的是"别把整理变成破坏"：
* 上一版按模板硬算，对 2301 首的精选集库给出"移动 2301 首、新建 1495 个目录"
  的方案 —— 每个作者一个目录再套"单曲"子目录，纯粹是制造混乱。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.core import organize as OG
from app.core import paths as P


class FuzzyMatchTest(unittest.TestCase):

    def test_exact_substring_wins(self):
        self.assertEqual(1.0, P.fuzzy_match('晴天', '01 - 周杰伦 - 晴天 (320k).flac'))

    def test_ignores_track_number_and_quality_noise(self):
        # 归一化会去掉 "01 -" 与 "(320k)"，所以搜歌名照样命中
        self.assertGreater(P.fuzzy_match('晴天', '01 - 周杰伦 - 晴天 (320k).flac'), 0.9)

    def test_multi_word_query(self):
        self.assertGreater(P.fuzzy_match('周杰伦 晴天', '01 - 周杰伦 - 晴天.flac'), 0.9)

    def test_typo_tolerated_on_short_field(self):
        """短字段上的错别字要能命中（这是"按字段打分"的理由）。"""
        self.assertGreater(P.fuzzy_match('青花词', '青花瓷'), 0.6)
        # 但在长文件名上直接比就会掉到阈值以下——所以搜索接口是分字段调的
        self.assertLess(P.fuzzy_match('青花词', '周杰伦 - 青花瓷.flac'), 0.6)

    def test_abbreviation_subsequence(self):
        """拼音首字母缩写：zjl → zhoujielun（子序列匹配）。"""
        self.assertGreater(P.fuzzy_match('zjl', 'zhoujielun - qingtian.flac'), 0.4)
        # 更长的拉丁缩写也认（要求 ≥4 个字符，避免短词到处乱命中）
        self.assertGreater(P.fuzzy_match('adele', 'adele hello.flac'), 0.4)

    def test_short_latin_query_does_not_match_unrelated_long_name(self):
        """短拉丁查询不能因为"沾边"就命中长文件名（'adele' vs 'bohemian'）。"""
        self.assertEqual(0.0, P.fuzzy_match('adele', '04 - Queen - Bohemian.flac'))

    def test_no_false_positive(self):
        self.assertEqual(0.0, P.fuzzy_match('完全不相干的东西', '周杰伦 - 晴天.flac'))
        self.assertEqual(0.0, P.fuzzy_match('zzzzz', '周杰伦 - 晴天.flac'))

    def test_empty_query_matches_nothing(self):
        self.assertEqual(0.0, P.fuzzy_match('', 'anything'))
        self.assertEqual(0.0, P.fuzzy_match('   ', 'anything'))
        self.assertEqual(0.0, P.fuzzy_match('x', ''))


def _t(path, artist, album, size=1000, title=None, name=None):
    n = name or path.rsplit('/', 1)[-1]
    return {
        'source_id': 's1', 'path': path, 'rel_path': path, 'name': n,
        'parent': path.rsplit('/', 1)[0], 'size': size, 'mtime': 1.0,
        'title': title if title is not None else n.rsplit('.', 1)[0],
        'artist': artist, 'album': album,
    }


class OrganizePlanTest(unittest.TestCase):
    """智能整理：只修"能证明放错了、且有明确该去的地方"的歌。

    判定三原则：
      · 收纳区（单曲01 / 英语歌精选集 …）里什么都有 → 一律不动
      · 目录名跟歌的作者/专辑对得上 → 位置正确
      · 目录里大多数歌属于同一作者/专辑，这首不是 → 走错门了
    第一条和第三条都是为了不再重演早期版本"按模板重排整个库"（实测会给出
    "移动 2301 首、新建 1495 个目录"）那种破坏性方案。
    """
    ROOT = 'D:/M'

    def _plan(self, rows, **kw):
        return OG.plan_relocate(rows, self.ROOT, **kw)

    def _home(self, rows, artist='周杰伦', album='范特西', n=3,
              home_dir='别人的目录', with_home=True):
        """造出完整场景：一个"别人的目录" +（可选）一个"正确的目录"。

        `home_dir` 里有 n 首**另一个作者**的歌，构成明确的"主流"；
        `with_home=True` 时另外建好 `artist/album`，作为走错门的歌该去的地方。
        测"孤曲策略"时要传 `with_home=False`，否则那些填充歌自己也会被
        当成"谁都不是"的歌，把断言搅乱。
        """
        out = [_t(f'D:/M/{home_dir}/别人的歌{i}.flac', '别的歌手', '别的专辑')
               for i in range(n)]
        if with_home:
            out += [_t(f'D:/M/{artist}/{album}/正主{i}.flac', artist, album)
                    for i in range(n)]
        return out + list(rows)

    # ---------------------------------------------------------- 收纳区
    def test_sink_folders_are_recognized(self):
        """单曲01 / 英语歌精选集 这类目录要自动识别成"故意混放"。"""
        for name in ('单曲01', '单曲', '英语歌精选集', '日语歌精选集', '杂项',
                     '未分类', 'singles', 'Misc', 'Various Artists',
                     '我的合集', '精选集'):
            self.assertTrue(OG.is_sink_name(name), name)
        for name in ('周杰伦', 'HOYO-MiX', '范特西', 'Rhythm Doctor OST'):
            self.assertFalse(OG.is_sink_name(name), name)

    def test_sink_contents_are_never_touched(self):
        """收纳区里的歌一律不动，哪怕它们互相之间毫无关系。"""
        rows = [_t(f'D:/M/单曲01/歌{i}.flac', f'作者{i}', f'专辑{i}') for i in range(10)]
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'], plan['summary'])
        self.assertEqual(10, plan['summary']['keep'])
        self.assertIn('单曲01', plan['summary']['sinks'])

    def test_sink_can_be_specified_manually(self):
        """用户手动指定的收纳区同样不被碰。"""
        rows = [_t(f'D:/M/我的大杂烩/歌{i}.flac', f'作者{i}', f'专辑{i}')
                for i in range(10)]
        plan = self._plan(rows, sinks=['我的大杂烩'])
        self.assertEqual(0, plan['summary']['move'])
        self.assertIn('我的大杂烩', plan['summary']['sinks'])

    # ---------------------------------------------------------- 放对了就不动
    def test_artist_folder_is_respected(self):
        """只按作者分目录（没有专辑子目录）也是对的，不该被"补全"成 作者/专辑。"""
        rows = [_t(f'D:/M/周杰伦/歌{i}.flac', '周杰伦', f'专辑{i}') for i in range(6)]
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'], plan['summary'])

    def test_album_folder_is_respected(self):
        """只按专辑分目录同样是对的。"""
        rows = [_t(f'D:/M/范特西/歌{i}.flac', f'歌手{i}', '范特西') for i in range(6)]
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'])

    def test_correctly_placed_library_is_untouched(self):
        rows = [_t(f'D:/M/周杰伦/范特西/{i}.flac', '周杰伦', '范特西')
                for i in range(6)]
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'])
        self.assertEqual(6, plan['summary']['keep'])

    # ---------------------------------------------------------- 放错了要修
    def test_isolated_track_in_wrong_folder_is_relocated(self):
        """一首周杰伦的歌躺在别人的目录里 → 挪到周杰伦目录。"""
        rows = self._home([_t('D:/M/乱放/走错门.flac', '周杰伦', '范特西')])
        plan = self._plan(rows)
        self.assertEqual(1, plan['summary']['move'], plan['summary'])
        mv = plan['moves'][0]
        self.assertEqual('周杰伦/范特西', mv['dst_dir'])
        self.assertEqual('乱放', mv['from_dir'])

    def test_no_evidence_means_no_move(self):
        """目录里只有一两首、看不出主流，库里也没有对应目录 → 不动。"""
        rows = [_t('D:/M/某人的收藏/一首歌.flac', '周杰伦', '范特西')]
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'], plan['summary'])

    def test_isolated_track_without_home_goes_to_sink(self):
        """放错位置、又没有合适的现成目录 → 收进收纳区（需显式选该策略）。"""
        rows = [_t('D:/M/乱放/孤立歌.flac', '没人听过的歌手', '没人听过的专辑')]
        plan = self._plan(rows, singles_dir='单曲01', orphan_policy='sink')
        self.assertEqual(1, plan['summary']['move'], plan['summary'])
        self.assertEqual('单曲01', plan['moves'][0]['dst_dir'])

    def test_orphan_policy_keep_is_the_default(self):
        """默认策略是"原地不动"：找不到明确去处就不动，工具不凭空造结构。"""
        rows = [_t('D:/M/乱放/孤立歌.flac', '没人听过的歌手', '没人听过的专辑')]
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'], plan['summary'])
        # 但要在报告里如实说出来："这歌看着也可疑，只是没有明确该去哪"
        self.assertEqual(1, plan['summary']['no_home'],
                         '应如实记录"没有明确去处"的歌，而不是假装它没问题')

    def test_existing_sink_is_reused_not_recreated(self):
        """库里已有 `0单曲` 这种收纳区时，要复用它，别再新建一个 `单曲`。"""
        rows = [_t('D:/M/0单曲/别人的歌1.flac', '甲', '一'),
                _t('D:/M/0单曲/别人的歌2.flac', '乙', '二'),
                _t('D:/M/乱放/孤立歌.flac', '没听过的歌手', '没听过的专辑')]
        plan = self._plan(rows, orphan_policy='sink')
        self.assertTrue(plan['moves'], plan['summary'])
        dst = {m['dst_dir'] for m in plan['moves']}
        self.assertEqual({'0单曲'}, dst, dst)

    def test_sink_is_preferred_over_creating_new_folder(self):
        """同组攒得多但用户选了"收进收纳区" → 不新建目录。"""
        rows = [_t(f'D:/M/各处{i}/歌{i}.flac', '某歌手', '') for i in range(8)]
        plan = self._plan(rows, singles_dir='单曲01', orphan_policy='sink')
        dirs = {m['dst_dir'] for m in plan['moves']}
        self.assertEqual({'单曲01'}, dirs, dirs)

    def test_new_folder_suggested_when_group_is_big_enough(self):
        """选了"够 N 首就新建"且同组够多 → 建议新建目录。"""
        rows = [_t(f'D:/M/各处{i}/歌{i}.flac', '某歌手', '某专辑') for i in range(6)]
        plan = self._plan(rows, orphan_policy='new', min_group=5)
        dirs = {m['dst_dir'] for m in plan['moves']}
        self.assertEqual({'某歌手/某专辑'}, dirs, dirs)

    def test_small_group_below_threshold_goes_to_sink(self):
        """选了"新建"但不够门槛 → 退回收纳区。"""
        rows = [_t(f'D:/M/各处{i}/歌{i}.flac', '某歌手', '某专辑') for i in range(3)]
        plan = self._plan(rows, orphan_policy='new', min_group=5, singles_dir='单曲')
        self.assertEqual({'单曲'}, {m['dst_dir'] for m in plan['moves']})

    def test_scattered_group_is_not_shuffled_between_temp_folders(self):
        """散在多个"名字对不上"的目录里时，必须归到**正确命名**的目录。

        不能随便挑一个同组目录就算家 —— 早期版本会产出
        `各处0 → 各处1`、`各处1 → 各处0` 这种原地打转的方案。
        """
        # 文件名不要含专辑名，否则会命中"文件名支持待在这儿"这条规则
        rows = [_t(f'D:/M/各处{i}/歌{i}.flac', '周杰伦', '范特西') for i in range(6)]
        rows = self._home(rows)          # 已有 周杰伦/范特西/ 这个正确目录
        plan = self._plan(rows)
        dirs = {m['dst_dir'] for m in plan['moves']}
        self.assertEqual({'周杰伦/范特西'}, dirs, dirs)
        self.assertNotIn('各处0', dirs)
        self.assertNotIn('各处1', dirs)

    def test_untagged_tracks_are_left_alone(self):
        """完全没标签的歌无从判断对错，不该被乱挪。"""
        rows = [_t('D:/M/某处/无标签.flac', '', '')]
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'])

    # ---------------------------------------------------------- 范围与合并
    def test_scope_limits_where_problems_are_looked_for(self):
        """只在指定目录里找问题，范围外的碰都不碰。"""
        rows = self._home(
            [_t('D:/M/整理区/乱放/走错门.flac', '周杰伦', '范特西'),
             _t('D:/M/范围外/也走错门.flac', '周杰伦', '范特西')],
            home_dir='整理区/乱放')
        plan = self._plan(rows, scopes=['D:/M/整理区'])
        self.assertEqual(1, plan['summary']['move'], plan['summary'])
        self.assertEqual('整理区/乱放', plan['moves'][0]['from_dir'])
        # 范围外那首不该出现在任何动作里
        self.assertNotIn('范围外', str([m['path'] for m in plan['moves']]))

    def test_absolute_scope_is_accepted(self):
        """范围允许传绝对路径（界面上选的就是绝对路径），内部会转成相对。"""
        rows = self._home([_t('D:/M/整理区/乱放/走错门.flac', '周杰伦', '范特西')],
                          home_dir='整理区/乱放')
        rel = self._plan(rows, scopes=['整理区'])
        absolute = self._plan(rows, scopes=['D:/M/整理区'])
        self.assertEqual(rel['summary']['move'], absolute['summary']['move'])
        self.assertEqual(rel['summary']['scopes'], absolute['summary']['scopes'])

    def test_gather_merges_scattered_album(self):
        """同一张专辑被拆到多个目录 → 合并（这是真正值得做的整理）。"""
        rows = [_t(f'D:/M/地{i % 3}/范特西{i}.flac', '周杰伦', '范特西')
                for i in range(6)]
        rows = self._home(rows)
        plan = self._plan(rows, include_gather=True, min_group=5)
        dirs = {m['dst_dir'] for m in plan['moves']}
        self.assertEqual({'周杰伦/范特西'}, dirs, dirs)

    def test_gather_is_separate_from_relocation(self):
        """"合并"和"把放错的歌归位"是两件事，互不干扰。

        构造：一首躺错目录的歌（该归位）+ 一首本该在 `周杰伦/范特西/`
        却跑到 `地9/` 的歌。关掉合并只影响"把散在多处的同组凑一起"，
        归位动作该做还是要做；本来就待在正确目录里的歌则一律不动。
        """
        rows = self._home([
            _t('D:/M/乱放/走错门.flac', '周杰伦', '范特西'),
            _t('D:/M/地9/该回家.flac', '周杰伦', '范特西'),
        ])
        # 合并打开：乱放 和 地9 里的两首都归到 周杰伦/范特西
        on = self._plan(rows, include_gather=True)
        self.assertEqual(2, on['summary']['move'], on['summary'])
        self.assertEqual({'周杰伦/范特西'}, {m['dst_dir'] for m in on['moves']})
        # 合并关掉：「放错位置」那首仍然要归位
        off = self._plan(rows, include_gather=False)
        self.assertGreaterEqual(off['summary']['move'], 1, off['summary'])
        self.assertEqual({'周杰伦/范特西'}, {m['dst_dir'] for m in off['moves']})

    def test_track_already_with_its_artist_cohort_is_never_moved(self):
        """已经在"同作者聚集"的目录里 → 绝不搬走（哪怕专辑标签不一致）。

        真实例子：`Minecraft - Volume Alpha/` 里有 23 首 C418 的歌，其中
        `Beginning - C418.flac` 的 album 标签错写成 Volume Beta。歌其实在
        正确的位置，按标签搬走属于过度干预。
        """
        rows = [_t(f'D:/M/Minecraft - Volume Alpha/曲{i}.flac', 'C418',
                   'Minecraft - Volume Alpha') for i in range(23)]
        rows.append(_t('D:/M/Minecraft - Volume Alpha/Beginning - C418.flac',
                       'C418', 'Minecraft - Volume Beta'))   # 标签写错了
        rows.append(_t('D:/M/Minecraft - Volume Beta/别的.flac', 'C418',
                       'Minecraft - Volume Beta'))
        plan = self._plan(rows)
        self.assertEqual(0, plan['summary']['move'], plan['summary'])
        self.assertIn('同作者的歌', plan['reasons'].get(rows[-2]['path'], ''))

    # ---------------------------------------------------------- 安全
    def test_target_path_is_relative_and_safe(self):
        rows = [_t('D:/M/别的/走错门.flac', '周杰伦', '范特西'),
                _t('D:/M/周杰伦/范特西/1.flac', '周杰伦', '范特西')]
        plan = self._plan(rows)
        self.assertTrue(plan['moves'])
        for m in plan['moves']:
            self.assertFalse(m['dst_dir'].startswith('/'), m['dst_dir'])
            self.assertFalse(m['dst_path'].startswith('/'), m['dst_path'])
            self.assertNotIn('..', m['dst_path'])

    def test_folder_stats_flags_sinks(self):
        rows = [_t('D:/M/单曲01/a.flac', 'x', 'y'),
                _t('D:/M/周杰伦/范特西/b.flac', '周杰伦', '范特西')]
        stats = {s['folder']: s for s in OG.stats_by_folder(rows, self.ROOT)}
        self.assertTrue(stats['单曲01']['sink'])
        self.assertFalse(stats['周杰伦/范特西']['sink'])
        self.assertTrue(stats['周杰伦/范特西']['pure'])


class MergeSuggestionTest(unittest.TestCase):
    """"重复目录"检测：这是"≥5 首合并"真正该管的场景。

    两个陷阱都是被真实误报逼出来的：
      · 同系列不同卷（`星空剧场2` / `星空剧场3`、`上篇` / `下篇`）名字 94% 像，
        但合并等于把两张专辑搅在一起；
      · 同作者的不同专辑（`Minecraft - Nether Update` / `...The Wild Update`、
        `Oneshot - Solstice` / `OneShot`）名字 88~92% 像，但它们是不同的作品。
    宁可漏报也不要误报。
    """
    ROOT = 'D:/M'

    def test_volume_signature_separates_volumes(self):
        self.assertNotEqual(OG.volume_signature('星空剧场2 Astral Theater Vol.2'),
                            OG.volume_signature('星空剧场3 Astral Theater Vol.3'))
        self.assertNotEqual(OG.volume_signature('上篇 Disc1'), OG.volume_signature('下篇 Disc2'))
        self.assertEqual('2', OG.volume_signature('星空剧场2 Astral Theater Vol.2'))
        self.assertEqual('1', OG.volume_signature('上篇 The Flapper Sinthome Disc1'))
        self.assertEqual('1', OG.volume_signature('A Story 01'), '前导零要归一化')
        self.assertEqual(OG.volume_signature('Vicetone'),
                         OG.volume_signature('Vexento'), '都没有卷号')

    def test_distinctive_name_strips_generic_words(self):
        self.assertEqual('minecraft nether',
                         OG.distinctive_name('Minecraft - Nether Update (Original Game Soundtrack)'))
        self.assertEqual('vicetone', OG.distinctive_name('Vicetone'))

    def test_typoed_duplicate_is_reported(self):
        """一眼就是拼错的重复目录要报出来。"""
        rows = ([_t(f'D:/M/Elektornomia/e{i}.flac', 'Elektronomia', f'专辑{i}')
                 for i in range(6)]
                + [_t(f'D:/M/Elektronomia/e{i}.flac', 'Elektronomia', f'专辑{i}')
                   for i in range(4)])
        ms = OG.suggest_folder_merges(rows, self.ROOT)
        self.assertEqual(1, len(ms), ms)
        # 目录名跟标签对得上的那个应该是"保留"的一方
        self.assertEqual('Elektronomia', ms[0]['keep'])
        self.assertEqual('Elektornomia', ms[0]['drop'])

    def test_different_volumes_are_not_reported(self):
        """同系列不同卷不能报成重复目录。"""
        rows = ([_t(f'D:/M/星空剧场2 Astral Theater Vol.2/a{i}.flac', 'HOYO-MiX', 'V2')
                 for i in range(30)]
                + [_t(f'D:/M/星空剧场3 Astral Theater Vol.3/a{i}.flac', 'HOYO-MiX', 'V3')
                   for i in range(25)])
        self.assertEqual([], OG.suggest_folder_merges(rows, self.ROOT))

    def test_different_albums_of_same_artist_are_not_reported(self):
        """同作者的不同专辑不能报成重复目录。"""
        rows = ([_t(f'D:/M/Minecraft - Nether Update (Original Game Soundtrack)/a{i}.flac',
                    'Lena Raine/Minecraft', 'Nether') for i in range(5)]
                + [_t(f'D:/M/Minecraft - The Wild Update (Original Game Soundtrack)/a{i}.flac',
                      'Lena Raine/Minecraft', 'Wild') for i in range(4)])
        self.assertEqual([], OG.suggest_folder_merges(rows, self.ROOT))

    def test_different_artists_with_similar_names_are_not_reported(self):
        """`Vicetone` 与 `Vexento` 是两个不同的音乐人，不该建议合并。"""
        rows = ([_t(f'D:/M/Vicetone/a{i}.flac', 'Vicetone', f'专辑{i}') for i in range(8)]
                + [_t(f'D:/M/Vexento/a{i}.flac', 'Vexento', f'专辑{i}') for i in range(5)])
        self.assertEqual([], OG.suggest_folder_merges(rows, self.ROOT))


class PathGuardTest(unittest.TestCase):
    """接口层的路径校验。

    踩过的坑：越界检查原先放在"拼接到根目录"**之后**，而 `P.join` 会把 `..`
    折叠掉，于是 `../etc` 变成根目录的兄弟目录、检查什么也查不到 —— 直接
    通过了。现在检查在拼接之前做。
    同时要支持相对路径：界面把计划接口返回的 `0单曲` 这种目录名原样回传，
    所以相对写法必须能用。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-pathguard-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        cls.root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(os.path.join(cls.root, "某歌手"), exist_ok=True)
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

    def _plan(self, **body):
        r = self.client.post("/api/organize/plan", json={"source": "s1", **body})
        return r.status_code, r.json() if r.content else {}

    def test_traversal_is_rejected(self):
        """各种越界写法都必须 400，不能静默通过。"""
        bad = [
            {"scopes": ["../etc"]},
            {"scopes": ["../../windows"]},
            {"sinks": ["../../windows"]},
            {"scopes": ["D:/别的盘/音乐"]},
            {"fallback_sink": "../outside"},
            {"scopes": [f"{self.root}/../../windows"]},
        ]
        for body in bad:
            st, d = self._plan(**body)
            self.assertEqual(400, st, f'{body} 竟然通过了: {d}')

    def test_prefix_lookalike_is_rejected(self):
        """`D:/M2` 不是 `D:/M` 的子目录，不能因为字符串前缀相同就放行。"""
        st, _d = self._plan(scopes=[self.root + "Extra"])
        self.assertEqual(400, st)

    def test_relative_and_absolute_paths_both_work(self):
        rel_st, rel = self._plan(scopes=["某歌手"])
        abs_st, absolute = self._plan(scopes=[f"{self.root}/某歌手"])
        self.assertEqual(200, rel_st, rel)
        self.assertEqual(200, abs_st, absolute)
        self.assertEqual(rel["summary"]["scopes"], absolute["summary"]["scopes"])
        self.assertEqual(["某歌手"], rel["summary"]["scopes"])

    def test_trailing_slash_is_tolerated(self):
        st, d = self._plan(scopes=[f"{self.root}/某歌手/"])
        self.assertEqual(200, st, d)
        self.assertEqual(["某歌手"], d["summary"]["scopes"])


if __name__ == '__main__':
    unittest.main()
