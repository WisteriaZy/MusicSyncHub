"""外部审查（AUDIT_REPORT_20261018_135752）9 条问题的回归测试。

## 为什么大部分是"结构断言"

这 9 条里有 6 条属于**分支漏写 / 调用位置错**（少一个 if、锁放错层级、
用错结束方法）。它们没有独立的纯函数可测，但都有一条"代码里必须有/不能有
某段"的硬特征 —— 用源码断言锁住比造一整套场景更可靠，也更快。

唯一有真行为差异的是第 1 条（复制时 rename 撞名必须报错而不是覆盖），
用真的 Executor 跑一遍。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
JS = (_PROJ / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
OPS = (_PROJ / "app" / "core" / "ops.py").read_text(encoding="utf-8")
MATCH = (_PROJ / "app" / "core" / "matching.py").read_text(encoding="utf-8")
FILES = (_PROJ / "app" / "routes" / "files.py").read_text(encoding="utf-8")
STATION = (_PROJ / "app" / "station_api.py").read_text(encoding="utf-8")
SEARCH = (_PROJ / "app" / "search_api.py").read_text(encoding="utf-8")
FOLDERS = (_PROJ / "app" / "sources" / "folders_api.py").read_text(encoding="utf-8")
UPLOAD = (_PROJ / "app" / "upload_api.py").read_text(encoding="utf-8")
COMPARE = (_PROJ / "app" / "routes" / "compare.py").read_text(encoding="utf-8")


class CopyRenameGuardTest(unittest.TestCase):
    """1) 复制时目标已存在 + policy=rename → 必须报错，不能覆盖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msh-a9-")
        os.environ.setdefault("MSH_DATA_DIR", self.tmp.name)
        from app.core.config import SourceConfig
        from app.sources.base import build_source
        root = Path(self.tmp.name) / "M"
        (root / "A").mkdir(parents=True)
        (root / "B").mkdir(parents=True)
        (root / "A" / "song.mp3").write_bytes(b"NEW" * 100)
        (root / "B" / "song.mp3").write_bytes(b"OLD" * 100)
        self.root = root
        self.src = build_source(SourceConfig(
            id="m", name="M", type="local", root=str(root).replace("\\", "/")))
        from app.core.ops import Executor
        self.ex = Executor({"m": self.src}, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rename_conflict_errors_instead_of_overwriting(self):
        # 强制走"认领后目标仍然存在"的分支（模拟并发占用）
        self.ex._claim_target = lambda st, want, policy="skip": want
        st = {"id": "s1", "op": "copy", "src": "m", "dst": "m",
              "src_path": "A/song.mp3", "dst_path": "B/song.mp3"}
        r = self.ex._do_copy(st, "rename", verify=False, verify_hash=False)
        self.assertEqual("error", r["state"], r)
        self.assertEqual(b"OLD" * 100, (self.root / "B" / "song.mp3").read_bytes(),
                         "原有文件被覆盖了")


class SourceAssertions(unittest.TestCase):
    """其余 8 条：用源码特征锁住。"""

    def test_2_transfer_case_insensitive_collision(self):
        seg = FILES[FILES.index("_names = [P.basename"):][:600]
        self.assertIn("lower()", seg)
        self.assertIn("os.name", seg)

    def test_3_station_restore_preflight(self):
        i = STATION.index("def station_restore")
        seg = STATION[i:i + 3000]
        self.assertIn("_owners", seg)
        # 预检必须在循环之前
        self.assertLess(seg.index("require_writable"), seg.index("for n in names"))

    def test_4_duplicate_bucket_not_constant(self):
        i = MATCH.index('if not k or k.startswith("|")')
        seg = MATCH[i:i + 900]   # BUG-18 后 key 计算换成了 if/else，窗口放宽
        self.assertNotIn('k = "name:" + P.name_key', seg,
                         "空 name_key 不许落进同一个常量桶")
        self.assertIn("raw:", seg)

    def test_5_album_apply_waits(self):
        self.assertIn('"wait": True', SEARCH)
        self.assertNotIn('organize_album_plan({**payload, "source": sid})', SEARCH)

    def test_6_empty_remove_has_readonly_guard(self):
        i = FOLDERS.index("def empty_remove")
        seg = FOLDERS[i:i + 900]
        self.assertIn("_guard_write(src)", seg)
        # 只读的扫描不该加这个闸（否则只读源上连"看有哪些空目录"都做不了）
        j = FOLDERS.index("def empty_scan")
        self.assertNotIn("_guard_write(src)", FOLDERS[j:j + 900])

    def test_7_player_ended_handled_once(self):
        self.assertNotIn("addEventListener('ended'", JS)
        i = JS.index("a.onended =")
        seg = JS[i:i + 900]
        self.assertIn("shuffle && queue.items.length >= 2", seg)

    def test_8_upload_dedup_uses_finish(self):
        i = UPLOAD.index("def work():")
        seg = UPLOAD[i:i + 700]
        self.assertIn("get_jobs().finish(job", seg)
        self.assertNotIn('job.state = "done"', seg)

    def test_9_set_keep_has_no_exclusive_guard(self):
        i = COMPARE.index("def set_keep")
        line = COMPARE[COMPARE.rindex("@router", 0, i):i]
        self.assertNotIn("exclusive", line.split("\n")[-1] + line[-200:])


if __name__ == "__main__":
    unittest.main()
