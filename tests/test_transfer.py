"""跨库传输的回归测试。

核心是**顺序与回滚**：跨库搬运必须"复制 → 校验 → 再删源"。
任何一步失败，源都不能消失。

这里用两个真实的本地数据源目录来跑，覆盖：
  · 正常传输：目标有、源没了（进回收站）
  · 目标是只读 → blocked，源不动
  · 源不允许删除 → blocked，源不动
  · 目标已存在 + skip → conflict，源不动
  · 目标已存在 + rename → 自动改名，两个都在
  · 同源传输 → 明确拒绝（该走"移动到…"）
  · 源不存在 → error
  · 校验失败 → 目标那份被清掉、源保留
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import transfer as X  # noqa: E402
from app.core.config import SourceConfig  # noqa: E402
from app.sources.local import LocalSource  # noqa: E402

MP3 = b"\xff\xfb\x90\x00" + bytes(413)


class _FakeJob:
    def __init__(self):
        self.touches = 0

    def touch(self):
        self.touches += 1


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msh-xfer-")
        self.a_root = os.path.join(self.tmp, "A").replace("\\", "/")
        self.b_root = os.path.join(self.tmp, "B").replace("\\", "/")
        for r in (self.a_root, self.b_root):
            os.makedirs(os.path.join(r, "sub"), exist_ok=True)
        self.a = self._src("sa", "库A", self.a_root)
        self.b = self._src("sb", "库B", self.b_root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src(self, sid, name, root, **kw):
        cfg = SourceConfig(id=sid, name=name, type="local", root=root, **kw)
        return LocalSource(cfg)

    def _put(self, root, rel, data=MP3):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return p.replace("\\", "/")

    def _exists(self, root, rel):
        return os.path.isfile(os.path.join(root, rel))

    def _ap(self, rel):
        return os.path.join(self.a_root, rel).replace("\\", "/")

    def _bp(self, rel):
        return os.path.join(self.b_root, rel).replace("\\", "/")

    # ---------------------------------------------------------------- 正常路径
    def test_transfer_moves_file_and_removes_source(self):
        self._put(self.a_root, "sub/song.mp3")
        r = X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                           "sub/song.mp3")
        self.assertEqual("done", r["state"], r)
        self.assertTrue(self._exists(self.b_root, "sub/song.mp3"),
                        "目标没有这个文件")
        self.assertFalse(self._exists(self.a_root, "sub/song.mp3"),
                         "源文件还在")
        self.assertIn("copy", r["steps_done"])
        self.assertIn("verify", r["steps_done"])
        self.assertIn("delete-src", r["steps_done"])

    def test_source_goes_to_trash_not_deleted(self):
        """默认必须进回收站 —— 跨库搬运是最需要可逆的操作之一。"""
        self._put(self.a_root, "sub/song.mp3")
        r = X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                           "sub/song.mp3")
        self.assertEqual("done", r["state"], r)
        self.assertTrue(r.get("trashed"), "没有返回回收站路径")
        trash_root = os.path.join(self.a_root, ".music-sync-trash")
        self.assertTrue(os.path.isdir(trash_root), "回收站目录没建出来")
        found = []
        for base, _d, fs in os.walk(trash_root):
            found += fs
        self.assertIn("song.mp3", found, "源文件不在回收站里")

    def test_content_is_identical(self):
        payload = b"\xff\xfb\x90\x00" + os.urandom(2000)
        self._put(self.a_root, "sub/x.mp3", payload)
        X.transfer_one(self.a, self.b, self._ap("sub/x.mp3"), "sub/x.mp3")
        with open(self._bp("sub/x.mp3"), "rb") as fh:
            self.assertEqual(payload, fh.read())

    def test_job_gets_heartbeat(self):
        """传输过程中要打心跳，否则大批量会被误判"已中断"。"""
        self._put(self.a_root, "sub/song.mp3")
        job = _FakeJob()
        X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                       "sub/song.mp3", job=job)
        self.assertGreaterEqual(job.touches, 2)

    # ---------------------------------------------------------------- 权限
    def test_readonly_target_blocks_and_source_survives(self):
        self._put(self.a_root, "sub/song.mp3")
        ro = self._src("sb", "库B", self.b_root, read_only=True)
        r = X.transfer_one(self.a, ro, self._ap("sub/song.mp3"), "sub/song.mp3")
        self.assertEqual("blocked", r["state"], r)
        self.assertTrue(self._exists(self.a_root, "sub/song.mp3"),
                        "★ 被拒绝时源不能消失")
        self.assertFalse(self._exists(self.b_root, "sub/song.mp3"))

    def test_undeletable_source_blocks(self):
        """源不允许删 → 不能"传过去但源还在"地假装成功。

        这一步**必须在复制之前**就挡住，否则会白白复制一份再报错。
        """
        self._put(self.a_root, "sub/song.mp3")
        nodelete = self._src("sa", "库A", self.a_root, read_only=True)
        r = X.transfer_one(nodelete, self.b, self._ap("sub/song.mp3"),
                           "sub/song.mp3")
        self.assertEqual("blocked", r["state"], r)
        self.assertFalse(self._exists(self.b_root, "sub/song.mp3"),
                         "被挡住时不该已经在目标留一份")
        self.assertTrue(self._exists(self.a_root, "sub/song.mp3"))

    # ---------------------------------------------------------------- 冲突
    def test_conflict_skip_keeps_source(self):
        self._put(self.a_root, "sub/song.mp3", MP3 + b"A")
        self._put(self.b_root, "sub/song.mp3", MP3 + b"B")
        r = X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                           "sub/song.mp3", policy="skip")
        self.assertEqual("conflict", r["state"], r)
        self.assertTrue(self._exists(self.a_root, "sub/song.mp3"),
                        "★ 跳过时源不能消失")
        with open(self._bp("sub/song.mp3"), "rb") as fh:
            self.assertEqual(MP3 + b"B", fh.read(), "目标被覆盖了")

    def test_conflict_rename_keeps_both(self):
        self._put(self.a_root, "sub/song.mp3", MP3 + b"A")
        self._put(self.b_root, "sub/song.mp3", MP3 + b"B")
        r = X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                           "sub/song.mp3", policy="rename")
        self.assertEqual("done", r["state"], r)
        self.assertTrue(os.path.isfile(r["target"]))
        self.assertNotEqual(self._bp("sub/song.mp3"), r["target"])
        self.assertTrue(self._exists(self.b_root, "sub/song.mp3"))
        self.assertFalse(self._exists(self.a_root, "sub/song.mp3"))

    def test_conflict_overwrite_replaces_target(self):
        self._put(self.a_root, "sub/song.mp3", MP3 + b"NEW")
        self._put(self.b_root, "sub/song.mp3", MP3 + b"OLD")
        r = X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                           "sub/song.mp3", policy="overwrite")
        self.assertEqual("done", r["state"], r)
        self.assertTrue(r.get("overwrote"))
        with open(self._bp("sub/song.mp3"), "rb") as fh:
            self.assertEqual(MP3 + b"NEW", fh.read())

    # ---------------------------------------------------------------- 守卫
    def test_same_source_is_rejected(self):
        """同源要走「移动到…」（原子改名），不能让跨库传输代劳。"""
        self._put(self.a_root, "sub/song.mp3")
        r = X.transfer_one(self.a, self.a, self._ap("sub/song.mp3"),
                           "other/song.mp3")
        self.assertEqual("error", r["state"], r)
        self.assertIn("同一个数据源", r["detail"])
        self.assertTrue(self._exists(self.a_root, "sub/song.mp3"))

    def test_missing_source_is_error_and_target_untouched(self):
        r = X.transfer_one(self.a, self.b, self._ap("sub/nope.mp3"),
                           "sub/nope.mp3")
        self.assertEqual("error", r["state"], r)
        self.assertFalse(self._exists(self.b_root, "sub/nope.mp3"))

    def test_directory_source_is_rejected(self):
        os.makedirs(os.path.join(self.a_root, "sub", "adir"), exist_ok=True)
        r = X.transfer_one(self.a, self.b, self._ap("sub/adir"), "sub/adir")
        self.assertEqual("error", r["state"], r)
        self.assertIn("目录", r["detail"])

    def test_none_sources(self):
        r = X.transfer_one(None, self.b, "x", "y")
        self.assertEqual("error", r["state"])
        r2 = X.transfer_one(self.a, None, "x", "y")
        self.assertEqual("error", r2["state"])

    # ---------------------------------------------------------------- 校验失败
    def test_verify_failure_keeps_source_and_cleans_target(self):
        """★ 最关键的一条：目标写歪了就不能删源。

        做法：让目标数据源的 copy_from 写一个**大小不对**的文件，
        模拟"写到一半 / 盘满 / 源在读取时变了"。
        """
        self._put(self.a_root, "sub/song.mp3")
        real_copy = self.b.copy_from

        def bad_copy(src, sp, dp, progress=None):
            out = real_copy(src, sp, dp, progress=progress)
            # 故意截断目标（模拟不完整写入）
            with open(dp, "r+b") as fh:
                fh.truncate(10)
            return out

        self.b.copy_from = bad_copy                      # type: ignore[method-assign]
        try:
            r = X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                               "sub/song.mp3")
        finally:
            self.b.copy_from = real_copy                 # type: ignore[method-assign]
        self.assertEqual("error", r["state"], r)
        self.assertEqual("verify", r.get("error_kind"))
        self.assertTrue(self._exists(self.a_root, "sub/song.mp3"),
                        "★ 校验失败时源必须保留")
        self.assertFalse(self._exists(self.b_root, "sub/song.mp3"),
                         "不完整的目标那份应当被清掉")

    def test_delete_failure_reports_both_copies_exist(self):
        """删源失败 → 如实说"两边都有"，**不能**回滚目标。

        回滚目标会把一份完好的文件删掉，那才是真的丢数据。
        """
        self._put(self.a_root, "sub/song.mp3")
        real_remove = self.a.remove

        def bad_remove(path, hard=False):
            raise OSError("模拟设备已拔出")

        self.a.remove = bad_remove                       # type: ignore[method-assign]
        try:
            r = X.transfer_one(self.a, self.b, self._ap("sub/song.mp3"),
                               "sub/song.mp3")
        finally:
            self.a.remove = real_remove                  # type: ignore[method-assign]
        self.assertEqual("error", r["state"], r)
        self.assertEqual("delete-src", r.get("error_kind"))
        self.assertTrue(self._exists(self.b_root, "sub/song.mp3"),
                        "目标那份是好的，不能被回滚掉")
        self.assertTrue(self._exists(self.a_root, "sub/song.mp3"))
        self.assertIn("两边都有", r["detail"])

    # ---------------------------------------------------------------- 计划
    def test_plan_transfer_shape(self):
        steps = X.plan_transfer("sa", "sb", ["D:/A/sub/x.mp3", "D:/A/y.flac"],
                                "Dst/Dir")
        self.assertEqual(2, len(steps))
        self.assertEqual("transfer", steps[0]["op"])
        self.assertEqual("sa", steps[0]["src"])
        self.assertEqual("sb", steps[0]["dst"])
        self.assertEqual("Dst/Dir/x.mp3", steps[0]["dst_path"])
        self.assertEqual("Dst/Dir/y.flac", steps[1]["dst_path"])
        # id 必须互不相同（_sync_index 靠它把结果对回步骤）
        self.assertNotEqual(steps[0]["id"], steps[1]["id"])

    def test_plan_transfer_root_destination(self):
        steps = X.plan_transfer("sa", "sb", ["D:/A/x.mp3"])
        self.assertEqual("x.mp3", steps[0]["dst_path"])


class ExecutorTransferOpTest(unittest.TestCase):
    """`transfer` 作为 Executor 的一个 op（对比页"安全搬运"走这条）。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="msh-xop-")
        os.environ["MSH_DATA_DIR"] = self._tmp.name
        self.A = os.path.join(self._tmp.name, "A").replace("\\", "/")
        self.B = os.path.join(self._tmp.name, "B").replace("\\", "/")
        for r in (self.A, self.B):
            os.makedirs(r, exist_ok=True)
        from app.core.config import (AppConfig, Settings, SourceConfig,
                                     load_config, save_config)
        save_config(AppConfig(
            sources=[SourceConfig(id="sa", name="库A", type="local", root=self.A),
                     SourceConfig(id="sb", name="库B", type="local", root=self.B)],
            settings=Settings(station_dir=os.path.join(self._tmp.name, "st"),
                              staging_dir=os.path.join(self._tmp.name, "up"))))
        load_config(force=True)
        from app.sources import manager
        self.sa, self.sb = manager.get("sa"), manager.get("sb")

    def tearDown(self):
        try:
            from app.core.jobs import reset_for_tests
            reset_for_tests()
        except Exception:
            pass
        self._tmp.cleanup()

    def _steps(self):
        return [{"id": "t1", "op": "transfer", "src": "sa",
                 "src_path": self.A + "/sub/song.mp3", "dst": "sb",
                 "dst_path": "sub/song.mp3", "size": 5012}]

    def test_transfer_op_moves_and_trashes_source(self):
        from app.core.ops import Executor
        self.sa.mkdirs(self.A + "/sub")
        self.sa.write_file(self.A + "/sub/song.mp3", MP3)
        self.sb.mkdirs(self.B + "/sub")
        r = Executor({"sa": self.sa, "sb": self.sb}, None).run(
            self._steps(), execute=True, workers=2)
        res = r["results"][0]
        self.assertEqual("done", res["state"], res)
        self.assertEqual(["copy", "verify", "delete-src"],
                         res.get("steps_done"), res)
        self.assertTrue(self.sb.exists(self.B + "/sub/song.mp3"))
        self.assertFalse(self.sa.exists(self.A + "/sub/song.mp3"),
                         "源还在")
        trash = os.path.join(self.A, ".music-sync-trash")
        found = [f for _b, _d, fs in os.walk(trash) for f in fs]
        self.assertIn("song.mp3", found, "源没进回收站")

    def test_transfer_op_blocked_on_readonly_target(self):
        from app.core.ops import Executor
        self.sa.mkdirs(self.A + "/sub")
        self.sa.write_file(self.A + "/sub/song.mp3", MP3)
        self.sb.cfg.read_only = True
        try:
            r = Executor({"sa": self.sa, "sb": self.sb}, None).run(
                self._steps(), execute=True, workers=2)
        finally:
            self.sb.cfg.read_only = False
        self.assertEqual("blocked", r["results"][0]["state"],
                         r["results"][0])
        self.assertTrue(self.sa.exists(self.A + "/sub/song.mp3"),
                        "★ 被拒时源不能消失")

    def test_transfer_index_removes_source_record(self):
        """搬运成功后源库索引里不能再留着这条记录。

        留着的话就是"文件已经搬走了、搜索还能搜到"，点进去播放失败。
        """
        import tempfile
        from app.core import scanner as S
        from app.core import jobs as J
        from app.core.db import Database
        from app.core.ops import Executor
        db = Database(os.path.join(self._tmp.name, "idx.sqlite3"))
        J.reset_for_tests()
        J.init(db)
        self.sa.mkdirs(self.A + "/sub")
        self.sa.write_file(self.A + "/sub/song.mp3", MP3)
        sb_root_files = None
        S.scan_source("sa", self.sa, db)
        before = [r["path"] for r in db.all_tracks("sa")]
        self.assertTrue(before, "扫描后索引里应该有它")
        self.sb.mkdirs(self.B + "/sub")
        ex = Executor({"sa": self.sa, "sb": self.sb}, db)
        r = ex.run(self._steps(), execute=True, workers=2)
        self.assertEqual("done", r["results"][0]["state"], r["results"][0])
        after = [r2["path"] for r2 in db.all_tracks("sa")]
        self.assertNotIn(self.A + "/sub/song.mp3", after,
                         "★ 源索引记录没清掉")
        db.close_all()
        J.reset_for_tests()

    def test_unknown_op_still_skipped(self):
        """加新 op 不能把未知 op 的处理改坏。"""
        from app.core.ops import Executor
        r = Executor({"sa": self.sa, "sb": self.sb}, None).run(
            [{"id": "x", "op": "wat", "src": "sa", "dst": "sb"}],
            execute=True, workers=1)
        self.assertEqual("skipped", r["results"][0]["state"])


