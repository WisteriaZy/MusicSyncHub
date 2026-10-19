"""设置项必须**真的接线**，不能只是界面上的一个输入框。

## 为什么需要这两条测试

这个项目里出现过两次同一类缺陷：

  · `cover_cache_size` —— 界面能改，后端硬编码 2000，**改了完全没效果**
  · `ignored_names`     —— 界面能填、存进 config.toml，**后端零消费**

两者的共同点是：**看起来有这个设置项，但设了等于没设，而且不报错。**
比压根没有这个选项更误导 —— 用户会以为自己的意图被采纳了。

所以：
  · `SettingsWiringTest` 用静态分析保证每个 `Settings` 字段在 `config.py`
    **之外**至少被引用一次；
  · `IgnoredNamesBehaviourTest` 用真实扫描验证忽略名单真的生效。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile
import unittest

_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent
APP = _PROJ / "app"


def _backend_sources() -> dict[str, str]:
    """除 config.py 以外的所有后端源码（配置的定义处不算"被消费"）。"""
    out = {}
    for f in APP.rglob("*.py"):
        if "__pycache__" in f.parts or f.name == "config.py":
            continue
        out[f.relative_to(_PROJ).as_posix()] = f.read_text(encoding="utf-8")
    return out


class SettingsWiringTest(unittest.TestCase):
    """每个 Settings 字段都要有消费者。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(_PROJ))
        os.environ.setdefault("MSH_DATA_DIR",
                              tempfile.mkdtemp(prefix="msh-wiring-"))
        from app.core.config import Settings
        cls.settings_cls = Settings
        cls.sources = _backend_sources()
        cls.all_text = "\n".join(cls.sources.values())

    def test_every_field_has_a_consumer(self):
        import dataclasses
        dead = []
        for f in dataclasses.fields(self.settings_cls):
            if not re.search(rf"\b{re.escape(f.name)}\b", self.all_text):
                dead.append(f.name)
        self.assertEqual(
            [], dead,
            "这些设置项在 config.py 之外**没有任何引用** —— "
            "界面可能能改，但改了不会有任何效果：\n  " + "\n  ".join(dead) +
            "\n\n修法：要么把它接线，要么从 Settings 和界面上一起删掉。")

    def test_field_names_are_stable(self):
        """字段名被前端用字符串索引，改名必须同步前端。

        前端通过 `/api/settings` 拿一个以字段名为键的对象，
        所以后端改字段名而前端没改时，那个控件会静默失效。
        """
        import dataclasses
        js_dir = APP / "web" / "static" / "js"
        js_text = "\n".join(f.read_text(encoding="utf-8")
                            for f in js_dir.glob("*.js"))
        # 这两个是**后台守护**的高级开关：默认开启，界面上没有单独控件
        # （要关掉只能改 config.toml）。它们不是"前端漏了"，是刻意不给界面。
        _NO_UI_OK = {"auto_index_refresh_seconds", "auto_full_rescan_hours"}
        missing = [f.name for f in dataclasses.fields(self.settings_cls)
                   # 只检查"像设置项"的：前端至少要提到这个名字
                   if f.name not in js_text and f.name not in _NO_UI_OK]
        self.assertEqual(
            [], missing,
            "这些 Settings 字段在前端 JS 里一次都没出现 —— "
            "如果它们本该有界面，说明前端漏了；如果本来就没有界面，"
            "请把名字加进本测试的白名单并注明原因：\n  " + "\n  ".join(missing))


class IgnoredNamesBehaviourTest(unittest.TestCase):
    """`ignored_names` 必须真的影响扫描结果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="msh-ign-")
        self.tmp = pathlib.Path(self._tmp.name)
        self._old = os.environ.get("MSH_DATA_DIR")
        os.environ["MSH_DATA_DIR"] = str(self.tmp)

    def tearDown(self):
        try:
            from app.core.db import Database
            Database.close_all()
        except Exception:
            pass
        try:
            from app.core import jobs as J
            J.reset_for_tests()
        except Exception:
            pass
        if self._old is None:
            os.environ.pop("MSH_DATA_DIR", None)
        else:
            os.environ["MSH_DATA_DIR"] = self._old
        self._tmp.cleanup()
        for mod in [m for m in list(sys.modules) if m.startswith("app.")]:
            if mod in ("app.core.config",):
                continue
        try:
            from app.core import config as C
            C.load_config(force=True) if hasattr(C, "load_config") else None
        except Exception:
            pass

    def test_ignored_directory_is_skipped(self):
        from app.core.config import AppConfig, Settings, SourceConfig, save_config
        from app.core.db import Database
        from app.core import jobs as J, scanner as S
        from app.sources.base import build_source

        root = self.tmp / "lib"
        (root / "album").mkdir(parents=True)
        (root / "@eaDir").mkdir(parents=True)
        (root / "album" / "a.mp3").write_bytes(b"ID3" + b"\x00" * 200)
        (root / "@eaDir" / "b.mp3").write_bytes(b"ID3" + b"\x00" * 200)

        cfg = AppConfig(
            settings=Settings(ignored_names=["@eaDir"]),
            sources=[SourceConfig(id="s1", name="L", type="local",
                                  root=str(root).replace("\\", "/"))])
        save_config(cfg)
        db = Database(self.tmp / "i.sqlite3")
        J.init(db)
        try:
            src = build_source(cfg.sources[0])
            S.scan_source("s1", src, db)
            names = sorted(pathlib.Path(x["path"]).name
                           for x in db.all_tracks("s1"))
            self.assertNotIn("b.mp3", names,
                             "ignored_names 里的目录没被跳过")
            self.assertIn("a.mp3", names,
                          "正常目录被误跳过了 —— 忽略名单不能变成"
                          "「什么都不扫」")
        finally:
            db.close_all()
            J.reset_for_tests()


if __name__ == "__main__":
    unittest.main()
