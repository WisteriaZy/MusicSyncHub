"""搜索+分析（/api/organize/analyze）的回归测试。

这是"排错分配"的核心：给一首歌，在**库里现有的文件夹**之间找它该去的地方，
并给出证据。锁住的几个坑都来自真实库：

* `Beginning - C418.flac` 的 album 标签错写成 Volume Beta，但歌其实在
  Volume Alpha（同目录 23 首同作者）；工具必须看出"它已经在同作者群里"，
  不能按错的标签把它搬走。
* `K-391_Alan Walker_..._Play（白井冰Shiroi-Ice remix） - Shiroi-Ice.flac`
  的 artist 标签是原作者名单，但它是 Shiroi-Ice 的 remix，放 `Shiroi-ice/`
  是对的 —— 派生作品不能按 artist 判断。
* 用户的 `Vicetone/` 里躺着 8 首 Vexento 的歌（目录名拼错了），
  判断"同作者聚集"必须容忍拼写差异。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient


def _rec(rel, name, artist, album='', title=None, size=1000):
    return {
        'source_id': 's1', 'path': f'D:/M/{rel}', 'rel_path': rel, 'name': name,
        'parent': 'D:/M/' + rel.rsplit('/', 1)[0] if '/' in rel else 'D:/M',
        'ext': name.rsplit('.', 1)[-1], 'size': size, 'mtime': 1.0,
        'title': title if title is not None else name.rsplit('.', 1)[0],
        'artist': artist, 'album': album, 'duration': 100.0, 'bitrate': 320000,
        'sample_rate': 44100, 'channels': 2, 'bits_per_sample': 16,
        'codec': 'FLAC', 'is_lossless': 1, 'has_cover': 0, 'quality': 50.0,
        'hash_quick': None, 'hash_full': None, 'hash_state': 'none',
    }


class AnalyzeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='msh-analyze-')
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

    def _load(self, recs):
        from app.core import matching as M
        from app.main import db
        with db().write() as conn:
            conn.execute("DELETE FROM track WHERE source_id='s1'")
        out = []
        for r in recs:
            r = dict(r)
            r['dup_key'] = M.dup_key(r)
            r['name_key'] = str(r.get('title') or '').lower()
            out.append(r)
        db().upsert_tracks(out)

    def _analyze(self, **params):
        r = self.client.get('/api/organize/analyze', params={'sid': 's1', **params})
        return r.status_code, (r.json() if r.content else {})

    # ------------------------------------------------------------ 标签冲突
    def test_album_tag_conflict_is_flagged_not_auto_moved(self):
        """专辑标签写错、但歌已在同作者群里 → 判"位置正确"，并提示冲突。"""
        recs = [_rec(f'Minecraft - Volume Alpha/曲{i}.flac', f'曲{i}.flac',
                     'C418', 'Minecraft - Volume Alpha') for i in range(23)]
        recs.append(_rec('Minecraft - Volume Alpha/Beginning - C418.flac',
                         'Beginning - C418.flac', 'C418', 'Minecraft - Volume Beta',
                         title='Beginning'))
        recs += [_rec(f'Minecraft - Volume Beta/b{i}.flac', f'b{i}.flac', 'C418',
                      'Minecraft - Volume Beta') for i in range(30)]
        self._load(recs)
        st, d = self._analyze(path='D:/M/Minecraft - Volume Alpha/Beginning - C418.flac')
        self.assertEqual(200, st, d)
        self.assertEqual('ok', d['verdict']['state'], d['verdict'])
        # 冲突要如实说出来，而不是假装没问题
        self.assertIn('conflict', d['verdict'], d['verdict'])
        self.assertEqual('Minecraft - Volume Beta', d['verdict']['conflict']['folder'])

    def test_search_prefers_exact_title_over_substring(self):
        """搜 "Beginning" 要优先返回曲名正好叫 Beginning 的那首。"""
        recs = [
            _rec('某人/Courage - Ahrix.flac', 'Courage - Ahrix.flac', 'Ahrix',
                 'Beginning of the Journey'),
            _rec('Minecraft - Volume Alpha/Beginning - C418.flac',
                 'Beginning - C418.flac', 'C418', 'Minecraft - Volume Alpha',
                 title='Beginning'),
        ]
        self._load(recs)
        st, d = self._analyze(q='Beginning')
        self.assertEqual(200, st, d)
        self.assertEqual('Beginning - C418.flac', d['track']['name'])
        self.assertTrue(d.get('also_matched'), '应把其余匹配列出来供切换')

    # ------------------------------------------------------------ 派生作品
    def test_remix_is_kept_in_the_remixers_folder(self):
        """remix 的 artist 是原作者，但归属看文件名署名。"""
        recs = [_rec(f'Shiroi-ice/s{i}.flac', f's{i}.flac', 'Shiroi-Ice', f'专辑{i}')
                for i in range(10)]
        recs.append(_rec(
            'Shiroi-ice/K-391_Alan Walker_Tungevaag_Mangoo-Play（白井冰Shiroi-Ice remix） - Shiroi-Ice.flac',
            'K-391_Alan Walker_Tungevaag_Mangoo-Play（白井冰Shiroi-Ice remix） - Shiroi-Ice.flac',
            'K-391/Alan Walker/Tungevaag/Mangoo', 'Play'))
        recs += [_rec(f'K-391/k{i}.flac', f'k{i}.flac', 'K-391', 'Play') for i in range(4)]
        self._load(recs)
        u = ('D:/M/Shiroi-ice/K-391_Alan Walker_Tungevaag_Mangoo-Play'
             '（白井冰Shiroi-Ice remix） - Shiroi-Ice.flac')
        st, d = self._analyze(path=u)
        self.assertEqual(200, st, d)
        self.assertEqual('ok', d['verdict']['state'], d['verdict'])
        self.assertTrue(d['track']['is_derivative'])
        self.assertEqual('Shiroi-Ice', d['track']['filename_artist'])

    # ------------------------------------------------------------ 拼写差异
    def test_cohort_matching_tolerates_misspelled_folder(self):
        """目录名拼错（Vicetone vs Vexento）时，"同作者聚集"仍要认出来。"""
        recs = [_rec(f'Vicetone/v{i}.flac', f'v{i}.flac', 'Vexento', f'专辑{i}')
                for i in range(8)]
        self._load(recs)
        st, d = self._analyze(path='D:/M/Vicetone/v0.flac')
        self.assertEqual(200, st, d)
        self.assertEqual('ok', d['verdict']['state'], d['verdict'])

    # ------------------------------------------------------------ 收纳区
    def test_sink_membership_is_reported(self):
        recs = [_rec(f'单曲01/各种{i}.flac', f'各种{i}.flac', f'作者{i}', f'专辑{i}')
                for i in range(6)]
        self._load(recs)
        st, d = self._analyze(path='D:/M/单曲01/各种0.flac')
        self.assertEqual(200, st, d)
        self.assertEqual('sink', d['verdict']['state'], d['verdict'])

    # ------------------------------------------------------------ 找候选
    def test_candidates_are_ranked_with_evidence(self):
        """混进别人目录的歌，要能被指向"名字对得上"的那个目录。"""
        recs = [_rec(f'别人的目录/x{i}.flac', f'x{i}.flac', '别的歌手', '别的专辑')
                for i in range(5)]
        recs += [_rec(f'周杰伦/范特西/z{i}.flac', f'z{i}.flac', '周杰伦', '范特西')
                 for i in range(6)]
        recs.append(_rec('别人的目录/走错门.flac', '走错门.flac', '周杰伦', '范特西'))
        self._load(recs)
        st, d = self._analyze(path='D:/M/别人的目录/走错门.flac')
        self.assertEqual(200, st, d)
        self.assertTrue(d['candidates'], d)
        top = d['candidates'][0]
        self.assertEqual('周杰伦/范特西', top['folder'])
        self.assertGreaterEqual(top['score'], 4, top)
        self.assertTrue(top['why'])

    def test_missing_argument_is_rejected(self):
        st, _d = self._analyze()
        self.assertEqual(400, st)

    def test_unknown_path_is_404(self):
        self._load([_rec('A/a.flac', 'a.flac', 'x', 'y')])
        st, _d = self._analyze(path='D:/M/不存在/没有这首歌.flac')
        self.assertEqual(404, st)


if __name__ == '__main__':
    unittest.main()
