"""非音乐文件处理的回归测试。

用户的需求：
  · "有些用户会往目录、中转站等目录扔乱七八糟的文件，
     这些文件是不应该被整理的"
  · "只能传音乐文件，乱七八糟的文件不能上传，
     但批量上传时这些文件不影响其他正常文件的上传"

这条链上有三层，本文件逐层锁住：
  1. 上传接口：只收音频，杂项被拒且**不阻断**同批正常文件
  2. 暂存区 / 中转站：手扔的杂物不进"待处理"列表，但看得见、占量如实
  3. 入库 / 归位：**最后一道闸** —— 客户端硬构造请求也搬不进音乐库
  4. 扫描：非音频不进索引（所以整理计划天然不会包含它们）
"""
from __future__ import annotations

import io
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MP3 = b"\xff\xfb\x90\x00" + bytes(413)
FLAC = b"fLaC" + bytes(64)
JUNK = {
    "notes.txt": b"hello\n" * 10,
    "cover.jpg": b"\xff\xd8\xff\xe0" + bytes(40),
    "archive.zip": b"PK\x03\x04" + bytes(40),
    "movie.mkv": b"\x1a\x45\xdf\xa3" + bytes(40),
    "installer.exe": b"MZ" + bytes(64),
    "noext": b"no extension here",
    "desktop.ini": b"[.ShellClassInfo]\n",
    "thumbs.db": b"\xd0\xcf\x11\xe0" + bytes(20),
}


class NonAudioBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="msh-nonaudio-", ignore_cleanup_errors=True)
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "i.sqlite3")
        cls.root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(cls.root, exist_ok=True)
        # ★ 中转站目录默认是 `<程序目录>/station`（**不是**数据目录下面的），
        #   所以只改 MSH_DATA_DIR 远远不够 —— 测试会把假文件写进**用户真实的
        #   station/ 目录**。我第一版就是这样，往真实目录里留了 9 个垃圾文件，
        #   最后手工才清掉。必须在配置里显式把它指到临时目录。
        cls._station = os.path.join(cls._tmp.name, "station")
        os.makedirs(cls._station, exist_ok=True)
        from app.core.config import (AppConfig, Settings, SourceConfig,
                                     load_config, save_config)
        save_config(AppConfig(
            sources=[SourceConfig(id="s1", name="t", type="local",
                                  root=cls.root)],
            settings=Settings(
                station_dir=cls._station,
                staging_dir=os.path.join(cls._tmp.name, "uploads"))))
        load_config(force=True)
        from app.main import app
        cls.client = TestClient(app)
        # 硬断言：中转站/暂存区必须都落在临时目录里，否则整批测试都不许跑
        from app import station_api as _S
        from app import upload_api as _U
        assert os.path.abspath(_S.station_dir()) == os.path.abspath(cls._station), \
            f"中转站没被隔离：{_S.station_dir()}"
        assert os.path.abspath(_U._uploads_root()).startswith(
            os.path.abspath(cls._tmp.name)), \
            f"暂存区没被隔离：{_U._uploads_root()}"

    @classmethod
    def tearDownClass(cls):
        try:
            from app import main as m
            m.DB.close()
            m.DB = None
        except Exception:
            pass
        try:
            from app.sources import manager
            manager.close_all()
        except Exception:
            pass
        import gc
        gc.collect()
        cls._tmp.cleanup()

    def _upload(self, names):
        files = []
        for n in names:
            data = JUNK.get(n) or (FLAC if n.endswith(".flac") else MP3)
            files.append(("files", (n, io.BytesIO(data),
                                    "application/octet-stream")))
        return self.client.post("/api/upload", files=files,
                                data={"source": "s1"}).json()


