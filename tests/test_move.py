"""移动（同源内换目录）的测试：执行器 move 步骤 + /api/files/move 接口。

移动是本项目里破坏性最强的操作之一——源文件一旦先删，用户就真的少了一首歌。
所以每个用例都顺带确认"最坏情况下源文件还在不在"，并单独覆盖 dry-run 不动文件。
"""
from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import paths as P
from app.core.config import SourceConfig
from app.core.db import Database
from app.core.ops import Executor
from app.sources.base import SourceError
from app.sources.local import LocalSource


def _track_row(sid: str, path: str, size: int = 2048) -> dict:
    return {"source_id": sid, "path": path, "rel_path": path, "name": P.basename(path),
            "parent": P.parent(path), "ext": P.ext(path), "size": size, "mtime": 1.0}


class MoveExecutorTest(unittest.TestCase):
    """直接跑 Executor：策略、只读、越界、索引同步。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msh-move-"))
        self.root = self.tmp / "Music"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.tmp / "index.sqlite3")
        cfg = SourceConfig(id="m", name="Music", type="local", root=str(self.root))
        self.src = LocalSource(cfg)
        self.ex = Executor({"m": self.src}, self.db)

    def tearDown(self):
        self.db.close_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 工具
    def _p(self, *parts: str) -> str:
        return P.norm(str(self.root.joinpath(*parts)))

    def _mk(self, rel: str, size: int = 2048, fill: bytes = b"\x11") -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(fill * size)
        return P.norm(str(p))

    def _step(self, src_path: str, dst_path: str, **kw) -> dict:
        st = {"id": kw.pop("id", "s1"), "op": "move", "src": kw.pop("src", "m"),
              "src_path": src_path, "dst": kw.pop("dst", "m"), "dst_path": dst_path,
              "size": kw.pop("size", 0)}
        st.update(kw)
        return st

    def _one(self, step: dict, **kw) -> dict:
        return self.ex.run([step], **kw)["results"][0]

    # ------------------------------------------------------------ 用例
    def test_dry_run_moves_nothing(self):
        cur = self._mk("A/song.flac")
        want = self._p("B", "song.flac")
        r = self._one(self._step(cur, want), execute=False)
        self.assertEqual("planned", r["state"])
        self.assertEqual("将移动", r["detail"])
        self.assertFalse(r["target_exists"])
        self.assertTrue(Path(cur).exists(), "dry-run 不能动源文件")
        self.assertFalse((self.root / "B").exists(), "dry-run 不能创建目标目录")
        self.assertFalse(Path(want).exists())

    def test_move_relocates_file(self):
        cur = self._mk("A/song.flac", 4096)
        data = Path(cur).read_bytes()
        want = self._p("B", "C", "song.flac")          # 目标目录不存在，应自动创建
        r = self._one(self._step(cur, want, size=4096), execute=True)
        self.assertEqual("done", r["state"])
        self.assertEqual("已移动", r["detail"])
        self.assertEqual(want, r["target"])
        self.assertEqual(4096, r["bytes"])
        self.assertFalse(Path(cur).exists(), "移动后源路径不该还在")
        self.assertEqual(data, Path(want).read_bytes())
        self.assertFalse(Path(want + ".mshpart").exists(), "不能留下半截文件")

    def test_target_exists_skip_keeps_both(self):
        cur = self._mk("A/song.flac", 2048, b"\xaa")
        want = self._mk("B/song.flac", 1024, b"\xbb")
        r = self._one(self._step(cur, want), execute=True, policy="skip")
        self.assertEqual("skipped", r["state"])
        self.assertTrue(Path(cur).exists(), "skip 不能删源文件")
        self.assertEqual(1024, Path(want).stat().st_size, "skip 不能改动目标文件")

    def test_target_exists_rename_adds_suffix(self):
        cur = self._mk("A/song.flac", 2048, b"\xaa")
        want = self._mk("B/song.flac", 1024, b"\xbb")
        r = self._one(self._step(cur, want), execute=True, policy="rename")
        self.assertEqual("done", r["state"])
        self.assertEqual(self._p("B", "song (2).flac"), r["target"])
        self.assertEqual(1024, Path(want).stat().st_size, "原有的那份要原样保留")
        self.assertEqual(2048, Path(r["target"]).stat().st_size)
        self.assertFalse(Path(cur).exists())

    def test_target_exists_overwrite_replaces(self):
        cur = self._mk("A/song.flac", 2048, b"\xaa")
        want = self._mk("B/song.flac", 1024, b"\xbb")
        r = self._one(self._step(cur, want), execute=True, policy="overwrite")
        self.assertEqual("done", r["state"])
        self.assertEqual(2048, Path(want).stat().st_size)
        self.assertFalse(Path(cur).exists())

    def test_target_is_a_directory_is_rejected(self):
        cur = self._mk("A/song.flac")
        (self.root / "B").mkdir(parents=True, exist_ok=True)
        for policy in ("skip", "rename", "overwrite"):
            r = self._one(self._step(cur, self._p("B")), execute=True, policy=policy)
            self.assertEqual("error", r["state"], policy)
            self.assertIn("目录", r["detail"])
        self.assertTrue(Path(cur).exists())
        self.assertEqual([], list((self.root / "B").iterdir()), "目标目录里不该多出东西")

    def test_summary_counts_moved_bytes(self):
        """界面用它显示"移动体积"：只统计 copy 的话移动永远是 0 B。"""
        cur = self._mk("A/song.flac", 3072)
        out = self.ex.run([self._step(cur, self._p("B", "song.flac"))], execute=True)
        self.assertEqual("done", out["results"][0]["state"])
        self.assertEqual(3072, out["summary"]["bytes_moved"])

    def test_same_path_is_skipped(self):
        cur = self._mk("A/song.flac")
        r = self._one(self._step(cur, cur), execute=True)
        self.assertEqual("skipped", r["state"])
        self.assertEqual("源与目标相同", r["detail"])
        self.assertTrue(Path(cur).exists())

    def test_missing_source_is_error_and_creates_nothing(self):
        cur, want = self._p("A", "ghost.flac"), self._p("B", "ghost.flac")
        r = self._one(self._step(cur, want), execute=True)
        self.assertEqual("error", r["state"])
        self.assertIn("不存在", r["detail"])
        self.assertFalse(Path(want).exists())
        self.assertFalse((self.root / "B").exists(), "失败时不该留下空目录")

    def test_readonly_source_is_blocked(self):
        cur = self._mk("A/song.flac")
        cfg = SourceConfig(id="ro", name="RO", type="local", root=str(self.root),
                           read_only=True)
        ex = Executor({"ro": LocalSource(cfg)}, self.db)
        step = {"id": "s", "op": "move", "src": "ro", "src_path": cur, "dst": "ro",
                "dst_path": self._p("B", "song.flac")}
        for kw in ({}, {"execute": True}):
            self.assertEqual("blocked", ex.run([step], **kw)["results"][0]["state"])
        self.assertTrue(Path(cur).exists())

    def test_cross_source_move_is_rejected(self):
        cur = self._mk("A/song.flac")
        other = LocalSource(SourceConfig(id="n", name="N", type="local",
                                         root=str(self.tmp / "N")))
        ex = Executor({"m": self.src, "n": other}, self.db)
        r = ex.run([self._step(cur, self._p("B", "song.flac"), dst="n")],
                   execute=True)["results"][0]
        self.assertEqual("error", r["state"])
        self.assertIn("同一个数据源", r["detail"])
        self.assertTrue(Path(cur).exists())

    def test_path_outside_root_is_rejected(self):
        """真正**越界**的一律拒绝，且不在外面留下任何东西。

        注意这里不再把"相对路径"算作越界 —— 相对路径现在是**合法**的
        （按数据源根下的相对路径解释，见
        `test_relative_paths_are_resolved_inside_root`）。
        相对写法里的 `..` 绕行仍然必须被挡住，所以补了一条相对绕行。
        """
        cur = self._mk("A/song.flac")
        outside = P.norm(str(self.tmp / "Elsewhere" / "song.flac"))
        sneaky = f"{self.root.as_posix()}/../Elsewhere/song.flac"
        for step in (self._step(cur, outside),          # 目标跑到 root 外
                     self._step(outside, self._p("B", "x.flac")),   # 源跑到 root 外
                     self._step(cur, P.norm(sneaky)),   # 用 .. 绕过前缀检查
                     self._step(cur, "A/../../Elsewhere/x.flac")):  # 相对写法绕行
            r = self.ex.run([step], execute=True)["results"][0]
            self.assertEqual("error", r["state"], step)
            self.assertIn("根目录", r["detail"])
        self.assertFalse((self.tmp / "Elsewhere").exists())
        self.assertTrue(Path(cur).exists())
        self.assertFalse((self.root / "B").exists())

    def test_relative_paths_are_resolved_inside_root(self):
        """★ 相对路径要能用（与 `folders_api._inside` 的约定一致）。

        原来 `move` 的 `_resolve_move` 直接拿原文去 `_inside_root`，
        而那里**只认绝对路径** —— 于是 `B/song.flac` 这种完全合理的
        相对路径会被报成"路径超出数据源根目录"。报错内容和真实原因
        （"我只认绝对路径"）完全不是一回事，直接调 API 的人会查很久。

        放开相对路径**不等于**放开越界：展开之后仍走同一套校验
        （见 `test_path_outside_root_is_rejected`）。
        """
        cur = self._mk("A/song.flac", 2048)
        r = self._one(self._step(cur, "B/song.flac"), execute=True)
        self.assertEqual("done", r["state"], r)
        self.assertTrue((self.root / "B" / "song.flac").exists())
        self.assertFalse(Path(cur).exists())
        # 源路径也用相对写法
        cur2 = self._mk("A/song2.flac", 2048)
        r2 = self._one(self._step("A/song2.flac", "B/song2.flac"), execute=True)
        self.assertEqual("done", r2["state"], r2)
        self.assertTrue((self.root / "B" / "song2.flac").exists())
        self.assertFalse(Path(cur2).exists())

    def test_index_follows_the_moved_file(self):
        """移完索引要跟着走，否则界面还指着旧路径（点开就是"文件不存在"）。"""
        cur = self._mk("A/song.flac", 3072)
        self.db.upsert_tracks([_track_row("m", cur, 3072)])
        want = self._p("B", "song.flac")
        r = self._one(self._step(cur, want), execute=True)
        self.assertEqual("done", r["state"])
        self.assertIsNone(self.db.track("m", cur), "旧路径的记录要更新掉")
        row = self.db.track("m", want)
        self.assertIsNotNone(row, "新路径必须能查到")
        self.assertEqual("B", row["parent"].rsplit("/", 1)[-1])
        self.assertEqual("song.flac", row["name"])
        self.assertEqual("B/song.flac", row["rel_path"])

    def test_index_stays_quiet_when_move_does_not_happen(self):
        cur = self._mk("A/song.flac")
        self.db.upsert_tracks([_track_row("m", cur)])
        want = self._mk("B/song.flac")
        r = self._one(self._step(cur, want), execute=True, policy="skip")
        self.assertEqual("skipped", r["state"])
        self.assertIsNotNone(self.db.track("m", cur), "没移动就不能改索引")


class LocalMoveFallbackTest(unittest.TestCase):
    """跨设备（EXDEV）时的降级路径：复制 → 校验 → 删源。

    同一个源里也会跨设备：目录联接（junction）/ 挂载点指到另一块盘时就是这样。
    这条路径是"先删源"最容易写错的地方，所以三种失败都要确认源文件还在。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msh-move-exdev-"))
        self.root = self.tmp / "Music"
        self.src = LocalSource(SourceConfig(id="m", name="M", type="local",
                                            root=str(self.root)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, rel: str, size: int = 4096) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x5a" * size)
        return p

    def _dst(self, rel: str = "B/song.flac") -> str:
        return P.norm(str(self.root / rel))

    @contextmanager
    def _exdev(self):
        """让第一次 os.replace 报 EXDEV（模拟跨设备），之后的调用保持真实行为。"""
        real = os.replace
        state = {"calls": 0}

        def fake(a, b):
            state["calls"] += 1
            if state["calls"] == 1:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(a, b)

        with mock.patch.object(os, "replace", fake):
            yield state

    def test_exdev_falls_back_to_copy_verify_delete(self):
        p = self._mk("A/song.flac")
        dst = self._dst()
        with self._exdev() as state:
            self.src.move(P.norm(str(p)), dst)
        self.assertGreaterEqual(state["calls"], 2, "应先试原子改名，失败后退化为复制")
        self.assertTrue(Path(dst).exists())
        self.assertEqual(4096, Path(dst).stat().st_size)
        self.assertFalse(p.exists(), "只有目标校验通过才允许删源文件")
        self.assertFalse(Path(dst + ".mshpart").exists(), "不能留下半截文件")

    def test_exdev_copy_failure_keeps_source(self):
        p = self._mk("A/song.flac")
        dst = self._dst()
        with self._exdev():
            with mock.patch.object(shutil, "copyfile",
                                   side_effect=OSError(28, "磁盘空间不足")):
                with self.assertRaises(OSError):
                    self.src.move(P.norm(str(p)), dst)
        self.assertTrue(p.exists(), "复制失败必须保留源文件")
        self.assertFalse(Path(dst).exists())
        self.assertFalse(Path(dst + ".mshpart").exists(), "半截文件要清掉")

    def test_exdev_size_mismatch_keeps_source(self):
        p = self._mk("A/song.flac", 4096)
        dst = self._dst()
        real_copy = shutil.copyfile

        def short_copy(a, b, **kw):
            real_copy(a, b, **kw)
            with open(b, "r+b") as fh:      # 少写了 16 字节，模拟没写全
                fh.truncate(4096 - 16)

        with self._exdev():
            with mock.patch.object(shutil, "copyfile", short_copy):
                with self.assertRaises(SourceError):
                    self.src.move(P.norm(str(p)), dst)
        self.assertTrue(p.exists(), "体积对不上时绝不能删源文件")
        self.assertEqual(4096, p.stat().st_size)
        self.assertFalse(Path(dst).exists(), "没写全的副本不能冒充成品")
        self.assertFalse(Path(dst + ".mshpart").exists())


class MoveApiTest(unittest.TestCase):
    """/api/files/move 的响应契约与 confirm 保护。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-move-api-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        cls.root = Path(cls._tmp.name) / "Music"
        cls.root.mkdir(parents=True, exist_ok=True)
        from app.core import config as C
        C.save_config(C.AppConfig(sources=[
            C.SourceConfig(id="m", name="Music", type="local", root=str(cls.root))]))
        from app import main as main_mod
        main_mod.DB = None
        main_mod.manager.reset()
        from fastapi.testclient import TestClient
        cls.client = TestClient(main_mod.app)

    @classmethod
    def tearDownClass(cls):
        try:
            from app import main as main_mod
            if main_mod.DB is not None:
                main_mod.DB.close_all()
                main_mod.DB = None
            main_mod.manager.reset()
        except Exception:
            pass
        os.environ.pop("MSH_DATA_DIR", None)
        os.environ.pop("MSH_DB", None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def setUp(self):
        # 每个用例都从干净的文件开始
        for p in self.root.iterdir():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        self.cur = self._mk("A/song.flac", 2048, b"\xaa")
        self.want = P.norm(str(self.root / "B" / "song.flac"))
        self.body = {"steps": [{"src": "m", "src_path": self.cur, "dst_path": self.want,
                                "size": 2048}]}

    def _mk(self, rel: str, size: int, fill: bytes) -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(fill * size)
        return P.norm(str(p))

    def test_dry_run_returns_plan_and_touches_nothing(self):
        r = self.client.post("/api/files/move", json={**self.body, "execute": False})
        self.assertEqual(200, r.status_code, r.text)
        data = r.json()
        self.assertTrue(data["dry_run"])
        self.assertEqual({"total": 1, "planned": 1}, {k: data["summary"][k]
                                                      for k in ("total", "planned")})
        self.assertEqual("planned", data["results"][0]["state"])
        self.assertTrue(Path(self.cur).exists())
        self.assertFalse(Path(self.want).exists())

    def test_execute_requires_confirm(self):
        r = self.client.post("/api/files/move", json={**self.body, "execute": True})
        self.assertEqual(400, r.status_code)
        self.assertIn("confirm", r.json()["detail"])
        self.assertTrue(Path(self.cur).exists(), "被拒绝的请求不能动文件")

    def test_execute_moves_file(self):
        r = self.client.post("/api/files/move",
                             json={**self.body, "execute": True, "confirm": True})
        self.assertEqual(200, r.status_code, r.text)
        data = r.json()
        self.assertFalse(data["dry_run"])
        self.assertEqual("done", data["results"][0]["state"])
        self.assertEqual(self.want, data["results"][0]["target"])
        self.assertEqual(1, data["summary"]["done"])
        self.assertFalse(Path(self.cur).exists())
        self.assertEqual(2048, Path(self.want).stat().st_size)

    def test_execute_updates_index_for_other_threads(self):
        """请求跑在别的线程里：索引必须真的提交，否则界面查询到的还是旧路径。"""
        from app.main import db
        with db().write() as conn:
            conn.execute("DELETE FROM track WHERE source_id='m'")
        db().upsert_tracks([_track_row("m", self.cur, 2048)])
        r = self.client.post("/api/files/move",
                             json={**self.body, "execute": True, "confirm": True})
        self.assertEqual("done", r.json()["results"][0]["state"])
        self.assertIsNone(db().track("m", self.cur), "旧路径的记录要更新掉")
        row = db().track("m", self.want)
        self.assertIsNotNone(row, "移动后必须能立刻查到新路径（跨线程可见）")
        self.assertEqual("B/song.flac", row["rel_path"])
        self.assertEqual("song.flac", row["name"])

    def test_step_cap_and_empty_body(self):
        steps = [{"src": "m", "src_path": self.cur, "dst_path": self.want}] * 3
        r = self.client.post("/api/files/move", json={"steps": steps, "max_steps": 2})
        self.assertEqual(400, r.status_code)
        self.assertIn("步骤过多", r.json()["detail"])
        r2 = self.client.post("/api/files/move", json={"steps": []})
        self.assertEqual(400, r2.status_code)

    def test_missing_source_id_is_rejected(self):
        r = self.client.post("/api/files/move",
                             json={"steps": [{"src_path": self.cur, "dst_path": self.want}]})
        self.assertEqual(400, r.status_code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
