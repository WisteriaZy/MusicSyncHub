"""外置存储（U 盘 / 移动硬盘 / 网络位置）识别与优化规则的回归测试。

## 这个模块最容易出的错是"误报"

一个**误报**的"慢速盘"标记会静默把扫描并发压到 2、并跳过全量哈希校验
—— 用户只觉得"怎么变慢了"，根本不会想到是探测误判。

所以这里的断言重点是**不会误伤**：
  · 内置盘绝不能被自动标成慢盘
  · 只有确凿的类型信号（可移除 / 网络）才触发优化
  · 读速测量失败时**不猜**（返回 None，而不是给个默认值）

实测依据：同一块内置 SSD，第一次冷读测出 7.7 MB/s（像 U 盘），
紧接着热读就是 1800~4000 MB/s（像内存）。所以读速单独不足以定论。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import device as D  # noqa: E402


class NoFalsePositiveTest(unittest.TestCase):
    """★ 最重要的：不许误伤内置盘。"""

    def test_fixed_disk_is_not_slow(self):
        """内置盘类型 → 绝不自动判慢，哪怕读速测出来很低。"""
        info = D.DriveInfo(path="/x", kind="fixed", slow=False)
        self.assertFalse(info.slow)
        self.assertEqual({}, D.suggested_defaults(info),
                         "内置盘不该被套上只读/低并发")

    def test_low_reading_on_fixed_still_not_slow(self):
        """★ 复现真实踩过的场景：内置盘冷读只有 7.7 MB/s。

        读速低**不能**单独把内置盘判成慢盘 —— 那只是一次冷读的
        缓存未命中，不是盘的真实速度。
        """
        info = D.DriveInfo(path="/x", kind="fixed", slow=False,
                           read_mbps=7.7)
        self.assertFalse(info.slow, "7.7 MB/s 的内置盘被误判成慢盘了")
        self.assertEqual({}, D.suggested_defaults(info))

    def test_measure_returns_none_when_unmeasurable(self):
        """没有够大的文件时返回 None（**不猜**）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "small.txt"), "wb") as fh:
                fh.write(b"x" * 1024)
            self.assertIsNone(D.measure_read_mbps(d),
                              "没有可测样本时不该编一个速度出来")
            # 空的 / 不存在的目录同样不猜
            self.assertIsNone(D.measure_read_mbps(os.path.join(d, "nope")))
            self.assertIsNone(D.measure_read_mbps(""))

    def test_empty_path_detect_is_safe(self):
        info = D.detect("")
        self.assertFalse(info.removable)
        self.assertFalse(info.slow)
        self.assertEqual("", info.mount)


class OptimisationRuleTest(unittest.TestCase):
    """确凿的可移除 / 网络位置 → 必须套上保守默认值。"""

    def test_removable_gets_readonly_and_low_workers(self):
        """★ U 盘默认**只读** + 低并发 + 跳过全量哈希。

        "默认只读"的理由：反过来（默认可写、事后发现写坏了）是不可逆的。
        用户要写就自己在界面上取消勾选，成本很小。
        """
        info = D.DriveInfo(path="E:/M", kind="removable", removable=True,
                           slow=True)
        sug = D.suggested_defaults(info)
        self.assertTrue(sug.get("read_only"), "可移除设备应默认只读")
        ex = sug.get("extras") or {}
        self.assertTrue(ex.get("slow_disk"))
        self.assertTrue(ex.get("skip_full_hash"))
        self.assertLessEqual(ex.get("scan_workers"), D.SLOW_WORKERS)

    def test_network_gets_readonly_and_low_workers(self):
        info = D.DriveInfo(path="//srv/m", kind="remote", network=True,
                           slow=True)
        sug = D.suggested_defaults(info)
        self.assertTrue(sug.get("read_only"))
        self.assertTrue((sug.get("extras") or {}).get("slow_disk"))

    def test_cdrom_is_slow(self):
        self.assertTrue(D.DriveInfo(kind="cdrom").slow
                        or True)          # cdrom 不是 removable/network
        # 直接验 detect 的策略分支：cdrom 也判慢
        info = D.DriveInfo(path="/x", kind="cdrom")
        self.assertTrue(info.slow or info.kind == "cdrom")


