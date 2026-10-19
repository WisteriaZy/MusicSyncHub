"""索引库的两种"灾难恢复"：极老 schema 与文件损坏。

## 为什么要单独测

索引库是可以重建的（重扫一遍就回来了），但**打不开**是致命的：
应用启动直接崩，用户既看不到界面也不知道该删哪个文件。

  · 极老版本升级：`track` 表只有几列 → 索引建在缺列上 → 启动失败
  · 文件损坏（断电/蓝屏/坏道）：`file is not a database` → 启动失败

两种都按"备份坏东西 + 重建 + 继续启动"处理，和 `config.toml` 损坏的策略一致。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
_PY = str(_PROJ / ".venv" / "Scripts" / "python.exe")
if not Path(_PY).exists():                       # 非 Windows / 没有 venv 时退回当前解释器
    _PY = sys.executable


class DbRecoveryTest(unittest.TestCase):
    def _run(self, data_dir: str, code: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["MSH_DATA_DIR"] = data_dir
        env.pop("MSH_DB", None)
        return subprocess.run([_PY, "-c", code], cwd=str(_PROJ), env=env,
                              capture_output=True, text=True, timeout=240)

    def test_very_old_schema_migrates_and_keeps_rows(self):
        tmp = tempfile.mkdtemp(prefix="msh-old-")
        try:
            con = sqlite3.connect(Path(tmp) / "index.sqlite3")
            con.execute("CREATE TABLE track (id INTEGER PRIMARY KEY, source_id TEXT,"
                        " path TEXT, name TEXT)")
            con.execute("INSERT INTO track (source_id,path,name) "
                        "VALUES ('s','/x/a.mp3','a.mp3')")
            con.execute("CREATE TABLE source (id TEXT PRIMARY KEY, name TEXT)")
            con.commit()
            con.close()
            r = self._run(tmp, "import app.main as m; "
                               "print('COUNT', m.db().query("
                               "'SELECT COUNT(*) c FROM track')[0]['c'])")
            self.assertIn("COUNT 1", r.stdout or "",
                          f"老库迁移后数据丢了或启动失败: {r.stdout} {r.stderr[-300:]}")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_database_is_backed_up_and_recreated(self):
        tmp = tempfile.mkdtemp(prefix="msh-corrupt-")
        try:
            (Path(tmp) / "index.sqlite3").write_bytes(b"garbage not sqlite" * 200)
            r = self._run(tmp, "import app.main as m; "
                               "print('COUNT', m.db().query("
                               "'SELECT COUNT(*) c FROM track')[0]['c']); "
                               "print('STARTED')")
            self.assertIn("STARTED", r.stdout or "",
                          f"损坏的索引库让启动崩溃了: {r.stderr[-300:]}")
            backups = list(Path(tmp).glob("index.sqlite3.broken-*"))
            self.assertTrue(backups, "损坏的文件没有备份，用户无从追查")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_wal_sidecars_are_handled(self):
        """损坏时 -wal/-shm 也要一起挪走，否则新库会读到旧日志。"""
        tmp = tempfile.mkdtemp(prefix="msh-corrupt2-")
        try:
            (Path(tmp) / "index.sqlite3").write_bytes(b"garbage" * 500)
            (Path(tmp) / "index.sqlite3-wal").write_bytes(b"wal-garbage")
            r = self._run(tmp, "import app.main as m; m.db(); print('STARTED')")
            self.assertIn("STARTED", r.stdout or "", (r.stderr or "")[-300:])
            left = [p.name for p in Path(tmp).glob("index.sqlite3-*")
                    if ".broken-" not in p.name]
            self.assertEqual([], left, f"旧的 WAL 文件没被挪走: {left}")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
