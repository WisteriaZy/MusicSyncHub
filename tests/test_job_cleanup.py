"""任务记录"忽略/清理"的测试（隔离版）。

## 为什么要 mock proclock

上一版测试直接用 `Jobs(db)` 建独占任务，会去抢**真实数据目录**里的跨进程
写锁 —— 在整套测试里跑时会把后面的 `test_move` 全部带崩（LockBusy）。
这里把 `proclock.acquire/release` 打桩：任务生命周期照测，但不碰真实锁文件。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))


class JobDismissTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="msh-dj-")
        # 隔离 proclock：不让测试去碰真实数据目录的锁文件
        self._p1 = mock.patch("app.core.proclock.acquire",
                              return_value="test-token")
        self._p2 = mock.patch("app.core.proclock.release",
                              return_value=None)
        self._p1.start()
        self._p2.start()
        from app.core.db import Database
        from app.core.jobs import Jobs
        self.db = Database(Path(self._tmp.name) / "idx.sqlite3")
        self.jobs = Jobs(self.db)

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        try:
            self.db.close_all()
        except Exception:
            pass
        self._tmp.cleanup()

    def _finished(self, kind="sync", state="done"):
        st = self.jobs.create(kind, total=1)
        st.done = 1
        self.jobs.finish(st)
        if state != "done":
            st.state = state
        return st

    # ------------------------------------------------------------ 核心
    def test_dismiss_removes_from_list_and_db(self):
        st = self._finished()
        self.assertEqual(1, len(self.jobs.list()))
        self.assertTrue(self.jobs.dismiss(st.id))
        self.assertEqual([], self.jobs.list(), "内存里还在")
        self.assertIsNone(self.db.get_job(st.id), "DB 里还在（重启会复活）")

    def test_running_job_cannot_be_dismissed(self):
        st = self.jobs.create("sync", total=1)      # 仍是 running
        self.assertFalse(self.jobs.dismiss(st.id), "运行中的任务不该能忽略")
        self.assertIsNotNone(self.jobs.get(st.id))

    def test_dismiss_finished_clears_all_terminal(self):
        self._finished()
        self._finished()
        self._finished(state="error")
        running = self.jobs.create("scan", total=1)
        n = self.jobs.dismiss_finished()
        self.assertEqual(3, n)
        left = {j["id"] for j in self.jobs.list()}
        self.assertEqual({running.id}, left, f"剩下的应该是运行中的：{left}")

    def test_ack_still_works(self):
        st = self._finished()
        self.assertTrue(self.jobs.ack(st.id))

    def test_dismiss_unknown_id_is_safe(self):
        self.assertTrue(self.jobs.dismiss("nonexistent-id"))


if __name__ == "__main__":
    unittest.main()
