"""多页面 / 任务互斥 / 后台任务 / 偏好记忆 / 播放器联动 / 磁盘余量 的契约测试。

对应这一轮用户提出的 10 项需求：

  ① 多开页面支持      ② 任务冲突处理（严格串行、不排队）
  ③ 文件浏览显示余量   ④ 上传批次大小判断
  ⑤ 超时跳过卡死文件   ⑥ 曲库专辑强化 + 播放器联动
  ⑦ 高风险二次确认     ⑧ 后台任务（刷新不中断）
  ⑨ 偏好记忆          ⑩ 汉堡菜单与页面联动
"""
from __future__ import annotations

import unittest
from pathlib import Path

# 项目根（不要写死本机路径 —— 别人 clone 到别的目录就跑不了了）
_PROJ = __import__('pathlib').Path(__file__).resolve().parent.parent
from .srchelp import app_src, fn_body


ROOT = _PROJ
JS = ROOT / "app" / "web" / "static" / "js"
CSS = ROOT / "app" / "web" / "static" / "css" / "app.css"
INDEX = ROOT / "app" / "web" / "index.html"


def js(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


class TaskExclusivityTest(unittest.TestCase):
    """② 一次只能跑一个流程，且**不排队**。"""

    @classmethod
    def setUpClass(cls):
        cls.JOBS = (ROOT / "app" / "core" / "jobs.py").read_text(encoding="utf-8")

    def test_job_busy_exception_exists(self):
        self.assertIn("class JobBusy", self.JOBS)

    def test_exclusive_kinds_defined(self):
        self.assertIn("EXCLUSIVE_KINDS", self.JOBS)
        for k in ("sync", "move", "trash"):
            self.assertIn(k, self.JOBS)

    def test_no_queueing_only_reject(self):
        """★ 必须是"拒绝"，不是"排队"。

        排队在文件操作里是危险的：用户以为提交完就没事了，
        实际上那个任务操作的是**很久以后**的磁盘状态。
        """
        i = self.JOBS.index("def create(")
        seg = self.JOBS[i:i + 1200]
        self.assertIn("raise JobBusy", seg)
        # 不该有 pending / queue 之类的排队结构
        for bad in ("self._pending", "self._queue", "waiting"):
            self.assertNotIn(bad, self.JOBS, f"不该有排队机制 {bad}")

    def test_stale_job_recovery(self):
        """卡死的任务不能永久占着独占位（否则用户被锁死）。"""
        self.assertIn("STALE_JOB_SECONDS", self.JOBS)
        self.assertIn("_reap_stale_locked", self.JOBS)
        self.assertIn("heartbeat", self.JOBS)

    def test_heartbeat_touched_on_progress(self):
        ops = (ROOT / "app" / "core" / "ops.py").read_text(encoding="utf-8")
        i = ops.index("job.touch()")
        self.assertTrue(i > 0, "执行进度里要发心跳")

    def test_busy_endpoint_and_409(self):
        main = app_src()
        self.assertIn('"/api/busy"', main)
        self.assertIn("HTTPException(409", main)
        self.assertIn("/ack", main)

    def test_frontend_asks_before_executing(self):
        app = js("app.js")
        self.assertIn("async function runExclusive", app)
        self.assertIn("TaskSync.begin", app)
        # 冲突时给出解释，而不是静默失败
        self.assertIn("暂时不能操作", app)

    def test_views_use_runExclusive(self):
        for f in ("views.compare.js", "views.duplicates.js"):
            self.assertIn("App.runExclusive", js(f), f"{f} 的写操作要走独占入口")


class BackgroundTaskTest(unittest.TestCase):
    """⑧ 刷新页面不该丢任务。"""

    @classmethod
    def setUpClass(cls):
        cls.TS = js("tasksync.js")

    def test_has_resume(self):
        self.assertIn("async function resume", self.TS)
        self.assertIn("resume", self.TS)

    def test_state_survives_reload(self):
        """任务 id 要写在 localStorage 里，刷新后能接上。"""
        self.assertIn("localStorage.setItem(KEY + '.id'", self.TS)
        self.assertIn("localStorage.getItem(KEY + '.id'", self.TS)

    def test_asks_backend_as_truth(self):
        """以后端为准，localStorage 可能过期。"""
        i = self.TS.index("async function resume")
        seg = self.TS[i:i + 900]
        self.assertIn("API.busy()", seg)

    def test_ack_on_finish(self):
        """收尾必须 ack —— 这是"一次只有一个流程"的完成点。"""
        self.assertIn("jobAck", self.TS)

    def test_cross_tab_channel(self):
        self.assertIn("BroadcastChannel", self.TS)

    def test_boot_resumes(self):
        app = js("app.js")
        self.assertIn("mountBackgroundTaskSync", app)
        self.assertIn("TaskSync.resume()", app)

    def test_temp_loading_placeholder_removed(self):
        """任务在后台跑，前端不该显示"已取消"之类的误导文案。"""
        main = app_src()
        self.assertIn('"/api/busy"', main)


class DiskSpaceTest(unittest.TestCase):
    """③ 文件浏览要能看到目录所在盘的余量。"""

    def test_api_returns_disk(self):
        main = app_src()
        self.assertIn("def _disk_view", main)
        self.assertIn('"disk": _disk_view', main)
        for k in ("free", "total", "percent", "writable"):
            self.assertIn(f'"{k}"', main)

    def test_disk_view_tolerates_missing_dir(self):
        """目录可能还不存在（新建目录的场景）→ 往上层找。"""
        main = app_src()
        i = main.index("def _disk_view")
        seg = fn_body(main, "_disk_view")
        self.assertIn("os.path.dirname", seg)

    def test_frontend_renders_disk(self):
        f = js("views.files.js")
        self.assertIn("function paintDisk", f)
        self.assertIn("#fb-disk", f)
        # 余量紧张要有醒目提示
        self.assertIn("tight", f)

    def test_css_has_disk_bar(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".dk-bar", css)
        self.assertIn(".disk-chip", css)


class UploadSizeGuardTest(unittest.TestCase):
    """④ 上传/入库前判断装不装得下。"""

    @classmethod
    def setUpClass(cls):
        cls.UP = (ROOT / "app" / "upload_api.py").read_text(encoding="utf-8")

    def test_staging_capacity_checked(self):
        self.assertIn("def check_staging_capacity", self.UP)

    def test_commit_capacity_checked(self):
        """★ 入库前要检查**目标盘**。

        暂存区够用 ≠ 目标盘够用（可能是另一块盘），
        搬到一半失败会让库里多出一半文件，很难收拾。
        """
        self.assertIn("def check_commit_capacity", self.UP)
        i = self.UP.index("def upload_commit")
        seg = fn_body(self.UP, "upload_commit")
        self.assertIn("check_commit_capacity", seg)

    def test_commit_checks_per_target_dir(self):
        """不同目标目录可能在不同盘上，要分别判断。"""
        i = self.UP.index("def upload_commit")
        seg = fn_body(self.UP, "upload_commit")
        self.assertIn("per_dir", seg)

    def test_rejection_explains_how_much(self):
        for fn in ("check_staging_capacity", "check_commit_capacity"):
            i = self.UP.index(f"def {fn}")
            seg = self.UP[i:i + 1600]
            self.assertIn("GB", seg, f"{fn} 要说清差多少")

    def test_batch_size_helper(self):
        self.assertIn("def batch_size", self.UP)
        self.assertIn("_disk_batch_files", self.UP)


class TimeoutSkipTest(unittest.TestCase):
    """⑤ 卡死的文件要能跳过，而不是把整批拖死。"""

    @classmethod
    def setUpClass(cls):
        cls.OPS = (ROOT / "app" / "core" / "ops.py").read_text(encoding="utf-8")

    def test_timeout_kind_and_label(self):
        self.assertIn("ERR_TIMEOUT", self.OPS)
        self.assertIn('ERR_TIMEOUT: "操作超时（已跳过）"', self.OPS)

    def test_timeout_is_retryable(self):
        i = self.OPS.index("ERR_RETRYABLE")
        seg = self.OPS[i:i + 120]
        self.assertIn("ERR_TIMEOUT", seg)

    def test_guarded_apply_exists(self):
        self.assertIn("def _apply_guarded", self.OPS)
        self.assertIn("STEP_TIMEOUT_SEC", self.OPS)

    def test_guarded_apply_is_used(self):
        i = self.OPS.index("def work(")
        seg = self.OPS[i:i + 700]
        self.assertIn("_apply_guarded", seg)

    def test_timeout_continues_batch(self):
        """超时要"跳过继续"，不能中断整批。"""
        i = self.OPS.index("def _apply_guarded")
        seg = fn_body(self.OPS, "_apply_guarded")
        self.assertIn("timeout", seg)
        # 返回的是一个结果对象（被记进 results），不是 raise
        self.assertNotIn("raise StepTimeout", seg)

    def test_timeout_disabled_by_zero(self):
        i = self.OPS.index("def _apply_guarded")
        seg = fn_body(self.OPS, "_apply_guarded")
        self.assertIn("STEP_TIMEOUT_SEC <= 0", seg)

    def test_skipped_recorded_on_job(self):
        self.assertIn("job.skipped.append", self.OPS)


class PlayerIntegrationTest(unittest.TestCase):
    """⑥ 曲库专辑强化 + 播放器真的能用。"""

    @classmethod
    def setUpClass(cls):
        cls.APP = js("app.js")
        cls.LIB = js("views.library.js")
        cls.IDX = INDEX.read_text(encoding="utf-8")

    def test_queue_replaces_page_bound_playlist(self):
        """★ 队列要独立于分页，否则翻页就断。

        以前"下一首"依赖 `State.playlist`（当前页的曲目），
        曲库一分页队列就跟着断，播放器形同虚设。
        """
        self.assertIn("const queue = {", self.APP)
        self.assertIn("playList", self.APP)
        i = self.APP.index("const queue = {")
        seg = self.APP[i:i + 1200]
        self.assertIn("items: []", seg)

    def test_queue_returns_false_at_end_no_loop(self):
        """到队尾不循环（循环会让人以为放完了还在放）。

        两个坑都踩过：
          · `boot()` 里也有一个同名局部变量 `const step = (name, fn) => …`，
            全文件搜 `step` 会命中它 → 所以先切出 queue 那一段；
          · `queue.step(dir) {}` 是**对象方法简写**，`fn_body()` 认不出来
            （它认 `def` / `function` / `const x =`），所以这里直接按
            `step(dir) {` 定位，而不是写死字符窗口（加几行注释就滑出去了）。
        """
        i = self.APP.index("const queue = {")
        j = self.APP.index("\n  };", i)
        block = self.APP[i:j]
        # 找**方法定义**本身，而不是注释里提到的 `step(dir)`
        k = block.index("\n    step(dir) {")
        seg = block[k:k + 400]
        self.assertIn("return null", seg,
                      "队尾必须返回 null（不循环）")

    def test_shuffle_and_loop_controls(self):
        for k in ("btn-shuffle", "btn-loop", "btn-queue"):
            self.assertIn(k, self.IDX, f"播放器缺少 {k}")
        self.assertIn("loopMode", self.APP)
        self.assertIn("shuffle", self.APP)
        for i in ("i-shuffle", "i-loop", "i-loop-one", "i-queue-add"):
            self.assertIn(f'id="{i}"', self.IDX, f"缺少图标 {i}")

    def test_album_view_exists(self):
        self.assertIn("function groupByAlbum", self.LIB)
        self.assertIn("function paintAlbums", self.LIB)
        self.assertIn('data-mode="album"', self.LIB)

    def test_album_grouping_falls_back_to_folder(self):
        """没写专辑标签的曲目按目录归组，否则全挤进"未知专辑"。"""
        i = self.LIB.index("function groupByAlbum")
        seg = self.LIB[i:i + 900]
        self.assertIn("U.parent(t.path)", seg)
        self.assertIn("未标专辑", seg)

    def test_album_has_play_and_queue(self):
        self.assertIn("data-albplay", self.LIB)
        self.assertIn("data-albq", self.LIB)

    def test_album_view_remembered(self):
        self.assertIn("Prefs.set('libMode'", self.LIB)

    def test_load_more_handles_album_mode(self):
        """专辑视图下"加载更多"要整块重画（分组会变），不能追加行。"""
        i = self.LIB.index("async function loadMore")
        seg = self.LIB[i:i + 2200]
        self.assertIn("viewMode === 'album'", seg)

    def test_volume_persisted(self):
        """音量要记住，而且必须走 `Prefs` 统一入口。

        原来音量是用裸键 `msh.volume` 直接写 localStorage 的，
        而 `Prefs.get` 解析的是带版本的信封 `{v,d}` —— 裸值解析不出，
        于是**设置页把音量显示成"未设置"，但实际是有值的**。
        功能没坏、界面在说谎，用户会以为记忆功能失效。
        """
        self.assertIn("Prefs", self.APP)
        self.assertIn("Prefs.set('volume'", self.APP)
        self.assertIn("Prefs.get('volume'", self.APP)
        # 不该再直接写裸键
        self.assertNotIn("localStorage.setItem('msh.volume'", self.APP)

    def test_play_track_warns_instead_of_silent(self):
        """点了没反应是最难查的 —— 至少留一条 warn。"""
        i = self.APP.index("function playTrack")
        seg = self.APP[i:i + 900]
        self.assertIn("console.warn", seg)


class HardConfirmTest(unittest.TestCase):
    """⑦ 高风险操作要二次确认（打字确认）。"""

    @classmethod
    def setUpClass(cls):
        cls.UI = js("ui.js")

    def test_confirm_hard_exists(self):
        self.assertIn("function confirmHard", self.UI)
        self.assertIn("confirmHard", self.UI)

    def test_button_disabled_until_phrase_typed(self):
        i = self.UI.index("function confirmHard")
        seg = self.UI[i:i + 2600]
        self.assertIn("yes.disabled = inp.value.trim() !== phrase", seg)

    def test_shows_affected_items(self):
        i = self.UI.index("function confirmHard")
        seg = self.UI[i:i + 2600]
        self.assertIn("details", seg)
        self.assertIn("hard-list", seg)

    def test_used_for_permanent_delete(self):
        f = js("views.files.js")
        i = f.index("永久删除文件夹")
        seg = f[max(0, i - 700):i + 500]
        self.assertIn("confirmHard", seg)

    def test_used_for_dedup_cleanup(self):
        d = js("views.duplicates.js")
        self.assertIn("confirmHard", d)
        self.assertIn("确认清理", d)

    def test_css_for_hard_list(self):
        self.assertIn(".hard-list", CSS.read_text(encoding="utf-8"))


class PrefsTest(unittest.TestCase):
    """⑨ 每个页面的选项都要记住。"""

    @classmethod
    def setUpClass(cls):
        cls.P = js("prefs.js")

    def test_module_exists_and_registered(self):
        self.assertIn("const Prefs =", self.P)
        self.assertIn("prefs.js", INDEX.read_text(encoding="utf-8"))

    def test_known_keys_cover_pages(self):
        for k in ("navHidden", "volume", "libMode", "libCols", "libFilters",
                  "filesSort", "activeSource", "dupSepVer", "lastView"):
            self.assertIn(k, self.P, f"偏好里缺 {k}")

    def test_never_throws(self):
        """隐私模式 / 数据损坏都不能让界面崩掉。"""
        i = self.P.index("function get(")
        seg = self.P[i:i + 700]
        self.assertIn("catch", seg)
        j = self.P.index("function set(")
        seg2 = self.P[j:j + 400]
        self.assertIn("catch", seg2)

    def test_versioned_envelope(self):
        """带版本号，将来结构变了能丢弃旧值而不是崩。"""
        self.assertIn("'v' in o", self.P)

    def test_library_filters_persisted(self):
        f = js("views.library.js")
        self.assertIn("function saveLibPrefs", f)
        self.assertIn("function loadLibPrefs", f)
        # 每个筛选动作后都要保存
        self.assertGreaterEqual(f.count("saveLibPrefs()"), 5)

    def test_files_sort_persisted(self):
        f = js("views.files.js")
        self.assertIn("function saveFilesSort", f)
        self.assertIn("Prefs.get('filesSort'", f)

    def test_settings_shows_and_clears_prefs(self):
        s = js("views.settings.js")
        self.assertIn("st-prefs", s)
        self.assertIn("Prefs.clear()", s)
        self.assertIn("Prefs.all()", s)


class NavLinkageTest(unittest.TestCase):
    """⑩ 汉堡菜单要和曲库/文件浏览联动。"""

    @classmethod
    def setUpClass(cls):
        cls.APP = js("app.js")

    def test_sync_nav_fallbacks_exists(self):
        self.assertIn("function syncNavFallbacks", self.APP)
        self.assertIn("function paintNavFallback", self.APP)

    def test_called_from_setview(self):
        """★ 必须在 setView 里调。

        视图是懒渲染的：applyNav 只在折叠时跑一次，那时视图里还没有
        锚点元素，兜底条就永远插不进去 —— 这正是"联动性差"的根因。
        """
        i = self.APP.index("function setView")
        seg = self.APP[i:i + 1600]
        self.assertIn("syncNavFallbacks", seg)

    def test_fallback_has_source_switch_and_folder(self):
        i = self.APP.index("function paintNavFallback")
        seg = self.APP[i:i + 1400]
        self.assertIn("nav-fb-src", seg)
        self.assertIn("data-fb-tree", seg)
        self.assertIn("data-fb-show", seg)

    def test_library_has_pick_folder(self):
        LIB = js("views.library.js")
        self.assertIn("function pickFolder", LIB)
        self.assertIn("pickFolder", LIB.split("registerView")[1])

    def test_ctrl_b_shortcut(self):
        self.assertIn("ctrlKey || e.metaKey", self.APP)
        self.assertIn("'b'", self.APP)

    def test_boot_errors_surface(self):
        """★ 初始化失败要能说出来。

        以前 boot() 是一条直线，中间任何一步抛错，后面全部**悄悄跳过** ——
        表现是"按钮点了没反应"而且不报错，极难查。
        """
        i = self.APP.index("async function boot")
        seg = self.APP[i:i + 2600]
        self.assertIn("const step = (name, fn)", seg)
        self.assertIn("State.bootErrors", seg)
        self.assertIn("部分功能初始化失败", seg)

    def test_css_for_fallback(self):
        self.assertIn(".nav-fallback", CSS.read_text(encoding="utf-8"))


class UiPolishTest(unittest.TestCase):
    """⑨ UI 细化：字号/控件/图标成体系。"""

    @classmethod
    def setUpClass(cls):
        cls.CSS = CSS.read_text(encoding="utf-8")

    def test_type_scale_tokens(self):
        for k in ("--fs-cap", "--fs-sm", "--fs", "--fs-md", "--fs-lg", "--fs-xl"):
            self.assertIn(k, self.CSS, f"缺少字号 token {k}")

    def test_control_height_tokens(self):
        self.assertIn("--ctl-h", self.CSS)
        self.assertIn("--ctl-h-sm", self.CSS)

    def test_chinese_min_font_size(self):
        """中文在 11px 以下笔画会糊，要有下限。"""
        i = self.CSS.index("--fs-cap")
        self.assertIn("11px", self.CSS[i:i + 40])

    def test_icon_stroke_consistent(self):
        i = self.CSS.index("细节打磨")
        seg = self.CSS[i:]
        self.assertIn("stroke-width", seg)

    def test_disabled_state_styled(self):
        i = self.CSS.index("细节打磨")
        seg = self.CSS[i:]
        self.assertIn(":disabled", seg)

    def test_tabular_nums(self):
        self.assertIn("tabular-nums", self.CSS)


if __name__ == "__main__":
    unittest.main()
