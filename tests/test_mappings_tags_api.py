"""映射规则 / 标签读写接口的回归测试。

这两个模块之前**一条测试都没有**（契约审计发现的），但都属于"会改用户东西"
的高风险路径：
  · 映射规则决定文件被搬到哪里 —— 记不住 / 写坏了，用户每次都要重设，
    而且可能把歌搬到错地方。
  · 标签写会**直接改音频文件的头** —— 写坏了文件就废了，所以必须有备份、
    必须原子写、必须能撤销。

这里只碰临时目录，绝不触碰真实音乐库。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="msh-map-tags-", ignore_cleanup_errors=True)
        os.environ["MSH_DATA_DIR"] = cls._tmp.name
        os.environ["MSH_DB"] = os.path.join(cls._tmp.name, "idx.sqlite3")
        cls.root = os.path.join(cls._tmp.name, "M").replace("\\", "/")
        os.makedirs(cls.root, exist_ok=True)
        from app.core.config import (AppConfig, SourceConfig, load_config,
                                     save_config)
        save_config(AppConfig(sources=[
            SourceConfig(id="s1", name="测试库", type="local", root=cls.root)]))
        load_config(force=True)
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        # Windows 上只要还有连接持有 .sqlite3，删临时目录就会 PermissionError。
        # 必须把 DB 和所有数据源连接都关掉 —— 顺便也验证了关闭逻辑本身。
        try:
            from app import main as main_mod
            main_mod.DB.close()
            main_mod.DB = None
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


class MappingRulesTest(_Base):
    """映射规则：能存、能读、能改、能删，且不会互相覆盖。"""

    def test_starts_empty(self):
        # 单测之间共享 mappings.json，先清干净再断言
        p = os.path.join(self._tmp.name, "mappings.json")
        if os.path.exists(p):
            os.remove(p)
        r = self.client.get("/api/mappings", params={"sid": "s1"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({}, r.json().get("rules") or {})

    def test_put_and_get_roundtrip(self):
        self._wipe()
        r = self.client.put("/api/mappings", json={
            "source": "s1",
            "rules": {"周杰伦": "华语/周杰伦", "Minecraft": "游戏/Minecraft"}})
        self.assertEqual(200, r.status_code, r.text)
        got = self.client.get("/api/mappings", params={"sid": "s1"}).json()
        self.assertEqual("华语/周杰伦", got["rules"]["周杰伦"])
        self.assertEqual("游戏/Minecraft", got["rules"]["Minecraft"])

    def test_survives_config_reload(self):
        """★ 必须落盘 —— 用户明确要求"映射功能也要记忆"。

        只放内存的话，重启程序就没了，用户每次都要重设。
        """
        self._wipe()
        self.client.put("/api/mappings", json={
            "source": "s1", "rules": {"X": "A/B"}})
        p = os.path.join(self._tmp.name, "mappings.json")
        self.assertTrue(os.path.exists(p), "mappings.json 没有落盘")
        with open(p, encoding="utf-8") as fh:
            disk = json.load(fh)
        self.assertEqual("A/B", disk["s1"]["X"])

    def test_no_partial_file_left(self):
        """原子写：不能留下 .part 残file。"""
        self.client.put("/api/mappings", json={
            "source": "s1", "rules": {"Y": "C/D"}})
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp.name, "mappings.json.part")))

    def test_other_source_untouched(self):
        """★ 改一个数据源的映射不能把另一个的冲掉。"""
        self._wipe()
        self.client.put("/api/mappings", json={
            "source": "s1", "rules": {"K1": "V1"}})
        self.client.put("/api/mappings", json={
            "source": "s2", "rules": {"K2": "V2"}})
        self.assertEqual("V1", self.client.get(
            "/api/mappings", params={"sid": "s1"}).json()["rules"]["K1"])
        self.assertEqual("V2", self.client.get(
            "/api/mappings", params={"sid": "s2"}).json()["rules"]["K2"])

    def _wipe(self):
        """清掉 mappings.json。

        单测之间共享这一个文件，而"损坏文件"用例会故意写坏它 ——
        不各自清干净的话结果就取决于用例执行顺序（踩过）。
        """
        for n in ("mappings.json", "mappings.json.part"):
            fp = os.path.join(self._tmp.name, n)
            if os.path.exists(fp):
                os.remove(fp)

    def test_single_rule_endpoint(self):
        """加一条 / 删一条（值传空字符串 = 删除）。"""
        self._wipe()
        self.client.post("/api/mappings/one", json={
            "source": "s1", "key": "单条", "dst": "Dst/One"})
        self.assertEqual("Dst/One", self.client.get(
            "/api/mappings", params={"sid": "s1"}).json()["rules"]["单条"])
        self.client.post("/api/mappings/one", json={
            "source": "s1", "key": "单条", "dst": ""})
        self.assertNotIn("单条", self.client.get(
            "/api/mappings", params={"sid": "s1"}).json()["rules"])

    def test_corrupt_file_does_not_crash(self):
        """★ 文件被写坏时不能把接口带崩 —— 返回空规则即可。"""
        p = os.path.join(self._tmp.name, "mappings.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        r = self.client.get("/api/mappings", params={"sid": "s1"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertIsInstance(r.json().get("rules"), dict)

    def test_all_sources_endpoint(self):
        r = self.client.get("/api/mappings")
        self.assertEqual(200, r.status_code, r.text)
        self.assertIn("sources", r.json())


class TagWriteTest(_Base):
    """标签写：有备份、可撤销、原子写、不碰坏文件。"""

    def setUp(self):
        self.d = os.path.join(self.root, "Album")
        os.makedirs(self.d, exist_ok=True)

    FFMPEG = (r"D:\ffmpeg-8.0.1\ffmpeg-2026-01-07-git-af6a1dd0b2-essentials_build"
              r"\bin\ffmpeg.exe")

    @classmethod
    def _ffmpeg(cls) -> str:
        import shutil as _sh
        if os.path.exists(cls.FFMPEG):
            return cls.FFMPEG
        return _sh.which("ffmpeg") or ""

    def _mk(self, name: str, codec: str) -> str:
        """用 ffmpeg 造一个 0.3 秒的**真实**音频文件。

        为什么不用手搓字节：手搓的"最小 FLAC"mutagen 读得出来但写不进去
        （`file said 1 bytes, read 0 bytes`）—— 那测的是夹具的毛病，
        不是产品的行为。真实编码器产出的文件才有代表性。
        没有 ffmpeg 就跳过，不用假夹具骗自己。
        """
        exe = self._ffmpeg()
        if not exe:
            self.skipTest("没有 ffmpeg，跳过真实音频标签测试")
        import subprocess
        out = os.path.join(self.d, name)
        args = [exe, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.3"]
        if codec == "flac":
            args += ["-c:a", "flac", out]
        elif codec == "mp3":
            args += ["-c:a", "libmp3lame", "-b:a", "128k", out]
        elif codec == "wav":
            args += ["-c:a", "pcm_s16le", out]
        elif codec == "m4a":
            args += ["-c:a", "aac", "-b:a", "128k", out]
        else:
            raise ValueError(codec)
        r = subprocess.run(args, capture_output=True, timeout=120)
        if not os.path.exists(out) or os.path.getsize(out) < 128:
            self.skipTest(f"ffmpeg 没能生成 {name}: "
                          f"{r.stderr.decode('utf-8','replace')[:200]}")
        return out.replace("\\", "/")

    def _fields(self, path: str) -> dict:
        """读标签。返回结构是 {fields:{...}}，不是扁平的 —— 别写成 .get('title')。"""
        return self.client.get("/api/tags/read",
                               params={"sid": "s1", "path": path}).json()["fields"]

    def test_preview_does_not_write(self):
        p = self._mk("b.mp3", "mp3")
        before = open(p, "rb").read()
        r = self.client.post("/api/tags/preview", json={
            "source": "s1", "paths": [p], "changes": {"title": "新标题"}})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(before, open(p, "rb").read(), "预演不该改文件")

    def test_write_makes_backup_and_applies(self):
        """★ 写入前必须有备份 —— 这是"改坏了能退回去"的唯一保障。"""
        p = self._mk("b.mp3", "mp3")
        r = self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p],
            "changes": {"title": "Hello", "artist": "World"},
            "confirm": True})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertGreaterEqual(body.get("ok", 0), 1, body)
        res = (body.get("results") or [{}])[0]
        self.assertTrue(res.get("backup"), "没有返回备份名")
        # backup 是**相对 backup_dir 的文件名**（不是完整路径）
        bp = os.path.join(body["backup_dir"].replace("/", os.sep), res["backup"])
        self.assertTrue(os.path.exists(bp), f"备份文件不存在: {bp}")
        # 读回来确认真的写进去了
        self.assertEqual("Hello", self._fields(p).get("title"))

    def test_no_temp_file_left(self):
        """原子写：临时文件不能留在目录里（否则会被当成音频扫进来）。"""
        p = self._mk("b.mp3", "mp3")
        self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p], "changes": {"title": "T"},
            "confirm": True})
        left = [f for f in os.listdir(self.d) if f.endswith(".mshtag")
                or f.endswith(".mshpart")]
        self.assertEqual([], left, f"残留临时文件: {left}")

    def test_restore_from_backup(self):
        """★ 撤销：恢复到备份后，标签要回到写之前的样子。"""
        p = self._mk("b.mp3", "mp3")
        self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p], "changes": {"title": "Original"},
            "confirm": True})
        # ★ 备份里存的是"**写之前**的标签"，所以第一次写的备份内容其实是空的。
        #   要还原到 "Original"，必须用**第二次**写（改之前）留下的那份备份。
        r = self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p], "changes": {"title": "Changed"},
            "confirm": True}).json()
        backup = r["results"][0]["backup"]      # 名字，restore 接口要的就是它
        self.assertEqual("Changed", self._fields(p)["title"])
        # 还原
        rr = self.client.post("/api/tags/restore", json={
            "source": "s1", "name": backup, "confirm": True})
        self.assertEqual(200, rr.status_code, rr.text)
        self.assertEqual("Original", self._fields(p)["title"])

    def test_backups_are_listed(self):
        p = self._mk("b.mp3", "mp3")
        self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p], "changes": {"title": "L"},
            "confirm": True})
        r = self.client.get("/api/tags/backups", params={"limit": 50})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json().get("backups") is not None)

    def test_write_requires_confirm(self):
        p = self._mk("b.mp3", "mp3")
        before = open(p, "rb").read()
        r = self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p], "changes": {"title": "X"}})
        self.assertIn(r.status_code, (400, 422), r.text)
        self.assertEqual(before, open(p, "rb").read(), "未确认却改了文件")

    def test_wav_not_corrupted(self):
        """★ WAV 必须保住 RIFF 头。

        踩过：早期用 ID3(path).save() 写 WAV，直接把 RIFF 头覆盖成 ID3，
        文件变成 `wave.Error: file does not start with RIFF id`。
        """
        p = self._mk("c.wav", "wav")
        r = self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p], "changes": {"title": "W"},
            "confirm": True}).json()
        self.assertGreaterEqual(r.get("ok", 0), 1, r)
        self.assertTrue(open(p, "rb").read().startswith(b"RIFF"),
                        "WAV 的 RIFF 头被破坏了")

    def test_flac_still_flac_after_write(self):
        p = self._mk("a.flac", "flac")
        r = self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [p], "changes": {"title": "F"},
            "confirm": True}).json()
        self.assertGreaterEqual(r.get("ok", 0), 1, r)
        self.assertTrue(open(p, "rb").read().startswith(b"fLaC"))

    def test_path_outside_root_rejected(self):
        """不能通过标签接口去改数据源之外的文件。"""
        import subprocess
        outside = os.path.join(self._tmp.name, "outside.mp3")
        exe = self._ffmpeg()
        if exe:
            subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                            "-t", "0.2", "-c:a", "libmp3lame", outside],
                           capture_output=True, timeout=120)
        if not os.path.exists(outside):
            self.skipTest("没有 ffmpeg")
        r = self.client.post("/api/tags/write", json={
            "source": "s1", "paths": [outside.replace("\\", "/")],
            "changes": {"title": "Bad"}, "confirm": True})
        raw = open(outside, "rb").read()
        self.assertFalse(b"Bad" in raw, "竟然修改了数据源之外的文件")


if __name__ == "__main__":
    unittest.main()