class UploadFormatGateTest(NonAudioBase):
    """1. 上传：只收音乐文件，且杂项**不阻断**同批正常文件。"""

    def test_audio_only(self):
        """★ 关键：混着传时，正常音频必须全部成功。"""
        names = ["a.mp3", "b.flac", "c.opus", "d.ape",
                 "junk.txt", "cover.jpg", "archive.zip"]
        j = self._upload(names)
        saved = {f["name"] for f in j["files"]}
        self.assertEqual({"a.mp3", "b.flac", "c.opus", "d.ape"}, saved,
                         f"音频没全部成功：{saved}")
        skipped = {s["name"] for s in j["skipped"]}
        self.assertEqual({"junk.txt", "cover.jpg", "archive.zip"}, skipped)

    def test_junk_reason_is_clear(self):
        j = self._upload(["notes.txt"])
        self.assertEqual(1, len(j["skipped"]))
        self.assertIn("不是音乐文件", j["skipped"][0]["reason"])
        self.assertEqual(0, j["saved"])

    def test_all_junk_batch_does_not_raise(self):
        """整批都是杂物：接口照常成功，只是 saved=0。"""
        j = self._upload(list(JUNK))
        self.assertEqual(0, j["saved"])
        self.assertEqual(len(JUNK), len(j["skipped"]))
        self.assertTrue(j.get("batch"))

    def test_all_official_formats_accepted(self):
        """★ 上传认的格式必须与扫描器认的**完全一致**。

        踩过：upload_api 里另抄了一份 16 个扩展名的清单，
        比 `paths.AUDIO_EXTS`（22 个）少 6 个 —— `.caf .m4b .oga .mpc .tta .wv`
        上传会被拒，但手动拷进库又能被扫描到，自相矛盾。
        """
        from app.core import paths as P
        j = self._upload([f"x{e}" for e in sorted(P.AUDIO_EXTS)])
        saved = {f["name"] for f in j["files"]}
        self.assertEqual(len(P.AUDIO_EXTS), len(saved),
                         f"这些格式被拒了："
                         f"{sorted(P.AUDIO_EXTS - {os.path.splitext(x)[1] for x in saved})}")

    def test_no_overwrite_on_same_name(self):
        """★ 同一批里两个同名文件不能互相覆盖。

        注意：**跨批次**同名不算冲突 —— 每次上传都开一个新批次目录，
        两次单传 `same.mp3` 各自落在自己的批次里是正常的（我第一版测试
        就是这么写的，断言错了）。真正的冲突场景是**同一请求里**传两个
        同名文件：它们落进同一个目录，必须自动改名。
        """
        files = [
            ("files", ("same.mp3", io.BytesIO(MP3), "audio/mpeg")),
            ("files", ("same.mp3", io.BytesIO(MP3), "audio/mpeg")),
        ]
        j = self.client.post("/api/upload", files=files,
                             data={"source": "s1"}).json()
        names = [f["name"] for f in j["files"]]
        self.assertEqual(2, len(names), f"两个同名文件都该收下：{names}")
        self.assertEqual(2, len(set(names)), f"同名没改名，会互相覆盖：{names}")
        self.assertIn("same.mp3", names)
        self.assertIn("same (1).mp3", names)

    def test_不同批次同名互不影响(self):
        """两次单传同名文件 → 各自独立批次，都保留。"""
        first = self._upload(["dup.mp3"])
        second = self._upload(["dup.mp3"])
        self.assertNotEqual(first["batch"], second["batch"])
        self.assertEqual("dup.mp3", first["files"][0]["name"])
        self.assertEqual("dup.mp3", second["files"][0]["name"])


