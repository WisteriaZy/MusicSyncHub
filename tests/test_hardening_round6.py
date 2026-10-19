"""第六轮修复的回归测试：junction / 跨进程锁 / 临时文件 / 孤儿索引 / revision。

## 对应问题

1. **junction（目录联接）绕过根目录校验**
   `os.path.islink()` 对 junction 返回 **False**，但它是 reparse point，
   读写会跑到完全不同的目录。库里的一个 junction 就能让扫描/移动/删除/
   改标签/上传全部落到源根之外（实测 8 个端点）。

2. **跨进程没有互斥**
   独占位是进程内的 `threading.Lock`。两个实例共享同一数据目录时
   各自都认为独占，实测两边都报 done 但只剩一份文件。

3. **`.mshpart` 没人清理**
   进程被强杀后临时文件永远留在库里。

4. **删除数据源留孤儿索引**
   `purge=false` 时 track 行还在，`/api/state` 的曲目数对不上，
   查重还会扫到幽灵数据。

5. **`/api/state` 检不出纯元数据更新**
   原来的判据是「曲目数 + WAL mtime」，实测 12 次快速改标题漏检 11 次。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))


class PathRealBoundaryTest(unittest.TestCase):
    """`inside_root_real` 必须识破 junction / 符号链接。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msh-junc-")
        self.root = Path(self.tmp.name) / "lib"
        self.out = Path(self.tmp.name) / "outside"
        self.root.mkdir()
        self.out.mkdir()
        (self.out / "secret.mp3").write_bytes(b"ID3" + b"\0" * 100)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plain_path_inside(self):
        from app.core import paths as P
        self.assertTrue(P.inside_root_real(str(self.root), str(self.root / "a.mp3")))
        self.assertFalse(P.inside_root_real(str(self.root), str(self.out / "secret.mp3")))

    def test_junction_is_detected(self):
        """真造一个 junction；造不出来（权限）就跳过。"""
        from app.core import paths as P
        link = self.root / "jlink"
        if os.name != "nt":
            self.skipTest("junction 只在 Windows 上测")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.out)],
                           capture_output=True, text=True)
        if r.returncode != 0 and not link.exists():
            self.skipTest(f"无法创建 junction：{(r.stderr or r.stdout)[:80]}")
        try:
            # 字符串判断会认为它在根内（骗过旧实现）
            self.assertTrue(P.inside_root(str(self.root), str(link / "secret.mp3")))
            # 真实路径判断必须识破
            self.assertFalse(P.inside_root_real(str(self.root), str(link / "secret.mp3")),
                             "junction 指向源根之外却没被识别")
        finally:
            try:
                os.rmdir(link)
            except OSError:
                pass

    def test_listdir_skips_junction(self):
        from app.core.config import SourceConfig
        from app.sources.base import build_source
        link = self.root / "jlink"
        if os.name != "nt":
            self.skipTest("junction 只在 Windows 上测")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.out)],
                           capture_output=True, text=True)
        if r.returncode != 0 and not link.exists():
            self.skipTest("无法创建 junction")
        try:
            src = build_source(SourceConfig(id="s", name="S", type="local",
                                            root=str(self.root).replace("\\", "/"),
                                            follow_symlinks=False))
            names = {e.name for e in src.listdir("")}
            self.assertNotIn("jlink", names,
                             "目录联接被当成普通目录列出来了")
        finally:
            try:
                os.rmdir(link)
            except OSError:
                pass


