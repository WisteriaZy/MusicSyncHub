"""2026-10-20 修复的守护测试：崩溃恢复链（K2）、超时后不删源（K1）、
跨站写请求（K17）、查重取消状态（K11）、预演一致快照（K9）、步骤上限（K16）。"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest


class RecoveryChainTest(unittest.TestCase):
    """★ K2：进程被杀后重启，"接着做完"必须真的可用。

    修复前实测三种情况全挂：
      · 运行中被杀 → 步骤读得回来，但启动提示**永远不出现**（state 被写成 error，
        而 /api/unfinished 只认 running/paused）
      · 步骤多（约 700+）→ JSON 被 [:200000] 截成半截，步骤全丢
      · 暂停后被杀 → finish() 用结果摘要覆盖了存步骤的 detail，步骤全丢
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="msh-rec-")
        from app.core.db import Database
        self.db = Database(os.path.join(self.tmp, "idx.sqlite3"))
        from app.core import jobs as J
        self.J = J
        self._saved = J.jobs

    def tearDown(self):
        self.J.jobs = self._saved
        self.db.close_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restart(self):
        self.J.jobs = None
        self.J.init(self.db)
        self.J.get_jobs().load_unfinished()      # main.lifespan 启动时做的事
        return self.J.get_jobs()

    def _steps(self, n):
        return [{"id": f"s{i}", "op": "copy", "src": "a", "dst": "b",
                 "src_path": f"d/{'x' * 60}/{i:05d}.flac",
                 "dst_path": f"d/{'x' * 60}/{i:05d}.flac"} for i in range(n)]

    def _check(self, n, pause_then_kill):
        jobs = self._restart()
        st = jobs.create("sync", total=n, exclusive=False)
        jobs.record_steps(st, self._steps(n))
        if pause_then_kill:
            st.pause = True
            st.done = n // 2
            jobs.finish(st, result={"summary": {"done": st.done}})
        jid = st.id
        jobs = self._restart()
        unf = [j for j in jobs.unfinished() if j["id"] == jid]
        self.assertTrue(unf, "重启后应该提示上次没做完")
        self.assertTrue(unf[0]["recoverable"])
        self.assertEqual(n, len(jobs.get(jid).steps), "步骤必须完整读回")
        # 处理过之后再重启：不应再提示
        jobs.settle_interrupted(jobs.get(jid), "已处理")
        jobs = self._restart()
        self.assertFalse([j for j in jobs.unfinished() if j["id"] == jid],
                         "处理过的中断任务重启后不应再提示")

    def test_killed_while_running(self):
        self._check(100, False)

    def test_many_steps_not_truncated(self):
        self._check(1500, False)

    def test_killed_after_pause(self):
        self._check(100, True)


class TimeoutKeepsSourceTest(unittest.TestCase):
    """★ K1：单步超时后，后台线程复制完**不能**再删源。

    修复前：界面报"超时（已跳过，可重试）"，几秒后那个线程把复制做完、
    把源移进回收站 —— 界面说失败，磁盘上已经搬完，索引也没跟上。
    """

    def test_transfer_after_timeout_keeps_source(self):
        from app.core import ops as O
        from app.core.config import SourceConfig
        from app.sources.local import LocalSource

        tmp = tempfile.mkdtemp(prefix="msh-k1-")
        a, b = os.path.join(tmp, "A"), os.path.join(tmp, "B")
        os.makedirs(a)
        os.makedirs(b)
        with open(os.path.join(a, "x.mp3"), "wb") as fh:
            fh.write(b"AUDIO" * 100)
        sa = LocalSource(SourceConfig(id="sa", name="sa", type="local", root=a))
        sb = LocalSource(SourceConfig(id="sb", name="sb", type="local", root=b))

        # 让复制慢到超过单步超时
        real_copy = sb.copy_from
        done = threading.Event()

        def slow_copy(*args, **kw):
            time.sleep(0.6)
            try:
                return real_copy(*args, **kw)
            finally:
                done.set()
        sb.copy_from = slow_copy

        old = O.STEP_TIMEOUT_SEC
        O.STEP_TIMEOUT_SEC = 0.2
        try:
            out = O.Executor({"sa": sa, "sb": sb}).run([
                {"id": "t", "op": "transfer", "src": "sa", "dst": "sb",
                 "src_path": "x.mp3", "dst_path": "x.mp3"}],
                execute=True, workers=1)
            r = out["results"][0]
            self.assertEqual("error", r["state"])
            self.assertTrue(r.get("timeout"), r)
            self.assertTrue(done.wait(5), "后台复制线程应该能跑完")
            time.sleep(0.3)                       # 给"删源"那一步留出时间
            self.assertTrue(os.path.exists(os.path.join(a, "x.mp3")),
                            "超时后源文件必须保留")
            self.assertTrue(os.path.exists(os.path.join(b, "x.mp3")),
                            "目标是完整写好的")
        finally:
            O.STEP_TIMEOUT_SEC = old
            shutil.rmtree(tmp, ignore_errors=True)