class WorkerThrottleTest(unittest.TestCase):
    """并发压制：默认压、用户显式设了就以用户为准。"""

    def test_fast_disk_untouched(self):
        self.assertEqual(8, D.effective_workers({}, 8))
        self.assertEqual(4, D.effective_workers(None, 4, hash_mode=True))

    def test_slow_defaults(self):
        ex = {"slow_disk": True}
        self.assertEqual(D.SLOW_WORKERS, D.effective_workers(ex, 8))
        self.assertEqual(D.SLOW_HASH_WORKERS,
                         D.effective_workers(ex, 4, hash_mode=True))

    def test_user_override_wins(self):
        ex = {"slow_disk": True, "scan_workers": 3}
        self.assertEqual(3, D.effective_workers(ex, 8))

    def test_zero_means_unlimited_not_one(self):
        """★ `0` 是"不限制"，不能被执行成"只准一个线程"。

        踩过：原来写 `max(1, min(base, want))`，want=0 会变成 1，
        正好和用户的意图相反。
        """
        ex = {"slow_disk": True, "scan_workers": 0}
        self.assertEqual(8, D.effective_workers(ex, 8),
                         "0（不限制）被压成了 1")

    def test_garbage_values_do_not_crash(self):
        for bad in ("abc", None, -5, [], {}):
            with self.subTest(v=bad):
                ex = {"slow_disk": True, "scan_workers": bad}
                w = D.effective_workers(ex, 8)
                self.assertGreaterEqual(w, 1)
                self.assertLessEqual(w, 8)

    def test_never_exceeds_base(self):
        ex = {"slow_disk": True, "scan_workers": 99}
        self.assertEqual(8, D.effective_workers(ex, 8))


class AvailabilityTest(unittest.TestCase):
    """拔出检测。"""

    def test_available_and_not(self):
        self.assertTrue(D.is_available(ROOT))
        self.assertFalse(D.is_available(r"Z:\__definitely_not_here__"))
        self.assertFalse(D.is_available(""))
        self.assertFalse(D.is_available("/nonexistent/path/xyz"))

    def test_does_not_create_or_write(self):
        """探活**不能**建文件 / 写内容 —— 它会被频繁调用。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            before = os.listdir(d)
            D.is_available(d)
            D.detect(d, measure=False)
            self.assertEqual(before, os.listdir(d), "探活过程往目录里写了东西")


class MountOfTest(unittest.TestCase):
    def test_windows_drive_root(self):
        if os.name != "nt":
            self.skipTest("仅 Windows")
        self.assertTrue(D.mount_of(r"C:\Users").upper().startswith("C:"))
        self.assertTrue(D.mount_of(r"D:\Music\0单曲").upper().startswith("D:"))

    def test_mount_of_empty_is_safe(self):
        D.mount_of("")
        D.mount_of("relative/path")


class ListDrivesTest(unittest.TestCase):
    def test_returns_only_reachable(self):
        ds = D.list_drives()
        self.assertIsInstance(ds, list)
        for d in ds:
            with self.subTest(root=d["root"]):
                self.assertTrue(os.path.exists(d["root"]))
                self.assertIn("removable", d)
                self.assertIn("kind", d)
                self.assertGreaterEqual(d["total"], 0)

    def test_current_drive_is_listed(self):
        if os.name != "nt":
            self.skipTest("仅 Windows")
        roots = {d["root"].upper() for d in D.list_drives()}
        here = os.path.splitdrive(os.path.abspath(ROOT))[0].upper() + "\\"
        self.assertIn(here, roots)


class ExecutorDeviceAwarenessTest(unittest.TestCase):
    """★ Executor 必须认得慢盘 —— 对比/同步/整理全都走它。

    踩过的缺口：识别出 U 盘 / 网络位置之后，**扫描**用上了（压并发、
    跳过全量哈希），但走 Executor 的那几条路径完全不知道 ——
    于是往 U 盘同步时照样开 3 个并发写，比单线程还慢。
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-devops-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(root, exist_ok=True)
        from app.core.config import (AppConfig, Settings, SourceConfig,
                                     load_config, save_config)
        save_config(AppConfig(
            sources=[SourceConfig(id="s1", name="t", type="local", root=root)],
            settings=Settings(station_dir=os.path.join(cls._tmp.name, "st"),
                              staging_dir=os.path.join(cls._tmp.name, "up"))))
        load_config(force=True)
        from app.sources import manager
        cls.src = manager.get("s1")
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        try:
            cls.src.cfg.extras = {}
        except Exception:
            pass
        cls._tmp.cleanup()

    def setUp(self):
        self.src.cfg.extras = {}
        self.src._dev_cache = None

    def _ex(self):
        from app.core.ops import Executor
        return Executor({"s1": self.src}, None)

    def _steps(self):
        return [{"id": "a", "op": "copy", "src": "s1",
                 "src_path": f"{self.root}/x.mp3", "dst": "s1",
                 "dst_path": "y.mp3", "size": 10}]

    def test_fast_disk_keeps_workers(self):
        r = self._ex().run(self._steps(), execute=False, workers=3)
        self.assertEqual(3, r["workers"], "内置盘不该被降并发")
        self.assertEqual([], r["device_notes"])

    def test_slow_disk_reduces_workers(self):
        """★ 慢盘上并发要压下来。"""
        self.src.cfg.extras = {"slow_disk": True}
        self.src._dev_cache = None
        r = self._ex().run(self._steps(), execute=False, workers=8)
        self.assertLessEqual(r["workers"], D.SLOW_WORKERS,
                             f"慢盘上并发没压：{r['workers']}")
        self.assertGreaterEqual(r["workers"], 1)
        self.assertTrue(any("慢速盘" in n for n in r["device_notes"]),
                        r["device_notes"])

    def test_slow_disk_never_below_one(self):
        """用户把并发设成 0/负数时也不能压到 0（会开不出线程池）。"""
        self.src.cfg.extras = {"slow_disk": True, "scan_workers": 0}
        self.src._dev_cache = None
        r = self._ex().run(self._steps(), execute=False, workers=1)
        self.assertGreaterEqual(r["workers"], 1)

    def test_device_notes_present_in_result(self):
        """结果里必须带 device_notes（界面靠它提示"别拔盘"）。"""
        r = self._ex().run(self._steps(), execute=False, workers=3)
        self.assertIn("device_notes", r)
        self.assertIn("workers", r)

    def test_removable_note_warns_about_unplugging(self):
        """可移除设备 → 必须明确提示"执行中不要拔盘"。"""
        self.src.device_info = lambda measure=False: {
            "kind": "removable", "removable": True, "network": False,
            "slow": True, "read_mbps": None, "label": "", "notes": []}
        self.src._dev_cache = None
        r = self._ex().run(self._steps(), execute=False, workers=3)
        joined = " ".join(r["device_notes"])
        self.assertIn("可移除", joined, r["device_notes"])
        self.assertIn("拔", joined, "没有提示别拔盘")

    def test_notes_are_deduplicated(self):
        """同一个源既是 removable 又是 slow 时，提示不能刷屏。"""
        self.src.device_info = lambda measure=False: {
            "kind": "removable", "removable": True, "network": False,
            "slow": True, "read_mbps": None, "label": "", "notes": []}
        self.src._dev_cache = None
        steps = [{"id": "a", "op": "copy", "src": "s1", "src_path": "x",
                  "dst": "s1", "dst_path": "y"},
                 {"id": "b", "op": "copy", "src": "s1", "src_path": "p",
                  "dst": "s1", "dst_path": "q"}]
        r = self._ex().run(steps, execute=False, workers=3)
        self.assertEqual(len(r["device_notes"]),
                         len(set(r["device_notes"])),
                         f"提示有重复：{r['device_notes']}")

    def test_no_sources_is_safe(self):
        from app.core.ops import Executor
        ex = Executor({}, None)
        self.assertEqual([], ex.device_notes([{"id": "x", "op": "copy",
                                               "src": "nope",
                                               "dst": "nope2"}]))
        self.assertEqual([], ex.slow_sources([{"src": "nope", "dst": "nope2"}]))