class ProcLockTest(unittest.TestCase):
    """跨进程锁：拿不到要报清楚，过期要能接管，只能删自己的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msh-plock-")
        self._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = self.tmp.name
        from app.core import proclock
        proclock._token = None

    def tearDown(self):
        from app.core import proclock
        try:
            proclock.release()
        except Exception:
            pass
        proclock._token = None
        if self._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = self._old
        self.tmp.cleanup()

    def test_acquire_and_release(self):
        from app.core import proclock
        tok = proclock.acquire()
        self.assertTrue(proclock.lock_path().exists())
        proclock.release(tok)
        self.assertFalse(proclock.lock_path().exists())

    def test_second_holder_is_refused(self):
        from app.core import proclock
        proclock.acquire()
        saved = proclock._token
        proclock._token = None            # 假装是"另一个进程"
        try:
            with self.assertRaises(proclock.LockBusy):
                proclock.acquire()
        finally:
            proclock._token = saved
            proclock.release()

    def test_stale_lock_is_stolen(self):
        from app.core import proclock
        p = proclock.lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("999999|dead|0", encoding="utf-8")
        old = time.time() - (proclock.STALE_SECONDS + 60)
        os.utime(p, (old, old))
        tok = proclock.acquire()          # 不该抛
        self.assertTrue(tok)
        proclock.release(tok)

    def test_release_without_holding_is_noop(self):
        """没持锁时 `release()` 必须**什么都不做**。

        ★ 真实场景：进程重启后「恢复未完成任务」，`Jobs.finish()` 会调
          `proclock.release()`。此时本进程并没有拿到锁 —— 如果它顺手把锁
          文件删了，另一个正在运行的实例就失去了互斥保护，两边同时写文件。
        """
        from app.core import proclock
        p = proclock.lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("999|otherproc|1", encoding="utf-8")
        proclock._token = None
        try:
            proclock.release()            # 不带 token
            self.assertTrue(p.exists(), "未持锁却删掉了别人的锁")
            proclock.release("wrong")     # 带错误的 token
            self.assertTrue(p.exists(), "错误 token 却删掉了锁")
        finally:
            try:
                p.unlink()
            except OSError:
                pass

    def test_release_does_not_remove_foreign_lock(self):
        from app.core import proclock
        p = proclock.lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("1234|someoneelse|1", encoding="utf-8")
        proclock.release("mytoken")
        self.assertTrue(p.exists(), "把别人的锁删了")


class StalePartsTest(unittest.TestCase):
    """.mshpart 只清过期的，正在写的不许动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msh-parts-")
        self.lib = Path(self.tmp.name) / "lib"
        self.lib.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_parts_removed_fresh_kept(self):
        from app.core.config import SourceConfig
        from app.core import ops as OPS
        from app.sources.base import build_source
        old = self.lib / "old.mp3.mshpart"
        fresh = self.lib / "fresh.mp3.mshpart"
        keep = self.lib / "song.mp3"
        for p in (old, fresh, keep):
            p.write_bytes(b"x" * 10)
        t_old = time.time() - 48 * 3600
        os.utime(old, (t_old, t_old))
        src = build_source(SourceConfig(id="s", name="S", type="local",
                                        root=str(self.lib).replace("\\", "/")))
        n = OPS.cleanup_stale_parts(src, older_than_hours=24)
        self.assertEqual(1, n)
        self.assertFalse(old.exists(), "过期的 .mshpart 没清掉")
        self.assertTrue(fresh.exists(), "刚写出来的 .mshpart 被误删了")
        self.assertTrue(keep.exists(), "正常文件被删了")


class StateRevisionTest(unittest.TestCase):
    """revision 必须单调，且能检出纯元数据更新。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msh-rev2-")
        from app.core.db import Database
        self.db = Database(Path(self.tmp.name) / "i.sqlite3")

    def tearDown(self):
        try:
            self.db.close_all()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_rev_increases_on_every_commit(self):
        d = self.db
        r0 = d.revision()
        d.upsert_source("s", "S", "local", "/tmp/s")
        r1 = d.revision()
        d.upsert_tracks([{"source_id": "s", "path": "/tmp/s/a.mp3",
                          "rel_path": "a.mp3", "name": "a.mp3",
                          "parent": "/tmp/s", "size": 10, "mtime": 0,
                          "ext": "mp3", "title": "t0"}])
        r2 = d.revision()
        for i in range(12):
            d.update_computed("s", "/tmp/s/a.mp3", title=f"t{i}")
        r3 = d.revision()
        self.assertGreater(r1, r0)
        self.assertGreater(r2, r1)
        self.assertEqual(12, r3 - r2, "纯元数据更新没有被逐次检出")


class DeleteSourcePurgeTest(unittest.TestCase):
    """删数据源必须一起清索引，别留孤儿。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-orphan-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        from app.core.config import AppConfig, SourceConfig, save_config
        root = Path(cls.tmp.name) / "lib"
        root.mkdir()
        (root / "a.mp3").write_bytes(b"ID3" + b"\0" * 200)
        save_config(AppConfig(sources=[SourceConfig(id="s1", name="L", type="local",
                                                    root=str(root).replace("\\", "/"))]))
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.post("/api/scan/s1", json={})
        for _ in range(200):
            if not cls.client.get("/api/busy").json().get("busy"):
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        try:
            import app.deps as D
            if getattr(D, "DB", None) is not None:
                D.DB.close_all()
                D.DB = None
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def test_delete_source_removes_index_rows(self):
        import app.deps as D
        before = D.db().query("SELECT COUNT(*) c FROM track WHERE source_id='s1'")[0]["c"]
        self.assertGreater(before, 0)
        r = self.client.delete("/api/sources/s1")     # 不传 purge
        self.assertEqual(200, r.status_code, r.text[:200])
        after = D.db().query("SELECT COUNT(*) c FROM track WHERE source_id='s1'")[0]["c"]
        self.assertEqual(0, after, "删除数据源后索引里还留着孤儿行")

    def test_state_revision_exposes_rev(self):
        body = self.client.get("/api/state").json()
        self.assertIn("rev", body)
        self.assertIsInstance(body["rev"], int)


if __name__ == "__main__":
    unittest.main()
