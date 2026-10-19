"""文件夹级操作（新建 / 改名 / 移动 / 删除）的回归测试。

安全底线（这些都必须成立，任何一条破都是丢数据的风险）：
* 只在数据源根目录内操作，拒绝 `..` 绕行与越界路径；
* 根目录本身不能被改名/删除（但可以作为新建的 parent、移动的目标父目录）；
* 删除是"移入回收站可恢复"，不是递归真删；永久删除必须显式二次确认；
* 只读数据源一律拒绝写入；
* 改名/移动后索引里所有后代曲目的 path/parent/rel_path 必须同步更新。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class FolderOpsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-folders-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        cls.root_fs = Path(cls._tmp.name) / "M"
        cls.root = str(cls.root_fs).replace("\\", "/")
        (cls.root_fs / "专辑A" / "sub").mkdir(parents=True)
        (cls.root_fs / "专辑A" / "a1.flac").write_bytes(b"x" * 100)
        (cls.root_fs / "专辑A" / "a2.flac").write_bytes(b"x" * 100)
        (cls.root_fs / "专辑A" / "sub" / "s1.flac").write_bytes(b"y" * 50)
        (cls.root_fs / "专辑B").mkdir()
        (cls.root_fs / "专辑B" / "b1.mp3").write_bytes(b"z" * 30)

        from app.core.config import AppConfig, SourceConfig, save_config, load_config
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="测试库", type="local", root=cls.root),
            SourceConfig(id="ro", name="只读库", type="local", root=cls.root,
                         read_only=True),
        ]))
        load_config(force=True)

        from app.main import app, db
        db().upsert_source("s1", "测试库", "local", cls.root)
        db().upsert_source("ro", "只读库", "local", cls.root)
        db().upsert_tracks([
            cls._row("专辑A/a1.flac", 100), cls._row("专辑A/a2.flac", 100),
            cls._row("专辑A/sub/s1.flac", 50), cls._row("专辑B/b1.mp3", 30),
        ])
        cls.client = TestClient(app)

    @classmethod
    def _row(cls, rel, size):
        return {
            "source_id": "s1", "path": f"{cls.root}/{rel}", "rel_path": rel,
            "name": rel.rsplit("/", 1)[-1],
            "parent": f"{cls.root}/{rel.rsplit('/', 1)[0]}",
            "ext": "." + rel.rsplit(".", 1)[-1], "size": size, "mtime": 1.0,
            "title": rel, "artist": "", "album": "", "duration": 100.0,
            "bitrate": 900000, "sample_rate": 44100, "channels": 2,
            "bits_per_sample": 16, "codec": "FLAC", "is_lossless": 1,
            "has_cover": 0, "quality": 100.0, "hash_quick": None,
            "hash_full": None, "hash_state": "none", "dup_key": rel, "name_key": rel,
        }

    @classmethod
    def tearDownClass(cls):
        try:
            from app import main as main_mod
            if main_mod.DB is not None:
                main_mod.DB.close_all()
                main_mod.DB = None
        except Exception:
            pass
        try:
            from app.core import config as cfg_mod
            cfg_mod._cache = None
        except Exception:
            pass
        os.environ.pop("MSH_DATA_DIR", None)
        os.environ.pop("MSH_DB", None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def idx_paths(self):
        con = sqlite3.connect(os.path.join(self._tmp.name, "idx.sqlite3"))
        try:
            return sorted(r[0] for r in
                          con.execute("SELECT path FROM track WHERE source_id='s1'"))
        finally:
            con.close()

    # ---------------------------------------------------------------- 新建
    def test_create_folder(self):
        r = self.client.post("/api/folders/create",
                             json={"source": "s1", "parent": f"{self.root}/专辑A",
                                   "name": "新目录"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue((self.root_fs / "专辑A" / "新目录").is_dir())

    def test_create_at_root_is_allowed(self):
        """根目录可以作为 parent —— 早期版本会把这种情况误判成"操作根目录"。"""
        r = self.client.post("/api/folders/create",
                             json={"source": "s1", "parent": self.root, "name": "根下新目录"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue((self.root_fs / "根下新目录").is_dir())

    def test_create_rejects_duplicate_and_bad_name(self):
        self.client.post("/api/folders/create",
                         json={"source": "s1", "parent": f"{self.root}/专辑A",
                               "name": "重名"})
        r = self.client.post("/api/folders/create",
                             json={"source": "s1", "parent": f"{self.root}/专辑A",
                                   "name": "重名"})
        self.assertEqual(409, r.status_code)
        r = self.client.post("/api/folders/create",
                             json={"source": "s1", "parent": f"{self.root}/专辑A",
                                   "name": "bad/name"})
        self.assertEqual(400, r.status_code)

    # ---------------------------------------------------------------- 保护
    def test_root_cannot_be_modified(self):
        r = self.client.post("/api/folders/delete", json={"source": "s1", "path": self.root})
        self.assertEqual(400, r.status_code)
        r = self.client.post("/api/folders/rename",
                             json={"source": "s1", "path": self.root, "name": "x"})
        self.assertEqual(400, r.status_code)

    def test_rejects_escape_paths(self):
        # 真正的越界：一律 400
        for bad in (f"{self.root}/../etc", "D:/elsewhere/x", "a/../b"):
            r = self.client.post("/api/folders/delete",
                                 json={"source": "s1", "path": bad})
            self.assertEqual(400, r.status_code, f"{bad} 应被拒绝")

    def test_relative_path_is_inside_root_not_escape(self):
        """★ 相对路径是**合法**的（相对数据源根），不是越界。

        原来 `_inside("s1", "Album")` 会走到"路径超出数据源根目录，已拒绝"
        —— 一条语义上完全合理的相对路径被当成了越界，报错内容也和真实
        原因（"我只认绝对路径"）完全不是一回事。

        现在它按"根下的相对路径"解析：这个目录不存在 → **404**（找不到），
        而不是 400（非法）。这是"路径被接受了、但那东西没有"该有的回应。
        """
        r = self.client.post("/api/folders/delete",
                             json={"source": "s1", "path": "relative/path"})
        self.assertEqual(404, r.status_code,
                         "相对路径应当是'找不到'，不是'越界'")

    def test_relative_path_resolves_into_root(self):
        """相对路径要真的解析到根下面同一个位置（与绝对路径等价）。"""
        self.client.post("/api/folders/create",
                         json={"source": "s1", "parent": self.root,
                               "name": "相对测试"})
        r1 = self.client.get("/api/folders/info",
                             params={"sid": "s1", "path": "相对测试"})
        r2 = self.client.get("/api/folders/info",
                             params={"sid": "s1", "path": f"{self.root}/相对测试"})
        self.assertEqual(200, r1.status_code, r1.text)
        self.assertEqual(200, r2.status_code, r2.text)
        self.assertEqual(r1.json().get("rel"), r2.json().get("rel"))
        self.assertEqual("相对测试", r1.json().get("rel"))

    def test_read_only_source_rejected(self):
        r = self.client.post("/api/folders/create",
                             json={"source": "ro", "parent": self.root, "name": "x"})
        self.assertEqual(403, r.status_code)
        r = self.client.post("/api/folders/delete",
                             json={"source": "ro", "path": f"{self.root}/专辑B"})
        self.assertEqual(403, r.status_code)

    # ---------------------------------------------------------------- 信息
    def test_folder_info(self):
        """在独立子树里验证统计（其它用例会在共享夹具里建目录，所以不能断言全局绝对值）。"""
        self.client.post("/api/folders/create",
                         json={"source": "s1", "parent": self.root, "name": "info测试"})
        base = self.root_fs / "info测试"
        (base / "子目录").mkdir()
        (base / "f1.flac").write_bytes(b"a" * 100)
        (base / "f2.flac").write_bytes(b"b" * 200)
        (base / "子目录" / "f3.mp3").write_bytes(b"c" * 50)
        (base / "note.txt").write_bytes(b"d" * 10)

        r = self.client.get("/api/folders/info",
                            params={"sid": "s1", "path": f"{self.root}/info测试"})
        d = r.json()
        self.assertEqual(4, d["files"], "含非音频文件")
        self.assertEqual(1, d["dirs"])
        self.assertEqual(360, d["bytes"])
        self.assertEqual(3, d["audio"], "只数音频扩展名")

    # ---------------------------------------------------------------- 改名
    def test_rename_folder_updates_index(self):
        src = f"{self.root}/专辑A"
        r = self.client.post("/api/folders/rename",
                             json={"source": "s1", "path": src, "name": "专辑A改名"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(3, r.json()["indexed"], "三个后代曲目都要改")
        self.assertTrue((self.root_fs / "专辑A改名" / "sub" / "s1.flac").exists())
        self.assertFalse((self.root_fs / "专辑A").exists())
        paths = self.idx_paths()
        self.assertFalse([p for p in paths if "/专辑A/" in p], "不应残留旧路径")
        self.assertTrue([p for p in paths if "专辑A改名/sub/s1.flac" in p],
                        "子目录里的曲目也要跟着改")

    def test_move_folder_changes_parent(self):
        self.client.post("/api/folders/create",
                         json={"source": "s1", "parent": f"{self.root}/专辑A",
                               "name": "待移动"})
        src = f"{self.root}/专辑A/待移动"
        r = self.client.post("/api/folders/rename",
                             json={"source": "s1", "path": src,
                                   "name": "移动后", "new_parent": self.root})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue((self.root_fs / "移动后").is_dir())

    def test_cannot_move_folder_into_itself(self):
        r = self.client.post("/api/folders/rename",
                             json={"source": "s1", "path": f"{self.root}/专辑A",
                                   "name": "x", "new_parent": f"{self.root}/专辑A/sub"})
        self.assertEqual(400, r.status_code)

    # ---------------------------------------------------------------- 删除
    def test_delete_moves_to_trash_and_clears_index(self):
        self.client.post("/api/folders/create",
                         json={"source": "s1", "parent": f"{self.root}/专辑A",
                               "name": "待删除"})
        (self.root_fs / "专辑A" / "待删除" / "t.flac").write_bytes(b"t" * 10)
        self.client.post("/api/folders/create",
                         json={"source": "s1", "parent": f"{self.root}/专辑A",
                               "name": "留着的"})
        src = f"{self.root}/专辑A/待删除"
        r = self.client.post("/api/folders/delete", json={"source": "s1", "path": src})
        d = r.json()
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse((self.root_fs / "专辑A" / "待删除").exists())
        self.assertTrue(d.get("trash"), "必须给出去哪儿了")
        self.assertTrue(Path(d["trash"]).exists(), "回收站里应能找到")
        # 回收站里内容完整（可恢复）
        self.assertEqual(1, len(list(Path(d["trash"]).rglob("*.flac"))))
        # 邻居目录不受影响
        self.assertTrue((self.root_fs / "专辑A" / "留着的").is_dir())

    def test_hard_delete_requires_explicit_confirm(self):
        self.client.post("/api/folders/create",
                         json={"source": "s1", "parent": f"{self.root}/专辑A",
                               "name": "硬删测试"})
        src = f"{self.root}/专辑A/硬删测试"
        r = self.client.post("/api/folders/delete",
                             json={"source": "s1", "path": src, "hard": True})
        self.assertEqual(400, r.status_code)
        self.assertTrue((self.root_fs / "专辑A" / "硬删测试").is_dir(), "未确认不能删")
        r = self.client.post("/api/folders/delete",
                             json={"source": "s1", "path": src, "hard": True,
                                   "confirm_hard": True})
        self.assertEqual(200, r.status_code, r.text)
        self.assertFalse((self.root_fs / "专辑A" / "硬删测试").exists())


class EmptyFolderCleanupTest(unittest.TestCase):
    """一键清空空文件夹（取代了原来的「解散文件夹」）。

    原来的"解散"要求为**每个文件**单独指定去向 —— 为了清几个空壳目录
    要做十几步判断，操作极重。现在改成：扫出所有空文件夹 → 默认全选 →
    用户取消勾选想保留的 → 一次删除。

    这里要锁住的核心是**安全**：只删确认为空的目录，任何非空目录一律跳过
    并如实报告，绝不能因为用户勾错就连带删掉文件。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-empty-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        cls.root_fs = Path(cls._tmp.name) / "M"
        cls.root_fs.mkdir(parents=True, exist_ok=True)
        cls.root = str(cls.root_fs).replace("\\", "/")
        from app.core.config import AppConfig, SourceConfig, save_config, load_config
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="测试库", type="local", root=cls.root)]))
        load_config(force=True)
        from app.main import app, db
        db().upsert_source("s1", "测试库", "local", cls.root)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            from app import main as main_mod
            if main_mod.DB is not None:
                main_mod.DB.close_all()
                main_mod.DB = None
        except Exception:
            pass
        try:
            from app.core import config as cfg_mod
            cfg_mod._cache = None
        except Exception:
            pass
        os.environ.pop("MSH_DATA_DIR", None)
        os.environ.pop("MSH_DB", None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def setUp(self):
        # 每个用例一个**全新的根目录**。
        # 不能复用同一个根再清理：`rmdir` 对非空目录会静默失败，
        # 残留的目录会让后面的用例看到上一个用例的数据
        # （实测 `test_scan_scoped_to_subfolder` 就是这样拿到 `乙` 的）。
        import shutil
        self.case_root = tempfile.mkdtemp(prefix="msh-empty-case-")
        self.root_fs = Path(self.case_root) / "M"
        self.root_fs.mkdir(parents=True, exist_ok=True)
        self.root = str(self.root_fs).replace("\\", "/")
        from app.core.config import AppConfig, SourceConfig, save_config, load_config
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="测试库", type="local", root=self.root)]))
        load_config(force=True)
        from app.main import db
        db().upsert_source("s1", "测试库", "local", self.root)
        self.addCleanup(shutil.rmtree, self.case_root, True)

    def _mk(self, rel, payload=b"x"):
        p = self.root_fs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if payload is not None:
            p.write_bytes(payload)
        else:
            p.mkdir(parents=True, exist_ok=True)
        return str(p).replace("\\", "/")

    def _scan(self, path=""):
        r = self.client.get("/api/folders/empty-scan",
                            params={"sid": "s1", "start": path})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _remove(self, paths):
        r = self.client.post("/api/folders/empty-remove",
                             json={"source": "s1", "paths": paths, "confirm": True})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    # ---------------------------------------------------------- 扫描
    def test_scan_findsNested_empty_dirs(self):
        """空目录里的空子目录也算空（递归意义上的空）。"""
        self._mk("空壳/里面也空", None)
        self._mk("有歌/歌.flac")
        d = self._scan()
        rels = {f["rel"] for f in d["folders"]}
        self.assertIn("空壳", rels)
        self.assertIn("空壳/里面也空", rels)
        self.assertNotIn("有歌", rels, "有文件的目录不算空")

    def test_scan_ignores_dir_with_only_empty_subdirs(self):
        """只有空子目录的父目录，父目录**也算**空（因为里面确实没文件）。"""
        self._mk("a/b/c", None)
        d = self._scan()
        rels = {f["rel"] for f in d["folders"]}
        self.assertEqual({"a", "a/b", "a/b/c"}, rels)

    def test_scan_scoped_to_subfolder(self):
        self._mk("甲/空", None)
        self._mk("乙/空", None)
        d = self._scan(self.root + "/甲")
        rels = {f["rel"] for f in d["folders"]}
        self.assertTrue(any(r.startswith("甲") for r in rels))
        self.assertFalse([r for r in rels if r.startswith("乙")],
                         "限定范围时不该报范围外的")

    def test_scan_reports_depth_and_parent(self):
        self._mk("a/b/c", None)
        d = self._scan()
        by = {f["rel"]: f for f in d["folders"]}
        self.assertEqual(1, by["a"]["depth"])
        self.assertEqual(3, by["a/b/c"]["depth"])
        self.assertEqual("a/b", by["a/b/c"]["parent"])
        # 浅的排前面
        self.assertEqual("a", d["folders"][0]["rel"])

    # ---------------------------------------------------------- 删除
    def test_remove_deletes_empty_dirs(self):
        self._mk("a/b", None)
        d = self._scan()
        r = self._remove([f["path"] for f in d["folders"]])
        self.assertEqual(2, r["removed_count"], r)
        self.assertFalse((self.root_fs / "a").exists())

    def test_remove_refuses_non_empty(self):
        """★ 用户勾错了也不能删掉非空目录 —— 必须跳过并如实报告。"""
        self._mk("有歌/歌.flac")
        d = self._scan()
        # 直接伪造一个非空目录的路径塞进去
        r = self._remove([self.root + "/有歌"])
        self.assertEqual(0, r["removed_count"])
        self.assertEqual(1, r["skipped_count"])
        self.assertIn("非空", r["skipped"][0]["reason"])
        self.assertTrue((self.root_fs / "有歌" / "歌.flac").exists(),
                        "文件必须原封不动")

    def test_remove_keeps_unselected(self):
        """没勾选的目录必须保留（这就是"用户选择哪些不舍弃"）。"""
        self._mk("保留/空", None)
        self._mk("清掉/空", None)
        d = self._scan()
        keep = [f for f in d["folders"] if f["rel"].startswith("保留")]
        drop = [f for f in d["folders"] if f["rel"].startswith("清掉")]
        r = self._remove([f["path"] for f in drop])
        self.assertEqual(2, r["removed_count"])
        self.assertTrue((self.root_fs / "保留" / "空").exists(), "保留的必须还在")
        self.assertFalse((self.root_fs / "清掉").exists())

    def test_remove_deepest_first(self):
        """要先删深的再删浅的，否则父目录被删后子路径就失效了。"""
        self._mk("a/b/c/d", None)
        d = self._scan()
        # 故意打乱顺序传进去，后端应自行按深度排序
        paths = [f["path"] for f in d["folders"]]
        r = self._remove(list(reversed(paths)))
        self.assertEqual(4, r["removed_count"], r)
        self.assertFalse((self.root_fs / "a").exists())

    def test_remove_requires_confirm(self):
        self._mk("a", None)
        r = self.client.post("/api/folders/empty-remove",
                             json={"source": "s1", "paths": [self.root + "/a"]})
        self.assertEqual(400, r.status_code)
        self.assertIn("confirm", r.json()["detail"])

    def test_remove_rejects_empty_list(self):
        r = self.client.post("/api/folders/empty-remove",
                             json={"source": "s1", "paths": [], "confirm": True})
        self.assertEqual(400, r.status_code)

    def test_remove_rejects_outside_root(self):
        """越界路径一律忽略；全被忽略时报 400，不能删到库外面去。"""
        r = self.client.post("/api/folders/empty-remove",
                             json={"source": "s1",
                                   "paths": [self.root + "/../outside"],
                                   "confirm": True})
        self.assertEqual(400, r.status_code)
        self.assertIn("越界", r.json()["detail"])

    def test_dissolve_endpoint_is_gone(self):
        """旧接口必须明确报"已移除"，而不是静默消失（让老前端有提示）。"""
        r = self.client.post("/api/folders/dissolve",
                             json={"source": "s1", "path": self.root})
        self.assertEqual(410, r.status_code)
        self.assertIn("已移除", r.json()["detail"])


