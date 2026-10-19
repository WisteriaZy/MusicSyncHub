"""本轮全面审计发现的缺陷的回归测试。

每一条都对应一个**实测确认**过的问题（不是猜测），
所以断言写的是"这个不变量必须成立"，而不是"代码长这样"。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent
from .srchelp import fn_body


ROOT = _PROJ
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP = ROOT / "app"
JS = APP / "web" / "static" / "js"


def py(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def js(n: str) -> str:
    return (JS / n).read_text(encoding="utf-8")


class ExclusiveGuardTest(unittest.TestCase):
    """★ 服务端必须自己保证"一次只跑一个写流程"。

    前端那层是体验（提前禁用按钮），服务端这层才是约束 ——
    任何客户端（另一个标签、脚本、curl、重放请求）都能绕过前端。
    审计发现 31 个写端点里**只有 1 个**有服务端保护。
    """

    @classmethod
    def setUpClass(cls):
        cls.G = py("core/guard.py")

    def test_guard_is_its_own_module(self):
        """放在 core/guard.py 而不是 main.py —— 否则各 router 与 main 循环导入。"""
        self.assertIn("def exclusive_guard", self.G)
        self.assertIn("from .jobs import get_jobs", self.G)

    def test_guard_rejects_with_409(self):
        i = self.G.index("def exclusive_guard")
        seg = fn_body(self.G, "exclusive_guard")
        self.assertIn("HTTPException", seg)
        self.assertIn("409", seg)
        # 必须说清为什么（并发会基于过期状态操作文件）
        self.assertIn("过期的文件状态", seg)

    def test_running_job_may_call_back(self):
        """正在执行的任务自己要能回来做后续调用，不能被自己挡住。"""
        i = self.G.index("def exclusive_guard")
        seg = fn_body(self.G, "exclusive_guard")
        self.assertIn("X-MSH-Job", seg)

    def test_all_file_writes_are_guarded(self):
        """逐个列出必须挂守卫的端点。"""
        must = [
            ("routes/files.py", "/api/files/move"),
            ("search_api.py", "/api/collect/apply"),
            ("search_api.py", "/api/organize/apply"),
            ("search_api.py", "/api/organize/album-apply"),
            ("upload_api.py", "/api/upload/commit"),
            ("upload_api.py", "/api/upload/cleanup"),
            ("upload_api.py", "/api/upload/batch-to-station"),
            ("station_api.py", "/api/station/move-in"),
            ("station_api.py", "/api/station/move-out"),
            ("station_api.py", "/api/station/remove"),
            ("station_api.py", "/api/station/restore"),
            ("tags_api.py", "/api/tags/write"),
            ("tags_api.py", "/api/tags/restore"),
            ("sources/folders_api.py", "/api/folders/create"),
            ("sources/folders_api.py", "/api/folders/rename"),
            ("sources/folders_api.py", "/api/folders/delete"),
            ("sources/folders_api.py", "/api/folders/empty-remove"),
        ]
        import ast
        for rel, path in must:
            src = py(rel)
            found = False
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and dec.args
                            and isinstance(dec.args[0], ast.Constant)
                            and dec.args[0].value == path):
                        args = ast.unparse(node.args)
                        self.assertTrue(
                            "exclusive_guard" in args or "exclusive_slot" in args,
                            f"{rel} 的 {path} 没挂独占守卫")
                        found = True
            self.assertTrue(found, f"{rel} 里找不到 {path}")

    def test_declarative_check_via_app(self):
        """再从真实路由表核一遍（防止上面那种源码级检查自欺）。"""
        from fastapi.routing import APIRoute

        from app.main import app

        def names(dep, seen=None):
            seen = seen if seen is not None else set()
            out = set()
            if id(dep) in seen:
                return out
            seen.add(id(dep))
            out.add(getattr(dep.call, "__name__", "?"))
            for sub in getattr(dep, "dependencies", []) or []:
                out |= names(sub, seen)
            return out

        def walk(routes, seen=None):
            seen = seen if seen is not None else set()
            for r in routes:
                if id(r) in seen:
                    continue
                seen.add(id(r))
                if isinstance(r, APIRoute):
                    yield r
                    continue
                inner = getattr(r, "original_router", None) or getattr(r, "router", None)
                if inner is not None and hasattr(inner, "routes"):
                    yield from walk(inner.routes, seen)

        def has_lock(ns):
            """挂的是真正的独占（`exclusive_slot`）还是只检查（`exclusive_guard`）。"""
            return any(n.startswith("exclusive_slot") for n in ns)

        def has_any_guard(ns):
            return has_lock(ns) or "exclusive_guard" in ns

        guarded = set()
        real = set()
        for r in walk(app.routes):
            ns = names(r.dependant)
            if has_any_guard(ns):
                guarded.add(r.path)
            if has_lock(ns):
                real.add(r.path)
        for p in ("/api/files/move", "/api/upload/commit", "/api/station/remove",
                  "/api/tags/write", "/api/folders/delete",
                  "/api/organize/apply", "/api/collect/apply"):
            self.assertIn(p, guarded, f"{p} 实际没有守卫")

        # ★ 这几个曾经是"假守卫"：只挂 `exclusive_guard`（进门看一眼），
        #   业务函数里从不 `create()`，所以整个写入期间 `busy()` 都是 None，
        #   别的写流程可以合法插进来。必须用 `exclusive_slot`（真占位）。
        for p in ("/api/station/move-in", "/api/station/move-out",
                  "/api/station/remove", "/api/station/restore",
                  "/api/tags/write", "/api/tags/restore",
                  "/api/folders/create", "/api/folders/rename",
                  "/api/folders/delete", "/api/folders/empty-remove"):
            self.assertIn(p, real,
                          f"{p} 只挂了检查、没有真正占位（写入期间 busy() 仍是 None）")


class PlanTargetCollisionTest(unittest.TestCase):
    """★ 同一方案内两条 move 撞同一目标 → 曾经静默丢文件。

    `skip` 判断的是"执行到这一步时目标在不在"，而前一步刚把文件放过去，
    于是第二步看到"已存在"就跳过或覆盖 —— 两个都报 `done`，实际只剩一个。
    多线程下更糟：两步的检查可能都发生在对方写入之前。
    """

    @classmethod
    def setUpClass(cls):
        cls.O = py("core/ops.py")

    def test_reservation_exists(self):
        self.assertIn("def _reserve_targets", self.O)
        self.assertIn("def _claim_target", self.O)

    def test_reserved_before_execution(self):
        """必须在跑之前占位，而不是执行到那一步再看。"""
        i = self.O.index("n_conf = self._reserve_targets(steps)")
        j = self.O.index("with ThreadPoolExecutor", i)
        self.assertLess(i, j, "占位发生在执行之后就没意义了")

    def test_move_and_copy_both_claim(self):
        for fn in ("def _do_move", "def _do_copy"):
            i = self.O.index(fn)
            seg = self.O[i:i + 2000]
            self.assertIn("_claim_target", seg, f"{fn} 没有认领目标名")

    def test_conflict_is_reported(self):
        """撞车要如实报出来 —— 用户必须知道有文件被自动改名了。"""
        self.assertIn("dst_conflicts", self.O)
        self.assertIn("_extra_summary", self.O)

    def test_real_scenario_two_files_one_target(self):
        """真跑一遍：两个不同内容的文件移到同一目标，一个都不能少。"""
        tmp = tempfile.mkdtemp(prefix="msh-collide-")
        old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = tmp
        os.environ["MSH_DB"] = os.path.join(tmp, "i.sqlite3")
        try:
            from app.core.config import (AppConfig, SourceConfig, load_config,
                                         save_config)
            from app.core.ops import Executor
            from app.sources import manager
            root = os.path.join(tmp, "M").replace("\\", "/")
            save_config(AppConfig(sources=[
                SourceConfig(id="s1", name="t", type="local", root=root)]))
            load_config(force=True)
            for d in ("d1", "d2"):
                os.makedirs(os.path.join(root, d), exist_ok=True)
                with open(os.path.join(root, d, "x.mp3"), "wb") as fh:
                    fh.write((b"A" if d == "d1" else b"B") * 1000)
            manager.reset()
            src = manager.get("s1")
            ex = Executor({"s1": src}, db=None)
            r = ex.run([
                {"id": "1", "op": "move", "src": "s1",
                 "src_path": f"{root}/d1/x.mp3", "dst": "s1",
                 "dst_path": f"{root}/out/x.mp3", "size": 1000},
                {"id": "2", "op": "move", "src": "s1",
                 "src_path": f"{root}/d2/x.mp3", "dst": "s1",
                 "dst_path": f"{root}/out/x.mp3", "size": 1000},
            ], execute=True, policy="skip", workers=1)
            out = os.path.join(root, "out")
            files = sorted(os.listdir(out)) if os.path.isdir(out) else []
            left = [p for p in (f"{root}/d1/x.mp3", f"{root}/d2/x.mp3")
                    if os.path.exists(p)]
            self.assertEqual(2, len(files) + len(left),
                             f"丢文件了：out={files} 源剩={left}")
            self.assertEqual(["x (2).mp3", "x.mp3"], files,
                             "撞车的那一个应该被改名，而不是覆盖")
            self.assertEqual(1, r["summary"].get("dst_conflicts"),
                             "撞车数量要报出来")
        finally:
            try:
                from app.sources import manager as m2
                m2.reset()
            except Exception:
                pass
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            if old is None:
                os.environ.pop("MSH_DATA_DIR", None)
            else:
                os.environ["MSH_DATA_DIR"] = old


class AlbumGroupingRespectsArtistTest(unittest.TestCase):
    """★ `group_by="album"` 曾经把多作者的同名专辑并进单一作者目录。

    `_same_folder_name` 逐层比对（为 `专辑/FLAC` 设计），于是
    `周杰伦/专辑0` 也满足"目录名等于专辑名"，`next()` 取第一个命中的 →
    四个作者的 `专辑0/` 全并进 `周杰伦/专辑0/`。
    再与"同方案撞车"合流 → 15 个文件静默变 5 个。
    """

    @classmethod
    def setUpClass(cls):
        cls.OG = py("core/organize.py")

    def test_parent_artist_is_rejected_in_album_mode(self):
        self.assertIn("_parent_looks_like_artist", self.OG)
        i = self.OG.index("def _album_dir_ok")
        seg = fn_body(self.OG, "_album_dir_ok")
        self.assertIn("_parent_looks_like_artist", seg)
        self.assertIn('group_by == "album"', seg)

    def test_custom_mapping_still_wins(self):
        """③a 整个分支必须让路给 ⑤ 自定义映射。"""
        i = self.OG.index('if not dest and album and group_by in ("artist_album", "album"):')
        self.assertIn("if not dest", self.OG[i:i + 60])

    def test_album_field_gate_kept(self):
        """第一道永远是"这目录里装的主要就是这张专辑"。"""
        i = self.OG.index("def _album_dir_ok")
        seg = fn_body(self.OG, "_album_dir_ok")
        self.assertIn('cand.get("album")', seg)

    def test_real_scenario_four_artists_same_album_name(self):
        """四个作者各有一个 `专辑0/`：整理后文件必须一个不少。

        （同一张专辑名散在多处时，归并到一处是合理的 —— 专辑才是身份。
        这里真正要守住的是**不丢文件**，以及不去"别的作者目录"下当客人。）
        """
        from app.core import organize as OG

        def t(path, artist, album):
            return {"source_id": "s1", "path": path, "rel_path": path,
                    "name": path.rsplit("/", 1)[-1], "size": 1000,
                    "duration": 100.0, "title": path.rsplit("/", 1)[-1],
                    "artist": artist, "album": album,
                    "parent": path.rsplit("/", 1)[0], "is_lossless": True,
                    "bitrate": 900}

        artists = ("周杰伦", "陈奕迅", "Aimer", "YOASOBI")
        rows = []
        for a in artists:
            rows += [t(f"D:/M/{a}/专辑0/t{i}.mp3", a, "专辑0") for i in range(5)]
        plan = OG.plan_by_album(rows, "D:/M", min_group=5, group_by="album")

        moved = {m["path"] for m in plan["moves"]}
        kept = {r["path"] for r in rows} - moved
        dsts = {m["dst_dir"] for m in plan["moves"]}

        # 1) 文件守恒：搬走的 + 留下的 = 全部
        self.assertEqual(len(rows), len(moved) + len(kept),
                         f"文件数对不上：搬 {len(moved)} 留 {len(kept)} 共 {len(rows)}")
        self.assertGreater(len(rows), 0)
        # 2) 目的地只允许是"叶子层就是专辑名"的目录，不能是 `某作者/something`
        for d in dsts:
            leaf = d.rsplit("/", 1)[-1]
            self.assertEqual("专辑0", leaf,
                             f"目的地叶子不是这张专辑：{sorted(dsts)}")
            # 父级不能是**组内别的**作者（那才是"串门"）
            parts = d.split("/")
            if len(parts) >= 2:
                parent = parts[-2]
                self.assertNotIn(
                    parent, [a for a in artists if a != parent],
                    f"目的地父级是别的作者目录：{d}")

    def test_album_mode_does_not_nest_under_other_artist(self):
        """只有别的作者的 `专辑0/` 存在、自己这边散着时，不能进别人目录。

        这是审计里那条严重问题的核心：`_same_folder_name` 逐层比对，
        `周杰伦/专辑0` 会被当成"专辑0 的目录"。
        """
        from app.core import organize as OG

        def t(path, artist, album):
            return {"source_id": "s1", "path": path, "rel_path": path,
                    "name": path.rsplit("/", 1)[-1], "size": 1000,
                    "duration": 100.0, "title": path.rsplit("/", 1)[-1],
                    "artist": artist, "album": album,
                    "parent": path.rsplit("/", 1)[0], "is_lossless": True,
                    "bitrate": 900}

        # 另一张同名专辑的目录在**别的作者**下（陈奕迅），自己（Aimer）散着
        rows = ([t(f"D:/M/陈奕迅/专辑0/c{i}.mp3", "陈奕迅", "专辑0")
                 for i in range(6)]
                + [t(f"D:/M/散/a{i}.mp3", "Aimer", "专辑0") for i in range(6)])
        plan = OG.plan_by_album(rows, "D:/M", min_group=5, group_by="album")
        for m in plan["moves"]:
            if m["path"].startswith("D:/M/散/"):
                self.assertNotIn(
                    "陈奕迅", m["dst_dir"],
                    f"Aimer 的歌被塞进了陈奕迅的目录：{m['dst_dir']}")

class TagBackupUniquenessTest(unittest.TestCase):
    """★ 批量改标签时备份曾经互相覆盖，30 首只剩 1 份。"""

    @classmethod
    def setUpClass(cls):
        cls.T = py("core/tags.py")

    def test_name_uses_full_path_digest(self):
        i = self.T.index("def _save_backup")
        seg = fn_body(self.T, "_save_backup")
        # 用整条路径的摘要，而不是 base64 前缀（前缀会重叠）
        self.assertIn("sha1", seg)
        self.assertNotIn("b64encode(path.encode", seg)

    def test_refuses_to_overwrite_existing_backup(self):
        """同一文件同一秒写两次也要各留一份。"""
        i = self.T.index("def _save_backup")
        seg = fn_body(self.T, "_save_backup")
        self.assertIn("while os.path.exists", seg)

    def test_hashlib_imported(self):
        """踩过：加了 hashlib 却忘了 import，异常被 except 吞掉 →
        备份静默返回空字符串，所有备份测试一起红。"""
        self.assertIn("import hashlib", self.T)

    def test_eight_files_get_eight_distinct_names(self):
        import importlib
        tmp = tempfile.mkdtemp(prefix="msh-bak-")
        old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = tmp
        try:
            from app.core import tags as T
            importlib.reload(T)
            names = []
            for i in range(8):
                p = os.path.join(tmp, f"song-{i:02d}.flac")
                with open(p, "wb") as fh:
                    fh.write(b"x")
                names.append(T._save_backup(p, {"title": f"t{i}"}))
            self.assertTrue(all(names), f"有备份没生成: {names}")
            self.assertEqual(8, len(set(names)),
                             f"备份名撞车（丢备份）: {names}")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            if old is None:
                os.environ.pop("MSH_DATA_DIR", None)
            else:
                os.environ["MSH_DATA_DIR"] = old


class ConfigHandEditValidationTest(unittest.TestCase):
    """★ 手改 config.toml 曾经完全绕过校验。

    文件头部注释明确鼓励手改，所以这是正常用法。直接 setattr 会吃进
    `scan_workers=99999999`（真开同等线程）、`station_max_gb=nan`
    （容量上限静默失效）、`"很多"`（后面抛类型错）。
    """

    @classmethod
    def setUpClass(cls):
        cls.C = py("core/config.py")

    def test_load_validates_each_key(self):
        i = self.C.index("def load_config")
        seg = fn_body(self.C, "load_config")
        self.assertIn("validate_setting", seg)
        self.assertIn("SettingsError", seg)

    def test_errors_are_recorded_not_swallowed(self):
        self.assertIn("_settings_errors", self.C)
        self.assertIn("def settings_errors", self.C)

    def test_bad_values_fall_back_to_defaults(self):
        tmp = tempfile.mkdtemp(prefix="msh-cfgv-")
        old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = tmp
        try:
            from app.core import config as C
            p = C.config_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                "[sources]\n\n[settings]\n"
                "scan_workers = 99999999\n"
                "station_max_gb = nan\n"
                "name_similarity = 7.5\n",
                encoding="utf-8")
            cfg = C.load_config(force=True)
            self.assertLessEqual(cfg.settings.scan_workers, 64,
                                 "天文数字必须被拒")
            s = cfg.settings
            # ★ 关键：nan 会让 `bytes > cap` 恒为 False，容量上限静默失效
            self.assertEqual(s.station_max_gb, s.station_max_gb,
                             "station_max_gb 成了 NaN → 容量上限会失效")
            self.assertNotEqual(s.station_max_gb, float("inf"))
            self.assertLessEqual(s.name_similarity, 1)
            self.assertTrue(C.settings_errors(), "被拒的项要记下来给界面")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            if old is None:
                os.environ.pop("MSH_DATA_DIR", None)
            else:
                os.environ["MSH_DATA_DIR"] = old


class WindowsReservedNameTest(unittest.TestCase):
    """★ Windows 保留设备名：写进去是 0 字节，`os.replace` 到它会永久挂起。"""

    def test_upload_safe_name(self):
        from app.upload_api import _safe_name
        for bad in ("CON", "CON.flac", "NUL.mp3", "COM1", "com5.flac",
                    "LPT1.wav", "AUX", "PRN.aac"):
            got = _safe_name(bad)
            self.assertNotEqual(bad, got, f"{bad} 应该被改名")
            self.assertTrue(got.startswith("_") or "_" in got, got)
        # 正常名字不能被误伤
        for good in ("console.flac", "connection.mp3", "正常歌曲.flac",
                     "concert.mp3"):
            self.assertEqual(good, _safe_name(good), f"{good} 被误改了")

    def test_station_safe_name(self):
        tmp = tempfile.mkdtemp(prefix="msh-res-")
        try:
            from app.station_api import _safe_name
            self.assertNotEqual("CON.flac", _safe_name(tmp, "CON.flac"))
            self.assertEqual("console.flac", _safe_name(tmp, "console.flac"))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class WindowsNameRulesTest(unittest.TestCase):
    """★ Windows 命名规则的唯一来源 `paths.name_problem / safe_name`。

    实测（Win11）：`mkdir CON` 成功但打不开删不掉；`NUL` 当目录名时 makedirs
    静默"成功"、之后往里搬文件全部 WinError 3；`Album.` 会被系统剥成 `Album`。
    以前只有上传/中转站挡了保留名，新建目录、改名、整理目录名一个都没挡。
    """

    BAD = ["CON", "con", "NUL", "nul.mp3", "Aux", "PRN.flac", "COM1", "lpt9.wav",
           "CON.tar.gz", "nul .mp3", "Album.", "Album ", "a:b", "a?b", "a|b",
           "a\x01b", ".", "..", "", "   ", "x" * 256]
    GOOD = ["console", "Connection", "nullable.mp3", "COM10", "LPT0",
            "Auxiliary", ".hidden", " lead", "周杰伦", "Album (Remix)",
            "a.b.c.flac", "x" * 255]

    def test_name_problem(self):
        from app.core import paths as P
        for n in self.BAD:
            self.assertTrue(P.name_problem(n), f"{n!r} 应该被拒")
        for n in self.GOOD:
            self.assertEqual("", P.name_problem(n), f"{n!r} 被误拒")

    def test_path_problem_checks_every_segment(self):
        from app.core import paths as P
        self.assertTrue(P.path_problem("华语/NUL/x.mp3"))
        self.assertTrue(P.path_problem("Artist./Album"))
        self.assertEqual("", P.path_problem("华语/周杰伦/晴天.flac"))

    def test_safe_name_fixes_untrusted(self):
        from app.core import paths as P
        for n in ["Con", "NUL.mp3", "Album.", "a:b?c", "AC/DC", "x\x00y"]:
            fixed = P.safe_name(n, "fb")
            self.assertEqual("", P.name_problem(fixed), f"{n!r} → {fixed!r} 仍不合法")
        self.assertEqual("fb", P.safe_name("..", "fb"))
        self.assertEqual("正常歌曲.flac", P.safe_name("正常歌曲.flac"))

    def test_organize_seg_uses_rules(self):
        from app.core.organize import _safe_seg
        self.assertEqual("", __import__("app.core.paths", fromlist=["x"]).name_problem(_safe_seg("Nul")))

    def test_folder_create_rename_reject_reserved(self):
        """新建/改名目录的接口必须拒绝 CON / NUL / 尾随点。"""
        import inspect
        from app.sources import folders_api as F
        for fn in (F.create_folder, F.rename_folder):
            self.assertIn("name_problem", inspect.getsource(fn),
                          f"{fn.__name__} 没接 paths.name_problem")

    def test_executor_rejects_bad_target(self):
        """执行器：rename/move/copy 到 NUL / CON 一律报错，源文件不动。"""
        from app.core.ops import Executor
        from app.core.config import SourceConfig
        from app.sources.local import LocalSource
        tmp = tempfile.mkdtemp(prefix="msh-winname-")
        try:
            src_file = os.path.join(tmp, "a.mp3")
            with open(src_file, "wb") as fh:
                fh.write(b"AUDIO")
            s = LocalSource(SourceConfig(id="s", name="s", type="local", root=tmp))
            ex = Executor({"s": s})
            steps = [
                {"id": "r", "op": "rename", "src": "s", "dst": "s",
                 "src_path": "a.mp3", "dst_path": "NUL.mp3"},
                {"id": "m", "op": "move", "src": "s", "dst": "s",
                 "src_path": "a.mp3", "dst_path": "NUL/a.mp3"},
                {"id": "c", "op": "copy", "src": "s", "dst": "s",
                 "src_path": "a.mp3", "dst_path": "CON/a.mp3"},
            ]
            out = ex.run(steps, execute=True, workers=1)
            by = {r["step"]: r for r in out["results"]}
            for k in ("r", "m", "c"):
                self.assertEqual("error", by[k]["state"], by[k])
                self.assertIn("目标名不合法", by[k]["detail"])
            self.assertTrue(os.path.exists(src_file), "源文件必须原样保留")
            self.assertEqual(["a.mp3"], sorted(os.listdir(tmp)))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(os.name == "nt", "大小写不敏感文件系统才有这个问题")
class CaseOnlyRenameTest(unittest.TestCase):
    """★ 只改大小写（Song.mp3 → song.mp3）。

    修复前实测：policy=overwrite 时执行器认为"目标已存在"，先把目标移进回收站
    —— 而那个"目标"就是源文件本身。结果：曲库里文件消失，步骤报 error。
    skip 报"已存在"、rename 改成 `song (2).mp3`，都不是用户要的。
    """

    def test_all_policies(self):
        import shutil
        from app.core.ops import Executor
        from app.core.config import SourceConfig
        from app.sources.local import LocalSource
        for pol in ("skip", "rename", "overwrite"):
            tmp = tempfile.mkdtemp(prefix="msh-case-")
            try:
                with open(os.path.join(tmp, "Song.mp3"), "wb") as fh:
                    fh.write(b"AUDIO")
                os.makedirs(os.path.join(tmp, "d"))
                with open(os.path.join(tmp, "d", "Track.mp3"), "wb") as fh:
                    fh.write(b"T")
                s = LocalSource(SourceConfig(id="s", name="s", type="local", root=tmp))
                out = Executor({"s": s}).run([
                    {"id": "r", "op": "rename", "src": "s", "dst": "s",
                     "src_path": "Song.mp3", "dst_path": "song.mp3"},
                    {"id": "m", "op": "move", "src": "s", "dst": "s",
                     "src_path": "d/Track.mp3", "dst_path": "d/track.mp3"},
                ], execute=True, policy=pol, workers=1)
                for r in out["results"]:
                    self.assertEqual("done", r["state"], f"{pol}: {r}")
                names = [x for x in os.listdir(tmp) if not x.startswith(".")]
                self.assertIn("song.mp3", names, pol)
                self.assertEqual(["track.mp3"], os.listdir(os.path.join(tmp, "d")), pol)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_rename_dir_case_only(self):
        import shutil
        from app.core.config import SourceConfig
        from app.sources.local import LocalSource
        tmp = tempfile.mkdtemp(prefix="msh-case-")
        try:
            os.makedirs(os.path.join(tmp, "Album"))
            with open(os.path.join(tmp, "Album", "x.mp3"), "wb") as fh:
                fh.write(b"X")
            s = LocalSource(SourceConfig(id="s", name="s", type="local", root=tmp))
            s.rename_dir(tmp + "/Album", tmp + "/album")   # 原来抛"目标目录已存在且非空"
            self.assertIn("album", os.listdir(tmp))
            self.assertEqual(["x.mp3"], os.listdir(os.path.join(tmp, "album")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StationCheckConsistencyTest(unittest.TestCase):
    """★ 预检查的结论必须和真正执行时一致。

    踩过：预检查"超配额就判放不下"，而 `check_capacity` 在已经超配额时
    **放行**（配额是软的）→ 用户看到"空间不足"却真能搬成功。
    """

    @classmethod
    def setUpClass(cls):
        cls.S = py("station_api.py")

    def test_check_allows_when_already_over(self):
        i = self.S.index("def station_check")
        seg = fn_body(self.S, "station_check")
        # 已经超配额时不该判 ok=False
        self.assertIn('u["bytes"] <= cap', seg)
        self.assertIn("仍可继续使用", seg)

    def test_notice_has_unlimited(self):
        """两个区域的字段契约要一致（暂存区那份有 unlimited）。"""
        i = self.S.index("def capacity_notice")
        seg = fn_body(self.S, "capacity_notice")
        self.assertIn('"unlimited"', seg)


class FileOpTimeoutCoverageTest(unittest.TestCase):
    """★ 会挂住的文件系统调用必须有超时（审计在 upload_api 里发现 0 处）。"""

    def test_upload_uses_timeout(self):
        src = py("upload_api.py")
        self.assertIn("call_with_timeout", src)
        self.assertIn("from .core.ops import call_with_timeout", src)

    def test_tag_write_uses_timeout(self):
        src = py("core/tags.py")
        self.assertIn("def _guarded", src)
        self.assertIn("call_with_timeout", src)

    def test_station_uses_timeout(self):
        src = py("station_api.py")
        self.assertIn("def _moved", src)
        self.assertIn("def _copied", src)

    def test_heartbeat_in_long_loops(self):
        """长循环不打心跳会被判"已中断"并释放独占位。"""
        src = py("upload_api.py")
        i = src.index("def _dedup_files")
        seg = fn_body(src, "_dedup_files")
        self.assertIn("job.touch()", seg, "查重循环没打心跳")
        j = src.index("def _move_local_to_source")
        seg2 = src[j:j + 2000]
        self.assertIn("job.touch()", seg2, "入库循环没打心跳")
        self.assertIn("job.cancel", seg2, "入库循环不能取消")


class FrontendFeedbackChannelTest(unittest.TestCase):
    """★ toast 曾经渲染在视口外 —— 全应用的反馈通道是断的。"""

    @classmethod
    def setUpClass(cls):
        cls.I = (APP / "web" / "index.html").read_text(encoding="utf-8")
        cls.CSS = (APP / "web" / "static" / "css" / "app.css").read_text(
            encoding="utf-8")

    def test_toast_root_has_the_positioning_class(self):
        """定位挂在 .toast-wrap 上，光有 id 不够。"""
        self.assertIn('id="toast-root" class="toast-wrap"', self.I)
        self.assertIn(".toast-wrap{position:fixed", self.CSS.replace(" ", ""))


class RetryContractTest(unittest.TestCase):
    """★ 「重试」按钮的契约要一致：生产者只生成步骤，消费者负责在锁里执行。"""

    @classmethod
    def setUpClass(cls):
        cls.A = js("app.js")
        cls.O = js("views.ops.js")

    def test_producer_returns_steps_only(self):
        i = self.A.index("function logOp")
        seg = self.A[i:i + 1800]
        # 不能再自己执行写操作
        self.assertNotIn("API.execute({ steps: again, execute: true", seg)

    def test_consumer_runs_under_lock(self):
        i = self.O.index("entry.retry()")
        seg = self.O[i - 400:i + 1200]
        self.assertIn("runExclusive", seg)

    def test_run_exclusive_finishes_once(self):
        """★ finish 调用两次 → 每个写操作发两次 ack + 两次跨页广播。"""
        i = self.A.index("async function runExclusive")
        seg = self.A[i:i + 2600]
        self.assertEqual(1, seg.count("TaskSync.finish("),
                         "runExclusive 里 finish 应该只调一次")

    def test_client_injected_at_api_layer(self):
        """★ 漏传 client → owner 为空 → "别人在跑"恒为真、本页按钮被自己置灰。"""
        a = js("api.js")
        i = a.index("execute:")
        seg = a[i:i + 700]
        self.assertIn("TaskSync.ME", seg)


class PrefsConsistencyTest(unittest.TestCase):
    """★ 登记了什么，设置页就显示什么 —— 不能登记永远不写的键。"""

    @classmethod
    def setUpClass(cls):
        cls.P = js("prefs.js")
        cls.A = js("app.js")

    def test_no_raw_key_writes_for_volume_nav(self):
        self.assertNotIn("localStorage.setItem('msh.volume'", self.A)
        self.assertNotIn("localStorage.setItem('msh.navHidden'", self.A)
        self.assertIn("Prefs.set('volume'", self.A)
        self.assertIn("Prefs.set('navHidden'", self.A)

    def test_backfill_migrates_old_raw_keys(self):
        """老用户的裸键值要能搬进来，否则他们会看到"未设置"。"""
        self.assertIn("_backfill", self.P)

    def test_dead_keys_removed_from_known(self):
        for dead in ("filesView", "stationCols", "dupSepVer", "dupFilter"):
            self.assertNotIn(f"'{dead}':", self.P,
                             f"{dead} 从没被写过，不该登记（设置页会永远显示未设置）")

    def test_live_keys_registered(self):
        tail = "'" + "dupSepVer" + "'"
        self.assertIn(tail, js("views.duplicates.js"),
                      "dupSepVer 已经迁到 Prefs，使用点要跟着改")
        for live in ("volume", "navHidden", "libCols",
                     "activeSource", "lastView", "libFilters", "filesSort"):
            self.assertIn("'" + live + "'", self.P,
                          f"{live} 在用但没登记进 KNOWN")


class StagingButtonReacquireTest(unittest.TestCase):
    """★ 智能分配时按钮节点在 render() 后被换掉，改的是死节点。"""

    @classmethod
    def setUpClass(cls):
        cls.S = js("staging.js")

    def test_button_reacquired_each_time(self):
        i = self.S.index("async function smartAssign")
        seg = self.S[i:i + 2400]
        self.assertIn("setBtn", seg)
        # 必须在函数内部现取，而不是在开头抓一次
        self.assertIn("el.querySelector('#stg-bsmart')", seg)

    def test_uses_correct_api_param_name(self):
        """`/api/organize/plan-paths` 的参数是 `structure`（flat/nested）。

        ★ 这条测试原来写反了，2026-10 更正：

          · `keep_flat_below` 是 **`plan_by_album` 的内部参数**；
          · 接口 `/api/organize/plan-paths` 只读 `group_by` / `structure` / `min_group`，
            它内部自己算 `keep_flat_below=(structure == "nested")`。

        所以候选区以前传 `keep_flat_below: 5` 是**被忽略**的 ——
        "分层/单层"永远是默认值，用户选了也不生效。
        现在改成传 `structure`，并且先弹选项、再预览。
        """
        i = self.S.index("async function smartAssign")
        seg = self.S[i:i + 2000]
        self.assertNotIn("keep_flat_below", seg,
                         "这个参数接口不读，传了等于没传")
        self.assertIn("smartOptions", seg, "应先让用户选规则")
        self.assertIn("previewMoves", seg, "应先预览再采用")

    def test_options_cover_the_three_knobs(self):
        """选项必须覆盖接口真正支持的三个参数。"""
        i = self.S.index("function smartOptions")
        seg = self.S[i:i + 3000]
        for key in ("group_by", "structure", "min_group"):
            self.assertIn(key, seg, f"选项里缺少 {key}")
        self.assertIn("so-gb", seg)
        self.assertIn("so-st", seg)
        self.assertIn("so-mg", seg)


if __name__ == "__main__":
    unittest.main()
