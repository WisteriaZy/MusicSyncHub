"""上传暂存区 / 中转站：两个区域的边界与可管理性。

来自用户的追问："现在上传入库的暂存区和中转区有什么区别"，
以及要求"把事情收拾干净 + 让用户自定义两个区域的大小"。

两个区域的定位（代码里必须保持这个边界）：
  · **上传暂存区** `<data_dir>/uploads/<批次>/` —— 上传的中间态，
    属于"这次上传流程"，一次性的过路区。
  · **中转站** `<程序目录>/station/` —— "我还没决定放哪"，
    可长期存放，库里的文件也能临时挪进来。

修掉的具体毛病：
  1. `upload/batches` 只信 manifest，`if not files: continue` 会把
     manifest 丢失/没记全的批次**整个跳过** —— 文件占着盘却没有任何入口
     能看到或删掉（"幽灵文件"）。现在以磁盘为准。
  2. 上传的容量预检用错了尺子：文件落在**暂存区**，却拿**中转站**的配额
     去量。现在各有各的配额。
  3. 暂存区完全没有上限，涨到多大都没人管。
  4. 两个区域的位置和上限都不能改。现在都能在设置里配。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

# 项目根（不要写死本机路径）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent
from .srchelp import fn_body

ROOT = _PROJ


class ConfigFieldsTest(unittest.TestCase):
    """两个区域的位置和容量都必须可配置。"""

    @classmethod
    def setUpClass(cls):
        cls.CFG = (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
        from app.core.config import Settings
        cls.S = Settings

    def test_all_area_settings_exist(self):
        for k in ("station_dir", "station_max_gb",
                  "staging_dir", "staging_max_gb", "staging_stale_hours"):
            self.assertTrue(hasattr(self.S(), k), f"缺少设置项 {k}")

    def test_defaults_are_sane(self):
        s = self.S()
        # 暂存区只是过路区，默认上限应该比中转站**小**
        self.assertGreater(s.station_max_gb, 0)
        self.assertGreater(s.staging_max_gb, 0)
        self.assertLess(s.staging_max_gb, s.station_max_gb,
                        "暂存区是过路区，配额应小于中转站")
        self.assertGreater(s.staging_stale_hours, 0)

    def test_zero_means_unlimited(self):
        """0 = 不限制，这是文档承诺的语义。"""
        s = self.S(station_max_gb=0, staging_max_gb=0)
        self.assertEqual(0, s.station_max_gb)
        self.assertEqual(0, s.staging_max_gb)

    def test_settings_are_serialized(self):
        """必须能写进 config.toml（_dump_toml 是 asdict 遍历，天然支持）。"""
        from dataclasses import asdict
        d = asdict(self.S())
        for k in ("station_dir", "station_max_gb",
                  "staging_dir", "staging_max_gb", "staging_stale_hours"):
            self.assertIn(k, d)


class StagingDiskTruthTest(unittest.TestCase):
    """`upload/batches` 必须以**磁盘实际情况**为准，不能只信 manifest。"""

    API = None

    @classmethod
    def setUpClass(cls):
        cls.API = (ROOT / "app" / "upload_api.py").read_text(encoding="utf-8")

    def test_does_not_skip_batches_without_manifest(self):
        """★ 不能再有"清单里没文件就整批跳过"的逻辑。

        那正是"幽灵文件"的成因：文件占着盘，界面上却看不到、删不掉。
        """
        self.assertIn("def _disk_batch_files", self.API)
        i = self.API.index("def upload_batches")
        seg = fn_body(self.API, "upload_batches")
        self.assertNotIn("if not files:\n            continue", seg,
                         "不能因为 manifest 里没记录就跳过整个批次")
        self.assertIn("_disk_batch_files(d)", seg)

    def test_reports_orphan_batches(self):
        """清单缺失要**明确标出来**，而不是假装数据完好。"""
        i = self.API.index("def upload_batches")
        seg = fn_body(self.API, "upload_batches")
        self.assertIn('"orphan"', seg)

    def test_reports_stale_batches(self):
        i = self.API.index("def upload_batches")
        seg = fn_body(self.API, "upload_batches")
        self.assertIn('"stale"', seg)
        self.assertIn("staging_stale_hours", self.API)

    def test_lists_stray_files_in_root(self):
        """直接散落在 uploads/ 下的文件也要列出来。

        窗口按函数体长度取，不要写死一个小数字 —— 函数一长断言就会
        莫名其妙地失败（踩过：加了几行"ignored 杂物"逻辑之后就是如此）。
        """
        i = self.API.index("def upload_batches")
        j = self.API.index("\n@router.", i)
        seg = self.API[i:j]
        self.assertIn('"stray"', seg)

    def test_lists_non_audio_as_ignored(self):
        """★ 用户手扔进来的非音频要单独列出来，但不进待入库列表。"""
        i = self.API.index("def upload_batches")
        j = self.API.index("\n@router.", i)
        seg = self.API[i:j]
        self.assertIn('"ignored"', seg)
        self.assertIn("_disk_batch_others", seg)

    def test_skips_only_temp_and_manifest(self):
        """只有临时文件和 manifest 本身不算内容。"""
        i = self.API.index("def _disk_batch_files")
        seg = fn_body(self.API, "_disk_batch_files")
        self.assertIn(".mshpart", seg)
        self.assertIn("manifest.json", seg)


class StagingCapacityTest(unittest.TestCase):
    """容量：暂存区有自己的配额，上传要按它来拦。"""

    API = None

    @classmethod
    def setUpClass(cls):
        cls.API = (ROOT / "app" / "upload_api.py").read_text(encoding="utf-8")

    def test_has_own_quota_helper(self):
        self.assertIn("def staging_max_bytes", self.API)
        self.assertIn("def staging_usage", self.API)
        self.assertIn("def check_staging_capacity", self.API)

    def test_upload_checks_staging_not_station(self):
        """★ 上传要检查**暂存区**的配额。

        之前这里是 `_ST.check_capacity(...)`（中转站的），
        用错的尺子量：中转站满了会拦上传，暂存区涨爆了反而没人管。
        """
        i = self.API.index("async def upload(")
        seg = self.API[i:i + 1400]
        self.assertIn("check_staging_capacity", seg)
        self.assertNotIn("_ST.check_capacity", seg,
                         "上传不该用中转站的配额")

    def test_capacity_error_is_actionable(self):
        """拒绝时要告诉用户怎么办（调上限 / 去清理），不只是"空间不足"。"""
        i = self.API.index("def check_staging_capacity")
        seg = fn_body(self.API, "check_staging_capacity")
        self.assertIn("设置", seg)
        self.assertIn("中转站", seg)

    def test_usage_keys_do_not_collide(self):
        """★ `staging_usage()` 的键名不能和返回结构撞车。

        踩过：它原来返回 `count`/`batches`（数字），而
        `/api/upload/batches` 里 `batches` 是**列表**，
        `**u` 展开把列表覆盖成 int，前端 `len()` 直接报错。
        """
        i = self.API.index("def staging_usage")
        seg = fn_body(self.API, "staging_usage")
        self.assertIn('"file_count"', seg)
        self.assertIn('"batch_count"', seg)
        self.assertNotIn('"batches":', seg, "不能和 batches 列表撞名")
        self.assertNotIn('"count":', seg, "count 也容易撞车")


class StagingFlowTest(unittest.TestCase):
    """暂存区能看、能删、能转进中转站、能清理。"""

    API = None
    ST = None

    @classmethod
    def setUpClass(cls):
        cls.API = (ROOT / "app" / "upload_api.py").read_text(encoding="utf-8")
        cls.ST = (ROOT / "app" / "station_api.py").read_text(encoding="utf-8")

    def test_has_all_management_endpoints(self):
        for route in ("/api/upload/batches", "/api/upload/batch-to-station",
                      "/api/upload/cleanup"):
            self.assertIn(route, self.API, f"缺少端点 {route}")

    def test_cleanup_needs_confirm_for_full_wipe(self):
        """全清是破坏性的，必须 confirm。"""
        i = self.API.index("def upload_cleanup")
        seg = fn_body(self.API, "upload_cleanup")
        self.assertIn('mode == "all"', seg)
        self.assertIn("confirm", seg)

    def test_cleanup_supports_keep_list(self):
        """要让用户能"保留某几个批次，其余清掉"。"""
        i = self.API.index("def upload_cleanup")
        seg = fn_body(self.API, "upload_cleanup")
        self.assertIn("keep", seg)

    def test_transfer_to_station_not_delete(self):
        """转进中转站 ≠ 删除：文件要真的搬过去。"""
        i = self.API.index("def upload_batch_to_station")
        seg = fn_body(self.API, "upload_batch_to_station")
        self.assertIn("adopt_file", seg)

    def test_station_adopt_file_checks_capacity(self):
        i = self.API.index("def upload_batch_to_station")
        seg = fn_body(self.API, "upload_batch_to_station")
        self.assertIn("check_capacity", seg)

    def test_station_has_adopt_file(self):
        self.assertIn("def adopt_file", self.ST)

    def test_station_adopt_records_origin(self):
        """转到中转站也要记来源，否则以后没法追溯。"""
        i = self.ST.index("def adopt_file")
        seg = fn_body(self.ST, "adopt_file")
        self.assertIn("_record_origin", seg)

    def test_staging_dir_is_configurable(self):
        """暂存区位置可自定义（换到大盘上）。"""
        i = self.API.index("def _uploads_root")
        seg = fn_body(self.API, "_uploads_root")
        self.assertIn("staging_dir", seg)


class StationUiContractTest(unittest.TestCase):
    """界面：暂存区必须**看得见、管得了**。"""

    JS = None
    SET = None

    @classmethod
    def setUpClass(cls):
        base = ROOT / "app" / "web" / "static" / "js"
        cls.JS = (base / "views.station.js").read_text(encoding="utf-8")
        cls.SET = (base / "views.settings.js").read_text(encoding="utf-8")

    def test_station_page_has_staging_section(self):
        for k in ("stg-list", "stg-stats", "stg-dir"):
            self.assertIn(k, self.JS, f"缺少 {k}")

    def test_staging_section_has_management_buttons(self):
        for k in ("stg-to-station", "stg-del", "stg-clean-empty",
                  "stg-clean-stale", "stg-refresh"):
            self.assertIn(k, self.JS, f"缺少按钮 {k}")

    def test_staging_has_select_all(self):
        self.assertIn("stg-all", self.JS)
        self.assertIn("stg-none", self.JS)

    def test_staging_shows_stale_and_orphan_badges(self):
        """滞留 / 清单缺失都要在界面上标出来。"""
        self.assertIn("滞留", self.JS)
        self.assertIn("清单缺失", self.JS)
        self.assertIn("孤儿", self.JS)

    def test_transfer_is_not_labelled_as_delete(self):
        """「转入中转站」不能写成"删除"，否则用户以为是丢掉。"""
        i = self.JS.index("stg-to-station")
        seg = self.JS[i:i + 300]
        self.assertIn("转入中转站", seg)

    def test_settings_expose_area_fields(self):
        for k in ("station_dir", "station_max_gb",
                  "staging_dir", "staging_max_gb", "staging_stale_hours"):
            self.assertIn(k, self.SET, f"设置页缺少 {k}")

    def test_settings_show_current_usage(self):
        """改上限之后要能立刻看到两个区域现在占了多少。"""
        self.assertIn("st-areas-usage", self.SET)


if __name__ == "__main__":
    unittest.main()
