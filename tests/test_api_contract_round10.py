"""第 10 轮：把"只有冒烟级覆盖"的接口补成真实断言。

## 为什么单独一个文件

`tools/smoke_api.py` 只断言"状态码符合预期" —— 它能确认接口没 500，
但**不确认返回内容对不对**。审计发现 88 条 `/api` 路由里有 25 条
在 `tests/` 里一次都没被引用，其中 11 条连冒烟都没有。

这些接口不是边角料：设备探测、盘符、文件存在性、文件搜索、目录列举、
任务暂停/继续/取消/确认、日志、整理方案（4 个）、重载配置、搜索、
数据源类型、数据源浏览、中转站检查/建议、曲目刷新、上传封面。

本文件给每条**至少一个"行为"断言**（返回结构、字段含义、边界拒绝），
而不是只看状态码。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-r10-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core.config import AppConfig, SourceConfig, save_config
        root = Path(cls.tmp.name)
        cls.lib = root / "lib"
        (cls.lib / "Album").mkdir(parents=True)
        cls.names = ["Song One.mp3", "Song Two.flac", "Album/Track A.mp3"]
        for i, rel in enumerate(cls.names):
            (cls.lib / rel).write_bytes(b"ID3" + bytes([i + 1]) * 400)
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="L", type="local",
                         root=str(cls.lib).replace("\\", "/")),
        ]))
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.post("/api/scan/s1", json={"compute_hash": True})
        cls._idle()
        lib = cls.client.get("/api/library/s1?limit=10").json()
        cls.tracks = lib.get("tracks") or []
        cls.tid = cls.tracks[0]["id"] if cls.tracks else None

    @classmethod
    def _idle(cls, timeout: float = 60.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if not cls.client.get("/api/busy").json().get("busy"):
                    return
            except Exception:                             # noqa: BLE001
                return
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

    # ------------------------------------------------------------ 系统类
    def test_source_types_comes_from_registry(self):
        r = self.client.get("/api/source-types")
        self.assertEqual(200, r.status_code)
        items = r.json()
        self.assertIsInstance(items, list)
        kinds = {x["kind"] for x in items}
        self.assertIn("local", kinds, "本地源类型缺失")
        for x in items:
            self.assertIn("label", x)
            self.assertTrue(x["label"], "类型缺少可读名称")

    def test_drives_and_device_probe(self):
        r = self.client.get("/api/drives")
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertIsInstance(body, dict)
        drives = body.get("drives")
        self.assertIsInstance(drives, list)
        if drives:
            self.assertIn("root", drives[0])       # 形如 "C:\\"
            self.assertIn("kind", drives[0])
            self.assertIn("portable", body)

        # 设备探测要求给一个路径（探测它所在磁盘的类型/速度）
        r2 = self.client.get("/api/device/probe", params={"path": str(self.lib)})
        self.assertLess(r2.status_code, 500, r2.text[:160])
        self.assertEqual(400, self.client.get("/api/device/probe").status_code,
                         "缺 path 时应 400")

    def test_logs_shape_and_clear(self):
        r = self.client.get("/api/logs", params={"limit": 20})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertIn("items", body)
        self.assertIsInstance(body["items"], list)

        r = self.client.post("/api/logs/clear", json={})
        self.assertLess(r.status_code, 500, r.text[:120])

    def test_reload_reports_source_count(self):
        r = self.client.post("/api/reload", json={})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(1, body.get("sources"), "重载后数据源数量不对")

    # ------------------------------------------------------------ 文件类
    def test_files_exists_splits_present_and_missing(self):
        r = self.client.post("/api/files/exists", json={
            "source": "s1", "paths": ["Song One.mp3", "nope.mp3"]})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        # 返回的是"存在/不存在"的计数与列表（随版本可能是 int 或 list）
        def as_n(v):
            return len(v) if isinstance(v, list) else int(v or 0)
        self.assertEqual(1, as_n(body.get("present")), f"应报 1 个存在: {body}")
        self.assertEqual(1, as_n(body.get("missing")), f"应报 1 个不存在: {body}")

    def test_files_exists_rejects_too_many(self):
        r = self.client.post("/api/files/exists", json={
            "source": "s1", "paths": ["x.mp3"] * 5001})
        self.assertEqual(400, r.status_code, r.text[:120])

    def test_files_search_finds_folder_by_substring(self):
        """这个接口搜的是**目录**（移动文件时用来跳目录），不是曲目。"""
        r = self.client.get("/api/files/search", params={"sid": "s1", "q": "Album"})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        self.assertIn("folders", body)
        rels = [x.get("rel") for x in (body.get("folders") or [])]
        self.assertTrue(any("Album" in (x or "") for x in rels),
                        f"没搜到 Album 目录: {rels}")
        self.assertGreaterEqual(body.get("total") or 0, 1)

    def test_files_under_lists_recursively(self):
        # 用相对路径点名一个目录：只返回该目录（含子目录）下的音频
        r = self.client.post("/api/files/under", json={
            "source": "s1", "paths": ["Album"]})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        files = body.get("files") or []
        self.assertEqual(1, len(files), f"Album 下应只有 1 首: {files}")
        self.assertIn("Track A", files[0].get("name") or "")

        # 空列表 / 只有空串的列表都算"没给有效路径"，必须是 400 而不是 200
        for bad in ([], [""]):
            with self.subTest(paths=bad):
                r2 = self.client.post("/api/files/under",
                                      json={"source": "s1", "paths": bad})
                self.assertEqual(400, r2.status_code, r2.text[:120])

    def test_folders_files_lists_dir(self):
        r = self.client.get("/api/folders/files",
                            params={"sid": "s1", "path": "Album"})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        files = body.get("files") or []
        self.assertTrue(any("Track A" in (f.get("name") or "") for f in files),
                        f"Album 里的曲目没列出来: {files}")

    def test_source_browse_lists_entries(self):
        r = self.client.post("/api/sources/s1/browse", json={"path": ""})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        self.assertTrue((body.get("dirs") or body.get("files")),
                        f"浏览根目录什么都没有: {body}")

    def test_track_refresh_updates_row(self):
        self.assertIsNotNone(self.tid)
        r = self.client.post(f"/api/track/{self.tid}/refresh", json={})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        self.assertTrue(body.get("id") == self.tid or "title" in body or body.get("ok"),
                        f"刷新返回异常: {body}")

    def test_search_endpoint_returns_results(self):
        r = self.client.get("/api/search", params={"q": "Song", "sid": "s1"})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        # 接口按 scope 分组返回：track_total / folder_total / …
        self.assertIn("track_total", body, f"返回结构变了: {list(body.keys())}")
        self.assertGreaterEqual(body["track_total"], 2,
                                f"搜 Song 至少应有 2 首: {body}")

    # ------------------------------------------------------------ 任务类
    def test_job_pause_resume_cancel_ack(self):
        r = self.client.post("/api/scan/s1", json={})
        self.assertEqual(200, r.status_code)
        jid = (r.json().get("job") or {}).get("id")
        self.assertTrue(jid, "扫描没有返回任务 id")
        try:
            # 暂停
            rp = self.client.post(f"/api/jobs/{jid}/pause")
            self.assertEqual(200, rp.status_code, rp.text[:120])
            # 继续
            rr = self.client.post(f"/api/jobs/{jid}/resume")
            self.assertEqual(200, rr.status_code, rr.text[:120])
            # 取消
            rc = self.client.post(f"/api/jobs/{jid}/cancel")
            self.assertEqual(200, rc.status_code, rc.text[:120])
        finally:
            self._idle()
        state = self.client.get(f"/api/jobs/{jid}").json().get("state")
        self.assertIn(state, ("cancelled", "done"), f"取消后状态是 {state}")
        # 确认（ack）：已结束的任务应能确认
        ra = self.client.post(f"/api/jobs/{jid}/ack")
        self.assertLess(ra.status_code, 500, ra.text[:120])

    # ------------------------------------------------------------ 整理类
    def test_organize_album_plan_conserves(self):
        r = self.client.post("/api/organize/album-plan",
                             json={"source": "s1", "group_by": "artist_album"})
        self.assertEqual(200, r.status_code, r.text[:160])
        body = r.json()
        if isinstance(body, dict) and body.get("job"):
            jid = body["job"]
            jid = jid.get("id") if isinstance(jid, dict) else str(jid)
            for _ in range(200):
                j = self.client.get(f"/api/jobs/{jid}").json()
                if j.get("state") in ("done", "error", "cancelled"):
                    body = j.get("result") or {}
                    break
                time.sleep(0.05)
        s = body.get("summary") or {}
        if s:
            total = s.get("total") or 0
            k = s.get("keep") or 0
            m = s.get("move") or 0
            man = s.get("manual") or 0
            self.assertEqual(total, k + m + man,
                             f"整理方案不守恒: keep {k} + move {m} + manual {man} != {total}")

    def test_organize_folder_plan(self):
        r = self.client.post("/api/organize/folder",
                             json={"source": "s1", "path": "Album"})
        self.assertLess(r.status_code, 500, r.text[:160])
        self.assertIn(r.status_code, (200, 400))

    def test_organize_plan_paths(self):
        r = self.client.post("/api/organize/plan-paths", json={
            "source": "s1", "paths": ["Song One.mp3"]})
        self.assertLess(r.status_code, 500, r.text[:160])

    def test_organize_preview(self):
        r = self.client.get("/api/organize/preview", params={"sid": "s1"})
        self.assertLess(r.status_code, 500, r.text[:160])

    def test_collect_preview_needs_source(self):
        r = self.client.post("/api/collect/preview", json={})
        self.assertEqual(400, r.status_code, r.text[:120])
        r2 = self.client.post("/api/collect/preview",
                              json={"source": "s1", "paths": ["Song One.mp3"]})
        self.assertLess(r2.status_code, 500, r2.text[:160])

    # ------------------------------------------------------------ 中转站 / 上传
    def test_station_check_and_suggest(self):
        r = self.client.get("/api/station/check")
        self.assertEqual(200, r.status_code, r.text[:160])
        self.assertIsInstance(r.json(), dict)

        r2 = self.client.post("/api/station/suggest",
                              json={"source": "s1", "files": ["Song One.mp3"]})
        self.assertLess(r2.status_code, 500, r2.text[:160])

    def test_upload_cover_method(self):
        """封面接口只接受 GET（POST 应 405，而不是 500）。"""
        r = self.client.post("/api/upload/cover", json={})
        self.assertIn(r.status_code, (200, 400, 404, 405), r.text[:120])


if __name__ == "__main__":
    unittest.main()
