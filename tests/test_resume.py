"""断点续传：中途断了能接着传，但**绝不能拼错内容**。

## 为什么要做

跨盘 / 跨网络搬一个大文件（几十 GB 的母带、整张专辑镜像）时，网络抖动、
U 盘被拔、笔记本休眠都会让复制中断。以前 `.mshpart` 只保证"不污染库"，
**不保证省时间** —— 断了必须从头再来。

## 安全前提（比"能续"更重要）

临时文件是按**目标名**放的。如果上一次中断的是**另一个源文件**
（比如同一首曲子换了个版本重新传），盲目接着写就会拼出一个内容错乱的文件，
而且大小还可能正好对得上 —— 这是最坏的一种损坏（看着成功、听着是噪音）。

所以每次复制都会在临时文件旁边写一份元数据：

    {"src_id": "a", "src_path": "big.mp3", "src_size": 2097152, "ts": ...}

只有**源 id、源路径、源大小三者全等**才续传，否则从头写。
续传完成后再比一次总大小；不一致就丢弃临时文件。

失败时**保留**临时文件（下次能续）；过期残留由启动时的
`cleanup_stale_parts` 清（>24 小时，连 `.mshpart.json` 一起清）。
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
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

DATA = bytes(range(256)) * 8192          # 2 MiB，内容可校验


class ResumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="msh-resume-")
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
            for p in list(d.rglob("*")):
                if p.is_file():
                    p.unlink()
        (self.A / "big.mp3").write_bytes(DATA)

    # ------------------------------------------------------------ 工具
    def _part(self, dst: Path) -> Path:
        return Path(str(dst) + ".mshpart")

    def _seed(self, dst: Path, done: int, *, src_path="big.mp3",
              src_id="a", src_size=len(DATA)):
        part = self._part(dst)
        part.write_bytes(DATA[:done])
        Path(str(part) + ".json").write_text(json.dumps({
            "src_id": src_id, "src_path": src_path,
            "src_size": src_size, "ts": time.time(),
        }), encoding="utf-8")
        return part

    # ------------------------------------------------------------ 正常续传
    def test_resumes_from_partial_file(self):
        dst = self.B / "big.mp3"
        half = len(DATA) // 2
        self._seed(dst, half)
        r = self.sb.copy_from(self.sa, "big.mp3", "big.mp3")
        self.assertEqual(half, r.get("resumed_from"),
                         f"没有续传：{r}")
        self.assertEqual(DATA, dst.read_bytes(), "续传后内容不对")
        self.assertFalse(self._part(dst).exists(), "临时文件没清掉")

    def test_full_copy_when_no_meta(self):
        dst = self.B / "big.mp3"
        self._part(dst).write_bytes(DATA[:1000])       # 只有半截，没有元数据
        r = self.sb.copy_from(self.sa, "big.mp3", "big.mp3")
        self.assertEqual(0, r.get("resumed_from"))
        self.assertEqual(DATA, dst.read_bytes())

    def test_no_resume_when_source_differs(self):
        """临时文件是**另一个源文件**留下的 —— 必须从头写。"""
        dst = self.B / "big.mp3"
        self._seed(dst, 1000, src_path="other.mp3")
        r = self.sb.copy_from(self.sa, "big.mp3", "big.mp3")
        self.assertEqual(0, r.get("resumed_from"))
        self.assertEqual(DATA, dst.read_bytes(), "把别的文件的半截接上来了")

    def test_no_resume_when_size_differs(self):
        dst = self.B / "big.mp3"
        self._seed(dst, 1000, src_size=len(DATA) + 10)
        r = self.sb.copy_from(self.sa, "big.mp3", "big.mp3")
        self.assertEqual(0, r.get("resumed_from"))
        self.assertEqual(DATA, dst.read_bytes())

    def test_part_larger_than_source_restarts(self):
        dst = self.B / "big.mp3"
        part = self._seed(dst, len(DATA))
        r = self.sb.copy_from(self.sa, "big.mp3", "big.mp3")
        self.assertEqual(0, r.get("resumed_from"))
        self.assertEqual(DATA, dst.read_bytes())

    def test_cleanup_removes_meta_too(self):
        dst = self.B / "big.mp3"
        part = self._seed(dst, 1000)
        meta = Path(str(part) + ".json")
        old = time.time() - 48 * 3600
        os.utime(part, (old, old))
        os.utime(meta, (old, old))
        from app.core import ops as OPS
        n = OPS.cleanup_stale_parts(self.sb, older_than_hours=24)
        self.assertGreaterEqual(n, 1)
        self.assertFalse(part.exists(), "过期临时文件没清")
        self.assertFalse(meta.exists(), "续传元数据没清")

    # ------------------------------------------------------------ 执行器层
    def test_transfer_reports_resume(self):
        from app.core import transfer as X
        dst = self.B / "big.mp3"
        half = len(DATA) // 2
        self._seed(dst, half)
        r = X.transfer_one(self.sa, self.sb, "big.mp3", "big.mp3",
                           policy="skip", verify=True, hard_delete=False)
        self.assertEqual("done", r["state"], r)
        self.assertEqual(half, r.get("resumed_from"), r)
        self.assertIn("续传", r.get("detail", ""))
        self.assertEqual(DATA, dst.read_bytes())
        # 源进回收站（默认不删）
        self.assertFalse((self.A / "big.mp3").exists())


if __name__ == "__main__":
    unittest.main()
