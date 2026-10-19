"""外部审查（AUDIT_REPORT_20261018）里三条 CRITICAL 的回归测试。

## 为什么这些测试和以前的不一样

上一轮续传的测试是**人工预置** `.mshpart.json` 的 —— 于是它测的是
"有 meta 时能不能续"，而真实场景是"中断时 meta 根本不存在"。
这一轮改成**真的在复制途中抛异常**，验证中断后元数据已经在盘上。

## 三条 CRITICAL

1. NEW-01 元数据写在复制完成之后 → 真实中断永远续不上（测试用异常模拟断线）
2. NEW-02 `trash_purge` / `trash_restore` 没校验 `..` → 可删库外任意目录
3. NEW-03 `_do_move` / `_do_copy` / `_do_rename` 在锁外用 `_dedupe_name`
   → 并发时算出同一个候选名，互相覆盖
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

DATA = bytes(range(256)) * 4096          # 1 MiB


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-audit-")
        cls._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = cls.tmp.name
        os.environ.pop("MSH_DB", None)
        from app.core.config import AppConfig, SourceConfig
        from app.sources.base import build_source
        cls.A = Path(cls.tmp.name) / "A"
        cls.B = Path(cls.tmp.name) / "B"
        cls.A.mkdir()
        cls.B.mkdir()
        cfg = AppConfig(sources=[
            SourceConfig(id="a", name="A", type="local",
                         root=str(cls.A).replace("\\", "/")),
            SourceConfig(id="b", name="B", type="local",
                         root=str(cls.B).replace("\\", "/")),
            SourceConfig(id="ro", name="RO", type="local",
                         root=str(cls.B).replace("\\", "/"), read_only=True),
        ])
        cls.sa = build_source(cfg.sources[0])
        cls.sb = build_source(cfg.sources[1])

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = cls._old
        cls.tmp.cleanup()

    def setUp(self):
        for d in (self.A, self.B):
            for p in sorted(d.rglob("*"), reverse=True):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass
        (self.A / "big.bin").write_bytes(DATA)


class ResumeOnRealInterruptTest(_Base):
    """NEW-01：复制**途中**被掐断，元数据必须已经在盘上。"""

    def test_meta_exists_after_mid_copy_failure(self):
        import app.sources.local as L
        dst = self.B / "big.bin"
        part = Path(str(dst) + ".mshpart")
        meta = Path(str(part) + ".json")

        def boom(fs, fd, length=0):
            fd.write(fs.read(100))          # 只写了 100 字节就"断线"
            raise OSError("模拟拔盘 / 掉网")

        with mock.patch.object(L.shutil, "copyfileobj", boom):
            with self.assertRaises(OSError):
                self.sb.copy_from(self.sa, "big.bin", "big.bin")

        self.assertTrue(part.exists(), "半截临时文件应该保留")
        self.assertEqual(100, part.stat().st_size)
        self.assertTrue(meta.exists(),
                        "★ 中断后元数据必须在盘上，否则续传永远不生效")

    def test_resume_after_real_interrupt(self):
        import app.sources.local as L
        dst = self.B / "big.bin"

        def boom(fs, fd, length=0):
            fd.write(fs.read(4096))
            raise OSError("模拟断线")

        with mock.patch.object(L.shutil, "copyfileobj", boom):
            with self.assertRaises(OSError):
                self.sb.copy_from(self.sa, "big.bin", "big.bin")

        r = self.sb.copy_from(self.sa, "big.bin", "big.bin")
        self.assertEqual(4096, r.get("resumed_from"))
        self.assertEqual(DATA, dst.read_bytes(), "续传后内容必须完整")

    def test_no_resume_when_source_mtime_changed(self):
        """源文件在中断期间被改过（大小相同）→ 必须从头复制。"""
        import app.sources.local as L
        dst = self.B / "big.bin"

        def boom(fs, fd, length=0):
            fd.write(fs.read(2048))
            raise OSError("断线")

        with mock.patch.object(L.shutil, "copyfileobj", boom):
            with self.assertRaises(OSError):
                self.sb.copy_from(self.sa, "big.bin", "big.bin")

        # 换掉源内容（长度一致），并把 mtime 改掉
        new = bytes(reversed(DATA))
        src = self.A / "big.bin"
        src.write_bytes(new)
        os.utime(src, (1_600_000_000, 1_600_000_000))

        r = self.sb.copy_from(self.sa, "big.bin", "big.bin")
        self.assertEqual(0, r.get("resumed_from"), "源变了就不能续")
        self.assertEqual(new, dst.read_bytes())


class TrashTraversalTest(_Base):
    """NEW-02：带 `..` 的 rel 不许碰回收站外的任何东西。"""

    def test_purge_rejects_dotdot(self):
        from app.sources.base import SourceError
        outside = self.A.parent / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "keep.txt").write_bytes(b"must survive")
        with self.assertRaises(SourceError):
            self.sa.trash_purge("../../outside")
        self.assertTrue((outside / "keep.txt").exists(), "库外文件被删了！")

    def test_restore_rejects_dotdot(self):
        from app.sources.base import SourceError
        outside = self.A.parent / "outside"
        outside.mkdir(exist_ok=True)
        victim = outside / "victim.bin"
        victim.write_bytes(b"x")
        with self.assertRaises(SourceError):
            self.sa.trash_restore("../../outside/victim.bin")
        self.assertTrue(victim.exists(), "库外文件被搬走了！")

    def test_normal_trash_entry_still_works(self):
        (self.A / "gone.mp3").write_bytes(b"x" * 10)
        self.sa.remove("gone.mp3")
        items = self.sa.trash_list()
        self.assertEqual(1, len(items))
        self.sa.trash_purge(items[0]["rel"])           # 正常路径照样能删
        self.assertEqual([], self.sa.trash_list())


class ClaimTargetTest(_Base):
    """NEW-03：并发改名必须收敛到 `_claim_target` 的锁里。"""

    def _executor(self):
        from app.core.ops import Executor
        return Executor({"a": self.sa, "b": self.sb}, None)

    def test_second_step_gets_a_free_name(self):
        (self.A / "B").mkdir(exist_ok=True)
        (self.A / "B" / "x.mp3").write_bytes(b"existing")
        ex = self._executor()
        want = self.sa.abs_path("B/x.mp3")
        st1 = {"id": "s1", "src": "a", "dst": "a", "dst_path": "B/x.mp3"}
        st2 = {"id": "s2", "src": "a", "dst": "a", "dst_path": "B/x.mp3"}
        p1 = ex._claim_target(st1, want, "rename")
        p2 = ex._claim_target(st2, want, "rename")
        self.assertNotEqual(p1, p2, "两步拿到了同一个目标名")
        self.assertIn("(2)", p1)          # 第一个空位
        self.assertIn("(3)", p2)          # 第二个必须避开第一个已占的 (2)
        self.assertFalse(os.path.exists(p1))
        self.assertFalse(os.path.exists(p2))

    def test_prereserved_key_with_disk_file_still_renames(self):
        """方案预占把 want 记在**本步**名下，但磁盘上的同名文件是别人的。

        这时候不能走"本步已认领过"的短路 —— 短路就等于放弃改名，
        随后 `_do_move` 撞名报错 / `_do_copy` 覆盖。真实场景：
        `_reserve_targets` 先登记，`_do_move` 再带 policy 来认领。
        """
        (self.A / "B").mkdir(exist_ok=True)
        (self.A / "B" / "z.mp3").write_bytes(b"old")
        ex = self._executor()
        st = {"id": "s1", "src": "a", "dst": "a", "dst_path": "B/z.mp3"}
        # 模拟方案预占
        ex._planned[("a", "b/z.mp3")] = st
        got = ex._claim_target(st, self.sa.abs_path("B/z.mp3"), "rename")
        self.assertIn("(2)", got, f"没有改名，直接返回了 {got}")

    def test_rename_registers_target(self):
        (self.A / "B").mkdir(exist_ok=True)
        (self.A / "B" / "y.mp3").write_bytes(b"existing")
        ex = self._executor()
        st = {"id": "s1", "src": "a", "dst": "a",
              "src_path": "B/x.mp3", "dst_path": "B/y.mp3"}
        (self.A / "B" / "x.mp3").write_bytes(b"new")
        res = ex._do_rename(st, "rename")
        self.assertEqual("done", res["state"], res)
        # 改名 = 源文件换了名字，源名本来就不该还在
        self.assertFalse((self.A / "B" / "x.mp3").exists())
        self.assertTrue((self.A / "B" / "y (2).mp3").exists(),
                        "应该改名到 y (2).mp3，而不是覆盖 y.mp3")
        self.assertEqual(b"existing", (self.A / "B" / "y.mp3").read_bytes(),
                         "原有文件被覆盖了")
        self.assertEqual(b"new", (self.A / "B" / "y (2).mp3").read_bytes())


if __name__ == "__main__":
    unittest.main()