class CrossSiteWriteTest(unittest.TestCase):
    """★ K17：CORS 不再全开，跨站的写请求一律 403。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def test_foreign_origin_write_rejected(self):
        for origin in ("http://evil.example", "null"):
            r = self.client.post("/api/logs/clear", headers={"Origin": origin})
            self.assertEqual(403, r.status_code, origin)

    def test_same_origin_and_no_origin_allowed(self):
        r = self.client.post("/api/logs/clear",
                             headers={"Origin": "http://testserver"})
        self.assertEqual(200, r.status_code)
        r = self.client.post("/api/logs/clear")          # curl / 脚本
        self.assertEqual(200, r.status_code)

    def test_reads_not_blocked(self):
        r = self.client.get("/api/system", headers={"Origin": "http://evil.example"})
        self.assertEqual(200, r.status_code)

    def test_no_wildcard_cors(self):
        r = self.client.get("/api/system", headers={"Origin": "http://evil.example"})
        self.assertNotEqual("*", r.headers.get("access-control-allow-origin"))


class SmallContractTest(unittest.TestCase):

    def test_k11_dedup_cancel_not_overwritten(self):
        """查重被取消后，状态不能被改回 done。"""
        import inspect
        from app import upload_api
        src = inspect.getsource(upload_api)
        self.assertNotIn('job.state = "cancelled"', src,
                         "状态应交给 finish() 定，不要直接写")

    def test_k9_make_plan_requires_snapshot(self):
        import inspect
        from app.routes import compare
        self.assertIn("ensure_free()", inspect.getsource(compare.make_plan))

    def test_k16_max_steps_server_capped(self):
        import inspect
        from app.routes import compare, files
        for fn in (compare.execute_plan, files.files_move):
            self.assertIn("min(int(payload.get(\"max_steps\")", inspect.getsource(fn),
                          f"{fn.__name__} 的步骤上限必须由服务端封顶")


class MatchingDeterminismTest(unittest.TestCase):

    def test_a7_base_key_strips_multi_version(self):
        """★ A7：多标记版本（`instrumental|remix`）的 base_key 必须和原曲一样。"""
        from app.core import matching as M
        orig = M.group_to_dict([{"path": "/a.flac", "name": "a.flac",
                                 "dup_key": "晴天|周杰伦|196", "size": 1}])
        multi = M.group_to_dict([{"path": "/b.flac", "name": "b.flac",
                                  "dup_key": "晴天|周杰伦|196|instrumental|remix", "size": 1}])
        one = M.group_to_dict([{"path": "/c.flac", "name": "c.flac",
                                "dup_key": "晴天|周杰伦|196|live", "size": 1}])
        self.assertEqual("晴天|周杰伦|196", orig["base_key"])
        self.assertEqual(orig["base_key"], multi["base_key"])
        self.assertEqual(orig["base_key"], one["base_key"])

    def test_a3_representative_is_deterministic(self):
        """★ A3：组的代表项不能依赖行序。

        合并门禁比的是**代表项**的 name_key。原来代表项 = 名字最长的那个，
        同长取第一个 —— 组里两个等长、name_key 不同的成员谁排前面，
        决定了它能不能和隔壁组合并。这里直接构造这种组。
        """
        from app.core import matching as M
        # 组 G：同一个 dup_key、两个等长成员，name_key 不同
        g1 = {"path": "/s/1/aaaa.mp3", "name": "aaaa.mp3", "name_key": "aaaa"}
        g2 = {"path": "/s/2/bbbb.mp3", "name": "bbbb.mp3", "name_key": "bbbb"}
        captured = []
        real = M.score_pair

        def spy(a, b, tol):
            captured.append(a.get("path"))
            return real(a, b, tol)

        def pick(order):
            M.score_pair = spy
            captured.clear()
            rows = []
            for r in order:
                rows.append({**r, "source_id": "s", "rel_path": r["path"][3:],
                             "title": "T", "artist": "A", "duration": 200.0,
                             "size": 1, "quality": 1, "dup_key": "t|a|100"})
            # 隔壁组：name_key 只和 g1 相同，迫使门禁看代表项
            rows.append({"path": "/s/3/aaaa.flac", "name": "aaaa.flac", "name_key": "aaaa",
                         "source_id": "s", "rel_path": "3/aaaa.flac", "title": "T2",
                         "artist": "A", "duration": 200.0, "size": 1, "quality": 1,
                         "dup_key": "t2|a|100"})
            try:
                gs = M.group_duplicates(rows)
            finally:
                M.score_pair = real
            return sorted(tuple(sorted(x["path"] for x in g)) for g in gs)

        self.assertEqual(pick([g1, g2]), pick([g2, g1]),
                         "组内成员顺序变了，查重分组结果也变了")

    def test_a4_transitive_merge_respects_veto(self):
        """★ A4：A~B、B~C 都认，A~C 互相否决 → 不能三份并成一组。

        `Pink Floyd` / `Pink Floyd Live Band` / `Live Band`：并查集只看连通，
        会把三份并成"保留一份、清理其它"，两首不同的歌里有一首被建议删掉。
        """
        from app.core import matching as M, paths as P

        def rec(sid, artist):
            r = {"source_id": sid, "path": f"/{sid}/Echoes.mp3", "rel_path": "Echoes.mp3",
                 "name": "Echoes.mp3", "title": "Echoes", "artist": artist, "album": "",
                 "duration": 300.0, "size": 5000000, "quality": 1}
            r["name_key"] = P.name_key(r["name"])
            r["dup_key"] = M.dup_key(r)
            return r

        a, b, c = rec("a", "Pink Floyd"), rec("b", "Pink Floyd Live Band"), rec("c", "Live Band")
        self.assertFalse(M.fuzzy_admissible(M.score_pair(a, c, 2.0)), "前提：A~C 应互相否决")
        groups = M.merge_matched_pairs([(a, b), (b, c)])
        for g in groups:
            keys = {r["source_id"] for r in g}
            self.assertFalse({"a", "c"} <= keys, f"互相否决的 A、C 被并进同一组：{keys}")
        # 每个文件最多出现在一个组里（拆组不能重新引入重叠）
        seen = [r["source_id"] for g in groups for r in g]
        self.assertEqual(len(seen), len(set(seen)))
        # 可以合法成组的那一对仍然要保留
        self.assertEqual(1, len(groups))
        self.assertEqual(2, len(groups[0]))


if __name__ == "__main__":
    unittest.main()
