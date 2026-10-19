"""「智能整理」组内单曲勾选的回归测试。

需求原话："智能整理功能对每个组别整理的单个音乐也要支持勾选"

分组（按专辑/作者）后，"这一组"常常比用户想动的范围大 ——
一张合集里可能只想归位其中 3 首。原来只能整组要或整组不要。
现在每首歌一个复选框。

这条链有三段，都要成立：
  1. 前端：组内有逐曲复选框 + 组内全选/全不选
  2. 前端：取消的曲目要真的从 plan 里剔掉（**不是只灰掉**）
  3. 后端：`only` + `plan.actions[].items` 决定最终移动哪些（复用它已有的机制）
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent

ROOT = _PROJ
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .srchelp import fn_body  # noqa: E402


JS = (ROOT / "app" / "web" / "static" / "js").read_text  # noqa: E731


class OrganizePerTrackUiTest(unittest.TestCase):
    """1+2. 前端：逐曲勾选存在，且取消的曲目会被剔出计划。"""

    @classmethod
    def setUpClass(cls):
        cls.O = (ROOT / "app" / "web" / "static" / "js"
                 / "organize.js").read_text(encoding="utf-8")

    def test_track_checkbox_per_row(self):
        """每一行曲目都要有复选框。"""
        seg = fn_body(self.O, "trackListHtml")
        self.assertTrue(seg, "找不到 trackListHtml")
        self.assertIn("data-ogtrk=", seg)
        self.assertIn('type="checkbox"', seg)
        # 已取消的行要看得出来
        self.assertIn("excluded.has(", seg)

    def test_group_level_bulk_buttons(self):
        """组内全选 / 全不选 —— 组大时一首首点太累。"""
        seg = fn_body(self.O, "trackListHtml")
        self.assertIn("data-ogtrkall=", seg)
        self.assertIn("data-ogtrknone=", seg)

    def test_handlers_registered(self):
        seg = fn_body(self.O, "paintPlan")
        self.assertIn("[data-ogtrk]", seg)
        self.assertIn("[data-ogtrkall]", seg)
        self.assertIn("[data-ogtrknone]", seg)

    def test_exclusion_is_keyed_by_path_not_index(self):
        """★ 用 path 做键，不用下标。

        重新规划后 items 顺序可能变；用下标会错位到别的曲目上 ——
        用户"取消了一首"结果动了另一首，那是最坏的一类 bug。
        """
        seg = fn_body(self.O, "trackListHtml")
        self.assertIn("String(t.path", seg)
        self.assertIn("excluded", self.O)

    def test_excluded_tracks_removed_from_plan(self):
        """★ 关键：只在界面上灰掉是不够的，必须真的从 plan 里剔掉。

        后端拿的是整份 plan；不剔掉的话"我明明取消了这 3 首"照样会被搬走。
        """
        seg = fn_body(self.O, "run")
        self.assertTrue(seg, "找不到 run")
        self.assertIn("effPlan", seg)
        self.assertIn("excluded.has(", seg)
        # moves 和 actions[].items 都要同步 —— 后端按 items 反推 keep_paths
        self.assertIn("moves: keep", seg)
        self.assertIn("items:", seg)
        # 送出去的必须是 effPlan，不能还是原 plan
        self.assertIn("plan: effPlan", seg)

    def test_group_row_shows_excluded_count(self):
        """组行要标出"这组里有 N 首被单独取消"，否则看不出差别。"""
        seg = fn_body(self.O, "paintPlan")
        self.assertIn("offN", seg)


class OrganizeBackendOnlyTest(unittest.TestCase):
    """3. 后端：`only` + `items` 真的能收窄移动集合。"""

    @classmethod
    def setUpClass(cls):
        cls.S = (ROOT / "app" / "search_api.py").read_text(encoding="utf-8")

    def test_only_filters_by_items(self):
        seg = fn_body(self.S, "organize_album_apply")
        self.assertTrue(seg)
        self.assertIn("keep_paths", seg)
        self.assertIn("a.get(\"items\")", seg)
        self.assertIn("moves = [mv for mv in moves", seg)

    def test_merged_keys_are_honoured(self):
        """★ 合并组要按**全部来源键**判断，只看 `key` 会"勾了却没全搬"。"""
        seg = fn_body(self.S, "organize_album_apply")
        self.assertIn("a.get(\"keys\")", seg)
        self.assertIn("akeys & keys", seg)

    def test_real_narrowing(self):
        """真跑一遍：从 plan 里剔掉一首，后端就不再移动它。"""
        import importlib
        import os as _os
        import tempfile

        tmp = tempfile.mkdtemp(prefix="msh-pertrack-")
        _os.environ["MSH_DATA_DIR"] = tmp
        _os.environ["MSH_DB"] = _os.path.join(tmp, "i.sqlite3")
        root = _os.path.join(tmp, "M").replace("\\", "/")
        _os.makedirs(root, exist_ok=True)
        try:
            from app.core.config import (AppConfig, Settings, SourceConfig,
                                         load_config, save_config)
            save_config(AppConfig(
                sources=[SourceConfig(id="s1", name="t", type="local",
                                      root=root)],
                settings=Settings(
                    station_dir=_os.path.join(tmp, "st"),
                    staging_dir=_os.path.join(tmp, "up"))))
            load_config(force=True)
            from fastapi.testclient import TestClient

            from app.main import app
            c = TestClient(app)

            plan = {
                "moves": [
                    {"path": f"{root}/散/a.mp3", "name": "a.mp3",
                     "dst_dir": "专辑X", "dst_path": f"{root}/专辑X/a.mp3",
                     "size": 100, "from_dir": f"{root}/散"},
                    {"path": f"{root}/散/b.mp3", "name": "b.mp3",
                     "dst_dir": "专辑X", "dst_path": f"{root}/专辑X/b.mp3",
                     "size": 100, "from_dir": f"{root}/散"},
                ],
                "actions": [{
                    "key": "k1", "keys": ["k1"], "album": "专辑X",
                    "dst": "专辑X", "count": 2,
                    "items": [{"path": f"{root}/散/a.mp3"},
                              {"path": f"{root}/散/b.mp3"}],
                }],
                "summary": {"total": 2, "move": 2},
            }
            # 用户取消了 b.mp3 → 前端会把它从 moves 和 items 里都剔掉
            eff = {
                "moves": [plan["moves"][0]],
                "actions": [{**plan["actions"][0],
                             "items": [{"path": f"{root}/散/a.mp3"}]}],
                "summary": {"total": 2, "move": 1},
            }
            r = c.post("/api/organize/album-apply", json={
                "source": "s1", "only": ["k1"], "execute": False,
                "plan": eff,
            })
            self.assertEqual(200, r.status_code, r.text)
            j = r.json()
            paths = [x.get("src") for x in (j.get("results") or [])]
            src = " ".join(str(p) for p in paths)
            self.assertIn("a.mp3", src, f"勾选的那首要处理：{paths}")
            self.assertNotIn("b.mp3", src, f"取消的那首不该被处理：{paths}")
        finally:
            try:
                from app.sources import manager
                manager.close_all()
            except Exception:
                pass
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
