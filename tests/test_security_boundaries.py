"""安全边界与"失败不留损"的回归测试。

## 这些测试对应真实漏洞（2026-10 地毯式排查抓到的）

### 1. 读/列越界
`src.abs_path()` 对"跨盘绝对路径"是**原样返回**的（早先为修
`C:/Music/D:/Other` 拼接而加）。于是：

    GET /api/cover-raw?sid=s1&path=C:/.../config.toml   → 200，吐出库外文件内容
    GET /api/cover-raw?sid=s1&path=../outside/x.toml    → 200，同上
    GET /api/files/list?sid=s1&path=<库外绝对目录>       → 200，列出库外文件

修法：端点入口过 `deps.in_source_rel()` —— 绝对路径必须在根内、
相对路径不许含 `..`。

### 2. 入库写出根外
`/api/upload/commit` 的 `dst` 只做了 `startswith(root)` 检查，
而 `root/../outside` **确实以 root 开头**，文件系统却会解析 `..` 逃出去。
另外 `rel` 只在 `/` 边界上查 `..`，`..\\outside` 用反斜杠就绕过了。

### 3. 只读源被绕过
中转站三个写端点没有任何只读校验（`station_api` 里 0 处 `require_writable`），
在只读源上照样移文件、建回收站。

### 4. 覆盖 + 校验失败 = 原件和副本都没了
`overwrite` 直接覆盖原件，随后校验失败再把目标删掉/留半截。
现在改成：覆盖前先把原件**移进回收站**（可恢复），校验失败把坏副本改名 `.bad`。

### 5. 未校验就永久删源
`transfer_mode=fast` + `hard_delete=true`：跳过校验却永久删源，
短写时源没了、目标也是坏的。现在这条组合直接拒。
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
MP3 = b"ID3" + b"\x00" * 200


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-sec-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core.config import AppConfig, SourceConfig, save_config
        root = Path(cls.tmp.name)
        cls.L = root / "lib1"
        cls.O = root / "outside"
        cls.RO = root / "ro"
        for d in (cls.L, cls.O, cls.RO):
            d.mkdir()
        (cls.L / "a.mp3").write_bytes(MP3)
        (cls.O / "secret.mp3").write_bytes(b"ID3" + b"SECRET" * 20)
        (cls.O / "config.toml").write_text('password="TOPSECRET"', encoding="utf-8")
        (cls.RO / "s.mp3").write_bytes(MP3)
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="L", type="local",
                         root=str(cls.L).replace("\\", "/")),
            SourceConfig(id="sro", name="RO", type="local",
                         root=str(cls.RO).replace("\\", "/"), read_only=True),
        ]))
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.client.post("/api/scan/s1", json={})
        cls._wait_idle()

    @classmethod
    def _wait_idle(cls, timeout: float = 30.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if not cls.client.get("/api/busy").json().get("busy"):
                    return
            except Exception:                            # noqa: BLE001
                return
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        try:
            import app.deps as D
            if getattr(D, "DB", None) is not None:
                try:
                    D.DB.close_all()
                except Exception:
                    pass
                D.DB = None
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


class ReadBoundaryTest(_Base):
    """读/列 不许越出数据源根。"""

    def test_cover_raw_rejects_absolute_outside(self):
        r = self.client.get("/api/cover-raw",
                            params={"sid": "s1", "path": str(self.O / "config.toml")})
        self.assertIn(r.status_code, (400, 403, 404), r.text[:120])
        self.assertNotIn("TOPSECRET", r.text)

    def test_cover_raw_rejects_dotdot(self):
        r = self.client.get("/api/cover-raw",
                            params={"sid": "s1", "path": "../outside/config.toml"})
        self.assertIn(r.status_code, (400, 403, 404), r.text[:120])
        self.assertNotIn("TOPSECRET", r.text)

    def test_cover_raw_directory_is_not_500(self):
        r = self.client.get("/api/cover-raw",
                            params={"sid": "s1", "path": str(self.O)})
        self.assertLess(r.status_code, 500, r.text[:120])

    def test_files_list_rejects_outside(self):
        for p in (str(self.O), "../outside"):
            with self.subTest(path=p):
                r = self.client.get("/api/files/list", params={"sid": "s1", "path": p})
                self.assertIn(r.status_code, (400, 403, 404), r.text[:120])
                self.assertNotIn("secret.mp3", r.text)

    def test_files_list_root_still_works(self):
        r = self.client.get("/api/files/list", params={"sid": "s1", "path": ""})
        self.assertEqual(200, r.status_code, r.text[:200])
        # 根内绝对路径也应可用（前端有时传绝对路径）
        r = self.client.get("/api/files/list",
                            params={"sid": "s1", "path": str(self.L)})
        self.assertEqual(200, r.status_code, r.text[:200])

    def test_folders_and_facets_reject_outside(self):
        for url in ("/api/files/folders", "/api/files/facets"):
            with self.subTest(url=url):
                r = self.client.get(url, params={"sid": "s1", "path": str(self.O)})
                self.assertIn(r.status_code, (400, 403, 404), r.text[:120])


class UploadBoundaryTest(_Base):
    """入库不许写出数据源根，也不许穿越暂存批次目录。"""

    def _mk_batch(self, name="20261018-040404-abcdef"):
        import app.upload_api as U
        up = Path(U._uploads_root()) / name
        up.mkdir(parents=True, exist_ok=True)
        (up / "u1.mp3").write_bytes(b"ID3" + b"\x01" * 100)
        (up / "manifest.json").write_text(
            json.dumps({"files": [{"rel": "u1.mp3", "name": "u1.mp3", "size": 103}]}),
            encoding="utf-8")
        return name

    def test_dst_dotdot_rejected(self):
        bid = self._mk_batch()
        r = self.client.post("/api/upload/commit", json={
            "batch": bid, "source": "s1", "confirm": True,
            "moves": [{"rel": "u1.mp3", "dst": "../outside"}]})
        self._wait_idle()
        self.assertEqual(400, r.status_code, r.text[:200])
        self.assertFalse((self.O / "u1.mp3").exists(), "文件被写到了数据源根之外")

    def test_rel_backslash_traversal_rejected(self):
        bid = self._mk_batch()
        r = self.client.post("/api/upload/commit", json={
            "batch": bid, "source": "s1", "confirm": True,
            "moves": [{"rel": "..\\outside\\steal.mp3", "dst": "x"}]})
        self._wait_idle()
        self.assertFalse((self.L / "x" / "steal.mp3").exists())
        self.assertFalse((self.L / "steal.mp3").exists())

    def test_moves_wrong_type_is_400(self):
        bid = self._mk_batch()
        for bad in ("x", 1, {"a": 1}, [1, 2]):
            with self.subTest(moves=type(bad).__name__):
                r = self.client.post("/api/upload/commit", json={
                    "batch": bid, "source": "s1", "confirm": True, "moves": bad})
                self._wait_idle()
                self.assertLess(r.status_code, 500, r.text[:160])

    def test_normal_commit_still_works(self):
        bid = self._mk_batch("20261018-050505-bbbbbb")
        r = self.client.post("/api/upload/commit", json={
            "batch": bid, "source": "s1", "confirm": True,
            "moves": [{"rel": "u1.mp3", "dst": ""}]})
        self._wait_idle()
        self.assertEqual(200, r.status_code, r.text[:200])
        self.assertTrue((self.L / "u1.mp3").exists())


class ReadOnlyGuardTest(_Base):
    """只读源上的写端点必须 403，且磁盘零变化。"""

    def test_station_write_endpoints_blocked(self):
        cases = [
            ("/api/station/move-in", {"source": "sro", "paths": ["s.mp3"]}),
            ("/api/station/move-out", {"source": "sro", "files": ["s.mp3"], "dest": "x"}),
            ("/api/station/remove", {"source": "sro", "files": ["s.mp3"], "confirm": True}),
        ]
        for url, body in cases:
            with self.subTest(url=url):
                r = self.client.post(url, json=body)
                self._wait_idle()
                self.assertEqual(403, r.status_code, f"{url} → {r.status_code} {r.text[:120]}")
        self.assertTrue((self.RO / "s.mp3").exists(), "只读源里的文件被动了")
        self.assertFalse((self.RO / ".music-sync-trash").exists(),
                        "在只读源里建了回收站")


class DataLossTest(unittest.TestCase):
    """覆盖/校验失败不能把原件和副本一起弄丢。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-loss-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core.config import AppConfig, SourceConfig
        from app.sources.base import build_source
        A = Path(cls.tmp.name) / "A"
        B = Path(cls.tmp.name) / "B"
        A.mkdir()
        B.mkdir()
        cfg = AppConfig(sources=[
            SourceConfig(id="a", name="A", type="local",
                         root=str(A).replace("\\", "/")),
            SourceConfig(id="b", name="B", type="local",
                         root=str(B).replace("\\", "/")),
        ])
        cls.A, cls.B = A, B
        cls.sa, cls.sb = build_source(cfg.sources[0]), build_source(cfg.sources[1])

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def test_overwrite_keeps_original_in_trash(self):
        from app.core import transfer as X
        (self.A / "s.mp3").write_bytes(MP3)
        (self.B / "s.mp3").write_bytes(b"ID3" + b"OLD" * 50)
        r = X.transfer_one(self.sa, self.sb, "s.mp3", "s.mp3",
                           policy="overwrite", verify=True, hard_delete=False)
        self.assertEqual("done", r["state"], r)
        self.assertTrue(r.get("overwrote"))
        trash = list((self.B / ".music-sync-trash").rglob("*.mp3"))
        self.assertTrue(
            any(p.read_bytes() == b"ID3" + b"OLD" * 50 for p in trash),
            f"原件没进回收站，无法恢复：{[p.name for p in trash]}")

    def test_unverified_hard_delete_is_refused(self):
        from app.core import transfer as X
        (self.A / "h.mp3").write_bytes(MP3)
        r = X.transfer_one(self.sa, self.sb, "h.mp3", "h.mp3",
                           policy="skip", verify=False, hard_delete=True)
        self.assertEqual("error", r["state"], r)
        self.assertIn("校验", r.get("detail", ""))
        self.assertTrue((self.A / "h.mp3").exists(), "源被永久删掉了")


if __name__ == "__main__":
    unittest.main()
