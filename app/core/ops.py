"""执行器：把"计划步骤"安全地落到磁盘/远端。

安全设计：
1. **默认 dry-run**：不传 `execute=True` 只会返回将要做什么；
2. **写入先落临时文件再改名**（`.mshpart` → 正式名），中断不会留半截文件；
3. **删除默认进回收站**：`<源根>/.music-sync-trash/YYYYMMDD/`，只有显式 `hard=True` 才真删；
4. **只读源守护**：数据源勾了「只读」或本身是 HTTP，任何写操作直接拒绝；
5. **可选校验**：复制完成后比对大小，必要时比对抽样哈希；
6. **冲突策略**：skip（默认，跳过已存在）/ overwrite / rename（自动加 (2)）；
7. **移动只限同源**：`move` 要求 src == dst 且两端都在源根目录内，只读源直接拒绝——
   跨源搬运请用 copy（复制成功后再单独删源），避免"删了源却没搬过去"；
8. 单步失败不影响其它步骤，结果逐条记录，方便回滚核对。
"""
from __future__ import annotations

import logging

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import matching as M
from . import paths as P
from . import scanner as S
from .jobs import JobBusy, JobState, get_jobs
from ..sources.base import MusicSource, ReadOnlyError, SourceError


# ---------------------------------------------------------------- 错误分类
# 执行过程中最常见的几类异常。分类的意义是**告诉用户该怎么办**：
# "文件被占用"值得过一会儿重试；"路径不存在"重试也没用，得先扫描；
# "空间不足"要先清理。一锅端地报"失败"帮不上忙。
ERR_MISSING = "missing"        # 文件/目录突然不见了
ERR_LOCKED = "locked"          # 被占用 / 权限不足
ERR_NOSPACE = "nospace"        # 磁盘满
ERR_CONFLICT = "conflict"      # 目标已存在（同名冲突且策略是跳过）
ERR_READONLY = "readonly"      # 只读数据源
ERR_TIMEOUT = "timeout"        # 单步卡住超时（坏盘、网络盘掉线、文件锁死）
ERR_OTHER = "other"

ERR_LABEL = {
    ERR_MISSING: "文件不存在",
    ERR_LOCKED: "文件被占用或权限不足",
    ERR_NOSPACE: "磁盘空间不足",
    ERR_CONFLICT: "目标已存在",
    ERR_READONLY: "目标只读",
    ERR_TIMEOUT: "操作超时（已跳过）",
    ERR_OTHER: "其他错误",
}
# 哪些类别重试有意义（"重试"按钮据此给出建议）
ERR_RETRYABLE = {ERR_LOCKED, ERR_NOSPACE, ERR_TIMEOUT, ERR_OTHER}

# 单个文件的默认超时（秒）。0 = 不限制。
# 为什么需要：有些文件会莫名其妙卡住（坏扇区、网络盘掉线、被独占锁住），
# 既不报错也不返回 —— 整批任务就此停住，用户只看到进度条不动。
# 与其等它自己好，不如**跳过它、继续处理其他文件**，最后汇报哪些被跳过。
STEP_TIMEOUT_SEC = float(os.environ.get("MSH_STEP_TIMEOUT_SEC") or 120)


log = logging.getLogger("msh.ops")

class StepTimeout(RuntimeError):
    """单步超时。当作一种可跳过的错误，而不是让整批失败。"""


def call_with_timeout(fn: Callable[[], Any], seconds: Optional[float] = None,
                      what: str = "操作") -> Any:
    """在**带超时的子线程**里跑一次文件操作。

    为什么到处都要这个：文件相关的系统调用会因为各种原因**永久挂住** ——
    坏扇区、网络盘掉线、被别的程序独占锁住、U 盘拔了。
    挂住之后既不返回也不报错，整个请求就停在那儿，界面看着像卡死。

    做法和工作线程池里的超时一致：超时后**放弃等待**并把那一步当作失败
    返回。Python 没法真正杀掉卡在系统调用里的线程，所以：
      · 已经写了一半的临时文件由 `.mshpart` + 原子改名保证不会被当成成品；
      · 移动/删除这类操作本身是原子的（rename）或幂等的，重试是安全的。
    """
    limit = STEP_TIMEOUT_SEC if seconds is None else seconds
    if limit is None or limit <= 0:
        return fn()
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["r"] = fn()
        except Exception as exc:            # noqa: BLE001 —— 原样带回主线程
            box["e"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        raise StepTimeout(
            f"{what}超过 {int(limit)} 秒没有返回，已放弃"
            f"（文件可能损坏、被独占，或所在磁盘/网络异常）")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def classify_error(exc: BaseException, detail: str = "") -> str:
    """把异常归类，用于"哪些能重试"的判断。

    先看异常类型（Windows 的 PermissionError 常常同时意味着"被占用"），
    再看文本关键字兜底 —— 因为底层可能把 Errno 包成别的异常。
    """
    if isinstance(exc, StepTimeout):
        return ERR_TIMEOUT
    name = type(exc).__name__.lower()
    text = f"{exc} {detail}".lower()

    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return ERR_MISSING
    if isinstance(exc, IsADirectoryError):
        return ERR_CONFLICT
    if isinstance(exc, PermissionError):
        # Windows 上最常见的其实是"文件正被别的程序打开"
        return ERR_LOCKED
    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None)
        if errno in (2, 3):
            return ERR_MISSING
        if errno in (13, 32, 33):
            return ERR_LOCKED
        if errno == 28:
            return ERR_NOSPACE
        # ETIMEDOUT / 网络盘掉线
        if errno in (60, 110, 116):
            return ERR_TIMEOUT

    if any(k in text for k in ("超时", "timed out", "timeout", "errno 60",
                               "errno 110", "errno 116")):
        return ERR_TIMEOUT
    if any(k in text for k in ("no space", "disk full", "空间不足",
                               "errno 28", "enospc")):
        return ERR_NOSPACE
    if any(k in text for k in ("being used by another process", "另一个程序",
                               "正由另一进程使用", "permission denied",
                               "access is denied", "拒绝访问", "errno 13",
                               "errno 32", "winerror 32", "winerror 5",
                               "read-only", "只读")):
        # 只读数据源单独分一类，因为"重试"解决不了，得换目标
        if "只读" in text or "read-only" in text:
            return ERR_READONLY
        return ERR_LOCKED
    if any(k in text for k in ("does not exist", "no such file", "not found",
                               "找不到", "不存在", "errno 2")):
        return ERR_MISSING
    if any(k in text for k in ("already exists", "已存在", "冲突", "conflict")):
        return ERR_CONFLICT
    if "filenotfound" in name or "notfound" in name:
        return ERR_MISSING
    if "permission" in name:
        return ERR_LOCKED
    return ERR_OTHER


