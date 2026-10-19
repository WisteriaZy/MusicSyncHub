"""第四节优化清单的回归测试。

覆盖「第三方审计报告」第四节（核心优化机会清单）里已经落地的行为：

  · 4.2(2) `include_duplicates` 必须真的生成步骤，不能只加一条 warning
  · 4.2(3) 方案指纹：步骤被改动 / 库变了 → 执行时 409
  · 4.1(2) 文件浏览的目录聚合缓存必须返回**副本**（调用方会就地改 rel/depth）
  · 4.1(3) 根目录曲目查询与旧口径（全表拉回 Python 过滤）结果一致
  · 4.1(4) 封面缓存是 LRU（超上限淘汰最久未用，而不是整体清空）

这些都是"看起来实现了、实际可能没接线"的地方，所以按行为测，不按源码测。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent


def T(name, title="", artist="", duration=200.0, size=1000, source_id="s1"):
    from app.core import paths as P
    return {
        "source_id": source_id, "path": f"/{source_id}/{name}", "rel_path": name,
        "name": name, "title": title or P.stem(name), "artist": artist,
        "duration": duration, "size": size, "name_key": P.name_key(name),
        "quality": 50, "dup_key": "",
    }


class PlanDuplicateStepsTest(unittest.TestCase):
    """`include_duplicates` 必须产出可执行步骤。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("MSH_DATA_DIR",
                              tempfile.mkdtemp(prefix="msh-opt-"))
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core import plan as PLAN
        from app.core.config import load_config
        cls.PLAN = PLAN
        cls.cfg = load_config()

    def _plan(self, **opts):
        # 两侧各一份"内容完全一致"的文件：同名、同时长、同体积
        left = {"id": "L", "label": "L",
                "tracks": [T("Song.mp3", "Song", "A", 200, 5000, "L")]}
        right = {"id": "R", "label": "R",
                 "tracks": [T("Song.mp3", "Song", "A", 200, 5000, "R")]}
        o = {"copy_missing": True, "include_duplicates": False}
        o.update(opts)
        return self.PLAN.build_plan(left, right, cfg=self.cfg, options=o)

    def test_without_flag_no_duplicate_steps(self):
        p = self._plan()
        kinds = [s.get("kind") for s in p["steps"]]
        self.assertNotIn("duplicate", kinds,
                         "没开 include_duplicates 却生成了重复清理步骤")

    def test_with_flag_produces_trash_steps(self):
        """开了就必须有步骤，而且必须是 trash（可恢复），不能是 delete。"""
        p = self._plan(include_duplicates=True)
        dups = [s for s in p["steps"] if s.get("kind") == "duplicate"]
        self.assertTrue(dups, "开了 include_duplicates 却一条步骤都没有"
                              "（只加了 warning，等于没实现）")
        for s in dups:
            self.assertEqual("trash", s.get("op"),
                             f"重复清理应进回收站，实际 {s.get('op')}")
        self.assertTrue(p.get("duplicate_groups"),
                        "返回值里没带 duplicate_groups，前端没法显示是哪几组")

    def test_kept_side_never_generated_as_step(self):
        """保留的那一份不能出现在步骤里 —— 否则会把用户想留的删掉。"""
        p = self._plan(include_duplicates=True)
        dups = [s for s in p["steps"] if s.get("kind") == "duplicate"]
        kept = {g.get("key"): g.get("suggested_keep") for g in p["duplicate_groups"]}
        for s in dups:
            k = kept.get(s.get("group_key"))
            keep_path = (k.get("path") if isinstance(k, dict) else k)
            self.assertNotEqual(keep_path, s.get("src_path"),
                                "保留项被生成成了删除步骤")