class StagingJunkTest(NonAudioBase):
    """2+3. 暂存区：杂物不进待入库列表，且入库时被最后一道闸挡住。"""

    def _batch_with_junk(self):
        j = self._upload(["good.mp3"])
        b = j["batch"]
        from app import upload_api as U
        d = U._batch_dir(b)
        for n, data in JUNK.items():
            with open(os.path.join(d, n), "wb") as fh:
                fh.write(data)
        with open(os.path.join(d, "manual.flac"), "wb") as fh:
            fh.write(FLAC)
        return b

    def test_junk_not_in_pending_list(self):
        b = self._batch_with_junk()
        r = self.client.get("/api/upload/batches").json()
        bg = [x for x in r["batches"] if x["batch"] == b][0]
        names = {f["name"] for f in bg["files"]}
        self.assertIn("good.mp3", names)
        self.assertIn("manual.flac", names)
        for n in JUNK:
            self.assertNotIn(n, names, f"{n} 不该出现在待入库列表里")

    def test_junk_listed_as_ignored(self):
        """要看得见、删得掉 —— 否则文件占着盘却找不到。"""
        b = self._batch_with_junk()
        r = self.client.get("/api/upload/batches").json()
        bg = [x for x in r["batches"] if x["batch"] == b][0]
        ig = {x["name"] for x in bg["ignored"]}
        self.assertTrue(set(JUNK) <= ig, f"ignored 里缺：{set(JUNK) - ig}")
        self.assertEqual(len(JUNK), bg["other_count"])

    def test_junk_counted_in_usage(self):
        """配额管的是磁盘，杂物也真占盘 —— 必须计入。"""
        self._batch_with_junk()
        r = self.client.get("/api/upload/batches").json()
        self.assertGreaterEqual(r["other_count"], len(JUNK))
        self.assertGreater(r["other_bytes"], 0)

    def test_commit_rejects_junk(self):
        """★ 最后一道闸：直接构造 moves 也搬不进音乐库。"""
        b = self._batch_with_junk()
        r = self.client.post("/api/upload/commit", json={
            "batch": b, "source": "s1", "confirm": True, "policy": "skip",
            "scan": False,
            "moves": [{"rel": "notes.txt", "dst": "Album"},
                      {"rel": "cover.jpg", "dst": "Album"},
                      {"rel": "good.mp3", "dst": "Album"}]})
        self.assertEqual(200, r.status_code, r.text)
        j = r.json()
        in_lib = []
        for base, _d, fs in os.walk(self.root):
            in_lib += fs
        self.assertNotIn("notes.txt", in_lib, "杂物被搬进音乐库了")
        self.assertNotIn("cover.jpg", in_lib, "杂物被搬进音乐库了")
        self.assertIn("good.mp3", in_lib, "同批正常音频应该照常入库")
        self.assertEqual(1, j["moved"])
        self.assertTrue(j.get("not_music"), "要如实报告哪些被跳过")

    def test_commit_all_junk_gives_clear_error(self):
        b = self._batch_with_junk()
        r = self.client.post("/api/upload/commit", json={
            "batch": b, "source": "s1", "confirm": True, "policy": "skip",
            "scan": False,
            "moves": [{"rel": "notes.txt", "dst": ""},
                      {"rel": "cover.jpg", "dst": ""}]})
        self.assertEqual(400, r.status_code)
        self.assertIn("不是音乐文件", r.json()["detail"])

    def test_commit_success_path_works(self):
        """★ 入库的**成功路径**以前是坏的（从未被执行过）。

        踩过两处：
          · `m.start_scan(...)` —— `m` 在同函数里已被 manifest 覆盖；
          · `target_dirs` 用了 `r["dst_path"]`，而结果项的键是 `dst`。
        任何一个都会让"入库成功"变成 500。
        """
        b = self._upload(["ok.mp3"])["batch"]
        r = self.client.post("/api/upload/commit", json={
            "batch": b, "source": "s1", "confirm": True, "policy": "skip",
            "scan": False, "moves": [{"rel": "ok.mp3", "dst": "Sub"}]})
        self.assertEqual(200, r.status_code, r.text)
        j = r.json()
        self.assertEqual(1, j["moved"])
        self.assertIn("target_dirs", j)
        self.assertTrue(j["target_dirs"], "落点目录要报出来")
        self.assertEqual(0, j["remaining_in_batch"], "入库成功后应从批次里移除")