class Executor:
    def __init__(self, sources: Dict[str, MusicSource], db=None, default_policy: str = "skip"):
        self.sources = sources
        self.db = db
        self.policy = default_policy
        # 本方案内"已经安排把谁放到哪里"的占位表。
        # 见 `_reserve_targets`：不加这个的话，同一方案里两条 move 指向
        # 同一目标会**互相覆盖，而且两个都报成功**。
        self._planned: Dict[tuple, Dict[str, Any]] = {}
        self._planned_lock = threading.RLock()
        self._extra_summary: Dict[str, int] = {}
        # 索引同步失败记录。见 `_sync_index`：磁盘动了但索引没跟上，
        # 必须让用户知道（否则"移动成功"之后库里会对不上）。
        self._index_errors: List[Dict[str, Any]] = []
        # 当前正在跑的任务。存下来是为了让单步内部（比如一次大文件的
        # 复制/搬运）也能打心跳 —— 见 `_do_transfer`。
        self._job: Optional[JobState] = None

    # ------------------------------------------------------- 方案内目标占位
    def _reserve_targets(self, steps: Sequence[Dict[str, Any]]) -> int:
        """把方案里所有"会写入的目标路径"先占下来。

        为什么必须**先占**、而不是执行到那一步再看：
        `skip` 的判断是"执行到这一步时目标在不在"。同一方案里前一步刚把
        文件放到那里，第二步看到"已存在"就直接跳过或（overwrite 时）覆盖 ——
        两个都报 `done`，实际只剩一个文件。多线程下更糟：两步的检查
        可能都发生在对方写入之前。

        用户看不到任何警告，这是**静默丢文件**，必须挡掉。

        做法：按 (源, 目标) 记下第一个占用者。后来的同目标步骤在执行时
        会被改写成不冲突的新名字（`rename` 语义），并在汇总里报告
        `dst_conflicts`，让界面上能看见"有 N 个目标撞车、已自动改名"。
        """
        self._planned.clear()
        self._extra_summary = {}
        self._index_errors = []
        conflicts = 0
        with self._planned_lock:
            for st in steps:
                # ★ `transfer` 必须在内。它是"复制 → 校验 → 删源"，
                #   同样会往 `dst_path` 写文件。漏掉它的后果和当初
                #   漏掉 `copy` 一样：同一方案里两条 transfer 指向同一目标时
                #   会互相覆盖，而且**两个都报成功**（静默丢文件）。
                # ★ 白名单必须和 `_apply()` 实际支持的操作**严格一致**。
                #   这里原来多一个 `extract` —— 全项目没有任何地方生成它，
                #   `_apply()` 也没有对应分支。多一个白名单项的实际害处：
                #   它会让目标占位生效，而真正执行时掉进"未知操作"。
                # `link` 在底层被当作 copy 执行（会写目标），必须一起占位。
                if st.get("op") not in ("move", "copy", "rename", "transfer", "link"):
                    continue
                dst_path = str(st.get("dst_path") or "")
                if not dst_path:
                    continue
                # ★ 键统一用"目标源内相对路径"。
                #   调用方给的 `dst_path` 可能是相对的（`d/x.mp3`），
                #   也可能是绝对的（`D:/Music/out/x.mp3`）—— 两种写法都有人用
                #   （对比计划用相对、文件视图的移动用绝对）。
                #   不归一化就会出现"同样是 out/x.mp3，一个被当成两个目标"
                #   或者反过来"查不中、改名逻辑失效"。
                sid_key = st.get("dst") or st.get("src") or ""
                _src = self.sources.get(sid_key)
                try:
                    if _src is not None:
                        dst_path = _src.rel_path(dst_path)
                except Exception:
                    pass
                key = (sid_key, P.norm(dst_path).lower())
                if key in self._planned:
                    st["_dst_conflict"] = self._planned[key].get("id")
                    conflicts += 1
                else:
                    self._planned[key] = st
        if conflicts:
            self._extra_summary["dst_conflicts"] = conflicts
        return conflicts

    def _claim_target(self, st: Dict[str, Any], want: str,
                      policy: str = "skip") -> str:
        """执行前给这一步"认领"最终目标名。

        如果这个目标已经被**本方案里更早的步骤**占了，就改名
        （`a.mp3` → `a (2).mp3`），绝不复用别人已经写下去的名字。

        ★ 键的"坐标系"必须和 `_reserve_targets` 一致 —— 那边记的是
        **目标源内的相对路径**（`d/x.mp3`），而调用方传进来的是
        **绝对路径**（`C:/.../B/d/x.mp3`）。两边对不上就永远查不中，
        改名逻辑形同虚设。

        这个不一致造成的后果很隐蔽（也真实存在过）：

            同一方案里两条 copy 指向同一目标
            → `dst_conflicts: 1` 报出来了（看起来"已处理"）
            → 但实际只落了一个文件，另一条是 `conflict`
            → 用户看到的是"有 1 个目标撞车、已自动改名"，
              **而文件少了一个**

        历史上 `_do_copy` 一直带着这个 bug；`_do_transfer` 是新加的、
        同样中招。所以修在这里（统一键空间），两个调用点一起受益。
        """
        # policy="rename" 时，即使方案里没标 `_dst_conflict`，也要进锁确认
        # 目标名可用 —— 磁盘上的文件可能是在"生成方案"之后才出现的。
        if not st.get("_dst_conflict") and policy != "rename":
            return want
        # 统一到"目标源内相对路径"这个坐标系
        sid = st.get("dst") or st.get("src") or ""
        key_rel = want
        src = self.sources.get(sid)
        if src is not None:
            try:
                key_rel = src.rel_path(want)
            except Exception:
                key_rel = want
        key = (sid, P.norm(key_rel).lower())

        # ★★ 「查重 → 选名 → 登记」必须在**同一把锁**里完成。★★
        #
        #   原来分三次拿锁：先查 _planned（释放）→ 调 _dedupe_name 只问磁盘
        #   （释放）→ 再把新名字登记进去。于是同一方案里 ≥3 步写同一目标时：
        #
        #       线程 A 看到 x.mp3 被占 → 算候选，磁盘上 x (2).mp3 还不存在 → 选它
        #       线程 B 同时也看到 → 也算出 x (2).mp3
        #       两个都登记 x (2).mp3，都写同一路径 → **只落一个文件，三条都报 done**
        #
        #   实测：40 轮里丢 39 轮。这是静默丢文件，比报错严重得多，
        #   因为它看起来"全部成功"。
        #
        #   现在锁住整段：候选名必须同时满足
        #     · 磁盘上不存在（否则会覆盖别人）
        #     · `_planned` 里没人占（否则同方案的两步会撞）
        #   注：锁内做 `src.exists()` 是磁盘 I/O，但只有**发生冲突时**才走这条路，
        #   而且冲突本来就是少数情况。正确性优先。
        with self._planned_lock:
            owner = self._planned.get(key)
            disk_taken = False
            if src is not None:
                try:
                    disk_taken = src.exists(key_rel)
                except Exception:                              # noqa: BLE001
                    disk_taken = False
            # ★ "rename 且磁盘上已有"时，**不能**走"本步已认领过"的短路。
            #   方案预占（`_reserve_targets`）会先把 `want` 记在本步名下，
            #   而磁盘上的同名文件是**别人的**；短路返回 `want` 就等于放弃改名，
            #   最后 `_do_move` 撞名报错、`_do_copy` 直接覆盖。
            if owner is None and not (policy == "rename" and disk_taken):
                self._planned[key] = st
                return want
            if (owner is not None and owner.get("id") == st.get("id")
                    and not (policy == "rename" and disk_taken)):
                return want
            if src is None:
                return want
            for cand in _dedupe_candidates(want):
                try:
                    cand_rel = src.rel_path(cand)
                except Exception:
                    cand_rel = cand
                ck = (sid, P.norm(cand_rel).lower())
                if ck in self._planned and self._planned[ck].get("id") != st.get("id"):
                    continue
                if src.exists(cand_rel):
                    continue
                self._planned[ck] = st
                return cand
            # 候选都排满了（同名 >198 个）：退回时间戳名，仍然登记
            fallback = f"{P.stem(want)} ({int(time.time() * 1000)}){P.ext(want)}"
            try:
                fb_rel = src.rel_path(fallback)
            except Exception:
                fb_rel = fallback
            self._planned[(sid, P.norm(fb_rel).lower())] = st
            return fallback

    # ------------------------------------------------------------ 设备感知
    def device_notes(self, steps: Sequence[Dict[str, Any]]) -> List[str]:
        """这批步骤涉及的数据源里，有哪些"存储设备"层面的注意事项。

        踩过的教训：识别出 U 盘 / 网络位置之后，**扫描**会用上（压并发、
        跳过全量哈希），但**对比/同步/整理**这些走 Executor 的路径完全不知道
        —— 于是往 U 盘上同步时照样开 3 个并发写，比单线程还慢。

        现在统一在这里问一次，由 `run()` 把结论带出去。
        """
        notes: List[str] = []
        seen: set = set()
        for st in steps:
            for key, what in (("dst", "目标"), ("src", "源")):
                sid = st.get(key)
                if not sid or (sid, what) in seen:
                    continue
                seen.add((sid, what))
                s = self.sources.get(sid)
                if s is None:
                    continue
                try:
                    di = s.device_info()
                except Exception:
                    continue
                name = getattr(getattr(s, "cfg", None), "name", "") or sid
                if di.get("removable"):
                    notes.append(
                        f"{what}「{name}」是可移除存储（U 盘 / 移动硬盘）——"
                        f"执行过程中**不要拔盘**：写入中途断开可能留下不完整的文件"
                        + ("（目标侧）" if key == "dst" else ""))
                if di.get("network"):
                    notes.append(f"{what}「{name}」在网络位置上，读写延迟高，"
                                 f"大批量操作会比较慢")
                if di.get("slow"):
                    notes.append(f"{what}「{name}」是慢速盘，已自动降低并发")
        return notes

    def slow_sources(self, steps: Sequence[Dict[str, Any]]) -> List[str]:
        """这批步骤里落在慢速盘上的数据源 id（去重）。"""
        out: List[str] = []
        for st in steps:
            for key in ("dst", "src"):
                sid = st.get(key)
                if not sid or sid in out:
                    continue
                s = self.sources.get(sid)
                if s is None:
                    continue
                try:
                    if s.device_info().get("slow"):
                        out.append(sid)
                except Exception:
                    pass
        return out

    # ------------------------------------------------------------ 主入口
    def run(self, steps: Sequence[Dict[str, Any]], execute: bool = False,
            policy: Optional[str] = None, verify: bool = True,
            verify_hash: bool = False, hard_delete: bool = False,
            workers: int = 3, job: Optional[JobState] = None) -> Dict[str, Any]:
        policy = policy or self.policy
        # ★ 慢速盘自动降并发。
        #   调用方给的 `workers` 是"数据源数量 / 常规规模"下拍的值，
        #   而 U 盘上瓶颈在随机寻道 —— 3 个并发写只会互相抢带宽。
        #   只在**确实有慢盘**时压，且绝不低于 1。
        _slow = self.slow_sources(steps)
        if _slow:
            try:
                from . import device as _DEV
                workers = _DEV.effective_workers({"slow_disk": True}, workers)
            except Exception:
                workers = max(1, min(int(workers), 2))
        # ★ 预演**不占独占位**，也不写"可恢复步骤"。
        #
        #   原来无论预演还是真跑，都在这里 `create("sync")` —— 而 `sync` 在
        #   `EXCLUSIVE_KINDS` 里，于是预演（业内本该是**只读**的）会：
        #     1. 真的占住独占位 → 别的页面 `/api/busy` 变忙、写操作被 409；
        #     2. 走到 `record_steps` 把步骤落库，`create_job` 写的是
        #        `state='running'` → 预演途中进程被杀，重启后
        #        `/api/unfinished` 把它当成"可恢复任务"，
        #        用户点「接着做完」就把**只预演过的方案真的执行了**；
        #     3. `/api/execute` 没有 guard 也没包 try，若此刻真有独占任务在跑，
        #        `create` 抛 `JobBusy` → 冒到顶层 → **HTTP 500**
        #        （`JobBusy` 没有全局处理器）。前端"预演这批步骤"
        #        走的正是这条路。
        # ★ 预演虽然**不占**独占位，但也不能在别人写文件的时候跑。
        #
        #   理由：预演读的是"当前磁盘状态 + 索引"。如果此刻有写流程正在
        #   移动/删除文件，算出来的方案就是基于**正在变动的状态** ——
        #   用户拿着这份方案点"执行"，等于照着过期快照动手。
        #   所以这里主动问一次，忙就抛 `JobBusy`（由全局处理器转 409），
        #   让界面显示"请等它结束"而不是默默给一份不可靠的方案。
        if not execute:
            # 注意：这里**只**问一次，不要用 try/except 包住——
            # `JobBusy` 要原样往上抛（全局处理器转成 409）。
            get_jobs().ensure_free()
        job = job or get_jobs().create("sync", total=len(steps),
                                       exclusive=bool(execute))
        self._job = job
        job.total = len(steps)
        # ★ 每条步骤都要有 id。
        #
        #   踩过的 bug（用户反馈"移到回收站却删不掉"）：前端 `trashDialog`
        #   构造的步骤**没有 id**，而 `_sync_index` 是按 `results[].step`
        #   回查原始步骤的：
        #
        #       st = by_id.get(r.get("step") or "")
        #       if not st: continue          # ← 没有 id 就整段跳过
        #
        #   于是磁盘上的文件进了回收站、**索引里还留着** —— 曲库照样显示
        #   这首歌、数字不变，用户看到的就是"删了但没删掉"。
        #   在这里补默认 id，任何调用方（前端/脚本/旧客户端）都不会再踩。
        for _i, _st in enumerate(steps):
            if isinstance(_st, dict) and not _st.get("id"):
                _st["id"] = f"s{_i + 1}"
        # ★ 先把方案内的目标路径占下来：同一目标只能有一个写入者。
        #   不做这一步的话，"两条 move 撞同一目标"会静默覆盖（见 _reserve_targets）。
        n_conf = self._reserve_targets(steps)
        if execute:
            # 记下原始步骤：程序异常退出后，"接着做完"要靠它重放。
            # 重放是安全的 —— `skip` 策略下已完成的目标会被跳过，
            # 源文件不存在也会被跳过，不会覆盖或重复。
            # **预演不记**（它什么都没做，记下来只会变成"幽灵可恢复任务"）。
            get_jobs().record_steps(job, steps)
        results: List[Dict[str, Any]] = []
        lock = threading.RLock()
        done = 0

        if not execute:
            # 预演：只做安全检查，不动文件
            for st in steps:
                results.append(self._dry(st))
            get_jobs().finish(job, result={"dry_run": True, "steps": len(steps),
                                           "planned": _tally(results)})
            return {"dry_run": True, "policy": policy, "results": results,
                    "summary": _tally(results),
                    "device_notes": self.device_notes(steps),
                    "workers": workers}

        def work(st: Dict[str, Any]) -> None:
            nonlocal done
            if job.cancel:
                with lock:
                    results.append({"step": st.get("id"), "op": st.get("op"),
                                    "state": "cancelled", "detail": "用户取消"})
                return
            # 暂停：在**每一步开始前**等。已经做完的不回退，
            # 恢复后从下一个未处理的步骤继续。
            get_jobs().wait_if_paused(job)
            if job.cancel:
                with lock:
                    results.append({"step": st.get("id"), "op": st.get("op"),
                                    "state": "cancelled", "detail": "用户取消"})
                return
            try:
                r = self._apply_guarded(st, job, policy=policy, verify=verify,
                                        verify_hash=verify_hash,
                                        hard_delete=hard_delete)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                r = {"step": st.get("id"), "op": st.get("op"), "state": "error",
                     "src": st.get("src_path"), "dst": st.get("dst_path"),
                     # 分类后界面才能说清"这个该重试 / 那个重试也没用"
                     "error_kind": classify_error(exc, detail),
                     "detail": detail}
            with lock:
                results.append(r)
                done += 1
                job.done = done
                job.touch()          # 心跳：让"卡死检测"知道我们还活着
                job.current = str(st.get("src_path") or st.get("dst_path") or "")
                # 跳过/超时的也记下来，最后能汇报"哪些没做"
                if r.get("state") in ("error", "skipped", "conflict", "blocked"):
                    job.skipped.append({
                        "step": st.get("id"), "state": r.get("state"),
                        "kind": r.get("error_kind") or "",
                        "src": st.get("src_path"), "dst": st.get("dst_path"),
                        "detail": r.get("detail") or "",
                    })
                n_done = sum(1 for x in results if x['state'] == 'done')
                n_err = sum(1 for x in results if x['state'] == 'error')
                job.message = (f"执行中 {done}/{job.total}｜成功 {n_done}"
                               f"｜失败 {n_err}")
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            list(ex.map(work, steps))

        # （这里原来还有一次 `summary = _tally(results)`：下面追加完
        #   cancelled/pending 占位结果后会重新算一遍，第一次是死赋值。）
        by_id = {st.get("id"): st for st in steps}
        self._sync_index(results, by_id)
        # ★ 执行完让"文件浏览"的目录聚合缓存失效。
        #   缓存 TTL 20 秒，不失效的话删掉的文件还在列表里（幽灵），
        #   刷新有时又不见了 —— 用户看到的就是"时有时无"。
        #   延迟导入，避免 ops ←→ deps 循环依赖。
        try:
            from ..deps import _browse_cache_clear
            _browse_cache_clear()
        except Exception:                                     # noqa: BLE001
            pass
        # 被取消/暂停时，剩下的步骤要显式记成"没做"，
        # 否则界面上的数字对不上（总步数 100、结果只有 30 条）。
        done_ids = {r.get("step") for r in results}
        for st in steps:
            if st.get("id") not in done_ids:
                results.append({
                    "step": st.get("id"), "op": st.get("op"),
                    "state": "cancelled" if job.cancel else "pending",
                    "src": st.get("src_path"), "dst": st.get("dst_path"),
                    "detail": "用户取消" if job.cancel else "未执行（任务已暂停）",
                })
        summary = _tally(results)
        # 把"方案内目标撞车"如实报出来：这些步骤被自动改名了，
        # 用户必须知道（否则会以为"我明明放一起的"）。
        if self._extra_summary:
            summary.update(self._extra_summary)
        # 索引没跟上也要如实报：这些文件在磁盘上已经动过了，
        # 但索引里还是旧路径 —— 用户需要"重新扫描"才能对齐。
        if self._index_errors:
            summary["index_errors"] = len(self._index_errors)
        get_jobs().finish(job, result=summary)
        out = {"dry_run": False, "policy": policy, "results": results,
               "summary": summary,
               # 设备层面的提示（可移除盘别拔、网络位置慢、慢盘已降并发）
               "device_notes": self.device_notes(steps),
               # 实际用的并发：慢盘上会被压下来，界面要能说出来
               "workers": workers}
        if self._index_errors:
            out["index_errors"] = self._index_errors[:20]
            out.setdefault("warnings", []).append(
                f"有 {len(self._index_errors)} 个文件已经移动成功，"
                f"但索引没更新过来 —— 建议到「数据源」里重新扫描一次，"
                f"否则搜索/播放还会指向旧路径。")
        if job.pause:
            out["paused"] = True
            out["resumable"] = len(job.steps)
        elif job.cancel:
            out["cancelled"] = True
        return out

    # ------------------------------------------------------------ 单步
    def _apply_guarded(self, st: Dict[str, Any], job: JobState, **opts) -> Dict[str, Any]:
        """带超时的 `_apply`。

        一个文件卡住（坏扇区、网络盘掉线、被独占锁死）会让整批任务停住，
        进度条不动也不报错 —— 用户只能干等。这里给每一步加个上限，
        超时就**跳过它继续处理其他文件**，最后在汇总里汇报"哪些被跳过"。

        注意：Python 没法真正"杀掉"卡在系统调用里的线程，所以这里是在
        工作线程池层面**放弃等待**：那一步标记为超时，主流程继续往下走。
        已经写了一半的临时文件由 `_apply` 自己的 `.mshpart` + 原子改名机制
        保证不会污染目标（最坏情况是留一个 .mshpart 残留，不会被当成成品）。
        """
        if STEP_TIMEOUT_SEC <= 0:
            return self._apply(st, **opts)
        box: Dict[str, Any] = {}

        def run() -> None:
            try:
                box["r"] = self._apply(st, **opts)
            except Exception as exc:            # noqa: BLE001 —— 原样带回主线程
                box["e"] = exc

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(STEP_TIMEOUT_SEC)
        if t.is_alive():
            job.touch()
            return {"step": st.get("id"), "op": st.get("op"), "state": "error",
                    "src": st.get("src_path"), "dst": st.get("dst_path"),
                    "error_kind": ERR_TIMEOUT,
                    "detail": (f"操作超过 {int(STEP_TIMEOUT_SEC)} 秒没有返回，"
                               f"已跳过（文件可能损坏、被独占，"
                               f"或所在磁盘/网络异常）"),
                    "timeout": True}
        if "e" in box:
            raise box["e"]
        return box.get("r") or {"step": st.get("id"), "op": st.get("op"),
                                "state": "error", "detail": "没有返回结果",
                                "error_kind": ERR_OTHER}

    def _dry(self, st: Dict[str, Any]) -> Dict[str, Any]:
        op = st.get("op")
        out = {"step": st.get("id"), "op": op, "state": "planned",
               "src": st.get("src_path"), "dst": st.get("dst_path"), "detail": ""}
        try:
            if op in ("copy", "link"):
                src = self.sources.get(st.get("src"))
                dst = self.sources.get(st.get("dst"))
                if dst is not None and not dst.can_write:
                    out.update(state="blocked", error_kind=ERR_READONLY,
                               detail=f"目标源 {st.get('dst')} 只读，无法写入")
                    return out
                ap = dst.abs_path(st.get("dst_path") or "") if dst else ""
                exists = bool(dst and dst.exists(ap))
                out["detail"] = "目标文件已存在" if exists else "将复制"
                out["target_exists"] = exists
            elif op in ("trash", "delete"):
                src = self.sources.get(st.get("src"))
                if src is not None and not src.can_delete:
                    out.update(state="blocked", error_kind=ERR_READONLY,
                               detail=f"源 {st.get('src')} 不允许删除")
                    return out
                out["detail"] = "将移入回收站" if op == "trash" else "将永久删除"
            elif op == "rename":
                src = self.sources.get(st.get("src"))
                if src is not None and not src.can_write:
                    out.update(state="blocked", error_kind=ERR_READONLY,
                               detail="源只读，无法重命名")
                    return out
                out["detail"] = f"{P.basename(st.get('src_path') or '')} → {P.basename(st.get('dst_path') or '')}"
            elif op == "move":
                src = self.sources.get(st.get("src"))
                if src is None:
                    out.update(state="error", detail="数据源不存在")
                    return out
                cur, want, err = _resolve_move(src, st)
                if err:
                    out.update(err)
                    return out
                if cur == want:
                    out.update(state="skipped", detail="源与目标相同")
                    return out
                if src.stat(cur) is None:
                    out.update(state="error", detail="源文件不存在（可能已被移动）")
                    return out
                exists = src.exists(want)
                out["target_exists"] = exists
                out["detail"] = "目标文件已存在" if exists else "将移动"
        except Exception as exc:
            out.update(state="error", detail=f"{type(exc).__name__}: {exc}")
        return out

    def _apply(self, st: Dict[str, Any], policy: str, verify: bool,
               verify_hash: bool, hard_delete: bool) -> Dict[str, Any]:
        op = st.get("op")
        t0 = time.time()
        res: Dict[str, Any] = {"step": st.get("id"), "op": op, "state": "done",
                               "src": st.get("src_path"), "dst": st.get("dst_path"),
                               "detail": "", "kind": st.get("kind")}
        if op in ("copy", "link"):
            res.update(self._do_copy(st, policy, verify, verify_hash))
        elif op == "transfer":
            # 把调用方的 `verify_hash` 一起传下去（用户勾了"按内容校验"时
            # 就该对 transfer 步骤也生效，不能只对 copy 生效）
            res.update(self._do_transfer(st, policy=policy, verify=verify,
                                        verify_hash=verify_hash,
                                        hard_delete=hard_delete))
        elif op in ("trash", "delete"):
            res.update(self._do_remove(st, hard=hard_delete or op == "delete"))
        elif op == "rename":
            res.update(self._do_rename(st, policy))
        elif op == "move":
            res.update(self._do_move(st, policy))
        else:
            # 未知操作保持 `skipped`（有测试 `test_unknown_op_still_skipped`
            # 专门守着这个契约：加新 op 时不能把未知 op 的处理改坏）。
            #
            #★ 但这是一个**有意的取舍**，接手的人要知道代价：
            #   `skipped` 在结果表里看起来像"策略跳过、一切正常"，
            #   而未知操作其实是代码 bug（计划里有执行器不认识的 op）。
            #   所以 detail 必须写清"执行器不支持"，不要含糊成"已跳过"。
            #   如果以后要做"方案指纹/执行前校验"，这里应该改成 error。
            res.update(state="skipped",
                       detail=f"未知操作 {op}（执行器不支持，已跳过）")
        res["seconds"] = round(time.time() - t0, 2)
        return res

    # ------------------------------------------------------------ transfer
    def _do_transfer(self, st: Dict[str, Any], policy: str = "skip",
                     verify: Optional[bool] = None,
                     verify_hash: bool = False,
                     hard_delete: bool = False) -> Dict[str, Any]:
        """跨库**安全搬运**：复制 → 校验 → 再删源。

        复用 `core.transfer.transfer_one` —— 那里面已经把所有失败路径
        想清楚了（校验不过就删掉目标那份、保留源；删源失败就如实报
        "两边都有"、**绝不回滚目标**）。这里只做 Executor 侧的适配：
          · 把它返回的 state 映射到 Executor 的取值
          · 保留接口需要的字段名（target / bytes / error_kind）
        """
        from . import transfer as X
        src = self.sources.get(st.get("src"))
        dst = self.sources.get(st.get("dst"))
        if src is None or dst is None:
            return {"state": "error", "detail": "数据源不存在"}
        # ★ 先认领目标（和 `_do_copy` 一样）：同方案里如果有更早的步骤
        #   要写这个路径，改个不冲突的名字，而不是覆盖掉它。
        #   只把 op 加进 `_reserve_targets` 是不够的 —— 那里只是**标记**
        #   `_dst_conflict`，真正改名要靠这里认领。
        dst_path = st.get("dst_path") or ""
        try:
            if dst_path and hasattr(dst, "abs_path"):
                claimed = self._claim_target(st, dst.abs_path(dst_path))
                if claimed != dst.abs_path(dst_path):
                    dst_path = dst.rel_path(claimed)
        except Exception:
            pass
        try:
            r = X.transfer_one(
                src, dst,
                st.get("src_path") or "",
                dst_path,
                policy=policy,
                # verify 默认 None → 按源数据源的传输模式（standard/fast）决定
                verify=verify,
                # ★ 把调用方的 `verify_hash` 传下去。
                #   原来这里**写死 False** —— `/api/execute` 明明把用户的
                #   "按内容校验"选择传进来了，到 `transfer` 步骤却被丢掉，
                #   于是勾了等于没勾（和 duration_tolerance 那类"设置形同虚设"
                #   是同一个毛病）。
                verify_hash=bool(verify_hash),
                hard_delete=hard_delete,
                # ★ 把 job 传下去，让**单个大文件**搬运途中也能打心跳。
                #   不传的话，一个 40 GB 的文件在网络盘上搬 20 分钟，
                #   期间没有任何心跳 —— 超过 `STALE_JOB_SECONDS`(180s)
                #   就会被 `_reap_stale_locked` 判成"心跳中断"，
                #   任务被标成 error、独占位被释放，而它其实还在跑。
                #   那之后任何写操作都会被放进来，两边同时改文件。
                job=self._job)
        except Exception as exc:                                  # noqa: BLE001
            return {"state": "error",
                    "detail": f"{type(exc).__name__}: {exc}"}
        out: Dict[str, Any] = {
            "state": r.get("state") or "error",
            "detail": r.get("detail") or "",
        }
        for k in ("target", "bytes", "error_kind", "trashed", "steps_done",
                  "overwrote"):
            if k in r:
                out[k] = r[k]
        return out

    # ------------------------------------------------------------ copy
    def _do_copy(self, st: Dict[str, Any], policy: str, verify: bool,
                 verify_hash: bool) -> Dict[str, Any]:
        src = self.sources.get(st.get("src"))
        dst = self.sources.get(st.get("dst"))
        if src is None or dst is None:
            return {"state": "error", "detail": "数据源不存在"}
        if not dst.can_write:
            return {"state": "blocked", "error_kind": ERR_READONLY,
                    "detail": "目标源只读"}
        src_path = st.get("src_path") or ""
        dst_rel = st.get("dst_path") or ""
        bad = _guard_inside(src, src_path, "复制（源）")
        if bad:
            return bad
        bad = _guard_inside(dst, dst_rel, "复制（目标）")
        if bad:
            return bad
        dst_path = dst.abs_path(dst_rel)
        # ★ 先认领：同方案里如果有更早的步骤要写这个路径，改个不冲突的名字，
        #   而不是覆盖掉它（否则两个都报成功、实际少一个文件）。
        claimed = self._claim_target(st, dst_path, policy)
        if claimed != dst_path:
            dst_path = claimed
            dst_rel = dst.rel_path(dst_path)

        _had_old = dst.exists(dst_path)
        if _had_old:
            if policy == "skip":
                return {"state": "conflict", "error_kind": ERR_CONFLICT,
                        "detail": "目标已存在，跳过", "target": dst_path}
            if policy == "rename":
                # ★ claim 已经在锁内找过空位；还能撞说明有别的线程正在写
                #   同一个名字。**报错，绝不覆盖**（覆盖 = 静默丢一个文件）。
                #   原来这里没有 rename 分支，会直接穿透到 copy_from 覆盖。
                return {"state": "error", "target": dst_path,
                        "detail": "目标名冲突（并发占用），未执行复制，请重试"}
            if policy == "overwrite":
                # ★ 覆盖前先把原件移进回收站（可恢复）。直接覆盖 +
                #   校验失败会把原件写坏，用户什么都剩不下。
                try:
                    dst.remove(dst_path, hard=False)
                except Exception as exc:                      # noqa: BLE001
                    return {"state": "error",
                            "detail": f"覆盖前备份原件失败：{type(exc).__name__}: {exc}"}

        src_stat = src.stat(src_path)
        if src_stat is None:
            return {"state": "error", "detail": "源文件不存在（可能已被移动）"}
        if src_stat.is_dir:
            return {"state": "error", "detail": "源路径是目录"}

        before = dst.stat(dst_path)
        # 传输进度由全局 job 的消息体现，这里不做高频回调（网络源回调开销大）
        #
        # ★ 但要有**心跳**：单文件复制（尤其本地→本地走 shutil.copyfile）
        #   中途没有任何回调，一个 6 GB 的文件拷到 U 盘要 ~300 秒，
        #   超过 `STALE_JOB_SECONDS`(180s) 就会被判"心跳中断"、
        #   标 error 并**释放独占位**，而复制还在跑 ——
        #   之后任何写操作都能插进来，两边同时改文件。
        try:
            from .transfer import _Heartbeat as _HB
            with _HB(self._job):
                info = dst.copy_from(src, src_path, dst_path, progress=None)
        except ImportError:
            info = dst.copy_from(src, src_path, dst_path, progress=None)
        _resumed = int((info or {}).get("resumed_from") or 0)
        out = {"state": "done", "detail": "已复制", "target": dst_path,
               "bytes": info.get("size") or src_stat.size,
               "resumed_from": _resumed,
               "overwrote": bool(before) or _had_old}
        if _resumed:
            out["detail"] = f"已复制（断点续传：从 {_resumed} 字节处继续）"

        # 校验
        if verify:
            st2 = dst.stat(dst_path)
            if st2 is None:
                out.update(state="error", detail="复制后目标文件不存在")
                return out
            if src_stat.size and st2.size != src_stat.size:
                # 不留在正式名下（会被当成正常文件入库），也不直接删
                import uuid as _uuid
                bad = f"{dst_path}.bad-{_uuid.uuid4().hex[:8]}"
                try:
                    dst.move(dst_path, bad)
                except Exception:                             # noqa: BLE001
                    bad = dst_path
                out.update(state="error",
                           detail=(f"大小不一致：源 {src_stat.size} 目标 {st2.size}"
                                   f"（文件可能仍在传输）；不完整的那份已改名：{bad}"))
                return out
            if verify_hash:
                try:
                    h1 = S.quick_hash(src, src_path, src_stat.size or 0)
                    h2 = S.quick_hash(dst, dst_path, st2.size or 0)
                    if h1 != h2:
                        out.update(state="error", detail="抽样哈希不一致，内容可能损坏")
                        return out
                    out["verified"] = "quick-hash"
                except Exception as exc:
                    out["verified"] = f"跳过校验: {exc}"
            else:
                out["verified"] = "size"
        return out

    # ------------------------------------------------------------ remove
    def _do_remove(self, st: Dict[str, Any], hard: bool) -> Dict[str, Any]:
        src = self.sources.get(st.get("src"))
        if src is None:
            return {"state": "error", "detail": "数据源不存在"}
        if not src.can_delete:
            return {"state": "blocked", "error_kind": ERR_READONLY,
                    "detail": "该数据源不允许删除（只读）"}
        path = st.get("src_path") or ""
        if not path:
            return {"state": "error", "detail": "缺少路径"}
        bad = _guard_inside(src, path, "永久删除" if hard else "移入回收站")
        if bad:
            return bad
        # ★ 幂等：文件已经不在 → skipped。
        #   同一批里出现重复路径（列表勾选 + 目录树勾选）时，第二条会抛
        #   FileNotFoundError 被记成 error —— 界面就显示"失败 N"，
        #   而实际文件早就删掉了，看起来像"删了但报错"。
        try:
            _exists = src.stat(path) is not None
        except Exception:                                     # noqa: BLE001
            _exists = False
        if not _exists:
            return {"state": "skipped",
                    "detail": "文件已不存在（可能已被删除或移走）"}
        try:
            where = src.remove(path, hard=hard)
        except (FileNotFoundError, NotADirectoryError):
            # ★ 并发兜底：同一路径的两条步骤可能同时通过上面的存在性检查
            #   （执行器是多线程的），先到的那条把文件移走了，后到这条会抛
            #   FileNotFoundError。这不是失败 —— 目标状态已经达成。
            return {"state": "skipped",
                    "detail": "文件已不存在（可能已被删除或移走）"}
        return {
            "state": "done",
            "detail": "已永久删除" if hard else "已移入回收站",
            "trash": where or None,
            "bytes": st.get("size") or 0,
        }

    # ------------------------------------------------------------ rename
    def _do_rename(self, st: Dict[str, Any], policy: str) -> Dict[str, Any]:
        src = self.sources.get(st.get("src"))
        if src is None:
            return {"state": "error", "detail": "数据源不存在"}
        if not src.can_write:
            return {"state": "blocked", "detail": "源只读"}
        cur, want = st.get("src_path") or "", st.get("dst_path") or ""
        if not cur or not want:
            return {"state": "error", "detail": "缺少路径"}
        for _p, _what in ((cur, "重命名"), (want, "重命名目标")):
            bad = _guard_inside(src, _p, _what)
            if bad:
                return bad
        # ★ 重命名也要在锁内认领目标名。
        #   原来这里完全没有 `_claim_target`，直接调裸 `_dedupe_name`：
        #   同一方案里多条 rename 指向同一个名字时，两个线程都算出
        #   `x (2).mp3`，后写的覆盖先写的 —— 两个都报 done，文件少一个。
        abs_want = src.abs_path(want)
        want = src.rel_path(self._claim_target(st, abs_want, policy))
        if src.exists(want):
            if policy == "skip":
                return {"state": "skipped", "detail": "目标名已存在，跳过重命名"}
            if policy != "overwrite":
                return {"state": "error", "target": want,
                        "detail": "目标名冲突（并发占用），未执行重命名，请重试"}
            # ★ overwrite 必须**先备份再覆盖**：底层 src.replace 是
            #   os.replace，直接把同名目标物理抹掉、不进回收站。
            #   用户以为"只是换个名"，实际上原文件永久没了。
            try:
                src.remove(want, hard=False)
            except Exception as exc:                      # noqa: BLE001
                return {"state": "error", "target": want,
                        "detail": f"覆盖前备份原件失败：{type(exc).__name__}: {exc}"}
        # 同源内改名：借道临时名，兼容没有 rename 的源
        tmp = cur + ".mshren"
        try:
            src.replace(cur, tmp)
            src.replace(tmp, want)
        except Exception:
            if src.exists(tmp) and not src.exists(cur):
                try:
                    src.replace(tmp, cur)
                except Exception:
                    pass
            raise
        return {"state": "done", "detail": "已重命名", "target": want}

    # ------------------------------------------------------------ move
    def _do_move(self, st: Dict[str, Any], policy: str) -> Dict[str, Any]:
        """同源内移动文件（换目录，名字可以不变）。

        与 `_do_copy` 的区别：不跨源、不复制，直接让数据源做原子改名；
        代价是只认同一个源（src 必须等于 dst）——跨源搬运走 copy 计划。
        """
        src = self.sources.get(st.get("src"))
        if src is None:
            return {"state": "error", "detail": "数据源不存在"}
        cur, want, err = _resolve_move(src, st)
        if err:
            return err
        if cur == want:
            # 目标就是自己：什么都没做，报 skipped 而不是 done，避免界面显示"已移动"
            return {"state": "skipped", "detail": "源与目标相同"}
        st_cur = src.stat(cur)
        if st_cur is None:
            return {"state": "error", "detail": "源文件不存在（可能已被移动）"}
        if st_cur.is_dir:
            return {"state": "error", "detail": "源路径是目录"}
        st_want = src.stat(want)
        if st_want is not None and st_want.is_dir:
            # 目标是个目录：硬来只会让 os.replace 报难懂的错，或者把文件改名成 "目录 (2)"
            return {"state": "error", "detail": "目标路径是目录，需要给出完整的目标文件名"}
        # ★ 先认领目标名：如果本方案里更早的步骤已经要写这个路径，
        #   就改成不冲突的名字，**绝不复用**别人写下去的那个名字。
        want = self._claim_target(st, want, policy)
        st_want = src.stat(want)
        if st_want is not None and st_want.is_dir:
            return {"state": "error", "detail": "目标路径是目录，需要给出完整的目标文件名"}
        if st_want is not None:
            if policy == "skip":
                return {"state": "skipped", "detail": "目标已存在，跳过移动", "target": want}
            if policy == "rename":
                # ★ 认领时已经找过空位；这里还能撞，说明有别的线程正在写
                #   同一个名字。**报错，绝不覆盖**（覆盖 = 静默丢一个文件）。
                return {"state": "error", "target": want,
                        "detail": "目标名冲突（并发占用），未执行移动，请重试"}
            # ★ overwrite 同理：src.move 底层是 os.replace，不先备份就是硬删。
            try:
                src.remove(want, hard=False)
            except Exception as exc:                      # noqa: BLE001
                return {"state": "error", "target": want,
                        "detail": f"覆盖前备份原件失败：{type(exc).__name__}: {exc}"}
        # 目录先建好；放在 skip/rename 判断之后，免得"其实什么都不做"也留下空目录
        src.mkdirs(P.parent(want))
        src.move(cur, want)

        # 校验：目标必须真的在，且（源报了体积时）体积一致；不通过就如实报错，
        # 让用户自己去看文件，绝不假装成功。
        got = src.stat(want)
        if got is None:
            return {"state": "error", "detail": "移动后目标文件不存在", "target": want}
        if st_cur.size and got.size != st_cur.size:
            return {"state": "error", "target": want,
                    "detail": f"移动后大小不一致：源 {st_cur.size} 目标 {got.size}"}
        return {"state": "done", "detail": "已移动", "target": want,
                "bytes": got.size or st_cur.size or int(st.get("size") or 0)}

    # ------------------------------------------------------------ 索引同步
    def _drop_track(self, sid: str, path: str) -> int:
        """把一条索引记录删掉，**路径形态不敏感**。

        ★ 踩过的 bug（用户反馈"移到回收站却删不掉"）：

          `_sync_index` 原来直接 `remove_track(sid, st["src_path"])`，只按
          **原样**匹配。而索引里存的是**绝对路径**；只要调用方传的是相对
          路径（脚本、旧客户端、或某些前端分支），DELETE 就命中 0 行 ——
          磁盘上的文件已经进回收站了，曲库里却还留着它。用户看到的现象
          就是"删了但没删掉"。

        这里依次尝试：绝对路径、源内相对路径、原样路径；
        都没命中再按**目录前缀**删一次（防止这条步骤其实指向一个目录）。
        """
        if self.db is None:
            return 0
        src = self.sources.get(sid)
        cands: List[str] = []
        if src is not None:
            try:
                cands.append(src.abs_path(path))
            except Exception:                              # noqa: BLE001
                pass
            try:
                cands.append(src.rel_path(path))
            except Exception:                              # noqa: BLE001
                pass
        cands.append(path)
        seen = set()
        for c in cands:
            c = P.norm(c) if c else ""
            if not c or c in seen:
                continue
            seen.add(c)
            try:
                hit = self.db.query_one(
                    "SELECT 1 AS x FROM track WHERE source_id=? AND path=?",
                    (sid, c))
            except Exception:                              # noqa: BLE001
                hit = None
            if hit:
                self.db.remove_track(sid, c)
                return 1
        # 兜底：当作目录，按前缀删
        if src is not None:
            try:
                base = P.norm(src.abs_path(path)).rstrip("/")
                # ★ 不能用 LIKE：SQLite 里 `_` 是单字符通配符，
                #   目录 `Top_100` 的前缀会连 `Top-100/` 一起匹配、误删索引。
                #   用 SUBSTR 做严格前缀比较。
                rows = self.db.query(
                    "SELECT path FROM track WHERE source_id=? "
                    "AND (path=? OR SUBSTR(path, 1, ?) = ?)",
                    (sid, base, len(base) + 1, base + "/"))
                if rows:
                    return self.db.delete_tracks_by_paths(
                        sid, [r["path"] for r in rows])
            except Exception:                              # noqa: BLE001
                pass
        return 0

    def _sync_index(self, results: Sequence[Dict[str, Any]],
                    by_id: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        """把成功的操作同步进索引，保证界面立刻正确（不做网络往返）。

        * trash/delete 成功 → 移除对应索引记录
        * rename 成功 → 更新路径与文件名
        * move 成功 → 同样更新路径、父目录、文件名与相对路径
        * copy 成功 → 目标文件留到下次扫描补全元数据（避免执行期再做一次远端解析）
        """
        if self.db is None:
            return
        by_id = by_id or {}
        for r in results:
            if r.get("state") != "done":
                continue
            st = by_id.get(r.get("step") or "")
            if not st:
                continue
            op = r.get("op")
            try:
                if op in ("trash", "delete"):
                    self._drop_track(st.get("src"), st.get("src_path"))
                elif op == "copy":
                    # ★ 复制成功后立刻把目标索引出来。
                    #   原来要等下一次扫描 —— 目标库在界面里"少一首"，
                    #   用户会以为没复制成功。
                    _tgt = r.get("target")
                    if _tgt:
                        try:
                            from .. import deps as _D2
                            if _D2.DB is self.db:
                                _D2.refresh_tracks(
                                    st.get("dst") or st.get("src"), [_tgt])
                        except Exception:                     # noqa: BLE001
                            pass
                elif op == "transfer":
                    # ★ 目标那份要**立刻**索引出来（注释一直写着"同 copy"，
                    #   代码却只删了源）。只删源不补目标的后果：文件已经在
                    #   目标库里，索引里却没有 —— 界面显示"搬丢了"，
                    #   直到用户手动全量扫描。
                    _tgt2 = r.get("target") or st.get("dst_path")
                    if _tgt2 and st.get("dst"):
                        try:
                            # 只在"执行器用的就是全局 DB"时刷新 —— 也就是
                            # 生产接线。单测会用自己的 Database 实例，
                            # 那时全局 deps 不可用（还会互相干扰）。
                            from .. import deps as _D
                            if _D.DB is self.db:
                                _D.refresh_tracks(st.get("dst"), [_tgt2])
                        except Exception:                     # noqa: BLE001
                            pass
                    # 安全搬运成功 = 源那份**已经删了**（进回收站）。
                    #   不在这里删索引的话，源库里会留着一条指向
                    #   已不存在文件的记录 —— 搜索得到、点进去播放失败。
                    #   目标那份留到下次扫描补元数据（同 copy 的处理）。
                    self._drop_track(st.get("src"), st.get("src_path"))
                elif op == "rename":
                    # 必须走 db.write()：它才会 commit。原先用 db.execute() 裸执行，
                    # 事务一直不提交——本线程看得见，别的连接（也就是界面的后续请求）
                    # 仍然读到旧路径，进程退出还会丢掉这次更新。
                    # ★ 坐标系统一：DB 里 path 存的是**绝对路径**，而步骤里
                    #   是源内相对路径。直接拿相对路径当 WHERE 键 → rowcount=0
                    #   （文件已改名、索引还是旧路径）；拿相对路径写回 →
                    #   track.path 被污染成 "d.mp3"、parent 变空、详情 404。
                    _rsrc = self.sources.get(st.get("src"))
                    _raw_tgt = r.get("target") or st.get("dst_path") or ""
                    _raw_src = st.get("src_path") or ""
                    try:
                        _new_abs = (_rsrc.abs_path(_raw_tgt)
                                    if _rsrc is not None else _raw_tgt)
                    except Exception:                     # noqa: BLE001
                        _new_abs = _raw_tgt
                    try:
                        _old_abs = (_rsrc.abs_path(_raw_src)
                                    if _rsrc is not None else _raw_src)
                    except Exception:                     # noqa: BLE001
                        _old_abs = _raw_src
                    with self.db.write() as conn:
                        conn.execute(
                            "UPDATE track SET path=?, parent=?, name=?, rel_path=? "
                            "WHERE source_id=? AND (path=? OR path=?)",
                            (_new_abs, P.parent(_new_abs), P.basename(_new_abs),
                             (_rsrc.rel_path(_new_abs) if _rsrc is not None else _raw_tgt),
                             st.get("src"), _old_abs, _raw_src),
                        )
                elif op == "move":
                    # 移动必然换目录：rel_path 也要跟着重算，否则按目录分组/展示会错位。
                    # rel_path 只能由数据源算（它知道自己的 root 怎么剥），
                    # 拿不到源实例时才退化成整条路径。
                    new_path = r.get("target") or st.get("dst_path") or ""
                    src = self.sources.get(st.get("src"))
                    # ★ 同样要归一成绝对路径（DB 里是绝对路径）。
                    _raw_src2 = st.get("src_path") or ""
                    if src is not None:
                        try:
                            new_path = src.abs_path(new_path)
                        except Exception:                 # noqa: BLE001
                            pass
                        try:
                            _old_abs2 = src.abs_path(_raw_src2)
                        except Exception:                 # noqa: BLE001
                            _old_abs2 = _raw_src2
                    else:
                        _old_abs2 = _raw_src2
                    rel = src.rel_path(new_path) if src is not None else new_path
                    # 走 write() 而不是裸 execute()：接口是别的线程处理的，
                    # 不提交的话本次改动对其它连接（也就是界面后续的查询）根本不可见，
                    # "移动完界面立刻正确"就成了空话。
                    with self.db.write() as conn:
                        conn.execute(
                            "UPDATE track SET path=?, parent=?, name=?, rel_path=? "
                            "WHERE source_id=? AND (path=? OR path=?)",
                            (new_path, P.parent(new_path), P.basename(new_path), rel,
                             st.get("src"), _old_abs2, _raw_src2),
                        )
            except Exception as exc:                              # noqa: BLE001
                # ★ 不能静默吞掉。
                #   这一步失败意味着"磁盘已经动了、索引还是旧的"：
                #   文件明明移走了，搜索还能搜到、点进去播放失败。
                #   原来这里是 `except: pass`，用户只会看到"移动成功"，
                #   然后过一阵发现库里对不上，而且完全不知道原因。
                #   现在如实记下来，由 run() 汇总进 summary 并提示重新扫描。
                self._index_errors.append({
                    "step": r.get("step"), "op": op,
                    "src_path": st.get("src_path"),
                    "error": f"{type(exc).__name__}: {exc}",
                })
        return


def _inside_root(src: MusicSource, path: str) -> bool:
    """路径是否确实落在数据源根目录内。

    判定逻辑统一在 `paths.inside_root`（含 `..` 与 Windows 大小写处理）。
    这里只做转发，避免两份口径漂移。
    """
    return P.inside_root(src.root, path)


def _guard_inside(src: MusicSource, path: str, what: str) -> Optional[Dict[str, Any]]:
    """破坏性操作前的统一越界检查。在根内返回 None，越界返回错误 dict。

    ★ 为什么必须有这一层（真实漏洞）：

      `_inside_root` 原来**只有 `move` 在用**。`delete` / `trash` / `rename` /
      `copy` 都直接信任 `src_path`，而 Windows 上 `LocalSource.abs_path()`
      对"别的盘的绝对路径"是原样返回的。于是只要构造一个
      `{"op": "delete", "src_path": "D:/别处/重要文件.txt"}` 的步骤
      （`/api/execute` 接受客户端自带的 steps），就能**永久删除数据源根目录
      之外的文件，并且返回 done**。实测确认过。

      现在所有动文件的入口都过一遍这里。
    """
    try:
        abs_p = src.abs_path(path)
    except Exception as exc:                      # noqa: BLE001
        return {"state": "error", "detail": f"路径无法解析：{exc}"}
    if not _inside_root(src, abs_p):
        return {"state": "error",
                "detail": f"路径超出数据源根目录，已拒绝{what}",
                "src": path}
    # ★ 字符串判断挡不住 junction（目录联接）：它不在 islink() 里，
    #   但读写会跑到源根之外。这里再解析一次真实路径。
    if not P.inside_root_real(src.root, abs_p):
        return {"state": "error",
                "detail": f"路径经过目录联接/符号链接指向了数据源之外，已拒绝{what}",
                "src": path}
    return None


def _resolve_move(src: MusicSource, st: Dict[str, Any]) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """移动步骤的公共前置检查，返回 (源路径, 目标路径, 错误结果)。

    dry-run 与真正执行共用同一段判断——两边判断不一致才是最容易丢文件的地方。
    """
    if st.get("src") != st.get("dst"):
        return "", "", {"state": "error", "detail": "移动必须发生在同一个数据源内（src ≠ dst）"}
    if not src.can_write:
        return "", "", {"state": "blocked", "detail": "源只读，无法移动"}
    cur = st.get("src_path") or ""
    want = st.get("dst_path") or ""
    if not cur or not want:
        return "", "", {"state": "error", "detail": "缺少路径"}
    # ★ 相对路径按"数据源根下的相对路径"解释，再走同一套越界校验。
    #
    #   原来直接拿原文去 `_inside_root`，而那里**只认绝对路径**
    #   （这是有意的安全设计，见它的 docstring）—— 于是 `out/x.mp3`
    #   这种完全合理的相对路径会被报成"路径超出数据源根目录"。
    #   报错内容和真实原因（"我只认绝对路径"）完全不是一回事，
    #   直接调 API 的人会以为是越界，查半天。
    #
    #   注意：**先展开、再校验**，所以 `../别处/x` 展开后仍然落在 root 外，
    #   照样被挡住 —— 放开相对路径**不等于**放开越界检查。
    def _expand(p: str) -> str:
        n = P.norm(p)
        if not n or n.startswith("/") or P.is_windows_abs(n):
            return n
        return src.abs_path(n)

    cur_abs, want_abs = _expand(cur), _expand(want)
    if not (_inside_root(src, cur_abs) and _inside_root(src, want_abs)):
        return "", "", {"state": "error", "detail": "路径超出数据源根目录，已拒绝移动"}
    return cur_abs, want_abs, None


def _dedupe_candidates(path: str, limit: int = 200):
    """产出改名候选：`a.mp3` → `a (2).mp3`、`a (3).mp3` …

    单独抽出来是给 `_claim_target` 用的：那边必须在**同一把锁里**
    同时判断"磁盘上有没有"和"本方案里有没有人占"，所以不能直接调
    `_dedupe_name`（那个只看磁盘）。
    """
    base, ext = P.stem(P.basename(path)), P.ext(path)
    parent = P.parent(path)
    for i in range(2, limit):
        yield P.join(parent, f"{base} ({i}){ext}")
    # 极端情况下（同名文件特别多）用时间戳兜底
    yield P.join(parent, f"{base} ({int(time.time())}){ext}")


def _dedupe_name(src: MusicSource, path: str) -> str:
    """`a.mp3` 已存在 → `a (2).mp3`。

    ⚠️ 这个函数**只问磁盘、不问本方案的目标占位**。要判断"同方案里
    有没有别人占了这个名字"，必须用 `Executor._claim_target`（它在锁内做）。
    """
    for cand in _dedupe_candidates(path):
        if not src.exists(cand):
            return cand
    return path


def _tally(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"total": len(results)}
    for r in results:
        st = r.get("state", "unknown")
        out[st] = out.get(st, 0) + 1
    # move 也算"搬运的体积"：界面上的"移动体积"直接读这个字段，
    # 只统计 copy 的话移动完永远显示 0 B（而 copy 步骤里不会混进 move）。
    # `transfer` 同样要算 —— 它就是"搬运"（复制 + 校验 + 删源），
    # 漏掉的话跨库搬运完界面显示"搬运了 0 B"。
    out["bytes_moved"] = sum(int(r.get("bytes") or 0) for r in results
                             if r.get("state") == "done"
                             and r.get("op") in ("copy", "move", "transfer"))
    out["bytes_trashed"] = sum(int(r.get("bytes") or 0) for r in results
                               if r.get("state") == "done"
                               and r.get("op") in ("trash", "delete"))
    # 错误分类统计 + 可重试清单：让界面能说清"哪些该重试、哪些不用管"
    errs: Dict[str, int] = {}
    retryable: List[str] = []
    for r in results:
        if r.get("state") not in ("error", "skipped", "blocked", "conflict"):
            continue
        k = r.get("error_kind") or ""
        if not k:
            continue
        errs[k] = errs.get(k, 0) + 1
        if k in ERR_RETRYABLE:
            retryable.append(str(r.get("step") or ""))
    if errs:
        out["error_kinds"] = errs
        out["error_labels"] = {k: ERR_LABEL.get(k, k) for k in errs}
        out["retryable_steps"] = [x for x in retryable if x]
    return out


def cleanup_stale_parts(src, older_than_hours: float = 24.0) -> int:
    """清掉数据源里**过期**的 `.mshpart` 残留，返回删除个数。

    ★ 为什么需要：复制走的是"写 `.mshpart` → 校验 → 改名"。进程被强杀
      （断电、任务管理器结束进程）时，临时文件就留在了库里。它不会被当成
      成品（扫描只看音频扩展名），但会一直占空间，而且用户完全不知道
      这些文件是什么。

      只删**足够旧**的（默认 24 小时）：正在进行的复制刚写出来的文件
      不能被误删，而一次复制不可能持续一天。
    """
    import time as _t
    cutoff = _t.time() - max(1.0, older_than_hours) * 3600
    removed = 0
    try:
        it = src.walk("")
    except Exception:                                  # noqa: BLE001
        return 0
    for e in it:
        try:
            # `.mshpart` 是复制中的临时文件，`.mshpart.json` 是它的续传元数据
            _n = str(e.name)
            if e.is_dir or not (_n.endswith(".mshpart") or _n.endswith(".mshpart.json")):
                continue
            if (e.mtime or 0) > cutoff:
                continue
            src.remove(e.path, hard=True)
            removed += 1
        except Exception:                              # noqa: BLE001
            continue
    if removed:
        log.info("清理了 %d 个过期的 .mshpart 临时文件", removed)
    return removed
