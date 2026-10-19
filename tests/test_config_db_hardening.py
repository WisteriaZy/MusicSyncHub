"""配置与索引库的健壮性：坏文件、旧库、非法设置都不能让应用起不来。

## 对应真实问题（2026-10 并发/配置/DB 审计）

### 1. 旧版索引库缺列 → 应用**启动失败**
`SCHEMA` 里既有建表也有建索引，而索引建在 `name_key` / `dup_key` /
`hash_quick` 上。老库没有这些列时先执行 `CREATE INDEX` 就炸：

    sqlite3.OperationalError: no such column: name_key

从旧版升级的用户会直接打不开。修法：**建表 → 补列 → 建索引**。

### 2. config.toml 改坏 → 应用**启动失败**
手改配置是文档鼓励的用法，但少一个括号、重复写一节、存成 GBK、
`[[sources]]` 少个 `name`，都会让启动 rc=1，且没有任何界面能告诉用户原因。
修法：备份坏文件 → 用默认配置照常启动 → 把原因记进 `settings_errors`。

### 3. 设置校验的三处松口
- `scan_workers = true` → `float(True)=1.0`，被当成 1 悄悄吃进去
- `scan_workers = 1.9` → `int(1.9)=1`，用户以为设了 1.9
- `trash_dir = {"a": 1}` → 存成 `"{'a': 1}"`，之后当**相对路径**去 join
- 未知字段（`max_workers`）→ 静默 200，客户端以为存上了

### 4. 写盘/写库失败留半截状态
- config.toml 只读：内存配置变了、磁盘没变（GET 显示新值，重启又变回）
- 建数据源：config 写了、DB 没写（界面有源、一首歌都没有，重建还 409）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent


class OldDatabaseTest(unittest.TestCase):
    """旧版库缺列时必须能平滑升级，不能打不开。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msh-olddb-")
        self.db_path = Path(self.tmp.name) / "old.sqlite3"

    def tearDown(self):
        try:
            from app.core.db import Database
            Database.close_all()
        except Exception:
            pass
        self.tmp.cleanup()

    def _make_old(self, missing: str) -> None:
        cols = {"id": "INTEGER PRIMARY KEY", "source_id": "TEXT", "path": "TEXT",
                "rel_path": "TEXT", "name": "TEXT", "parent": "TEXT", "ext": "TEXT",
                "size": "INTEGER", "mtime": "REAL", "title": "TEXT", "artist": "TEXT",
                "album": "TEXT", "duration": "REAL", "name_key": "TEXT",
                "dup_key": "TEXT", "quality": "REAL", "hash_quick": "TEXT",
                "hash_full": "TEXT", "hash_state": "TEXT"}
        cols.pop(missing, None)
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE track (" +
                    ",".join(f"{k} {v}" for k, v in cols.items()) + ")")
        con.execute("INSERT INTO track (id, source_id, path, name) "
                    "VALUES (1,'s','/x/a.mp3','a.mp3')")
        con.commit()
        con.close()

    def test_missing_indexed_columns_still_open(self):
        """这三列上有索引 —— 缺任何一个都会让 CREATE INDEX 失败。"""
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core.db import Database
        for col in ("name_key", "dup_key", "hash_quick"):
            with self.subTest(missing=col):
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory(prefix="msh-olddb-")
                self.db_path = Path(self.tmp.name) / "old.sqlite3"
                self._make_old(col)
                db = Database(self.db_path)
                n = db.query("SELECT COUNT(*) c FROM track")[0]["c"]
                db.close_all()
                self.assertEqual(1, n, "老数据丢了")

    def test_missing_plain_columns_still_open(self):
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from app.core.db import Database
        for col in ("rel_path", "quality", "hash_full", "hash_state"):
            with self.subTest(missing=col):
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory(prefix="msh-olddb-")
                self.db_path = Path(self.tmp.name) / "old.sqlite3"
                self._make_old(col)
                db = Database(self.db_path)
                db.close_all()