class TargetConflictTest(unittest.TestCase):
    """★ 同一方案里两条步骤指向同一目标 —— **绝不能少文件**。

    这是一条真实存在过的静默丢文件 bug（`_do_copy` 一直带着它）：

        两条 copy/transfer 写同一个 `d/x.mp3`
        → `dst_conflicts: 1` 报出来了（看起来"已处理"）
        → 但实际只落了一个文件，另一条是 `conflict`
        → 用户看到"有 1 个目标撞车、已自动改名"，**而文件少了一个**

    根因：`_reserve_targets` 用**目标源内相对路径**（`d/x.mp3`）做键，
    而 `_do_copy`/`_do_transfer` 把**绝对路径**传给 `_claim_target`
    —— 两边坐标系不同，查不中，改名逻辑形同虚设。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="msh-conf-")
        os.environ["MSH_DATA_DIR"] = self._tmp.name
        self.A = os.path.join(self._tmp.name, "A").replace("\\", "/")
        self.B = os.path.join(self._tmp.name, "B").replace("\\", "/")
        for r in (self.A, self.B):
            os.makedirs(r, exist_ok=True)
        from app.core.config import (AppConfig, Settings, SourceConfig,
                                     load_config, save_config)
        save_config(AppConfig(
            sources=[SourceConfig(id="sa", name="库A", type="local", root=self.A),
                     SourceConfig(id="sb", name="库B", type="local", root=self.B)],
            settings=Settings(station_dir=os.path.join(self._tmp.name, "st"),
                              staging_dir=os.path.join(self._tmp.name, "up"))))
        load_config(force=True)
        from app.sources import manager
        self.sa, self.sb = manager.get("sa"), manager.get("sb")
        self.p1 = b"\xff\xfb\x90\x00" + b"AAAA" * 400
        self.p2 = b"\xff\xfb\x90\x00" + b"BBBB" * 400

    def tearDown(self):
        try:
            from app.core.jobs import reset_for_tests
            reset_for_tests()
        except Exception:
            pass
        self._tmp.cleanup()

    def _setup(self):
        self.sa.mkdirs(self.A + "/s1")
        self.sa.mkdirs(self.A + "/s2")
        self.sb.mkdirs(self.B + "/d")
        self.sa.write_file(self.A + "/s1/x.mp3", self.p1)
        self.sa.write_file(self.A + "/s2/x.mp3", self.p2)

    def _steps(self, op):
        return [
            {"id": "a", "op": op, "src": "sa", "src_path": self.A + "/s1/x.mp3",
             "dst": "sb", "dst_path": "d/x.mp3", "size": len(self.p1)},
            {"id": "b", "op": op, "src": "sa", "src_path": self.A + "/s2/x.mp3",
             "dst": "sb", "dst_path": "d/x.mp3", "size": len(self.p2)},
        ]

    def _check(self, op):
        from app.core.ops import Executor
        self._setup()
        r = Executor({"sa": self.sa, "sb": self.sb}, None).run(
            self._steps(op), execute=True, workers=1)
        target = os.path.join(self.B, "d")
        files = sorted(os.listdir(target))
        # 撞车必须被报出来（否则用户根本不知道发生过改名）
        self.assertGreaterEqual(r["summary"].get("dst_conflicts") or 0, 1,
                                "目标撞车没被报出来")
        # ★ 核心：两个文件都要在
        self.assertEqual(2, len(files),
                         f"{op} 撞车后少文件了：{files}（状态 "
                         f"{[x['state'] for x in r['results']]}）")
        self.assertEqual(sorted([len(self.p1), len(self.p2)]),
                         sorted(os.path.getsize(os.path.join(target, f))
                                for f in files),
                         "两个文件的内容大小不对")
        self.assertTrue(all(x["state"] == "done" for x in r["results"]),
                        f"有步骤没成功：{[(x['state'], x.get('detail')) for x in r['results']]}")

    def test_copy_target_conflict_keeps_both(self):
        self._check("copy")

    def test_transfer_target_conflict_keeps_both(self):
        self._check("transfer")

    def test_same_source_name_in_different_dirs_does_not_false_conflict(self):
        """不同目录下的同名文件**不该**被误判成撞车。

        键里必须带目录 —— 只比 basename 的话，
        `A/x.mp3` 和 `B/x.mp3` 会被当成同一个目标而白白改名。
        """
        from app.core.ops import Executor
        self.sa.mkdirs(self.A + "/s1")
        self.sa.mkdirs(self.A + "/s2")
        self.sb.mkdirs(self.B + "/d1")
        self.sb.mkdirs(self.B + "/d2")
        self.sa.write_file(self.A + "/s1/x.mp3", self.p1)
        self.sa.write_file(self.A + "/s2/x.mp3", self.p2)
        steps = [
            {"id": "a", "op": "copy", "src": "sa", "src_path": self.A + "/s1/x.mp3",
             "dst": "sb", "dst_path": "d1/x.mp3", "size": len(self.p1)},
            {"id": "b", "op": "copy", "src": "sa", "src_path": self.A + "/s2/x.mp3",
             "dst": "sb", "dst_path": "d2/x.mp3", "size": len(self.p2)},
        ]
        r = Executor({"sa": self.sa, "sb": self.sb}, None).run(
            steps, execute=True, workers=1)
        self.assertEqual(1, len(os.listdir(os.path.join(self.B, "d1"))))
        self.assertEqual(1, len(os.listdir(os.path.join(self.B, "d2"))))
        self.assertEqual(0, r["summary"].get("dst_conflicts") or 0,
                         "不同目录的同名文件被误判成撞车了")
        self.assertTrue(all(x["state"] == "done" for x in r["results"]))


class TransferHeartbeatTest(unittest.TestCase):
    """大文件搬运途中要打心跳。

    不打的后果：一个 40 GB 的文件在网络盘上搬 20 分钟，期间没有任何心跳
    —— 超过 `STALE_JOB_SECONDS`(180s) 就会被 `_reap_stale_locked` 判成
    "心跳中断"，任务被标 error、**独占位被释放**，而它其实还在跑。
    那之后任何写操作都会被放进来，两边同时改文件。
    """

    def test_heartbeat_advances_during_transfer(self):
        import tempfile
        import time as _t
        with tempfile.TemporaryDirectory(prefix="msh-hb-") as td:
            os.environ["MSH_DATA_DIR"] = td
            os.environ["MSH_DB"] = os.path.join(td, "i.sqlite3")
            A = os.path.join(td, "A").replace("\\", "/")
            B = os.path.join(td, "B").replace("\\", "/")
            for r in (A, B):
                os.makedirs(r, exist_ok=True)
            from app.core.config import (AppConfig, Settings, SourceConfig,
                                         load_config, save_config)
            save_config(AppConfig(
                sources=[SourceConfig(id="sa", name="A", type="local", root=A),
                         SourceConfig(id="sb", name="B", type="local", root=B)],
                settings=Settings(station_dir=os.path.join(td, "st"),
                                  staging_dir=os.path.join(td, "up"))))
            load_config(force=True)
            from app.core.db import Database
            from app.core import jobs as J
            from app.core.ops import Executor
            from app.sources import manager
            db = Database(os.path.join(td, "i.sqlite3"))
            J.reset_for_tests()
            J.init(db)
            sa, sb = manager.get("sa"), manager.get("sb")
            sa.mkdirs(A + "/s")
            sb.mkdirs(B + "/d")
            sa.write_file(A + "/s/big.mp3", MP3 + b"X" * (600 * 1024))
            job = J.get_jobs().create("sync", total=1)
            before = job.heartbeat
            _t.sleep(0.05)
            r = Executor({"sa": sa, "sb": sb}, None).run(
                [{"id": "t", "op": "transfer", "src": "sa",
                  "src_path": A + "/s/big.mp3", "dst": "sb",
                  "dst_path": "d/big.mp3", "size": 614404}],
                execute=True, workers=1, job=job)
            self.assertEqual("done", r["results"][0]["state"],
                             r["results"][0])
            self.assertGreater(job.heartbeat, before,
                               "搬运途中没有心跳")
            self.assertTrue(sb.exists(B + "/d/big.mp3"))
            db.close_all()
            J.reset_for_tests()


if __name__ == "__main__":
    unittest.main()