class StationJunkTest(NonAudioBase):
    """2+3. 中转站：同样的规则。"""

    def _station_with_junk(self):
        from app import station_api as S
        d = S.station_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "keep.flac"), "wb") as fh:
            fh.write(FLAC)
        for n, data in JUNK.items():
            with open(os.path.join(d, n), "wb") as fh:
                fh.write(data)
        return d

    def test_junk_not_in_pending_list(self):
        self._station_with_junk()
        r = self.client.get("/api/station").json()
        names = {f["name"] for f in r["files"]}
        self.assertEqual({"keep.flac"}, names, f"不该有杂物：{names}")

    def test_junk_listed_as_ignored(self):
        self._station_with_junk()
        r = self.client.get("/api/station").json()
        ig = {f["name"] for f in r["ignored"]}
        self.assertTrue(set(JUNK) <= ig, f"ignored 里缺：{set(JUNK) - ig}")

    def test_junk_counted_in_usage(self):
        self._station_with_junk()
        r = self.client.get("/api/station").json()
        self.assertGreaterEqual(r["other_count"], len(JUNK))
        self.assertGreater(r["other_bytes"], 0)

    def test_move_out_rejects_junk(self):
        self._station_with_junk()
        r = self.client.post("/api/station/move-out", json={
            "source": "s1", "dest": self.root + "/Restored",
            "files": ["notes.txt", "cover.jpg", "keep.flac"]}).json()
        moved = {m["name"] for m in r["moved"]}
        self.assertEqual({"keep.flac"}, moved, f"杂物被归位了：{moved}")
        errs = {f["name"]: f["error"] for f in r["failed"]}
        self.assertIn("notes.txt", errs)
        self.assertIn("不是音乐文件", errs["notes.txt"])


class ScannerAudioOnlyTest(NonAudioBase):
    """4. 扫描只收音频 → 整理计划天然不含非音频。"""

    def test_scan_indexes_audio_only(self):
        from app import main as m
        d = os.path.join(self.root, "Album")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "track.mp3"), "wb") as fh:
            fh.write(MP3)
        with open(os.path.join(d, "cover.jpg"), "wb") as fh:
            fh.write(JUNK["cover.jpg"])
        with open(os.path.join(d, "info.nfo"), "w", encoding="utf-8") as fh:
            fh.write("release info\n")
        r = self.client.post("/api/scan/s1", json={"workers": 1})
        self.assertEqual(200, r.status_code, r.text)
        for _ in range(80):
            time.sleep(0.25)
            jobs = self.client.get("/api/jobs").json()
            if any(x["kind"] == "scan"
                   and x["state"] in ("done", "error", "cancelled")
                   for x in jobs):
                break
        names = {t["name"] for t in m.db().all_tracks("s1")}
        self.assertIn("track.mp3", names)
        self.assertNotIn("cover.jpg", names)
        self.assertNotIn("info.nfo", names)

    def test_organize_plan_excludes_non_audio(self):
        """整理计划里绝不出现非音频路径。

        整理只吃索引里的曲目，而索引只收音频 —— 这条链要一直成立。
        """
        from app.core import organize as OG
        rows = [{"source_id": "s1", "path": f"{self.root}/散/a{i}.mp3",
                 "rel_path": f"散/a{i}.mp3", "name": f"a{i}.mp3", "size": 1000,
                 "duration": 100.0, "title": f"a{i}", "artist": "X",
                 "album": "Alb", "parent": f"{self.root}/散",
                 "is_lossless": False, "bitrate": 320} for i in range(6)]
        plan = OG.plan_by_album(rows, self.root, min_group=5)
        for m in plan.get("moves", []):
            self.assertTrue(m["path"].lower().endswith((".mp3", ".flac")),
                            f"计划里出现了非音频：{m['path']}")


class AudioExtSingleSourceTest(unittest.TestCase):
    """★ 音频扩展名只能有**一份**清单。

    踩过两次同类问题：
      · `station_api.AUDIO_EXT` 少了 10 个格式
      · `upload_api.AUDIO_EXTS` 少了 6 个格式
    都是"另抄一份"造成的漂移，结论自相矛盾（能扫描到却传不上去）。
    """

    def test_only_paths_defines_the_list(self):
        import re
        import pathlib
        app = pathlib.Path(ROOT) / "app"
        offenders = []
        for p in list(app.glob("*.py")) + list((app / "core").glob("*.py")) \
                + list((app / "sources").glob("*.py")):
            if p.name == "paths.py":
                continue
            t = p.read_text(encoding="utf-8")
            if re.search(r"^AUDIO_EXTS?\s*=", t, re.M):
                offenders.append(p.name)
        self.assertEqual([], offenders,
                         f"这些文件又抄了一份音频扩展名清单：{offenders}")

    def test_is_audio_is_the_single_judge(self):
        from app.core import paths as P
        # 每个正式格式都要被认
        for e in sorted(P.AUDIO_EXTS):
            self.assertTrue(P.is_audio("x" + e), f"{e} 没被认成音频")
        for e in (".txt", ".jpg", ".zip", ".exe", ".nfo", ".ini", ".db", ""):
            self.assertFalse(P.is_audio("x" + e), f"{e} 不该被认成音频")