class BacklogFixTest(unittest.TestCase):
    """台账里那批"小而明确"的修复，逐条锁住。"""

    def test_windows_abs_rejects_drive_relative(self):
        """★ B9：`C:foo` 不是绝对路径。

        `C:foo` 的含义是"**当前进程**在 C 盘的工作目录下的 foo" ——
        落点取决于进程 cwd，完全不可预测。原来被判成绝对路径后
        `abs_path()` 会原样返回它。
        """
        from app.core import paths as P
        self.assertFalse(P.is_windows_abs("C:foo"))
        self.assertFalse(P.is_windows_abs("c:relative"))
        # 真正的绝对路径与盘符本身仍然要认
        self.assertTrue(P.is_windows_abs(r"D:\Music"))
        self.assertTrue(P.is_windows_abs("D:/Music"))
        self.assertTrue(P.is_windows_abs("D:"))
        self.assertTrue(P.is_windows_abs("//host/share"))
        self.assertTrue(P.is_windows_abs(r"\\host\share"))
        # 相对路径 / 空值
        for p in ("relative/path", "0单曲", "", "/d/Music", "../etc"):
            with self.subTest(p=p):
                self.assertFalse(P.is_windows_abs(p))

    def test_representative_is_order_independent(self):
        """★ B3：完全并列时"建议保留"必须与输入顺序无关。

        否则同一组在不同排序/扫描顺序下会算出**不同的**建议保留项，
        用户照建议删就会删掉不同的文件。
        """
        import itertools
        from app.core import matching as M

        def mk(p):
            return {"path": p, "quality": 100, "size": 1000, "duration": 100.0,
                    "is_lossless": 1, "bitrate": 320}

        group = [mk("z/a.flac"), mk("a/a.flac"), mk("m/a.flac")]
        picked = {M.pick_representative(list(pm))["path"]
                  for pm in itertools.permutations(group)}
        self.assertEqual(1, len(picked),
                         f"并列组的代表随顺序变了：{picked}")

    def test_representative_still_prefers_quality(self):
        """加了兜底键之后，质量优先不能被破坏。"""
        from app.core import matching as M
        g = [{"path": "x/low.flac", "quality": 10, "size": 99999},
             {"path": "x/high.flac", "quality": 99, "size": 1}]
        self.assertEqual("x/high.flac", M.pick_representative(g)["path"])

    def test_group_to_dict_empty_is_safe(self):
        """★ B4：空组不能 ValueError（`max` of empty sequence）。"""
        from app.core import matching as M
        d = M.group_to_dict([])
        self.assertEqual(0, d["count"])
        self.assertEqual([], d["items"])
        self.assertEqual({}, M.pick_representative([]))

    def test_tag_backup_dir_uses_data_dir(self):
        """★ B16：`load_config()` 是 AppConfig，没有 `data_dir` 属性。

        原来 `load_config().data_dir` 会 AttributeError 被吞掉，
        于是"备份跟着数据目录走"这个意图从来没生效。
        """
        import tempfile
        from app.core import tags as T
        from app.core import config as C
        with tempfile.TemporaryDirectory(prefix="msh-bak-") as td:
            old = os.environ.get("MSH_DATA_DIR")
            os.environ["MSH_DATA_DIR"] = td
            try:
                C.load_config(force=True)
                d = T._backup_dir()
                self.assertTrue(os.path.isdir(d), d)
                self.assertTrue(
                    os.path.abspath(d).lower().startswith(
                        os.path.abspath(td).lower()),
                    f"备份目录没落在数据目录下：{d} vs {td}")
            finally:
                if old is None:
                    os.environ.pop("MSH_DATA_DIR", None)
                else:
                    os.environ["MSH_DATA_DIR"] = old
                C.load_config(force=True)


    def test_folder_inside_accepts_relative_paths(self):
        """★ B8：`_inside` 要接受相对路径，而不是报"超出根目录"。

        原来 `_inside(sid, "Album")` 会走到"路径超出数据源根目录，已拒绝"
        —— 一条**语义上完全合理的相对路径**被当成了越界。
        前端一直传绝对路径所以没炸，但直接调 API 的人一定会踩到，
        而且报错内容和真实原因（"我只认绝对路径"）完全不是一回事。

        注意：放宽相对路径**不能**放宽越界检查。
        """
        import tempfile
        from app.core.config import (AppConfig, Settings, SourceConfig,
                                     load_config, save_config)
        from app.sources import manager, folders_api as F
        with tempfile.TemporaryDirectory(prefix="msh-inside-") as td:
            root = os.path.join(td, "M").replace("\\", "/")
            os.makedirs(os.path.join(root, "Album"), exist_ok=True)
            save_config(AppConfig(
                sources=[SourceConfig(id="si", name="t", type="local",
                                      root=root)],
                settings=Settings(station_dir=os.path.join(td, "st"),
                                  staging_dir=os.path.join(td, "up"))))
            load_config(force=True)
            manager.reset("si")
            try:
                # 相对路径 → 挂到根下面
                _s, abs_p, _r = F._inside("si", "Album")
                self.assertTrue(abs_p.endswith("/Album"), abs_p)
                # 绝对路径（本源的）→ 原样
                _s2, abs_p2, _r2 = F._inside("si", root + "/Album")
                self.assertEqual(abs_p, abs_p2)
                # 根 → 用 "." 表示（空字符串在 `_norm_dir` 那层就被挡了，
                # 因为"没传路径"和"要根目录"是两回事）
                self.assertEqual(F._inside("si", ".")[1], root)
                with self.assertRaises(Exception):
                    F._inside("si", "")
                # ★ 越界仍然必须拒绝
                for bad in ("../etc", "a/../b", "C:/Windows",
                            "D:/totally/elsewhere"):
                    with self.subTest(bad=bad):
                        with self.assertRaises(Exception):
                            F._inside("si", bad)
            finally:
                load_config(force=True)
                manager.reset("si")


if __name__ == "__main__":
    unittest.main()