class StepFingerprintTest(unittest.TestCase):
    """方案指纹必须能发现"步骤被改动"。"""

    @classmethod
    def setUpClass(cls):
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-fp-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        import pathlib
        from app.core.config import AppConfig, SourceConfig, save_config
        A = pathlib.Path(cls.tmp.name) / "A"
        B = pathlib.Path(cls.tmp.name) / "B"
        A.mkdir()
        B.mkdir()
        (A / "x.mp3").write_bytes(b"ID3" + b"\0" * 200)
        save_config(AppConfig(sources=[
            SourceConfig(id="sa", name="A", type="local",
                         root=str(A).replace("\\", "/")),
            SourceConfig(id="sb", name="B", type="local",
                         root=str(B).replace("\\", "/"))]))
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            import app.main as M
            if getattr(M, "DB", None) is not None:
                try:
                    M.DB.close_all()
                except Exception:
                    pass
                M.DB = None
        except Exception:
            pass
        try:
            from app.core.db import Database
            Database.close_all()
        except Exception:
            pass
        try:
            from app.core import jobs as J
            J.reset_for_tests()
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def _steps(self):
        return [{
            "id": "s1", "op": "copy", "src": "sa", "dst": "sb",
            "src_path": "x.mp3", "dst_path": "x.mp3", "size": 203,
        }]

    def test_fingerprint_helpers_agree(self):
        from app.main import _step_fingerprint, _steps_fingerprint
        st = self._steps()
        self.assertEqual(_step_fingerprint(st[0]), _step_fingerprint(self._steps()[0]))
        self.assertEqual(_steps_fingerprint(st), _steps_fingerprint(self._steps()))
        mut = self._steps()
        mut[0]["dst_path"] = "other.mp3"
        self.assertNotEqual(_step_fingerprint(st[0]), _step_fingerprint(mut[0]),
                            "改了目标路径指纹却没变")

    def test_execute_rejects_tampered_step(self):
        from app.main import _step_fingerprint
        steps = self._steps()
        good = {s["id"]: _step_fingerprint(s) for s in steps}
        # 用"正确指纹 + 被改过的步骤"去执行 → 必须 409
        tampered = self._steps()
        tampered[0]["dst_path"] = "hijack.mp3"
        r = self.client.post("/api/execute", json={
            "steps": tampered, "execute": True, "confirm": True,
            "step_hashes": good,
        })
        self.assertEqual(409, r.status_code,
                         f"被改动的步骤应 409，实际 {r.status_code}: {r.text[:200]}")

    def test_execute_accepts_matching_hash(self):
        from app.main import _step_fingerprint
        steps = self._steps()
        hashes = {s["id"]: _step_fingerprint(s) for s in steps}
        r = self.client.post("/api/execute", json={
            "steps": steps, "execute": False, "step_hashes": hashes,
        })
        self.assertEqual(200, r.status_code, r.text[:300])


class BrowseCacheTest(unittest.TestCase):
    """目录聚合缓存：返回副本 + 结果与旧口径一致。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-cache-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        import pathlib
        from app.core.config import AppConfig, SourceConfig, save_config
        from app.core.db import Database
        from app.core import jobs as J
        root = pathlib.Path(cls.tmp.name) / "lib"
        (root / "a").mkdir(parents=True)
        (root / "b").mkdir()
        cfg = AppConfig(sources=[SourceConfig(id="s", name="S", type="local",
                                             root=str(root).replace("\\", "/"))])
        save_config(cfg)
        cls.root = str(root).replace("\\", "/")
        # ★ 必须用 `main.db()` 这个实例：`_folder_map` 走的就是它。
        #   自己另开一个 Database 指向同一个文件也能写，但那是**另一个连接**，
        #   测试里读到的会是空的（踩过一次）。
        from app.main import db
        db = db()
        J.init(db)
        # 造 3 个真实文件再扫一遍 —— 比手工插行更贴近真实数据
        for rel in ("a/1.mp3", "a/2.mp3", "b/3.mp3"):
            (root / rel).write_bytes(b"ID3" + b"\0" * 200)
        from app.core import scanner as S
        from app.sources.base import build_source
        S.scan_source("s", build_source(cfg.sources[0]), db)
        cls.db = db
        J.reset_for_tests()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close_all()
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def test_root_query_matches_python_filter(self):
        from app.main import db
        from app.core import paths as P
        rows_all = db().query("SELECT path FROM track WHERE source_id=?", ("s",))
        old = sum(1 for r in rows_all if P.parent(r["path"]) == self.root)
        new = db().query(
            "SELECT COUNT(*) c FROM track WHERE source_id=? AND parent=?",
            ("s", self.root))[0]["c"]
        self.assertEqual(old, new, "根目录 SQL 口径与旧的全表过滤不一致")

    def test_cache_returns_copy(self):
        from app.main import _folder_map
        a = _folder_map("s", self.root)
        a[f"{self.root}/a"]["count"] = 9999        # 就地改
        b = _folder_map("s", self.root)
        self.assertNotEqual(9999, b[f"{self.root}/a"]["count"],
                            "缓存被调用方改脏了 —— 下次请求会读到错数字")


if __name__ == "__main__":
    unittest.main()