class CorruptConfigTest(unittest.TestCase):
    """config.toml 改坏时必须降级启动，而不是起不来。"""

    BAD = {
        "少括号": "[settings\nscan_workers = 4\n",
        "重复节": "[settings]\nscan_workers=4\n[settings]\nx=1\n",
        "顶层垃圾": "this is not toml\n",
        "sources是字符串": '[settings]\nsources = "abc"\n',
        "source缺name": '[[sources]]\nid="x"\nroot="D:/x"\n',
        "settings不是表": 'settings = "abc"\n',
        "空文件": "",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msh-badcfg-")
        self._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = self.tmp.name
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))

    def tearDown(self):
        from app.core import config as C
        C._cache = None
        C._settings_errors[:] = []
        if self._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = self._old
        self.tmp.cleanup()

    def test_bad_config_falls_back_to_defaults(self):
        from app.core import config as C
        path = Path(self.tmp.name) / "config.toml"
        for name, text in self.BAD.items():
            with self.subTest(case=name):
                C._cache = None
                C._settings_errors[:] = []
                path.write_text(text, encoding="utf-8")
                cfg = C.load_config(force=True)      # 不许抛
                self.assertIsNotNone(cfg)
                self.assertEqual([], cfg.sources)    # 降级成空源

    def test_broken_file_is_backed_up(self):
        from app.core import config as C
        path = Path(self.tmp.name) / "config.toml"
        C._cache = None
        path.write_text("[settings\n", encoding="utf-8")
        C.load_config(force=True)
        backups = list(Path(self.tmp.name).glob("config.toml.broken-*"))
        self.assertTrue(backups, "坏配置没有备份，用户的设置就丢了")

    def test_non_utf8_config(self):
        from app.core import config as C
        path = Path(self.tmp.name) / "config.toml"
        C._cache = None
        path.write_bytes(b"\xff\xfe\x00[settings]\n")
        C.load_config(force=True)                     # 不许抛


class SettingsStrictnessTest(unittest.TestCase):
    """设置校验：bool / 小数 / 错误类型 / 未知键 都要 400。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-strict-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        if str(_PROJ) not in sys.path:
            sys.path.insert(0, str(_PROJ))
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)

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
            from app.core import config as C
            C._cache = None
        except Exception:
            pass
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def test_bool_for_number_rejected(self):
        r = self.client.put("/api/settings", json={"scan_workers": True})
        self.assertEqual(400, r.status_code, r.text[:160])

    def test_fraction_for_int_rejected(self):
        r = self.client.put("/api/settings", json={"scan_workers": 1.9})
        self.assertEqual(400, r.status_code, r.text[:160])

    def test_path_setting_rejects_non_text(self):
        for bad in (True, 123, ["D:/x"], {"a": 1}):
            with self.subTest(value=bad):
                r = self.client.put("/api/settings", json={"trash_dir": bad})
                self.assertEqual(400, r.status_code, r.text[:160])

    def test_unknown_setting_key_is_reported(self):
        r = self.client.put("/api/settings", json={"max_workers": 7})
        self.assertIn(r.status_code, (400, 200), r.text[:160])
        if r.status_code == 200:
            body = r.json()
            self.assertIn("max_workers", (body.get("errors") or {}),
                          "未知设置键被静默吞掉了")
        else:
            self.assertIn("max_workers", r.text)


class ConfigWriteFailureTest(unittest.TestCase):
    """写盘失败要回滚内存态，别让 GET 显示一个磁盘上没有的值。"""

    def test_rollback_when_save_fails(self):
        tmp = tempfile.TemporaryDirectory(prefix="msh-savefail-")
        old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = tmp.name
        try:
            if str(_PROJ) not in sys.path:
                sys.path.insert(0, str(_PROJ))
            from fastapi.testclient import TestClient
            import app.main as M
            import app.routes.system as SYS
            from app.core import config as C
            C._cache = None
            client = TestClient(M.app, raise_server_exceptions=False)
            before = client.get("/api/settings").json()["settings"]["scan_workers"]

            def boom(_cfg):
                raise PermissionError("config.toml 是只读的")

            orig = SYS.save_config
            SYS.save_config = boom
            try:
                r = client.put("/api/settings", json={"scan_workers": 11})
            finally:
                SYS.save_config = orig
            self.assertEqual(500, r.status_code, r.text[:160])
            after = client.get("/api/settings").json()["settings"]["scan_workers"]
            self.assertEqual(before, after, "写盘失败后内存里却改了值")
        finally:
            try:
                import app.deps as D
                if getattr(D, "DB", None) is not None:
                    D.DB.close_all()
                    D.DB = None
            except Exception:
                pass
            from app.core import config as C
            C._cache = None
            if old is None:
                os.environ.pop("MSH_DATA_DIR", None)
            else:
                os.environ["MSH_DATA_DIR"] = old
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