class MappingRulesTest(unittest.TestCase):
    """自定义映射规则的持久化（查看 / 编辑 / 记住）。

    这组来自用户的直接要求："映射功能要支持查看规则和编辑规则，
    不要忘了映射功能也要记忆"。

    之前映射只存在于一次会话的内存里，关掉窗口就没了 ——
    用户每次都得重建，等于这个功能不存在。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="msh-map-")
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        root_fs = Path(cls._tmp.name) / "M"
        root_fs.mkdir(parents=True, exist_ok=True)
        cls.root = str(root_fs).replace("\\", "/")
        from app.core.config import AppConfig, SourceConfig, save_config, load_config
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="测试库", type="local", root=cls.root)]))
        load_config(force=True)
        from app.main import app, db
        db().upsert_source("s1", "测试库", "local", cls.root)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        try:
            from app import main as main_mod
            if main_mod.DB is not None:
                main_mod.DB.close_all()
                main_mod.DB = None
        except Exception:
            pass
        try:
            from app.core import config as cfg_mod
            cfg_mod._cache = None
        except Exception:
            pass
        os.environ.pop("MSH_DATA_DIR", None)
        os.environ.pop("MSH_DB", None)
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def setUp(self):
        self.client.put("/api/mappings", json={"source": "s1", "rules": {}})

    def _rules(self):
        return self.client.get("/api/mappings", params={"sid": "s1"}).json()["rules"]

    def test_save_and_read_back(self):
        self.client.put("/api/mappings",
                        json={"source": "s1", "rules": {"范特西": "周杰伦/范特西"}})
        self.assertEqual({"范特西": "周杰伦/范特西"}, self._rules())

    def test_survives_a_fresh_read(self):
        """★ 关键：写完之后**重新读**还在（这就是"记忆"）。"""
        self.client.post("/api/mappings/one",
                         json={"source": "s1", "key": "周杰伦", "dst": "华语/周杰伦"})
        again = self._rules()
        self.assertEqual("华语/周杰伦", again.get("周杰伦"))

    def test_add_one_and_delete_one(self):
        self.client.post("/api/mappings/one",
                         json={"source": "s1", "key": "A", "dst": "音乐/A"})
        self.client.post("/api/mappings/one",
                         json={"source": "s1", "key": "B", "dst": "音乐/B"})
        self.assertEqual({"A", "B"}, set(self._rules()))
        # dst 传空 = 删除这条
        self.client.post("/api/mappings/one",
                         json={"source": "s1", "key": "A", "dst": ""})
        self.assertEqual({"B"}, set(self._rules()))

    def test_clear_removes_the_source_entry(self):
        self.client.put("/api/mappings",
                        json={"source": "s1", "rules": {"X": "Y"}})
        r = self.client.put("/api/mappings", json={"source": "s1", "rules": {}})
        self.assertEqual(0, r.json()["count"])
        self.assertEqual({}, self._rules())

    def test_rules_persist_to_disk(self):
        """规则要真的落盘（进程重启后仍在）。"""
        self.client.put("/api/mappings",
                        json={"source": "s1", "rules": {"专辑甲": "目录甲"}})
        p = Path(self._tmp.name) / "mappings.json"
        self.assertTrue(p.exists(), "应该写出 mappings.json")
        raw = p.read_text(encoding="utf-8")
        self.assertIn("专辑甲", raw)
        self.assertIn("目录甲", raw)

    def test_blank_keys_and_values_are_dropped(self):
        self.client.put("/api/mappings", json={
            "source": "s1",
            "rules": {"好键": "好目录", "": "空键", "   ": "空白键", "空值": "  "},
        })
        self.assertEqual({"好键": "好目录"}, self._rules())

    def test_bad_payloads_rejected(self):
        for body, label in (
            ({"rules": {}}, "缺 source"),
            ({"source": "s1", "rules": []}, "rules 不是对象"),
        ):
            r = self.client.put("/api/mappings", json=body)
            self.assertEqual(400, r.status_code, label)
        r = self.client.post("/api/mappings/one", json={"key": "a", "dst": "b"})
        self.assertEqual(400, r.status_code)

    def test_corrupt_file_does_not_break(self):
        """mappings.json 坏了也要能继续用，不能整个功能挂掉。"""
        (Path(self._tmp.name) / "mappings.json").write_text(
            "{ not json", encoding="utf-8")
        r = self.client.get("/api/mappings", params={"sid": "s1"})
        self.assertEqual(200, r.status_code)
        self.assertEqual({}, r.json()["rules"])


if __name__ == "__main__":
    unittest.main()
