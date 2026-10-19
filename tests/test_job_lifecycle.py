"""任务/流程体系的核心不变量（批次 1 的验收）。

覆盖：
  · 任务能到达终态（不会永远 running）
  · 终态后独占位释放，后续写操作不被 409 挡住
  · 同一 kind 能再次发起
  · 失败路径也释放独占位
  · 暂停占位、暂停不算卡死
  · `init` 幂等（不会把注册表换掉）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MP3 = b"\xff\xfb\x90\x00" + bytes(413)


class JobLifecycleBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="msh-jobs-", ignore_cleanup_errors=True)
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "i.sqlite3")
        cls.root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(os.path.join(cls.root, "Album"), exist_ok=True)
        for i in range(3):
            with open(os.path.join(cls.root, "Album", f"t{i}.mp3"), "wb") as fh:
                fh.write(MP3)
        from app.core.config import (AppConfig, Settings, SourceConfig,
                                     load_config, save_config)
        save_config(AppConfig(
            sources=[SourceConfig(id="s1", name="t", type="local",
                                  root=cls.root)],
            settings=Settings(station_dir=os.path.join(cls._tmp.name, "st"),
                              staging_dir=os.path.join(cls._tmp.name, "up"))))
        load_config(force=True)
        from fastapi.testclient import TestClient

        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            from app import main as m
            m.DB.close()
            m.DB = None
        except Exception:
            pass
        try:
            from app.core.jobs import reset_for_tests
            reset_for_tests()
        except Exception:
            pass
        try:
            from app.sources import manager
            manager.close_all()
        except Exception:
            pass
        import gc
        gc.collect()
        cls._tmp.cleanup()

    def setUp(self):
        """每个用例前把索引库和任务注册表都重新对齐到本类的临时库。

        为什么必须显式做：同一个进程里换 `MSH_DB` 是不寻常的动作，
        `core.jobs` 的单例不会自动跟着换（自动换反而会引入
        "任务跑着跑着注册表被换掉"这种更难查的问题）。
        别的测试类跑完可能留下 `main.DB` 和注册表指向**它们自己的**临时库，
        于是在全量跑时这里建的任务会被记到别的库上，表现是
        "任务建好立刻从列表里消失" —— 单独跑却完全正常。
        """
        from app import main as m
        from app.core import config as cfg_mod
        from app.core.jobs import reset_for_tests
        cfg_mod.load_config(force=True)
        try:
            if m.DB is not None:
                m.DB.close_all()
        except Exception:
            pass
        m.DB = None
        reset_for_tests()
        m.db()                       # 重建到本类的库，并绑好注册表

    def _wait(self, jid, timeout=25.0):
        from app.core.jobs import get_jobs
        end = time.time() + timeout
        while time.time() < end:
            for j in get_jobs().list():
                if j["id"] == jid and j["state"] != "running":
                    return j
            time.sleep(0.15)
        for j in get_jobs().list():
            if j["id"] == jid:
                return j
        return None


class JobRegistryTest(JobLifecycleBase):
    """注册表本身的不变量。"""

    def test_init_is_idempotent(self):
        """★ `init` 不能重建注册表。

        踩过：`init_jobs` 原来无条件 `jobs = Jobs(db)`，而 `main.db()`
        在 `DB is None` 时会调它。于是"启动前先碰过 db()"的路径
        （测试、脚本）会让启动时**把整个任务注册表换掉** ——
        已建的任务连同独占位一起凭空消失：界面没了、`/api/busy` 说没人忙，
        可任务其实还在跑。实测表现是"任务建好 1.5 秒后从列表里消失"。
        """
        from app.core import jobs as J
        st = J.get_jobs().create("sync", total=1)
        before = id(J.get_jobs())
        J.init(J.get_jobs().db)                 # 再 init 一次
        after_id = id(J.get_jobs())
        self.assertEqual(before, after_id, "init 把注册表换掉了")
        self.assertTrue(any(x["id"] == st.id for x in J.get_jobs().list()),
                        "init 之后任务丢了")
        J.get_jobs().finish(st)

    def test_job_reaches_terminal_state(self):
        """扫描任务必须到达终态，不能永远停在 running。

        失败时把任务列表、注册表实例、心跳、索引条数全打出来 ——
        原来只有一句"任务从列表里消失了"，完全指不出原因
        （真正的原因是注册表被换掉，离报错点非常远）。
        """
        from app.core.jobs import get_jobs
        from app import main as _m
        r = self.client.post("/api/scan/s1", json={"workers": 1})
        self.assertEqual(200, r.status_code, r.text)
        jid = r.json()["job"]["id"]
        end = time.time() + 25.0
        seen = []
        while time.time() < end:
            js = get_jobs().list()
            mine = [x for x in js if x["id"] == jid]
            seen.append((len(js), mine[0]["state"] if mine else None,
                         mine[0].get("done") if mine else None))
            if mine and mine[0]["state"] != "running":
                break
            time.sleep(0.2)
        js = get_jobs().list()
        mine = [x for x in js if x["id"] == jid]
        diag = (f"\n  任务 {jid}\n"
                f"  注册表实例 id={id(get_jobs())}  "
                f"db={getattr(get_jobs().db, 'path', None)}\n"
                f"  main.DB={getattr(_m.DB, 'path', None)}\n"
                f"  列表共 {len(js)} 条: "
                f"{[(x['id'], x['kind'], x['state']) for x in js]}\n"
                f"  本任务仍在列表: {bool(mine)}\n"
                f"  索引条数: {len(_m.db().all_tracks('s1'))}\n"
                f"  采样: {seen[:5]} ... {seen[-3:]}")
        self.assertTrue(mine, f"任务从列表里消失了{diag}")
        self.assertEqual("done", mine[0]["state"], f"没到终态{diag}")

    def test_slot_released_after_done(self):
        """★ 任务结束后独占位必须释放，否则用户被锁死。"""
        from app.core.jobs import get_jobs
        jid = self.client.post("/api/scan/s1",
                               json={"workers": 1}).json()["job"]["id"]
        self._wait(jid)
        self.assertFalse(get_jobs().busy(), "独占位没释放")
        self.assertFalse(self.client.get("/api/busy").json()["busy"])
        # 后续写操作不该被 409 挡住
        r = self.client.post("/api/folders/create", json={
            "source": "s1", "parent": self.root, "name": "AfterScan"})
        self.assertNotEqual(409, r.status_code,
                            f"扫描结束后写操作仍被挡住：{r.text}")

    def test_same_kind_can_run_again(self):
        jid1 = self.client.post("/api/scan/s1",
                                json={"workers": 1}).json()["job"]["id"]
        self._wait(jid1)
        r2 = self.client.post("/api/scan/s1", json={"workers": 1})
        self.assertEqual(200, r2.status_code,
                         f"同一 kind 无法再次发起：{r2.text}")
        self._wait(r2.json()["job"]["id"])

    def test_failed_job_releases_slot(self):
        """失败路径也要释放独占位。

        用一个不存在的数据源触发失败：不能 500，也不能留下卡住的任务。
        """
        from app.core.jobs import get_jobs
        before = get_jobs().busy()
        self.assertIsNone(before)
        r = self.client.post("/api/scan/__nope__", json={"workers": 1})
        self.assertIn(r.status_code, (400, 404), r.text)
        self.assertFalse(get_jobs().busy(), "失败后仍占着独占位")

    def test_busy_reports_the_holder(self):
        """有人占位时 /api/busy 要报出**是谁**，界面才能说明白。"""
        from app.core.jobs import get_jobs
        st = get_jobs().create("sync", total=3)
        try:
            b = self.client.get("/api/busy").json()
            self.assertTrue(b["busy"])
            self.assertEqual(st.id, b["job"]["id"])
        finally:
            get_jobs().finish(st)

    def test_paused_still_occupies_slot(self):
        """★ 暂停中的任务**仍然占用**独占位。

        它随时可能继续；这时让别的流程插进来会操作同一批文件。
        """
        from app.core.jobs import get_jobs
        st = get_jobs().create("sync", total=3)
        st.pause = True
        get_jobs().finish(st)
        self.assertEqual("paused", st.state)
        self.assertTrue(get_jobs().busy(), "暂停的任务没占位")
        r = self.client.post("/api/folders/create", json={
            "source": "s1", "parent": self.root, "name": "WhilePaused"})
        self.assertEqual(409, r.status_code, "暂停时写操作没被挡住")
        # 收尾
        st.state = "cancelled"
        st.pause = False

    def test_paused_is_not_stale(self):
        """★ 暂停**不算卡死**。心跳本来就该停，否则会被误判成中断。"""
        from app.core.jobs import get_jobs
        st = get_jobs().create("sync", total=3)
        st.pause = True
        get_jobs().finish(st)
        self.assertEqual("paused", st.state)
        # 把心跳回拨到很久以前，模拟"暂停了很久"
        st.heartbeat = time.time() - 99999
        get_jobs().list()                       # 触发回收
        self.assertEqual("paused", st.state,
                         "暂停中的任务被当成卡死回收了")
        st.state = "cancelled"
        st.pause = False


class ExclusiveSlotSemanticsTest(unittest.TestCase):
    """★ 独占位的判据必须是"建任务时实际生效的 exclusive"，不是按 kind 猜。

    这个区别是实打实的 bug：`run_in_background` 一律用 `exclusive=False`
    建任务（跑的是**只读计算**，本来就不该挡写操作），但 `_busy_locked`
    和 `to_dict` 都按 `kind in EXCLUSIVE_KINDS` 判断 ——
    于是 `run_in_background("organize", …)` 一边"没占独占位"、
    一边**真的把写操作全挡住了**：实测那 6 秒里
    `/api/tags/write`、`/api/tags/restore` 全返回 409。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="msh-excl-")
        os.environ["MSH_DATA_DIR"] = self._tmp.name
        from app.core import jobs as J
        from app.core.db import Database
        self.J = J
        self.db = Database(os.path.join(self._tmp.name, "i.sqlite3"))
        self.reg = J.Jobs(self.db)

    def tearDown(self):
        # Windows 上 sqlite 文件被占用就删不掉临时目录 —— 先把连接关干净
        try:
            self.J.reset_for_tests()
        except Exception:
            pass
        try:
            self.db.close_all()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_readonly_bg_job_does_not_occupy_slot(self):
        """只读后台任务**不能**挡住独占任务。"""
        from app.core.bgjob import run_in_background
        # organize 在 EXCLUSIVE_KINDS 里，但我们用 exclusive=False 建它
        j = run_in_background("organize", 100, lambda job: {"ok": 1})
        try:
            self.assertFalse(j.to_dict()["exclusive"],
                             "只读后台任务被报成独占")
            self.assertIsNone(self.reg.busy(), "只读后台任务占了独占位")
            # 而且这时真的能建一个独占任务
            jx = self.reg.create("sync", total=1)
            self.reg.finish(jx)
        finally:
            self.J.get_jobs().finish(j)

    def test_real_exclusive_job_still_blocks(self):
        """真的独占任务仍然要挡住第二个（不能修过头）。"""
        j = self.reg.create("sync", total=1)
        self.assertTrue(j.to_dict()["exclusive"])
        self.assertIsNotNone(self.reg.busy())
        with self.assertRaises(self.J.JobBusy):
            self.reg.create("sync", total=1)
        self.reg.finish(j)
        self.assertIsNone(self.reg.busy(), "结束之后没释放独占位")

    def test_exclusive_field_matches_creation_flag(self):
        """`to_dict()['exclusive']` 必须等于建任务时传的值。"""
        for want in (True, False):
            with self.subTest(exclusive=want):
                j = self.reg.create("sync", total=1, exclusive=want)
                self.assertEqual(want, j.to_dict()["exclusive"])
                self.reg.finish(j)


if __name__ == "__main__":
    unittest.main()
