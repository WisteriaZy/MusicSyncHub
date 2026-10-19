"""暂停/取消、崩溃恢复、自动刷新、配额语义、后台任务、设置校验、超时。

对应这一轮的需求：
  ① 暂停/取消任务          ② 程序异常退出后可接着做完
  ③ 页面数据自动更新        ④ 配额不够只提示不影响使用 + 0=不限额
  ⑤ 查重/对比跟上新机制     ⑥ 上传进度
  ⑦ 设置参数校验（防特殊符号/天文数字）
  ⑧⑨ 小屏适配 / 字体图标统一
  ⑩ 文件操作的容错与超时
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent
from .srchelp import app_src, fn_body


ROOT = _PROJ
JS = ROOT / "app" / "web" / "static" / "js"
CSS = ROOT / "app" / "web" / "static" / "css" / "app.css"
INDEX = ROOT / "app" / "web" / "index.html"


def js(n: str) -> str:
    return (JS / n).read_text(encoding="utf-8")


def py(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class PauseCancelTest(unittest.TestCase):
    """① 暂停 / 继续 / 取消 三个动作都要有，而且行为不同。"""

    @classmethod
    def setUpClass(cls):
        cls.J = py("app/core/jobs.py")
        cls.O = py("app/core/ops.py")
        cls.M = app_src()

    def test_all_three_actions_exist(self):
        for fn in ("def pause", "def resume", "def cancel"):
            self.assertIn(fn, self.J, f"jobs 里缺 {fn}")

    def test_pause_is_not_cancel(self):
        """★ 暂停和取消必须分开：暂停后还能继续，取消就是不做了。"""
        self.assertIn("pause: bool = False", self.J)
        self.assertIn("cancel: bool = False", self.J)
        # 按**函数体**取，不用字符窗口（窗口会随注释/新增分支漂移）。
        seg = fn_body(self.J, "finish")
        self.assertTrue(seg, "找不到 finish()")
        self.assertIn("elif st.pause:", seg)
        self.assertIn('st.state = "paused"', seg)

    def test_paused_job_keeps_exclusive_slot(self):
        """★ 暂停中的任务**仍然占用**独占位。

        不然别的流程会插进来操作同一批文件 —— 而暂停的任务随时可能继续。
        """
        i = self.J.index("def _busy_locked")
        seg = fn_body(self.J, "_busy_locked")
        self.assertIn('("running", "paused")', seg)

    def test_paused_not_treated_as_stale(self):
        """★ 暂停中的任务不该被"卡死回收"误判成中断。

        它心跳本来就该停（用户主动停的）。
        """
        i = self.J.index("def is_stale")
        seg = fn_body(self.J, "is_stale")
        self.assertIn('self.state not in ("running",)', seg)
        self.assertNotIn('"paused"', seg.split("STALE_JOB_SECONDS")[0])

    def test_worker_waits_when_paused(self):
        self.assertIn("def wait_if_paused", self.J)
        i = self.O.index("def work(")
        seg = self.O[i:i + 900]
        self.assertIn("wait_if_paused", seg)

    def test_remaining_steps_marked(self):
        """取消/暂停后剩下的步骤要显式记成"没做"，否则数字对不上。"""
        i = self.O.index("summary = _tally(results)")
        seg = self.O[i:i + 1200]
        self.assertIn('"pending"', seg)
        self.assertIn('"cancelled"', seg)

    def test_endpoints(self):
        for p in ("/pause", "/resume", "/cancel", "/ack"):
            self.assertIn(p, self.M, f"缺少端点 {p}")

    def test_frontend_buttons(self):
        a = js("app.js")
        for k in ("data-pause", "data-resume", "data-cancel"):
            self.assertIn(k, a, f"任务中心缺按钮 {k}")
        self.assertIn("已暂停", a)


class CrashRecoveryTest(unittest.TestCase):
    """② 程序异常退出后，重启能接着做完。"""

    @classmethod
    def setUpClass(cls):
        cls.J = py("app/core/jobs.py")
        cls.M = app_src()
        cls.A = js("app.js")

    def test_steps_persisted_to_db(self):
        """★ 只放内存的话，进程一挂就没了，重启后什么都恢复不了。"""
        self.assertIn("def persist_steps", self.J)
        self.assertIn("def record_steps", self.J)
        i = self.J.index("def record_steps")
        seg = fn_body(self.J, "record_steps")
        self.assertIn("persist_steps", seg)

    def test_load_unfinished_on_start(self):
        self.assertIn("def load_unfinished", self.J)
        i = self.M.index("async def lifespan")
        seg = self.M[i:i + 1600]
        self.assertIn("load_unfinished", seg)

    def test_unfinished_endpoint(self):
        self.assertIn('"/api/unfinished"', self.M)
        self.assertIn("/api/unfinished/recover", self.M)

    def test_recover_uses_skip_policy(self):
        """★ 恢复必须用 skip 策略。

        这是"重放安全"的前提：已经完成的移动会因为源不存在被跳过，
        目标已存在的也会被跳过 —— 不会覆盖、不会重复。
        """
        i = self.M.index("def recover_job")
        seg = fn_body(self.M, "recover_job")
        self.assertIn('policy="skip"', seg)

    def test_recover_respects_exclusive_lock(self):
        i = self.M.index("def recover_job")
        seg = fn_body(self.M, "recover_job")
        self.assertIn("JobBusy", seg)

    def test_frontend_asks_user(self):
        self.assertIn("async function checkUnfinished", self.A)
        self.assertIn("上次有任务没做完", self.A)
        self.assertIn("不会覆盖也不会重复", self.A)
        # 启动时检查
        i = self.A.index("async function boot")
        seg = self.A[i:i + 2600]
        self.assertIn("checkUnfinished", seg)

    def test_keep_enough_history(self):
        """keep 太小的话，上一次运行的关键记录会被挤掉。"""
        i = self.J.index("def __init__(self, db=None, keep")
        seg = self.J[i:i + 120]
        self.assertNotIn("keep: int = 40", seg)


class AutoRefreshTest(unittest.TestCase):
    """③ 页面数据要自动更新，不用用户按 F5。"""

    @classmethod
    def setUpClass(cls):
        cls.A = js("app.js")
        cls.M = app_src()

    def test_state_endpoint(self):
        self.assertIn('"/api/state"', self.M)

    def test_frontend_polls_and_diffs(self):
        self.assertIn("async function autoRefreshTick", self.A)
        self.assertIn("function startAutoRefresh", self.A)
        i = self.A.index("async function autoRefreshTick")
        seg = self.A[i:i + 1500]
        # 没变就什么都不做（不闪、不打断滚动和选中）
        self.assertIn("sig === prevSig", seg)

    def test_skips_when_hidden(self):
        """页面在后台时不做无用功。"""
        i = self.A.index("async function autoRefreshTick")
        seg = self.A[i:i + 600]
        self.assertIn("document.hidden", seg)

    def test_backs_off_while_busy(self):
        """有任务在跑时数据一直在变，频繁重载反而打断用户看进度。"""
        i = self.A.index("async function autoRefreshTick")
        seg = self.A[i:i + 900]
        self.assertIn("busy", seg)

    def test_refreshes_current_view_only(self):
        self.assertIn("function refreshCurrentView", self.A)
        i = self.A.index("function refreshCurrentView")
        seg = self.A[i:i + 1200]
        for v in ("files", "library", "station"):
            self.assertIn(v, seg)


class QuotaSemanticsTest(unittest.TestCase):
    """④ 配额不够只提示不影响使用；0 = 不限额。"""

    @classmethod
    def setUpClass(cls):
        cls.S = py("app/station_api.py")
        cls.U = py("app/upload_api.py")

    def test_zero_means_unlimited(self):
        """0 = 不限额。措辞可以是"不限"或"不限额"，但两条路径都要有。"""
        for src, name in ((self.S, "station"), (self.U, "staging")):
            self.assertIn("0 = 不限", src, f"{name} 没说明 0 的含义")
            # 代码里必须真的把 0 当成"不限制"来处理
            self.assertIn("if not cap:", src, f"{name} 没有 cap=0 的分支")

    def test_physical_disk_is_the_hard_limit(self):
        """★ 物理写满是硬约束；用户配额是软的。"""
        i = self.S.index("def check_capacity")
        seg = fn_body(self.S, "check_capacity")
        self.assertIn("free_disk", seg)
        self.assertIn("磁盘剩余空间不足", seg)

    def test_over_quota_still_allowed(self):
        """★ 已经超了配额就放行 —— 否则用户被自己以前存的文件卡死。"""
        i = self.S.index("def check_capacity")
        seg = fn_body(self.S, "check_capacity")
        self.assertIn('if u["bytes"] > cap:', seg)
        self.assertIn("return", seg)
        i2 = self.U.index("def check_staging_capacity")
        seg2 = self.U[i2:i2 + 1600]
        self.assertIn('if u["bytes"] > cap:', seg2)

    def test_notice_when_over(self):
        """超了要**说清楚**，但不拦。"""
        for src, fn in ((self.S, "capacity_notice"), (self.U, "staging_notice")):
            self.assertIn(f"def {fn}", src)
            i = src.index(f"def {fn}")
            seg = src[i:i + 1000]
            self.assertIn("over_quota", seg)
            self.assertIn("仍可继续", seg)

    def test_notice_exposed_in_api(self):
        i = self.S.index('@router.get("/api/station")')
        self.assertIn("capacity_notice", self.S[i:i + 400])

    def test_frontend_shows_over_quota(self):
        s = js("views.station.js")
        self.assertIn("over_quota", s + self.S)


class BackgroundJobTest(unittest.TestCase):
    """⑤ 查重/对比跟上新机制：后台 + 进度 + 可取消。"""

    @classmethod
    def setUpClass(cls):
        cls.B = py("app/core/bgjob.py")
        cls.M = app_src()

    def test_runner_exists(self):
        self.assertIn("def run_in_background", self.B)
        self.assertIn("def cached_result", self.B)

    def test_readonly_jobs_dont_take_exclusive_slot(self):
        """★ 只读计算不占独占位 —— 否则用户查重时什么都干不了。"""
        i = self.B.index("get_jobs().create")
        seg = self.B[i:i + 200]
        self.assertIn("exclusive=False", seg)

    def test_duplicates_is_background(self):
        i = self.M.index("def duplicates(")
        seg = self.M[i:i + 1400]
        self.assertIn("run_in_background", seg)
        self.assertIn("_duplicates_core", seg)

    def test_duplicates_has_progress_and_cancel(self):
        i = self.M.index("def _duplicates_core")
        seg = fn_body(self.M, "_duplicates_core")
        self.assertIn("def step(", seg)
        self.assertIn("job.cancel", seg)

    def test_compare_is_background(self):
        i = self.M.index("def compare(")
        seg = self.M[i:i + 1200]
        self.assertIn("run_in_background", seg)
        self.assertIn("_compare_core", seg)

    def test_sync_fallback_for_tests(self):
        """脚本/测试要能同步拿结果，否则没法写断言。"""
        for fn in ("def duplicates(", "def compare("):
            i = self.M.index(fn)
            seg = self.M[i:i + 1400]
            self.assertIn('payload.get("wait")', seg)

    def test_hash_progress_reported(self):
        i = self.M.index("def _hash_groups")
        seg = fn_body(self.M, "_hash_groups")
        self.assertIn("on_progress", seg)

    def test_job_dialog_has_pause_cancel(self):
        u = js("ui.js")
        self.assertIn("function watchJobDialog", u)
        i = u.index("function watchJobDialog")
        seg = u[i:i + 2600]
        self.assertIn("wj-pause", seg)
        self.assertIn("wj-cancel", seg)
        self.assertIn("API.pauseJob", seg)
        self.assertIn("API.cancelJob", seg)

    def test_dedup_and_compare_use_dialog(self):
        self.assertIn("watchJobDialog", js("views.duplicates.js"))
        self.assertIn("watchJobDialog", js("views.compare.js"))


class UploadProgressTest(unittest.TestCase):
    """⑥ 上传要有进度，不然用户以为卡死。"""

    @classmethod
    def setUpClass(cls):
        cls.A = js("api.js")
        cls.U = js("upload.js")

    def test_xhr_based_progress(self):
        """★ fetch 没有上传进度事件，必须用 XMLHttpRequest。"""
        self.assertIn("XMLHttpRequest", self.A)
        self.assertIn("upload.onprogress", self.A)
        self.assertIn("uploadFilesProgress", self.A)

    def test_shows_bytes_and_speed(self):
        i = self.U.index("up-bytes")
        seg = self.U[i:i + 2200]
        self.assertIn("fmtBytes", seg)
        self.assertIn("/s", seg)

    def test_handles_network_errors(self):
        i = self.A.index("function uploadReqProgress")
        seg = self.A[i:i + 1600]
        for ev in ("onerror", "ontimeout", "onabort"):
            self.assertIn(ev, seg)


class SettingsValidationTest(unittest.TestCase):
    """⑦ 设置参数要有边界，防止特殊符号 / 天文数字搞炸程序。"""

    @classmethod
    def setUpClass(cls):
        cls.C = py("app/core/config.py")
        cls.M = app_src()
        cls.S = js("views.settings.js")

    def test_validator_exists(self):
        self.assertIn("def validate_setting", self.C)
        self.assertIn("class SettingsError", self.C)

    def test_numeric_limits_defined(self):
        self.assertIn("_NUM_LIMITS", self.C)
        for k in ("scan_workers", "max_compare_tracks",
                  "station_max_gb", "staging_max_gb"):
            self.assertIn(k, self.C)

    def test_rejects_nan_and_inf(self):
        """★ NaN / inf 会让后面的比较和格式化全失效，
        属于"看着是数字其实搞炸程序"的典型。"""
        i = self.C.index("def validate_setting")
        seg = fn_body(self.C, "validate_setting")
        self.assertIn("num != num", seg)
        self.assertIn('float("inf")', seg)

    def test_rejects_control_chars_in_paths(self):
        i = self.C.index("def validate_setting")
        seg = fn_body(self.C, "validate_setting")
        self.assertIn("不可见字符", seg)

    def test_rejects_illegal_path_chars(self):
        self.assertIn("_PATH_FIELDS", self.C)
        i = self.C.index("def validate_setting")
        seg = fn_body(self.C, "validate_setting")
        self.assertIn("路径里不能包含", seg)

    def test_rejects_too_long(self):
        self.assertIn("_STR_MAX", self.C)

    def test_put_settings_validates(self):
        i = self.M.index("def put_settings")
        seg = fn_body(self.M, "put_settings")
        self.assertIn("validate_setting", seg)
        self.assertIn("SettingsError", seg)

    def test_put_settings_reports_partial_failure(self):
        """部分没存上要明说，不能让用户以为全存上了。"""
        i = self.M.index("def put_settings")
        seg = fn_body(self.M, "put_settings")
        self.assertIn('"errors"', seg)
        self.assertIn('"warning"', seg)

    def test_frontend_bounds_match_backend(self):
        self.assertIn("NUM_BOUNDS", self.S)
        for k in ("scan_workers", "station_max_gb", "staging_max_gb"):
            self.assertIn(k, self.S)

    def test_frontend_prevalidates(self):
        i = self.S.index("async function save")
        seg = self.S[i:i + 1800]
        self.assertIn("需要填数字", seg)
        self.assertIn("请先修正", seg)

    def test_frontend_shows_backend_errors(self):
        self.assertIn("部分设置未保存", self.S)


class SmallScreenAndUiTest(unittest.TestCase):
    """⑧⑨ 小屏顶端不能显示不全；字体/图标要统一。"""

    @classmethod
    def setUpClass(cls):
        cls.C = CSS.read_text(encoding="utf-8")
        cls.I = INDEX.read_text(encoding="utf-8")

    def test_toolbar_wraps(self):
        """★ 宁可占两行，也不要把按钮藏到看不见的地方。"""
        self.assertIn("flex-wrap:wrap", self.C)
        i = self.C.index("小屏适配")
        seg = self.C[i:]
        self.assertIn(".toolbar", seg)

    def test_multiple_breakpoints(self):
        i = self.C.index("小屏适配")
        seg = self.C[i:]
        for w in ("1180", "980", "780", "560"):
            self.assertIn(w, seg, f"缺少 {w}px 断点")

    def test_secondary_controls_hidden_on_narrow(self):
        i = self.C.index("小屏适配")
        seg = self.C[i:]
        self.assertIn("display:none", seg)

    def test_no_relative_em_font_sizing(self):
        """★ 别用 `max(--fs-cap, 0.8em)` 之类**相对父元素**的字号：
        嵌套几层后算出来的值飘忽不定 —— 那不叫统一，只是看着像。

        只看真正的 CSS 声明，注释里提到这个写法（说明"别这么写"）不算。
        """
        i = self.C.index("细节打磨")
        seg = self.C[i:]
        # 去掉注释再检查
        code = re.sub(r"/\*.*?\*/", "", seg, flags=re.S)
        decls = re.findall(r"font-size:\s*([^;}]+)", code)
        bad = [d.strip() for d in decls if "em" in d and "rem" not in d]
        self.assertEqual([], bad, f"这些字号用了相对父元素的 em: {bad}")

    def test_font_scale_tokens_used(self):
        i = self.C.index("细节打磨")
        seg = self.C[i:]
        self.assertIn("var(--fs)", seg)
        self.assertIn("var(--fs-cap)", seg)

    def test_icons_follow_text_color(self):
        """图标统一描边、统一走 currentColor，避免"有的粗有的细"。"""
        i = self.C.index("细节打磨")
        seg = self.C[i:]
        self.assertIn("stroke-width", seg)

    def test_all_used_icons_defined(self):
        """★ 用到但没定义的图标会渲染成空白 —— 之前 `U.icon('upload')` 就是这样。"""
        import re
        defs = set(re.findall(r'<symbol id="(i-[\w-]+)"', self.I))
        body = ""
        for p in JS.glob("*.js"):
            body += p.read_text(encoding="utf-8")
        used = {f"i-{u}" for u in re.findall(r"U\.icon\('([\w-]+)'", body)}
        missing = sorted(used - defs)
        self.assertEqual([], missing, f"这些图标没定义: {missing}")

    def test_player_controls_wrap(self):
        i = self.C.index("小屏适配")
        self.assertIn("player-ctl", self.C[i:])


class FileOpTimeoutTest(unittest.TestCase):
    """⑩ 所有跟文件有关的操作都要有容错 + 超时。"""

    @classmethod
    def setUpClass(cls):
        cls.O = py("app/core/ops.py")
        cls.ST = py("app/station_api.py")
        cls.FA = py("app/sources/folders_api.py")

    def test_shared_timeout_helper(self):
        self.assertIn("def call_with_timeout", self.O)

    def test_station_moves_guarded(self):
        """★ 移动/复制会因坏扇区、网络盘掉线而永久挂住。"""
        self.assertIn("def _moved", self.ST)
        self.assertIn("def _copied", self.ST)
        # 业务路径里不该再有裸的 shutil.move / copy2（只允许出现在包装函数内部）
        # 包装函数本身用 shutil 是对的，所以按"调用点"检查
        calls = [m for m in re.findall(r"(?<!def )_moved\(|_copied\(", self.ST)]
        self.assertTrue(calls, "应该有地方用 _moved/_copied")
        raw = re.findall(r"^\s+(shutil\.(?:move|copy2)\()", self.ST, re.M)
        self.assertEqual([], raw, f"业务路径里还有裸调用: {raw}")

    def test_folder_ops_guarded(self):
        self.assertIn("def _guarded", self.FA)
        for what in ("新建目录", "重命名", "删除目录"):
            self.assertIn(what, self.FA)

    def test_timeout_error_is_actionable(self):
        i = self.O.index("def call_with_timeout")
        seg = fn_body(self.O, "call_with_timeout")
        self.assertIn("秒没有返回", seg)

    def test_executor_step_guarded(self):
        self.assertIn("def _apply_guarded", self.O)

    def test_folder_timeout_maps_to_504(self):
        self.assertIn("504", self.FA)

    def test_upload_commit_checks_capacity(self):
        u = py("app/upload_api.py")
        i = u.index("def upload_commit")
        seg = fn_body(u, "upload_commit")
        self.assertIn("check_commit_capacity", seg)


if __name__ == "__main__":
    unittest.main()