if __name__ == "__main__":
    unittest.main()


def _app_src_all() -> str:
    """所有后端源码拼在一起。

    `app/main.py` 已拆成 `deps.py` + `routes/*.py`，
    "某段代码是否存在"这类断言不该绑死单个文件。
    """
    parts = []
    for f in sorted(pathlib.Path(ROOT, "app").rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


class VersionLabelSingleSourceTest(unittest.TestCase):
    """版本类别（伴奏 / 重制 / Remix…）也只能有一个来源。

    踩过的坑：`app/web/static/js/views.duplicates.js` 里自己抄了一份
    `VERSION_LABEL` 映射，注释还写着"与后端 paths.VERSION_LABELS 一致"。
    实际后端有 14 类、前端只有 12 类 —— 漏了 `clip`（TV/片段）和
    `speed`（变速/Nightcore），这两类版本在查重页显示成英文而不是中文。

    现在前端从 `/api/system.version_kinds` 拿，本测试守住"不许再抄回去"。
    """

    @classmethod
    def setUpClass(cls):
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from app.core import paths as P
        cls.P = P
        cls.js_dir = os.path.join(ROOT, "app", "web", "static", "js")

    def _js_text(self) -> str:
        out = []
        for fn in sorted(os.listdir(self.js_dir)):
            if fn.endswith(".js"):
                with open(os.path.join(self.js_dir, fn), encoding="utf-8") as fh:
                    out.append(fh.read())
        return "\n".join(out)

    def test_all_kinds_have_a_label(self):
        """每个版本类别都要有短标签，否则界面只能显示英文。"""
        kinds = [k for k, _ in self.P.VERSION_KINDS]
        missing = [k for k in kinds if not self.P.VERSION_LABELS.get(k)]
        self.assertEqual([], missing,
                         f"这些版本类别没有中文短标签：{missing}")

    def test_labels_have_no_orphans(self):
        """VERSION_LABELS 里不许有 VERSION_KINDS 之外的键（防拼写错误）。"""
        kinds = {k for k, _ in self.P.VERSION_KINDS}
        orphans = sorted(set(self.P.VERSION_LABELS) - kinds)
        self.assertEqual([], orphans,
                         f"VERSION_LABELS 里有 VERSION_KINDS 之外的键：{orphans}")

    def test_frontend_has_no_second_label_map(self):
        """前端不许再抄一份完整映射。

        判据：如果前端 JS 里同时出现了 ≥8 个版本类别的名字，说明又抄了一份。
        （允许个别地方按名字特判单个类别，比如 `instrumental`。）
        """
        js = self._js_text()
        hits = [k for k, _ in self.P.VERSION_KINDS if f"'{k}'" in js or f'"{k}"' in js or f"{k}:" in js]
        self.assertLess(
            len(hits), 8,
            f"前端 JS 里出现了 {len(hits)} 个版本类别名 {hits} —— "
            f"看起来又抄了一份映射。请从 /api/system.version_kinds 读。")

    def test_api_exposes_kinds(self):
        """/api/system 必须把 version_kinds 带出去，否则前端没得用。"""
        src = _app_src_all()
        self.assertIn('"version_kinds"', src,
                      "/api/system 缺少 version_kinds —— 前端就没有唯一来源可读了")
        self.assertIn("P.VERSION_KINDS", src,
                      "version_kinds 应该从 paths.VERSION_KINDS 推导，不要手写第二份")
