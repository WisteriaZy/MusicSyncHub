"""轻量任务跟踪：扫描/哈希/同步这类长任务的状态，供前端轮询进度。

关于**任务互斥**（为什么要有）：

用户可以同时开好几个页面。如果没有互斥，两个页面同时发起
"移动/整理/入库"，就会出现：A 任务刚算出方案，B 任务已经把文件挪走了 ——
A 再执行就是基于**过期状态**操作，轻则报错，重则搬错文件。

所以这里做**严格串行**：
  · 同一时刻只允许一个"独占任务"在跑；
  · 正在跑的时候再发起 → 直接拒绝并告诉你当前在跑什么（**不排队**）；
    排队在文件操作里是危险的：用户以为提交了就完事，实际上操作的
    是很久以后的磁盘状态。
  · 任务结束后前端必须刷新状态，再决定下一个操作。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from . import proclock
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("msh.jobs")

# 超过这么久没有心跳，就认为这个任务已经死了（进程被杀 / 断电）。
# 有了它，卡死的任务不会永久占着独占位。
STALE_JOB_SECONDS = float(os.environ.get("MSH_STALE_JOB_SEC") or 180)
# 这些 kind 会改动磁盘，必须独占；只读的（扫描只是建索引，但也动库）
# 一律独占更安全，也让"一次只做一件事"这个规则好理解。
EXCLUSIVE_KINDS = {"sync", "move", "organize", "collect", "upload", "trash",
                   "tags", "station", "cleanup",
                   # 跨库传输会同时改两个数据源（目标写入 + 源删除），
                   # 必须独占 —— 和别的写流程并行会基于过期的文件状态操作
                   "transfer",
                   # ★ 这两个名字以前不在这儿，是实打实的漏网：
                   #   · `scan` 会**写索引**。注释里（main.py 的 start_scan）
                   #     明写着"扫描会写索引，和文件移动冲突 → 也要独占"，
                   #     但那时 `create("scan")` 走的是默认推导，而名字不在集合里
                   #     → `exclusive=False` → 扫描期间可以开始移动/整理，
                   #     索引里会留下指向**已经移走文件**的记录
                   #     （表现：搜索得到、点进去播放失败）。
                   #   · `upload-commit` 会把暂存区的文件**搬进库里**并改索引，
                   #     同样不能在别的写流程中途插进来。
                   "scan", "upload-commit"}


@dataclass
class JobState:
    id: str
    kind: str
    state: str = "running"          # running | paused | done | error | cancelled
    total: int = 0
    done: int = 0
    message: str = ""
    current: str = ""
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cancel: bool = False
    # 这个任务**实际**占不占独占位。见 `to_dict()` 里的说明：
    # 不能按 kind 重新猜 —— `EXCLUSIVE_KINDS` 里的 kind 也可以用
    # `exclusive=False` 建（比如只读的方案计算）。
    exclusive: bool = False
    # ★ 本任务持有的跨进程写锁 token。释放时必须用它，
    #   否则 `proclock.release()` 会去读全局 `_token` ——
    #   那是**当前正在跑的新任务**的锁，等于把别人的锁拔掉。
    lock_token: str = ""
    # 暂停：worker 每步检查一次。和"取消"分开 ——
    # 取消是**不做了**，暂停是**先停一下**，之后还能接着跑。
    pause: bool = False
    # ---- 多页面相关 ----
    # 谁发起的（页面实例 id）。别的页面看到不是自己的任务就知道"隔壁在忙"。
    owner: str = ""
    # 最后一次心跳。用来判断任务是不是已经卡死/进程没了。
    heartbeat: float = field(default_factory=time.time)
    # 被跳过的条目（超时 / 出错但继续），供"哪些要重试"用
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    # 结果被读过没有：前端刷新过状态才算"收尾完成"
    acked: bool = False
    # 崩溃恢复用的原始步骤（只在需要时可重放的操作上填）
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def touch(self) -> None:
        self.heartbeat = time.time()

    def is_stale(self, now: Optional[float] = None) -> bool:
        # 暂停中的任务**不算卡死** —— 它是用户主动停的，
        # 心跳本来就该停，否则会被"卡死回收"误判成中断。
        if self.state not in ("running",):
            return False
        return ((now or time.time()) - self.heartbeat) > STALE_JOB_SECONDS

    def to_dict(self) -> Dict[str, Any]:
        pct = round(100.0 * self.done / self.total, 1) if self.total else None
        elapsed = (self.finished or time.time()) - self.started
        rate = (self.done / elapsed) if elapsed > 0.3 and self.done else None
        eta = None
        if rate and self.total and self.done < self.total:
            eta = round((self.total - self.done) / rate, 1)
        return {
            "id": self.id, "kind": self.kind, "state": self.state,
            "total": self.total, "done": self.done, "percent": pct,
            "message": self.message, "current": self.current,
            "started": self.started, "finished": self.finished,
            "elapsed": round(elapsed, 1), "rate": round(rate, 2) if rate else None,
            "eta": eta, "result": self.result, "error": self.error,
            "owner": self.owner,
            "heartbeat": self.heartbeat,
            "idle": round(time.time() - self.heartbeat, 1),
            "stale": self.is_stale(),
            "skipped_count": len(self.skipped),
            # ★ 用**建任务时实际生效的**标记，而不是按 kind 重新猜一遍。
            #   原来这里写 `self.kind in EXCLUSIVE_KINDS` —— 于是
            #   `run_in_background("organize", …, exclusive=False)`（只读的
            #   方案计算，确实没占独占位）在界面和日志里**被报成独占**，
            #   看起来像"生成方案把写操作全挡住了"，排查时完全被带偏。
            "exclusive": bool(self.exclusive),
            "acked": self.acked,
            "paused": bool(self.pause),
            # 能不能暂停/取消：只有还在跑的才有意义
            "can_pause": self.state == "running",
            "can_cancel": self.state == "running",
            # 能不能恢复：暂停中的可以，中断/出错的也能（重放未完成部分）
            "can_resume": self.state == "paused",
            "recoverable": bool(self.steps) and self.state in
                           ("error", "paused", "cancelled"),
            "step_count": len(self.steps),
        }


class JobBusy(RuntimeError):
    """已经有独占任务在跑。带上占用者的信息，方便界面说清"谁在忙"。"""

    def __init__(self, job: JobState):
        self.job = job
        super().__init__(
            f"已有任务在执行：{job.kind}（{job.done}/{job.total or '?'}）"
            f"{'（' + job.message + '）' if job.message else ''}。"
            f"请等它结束后再操作 —— 同时执行多个流程会基于过期状态操作文件。"
        )


class Jobs:
    def __init__(self, db=None, keep: int = 200):
        # keep 从 40 提到 200：崩溃恢复要靠**上一次运行**留下的记录，
        # 留得太少的话，用户跑了一堆任务再崩溃，那条关键记录可能已经被挤掉了。
        self._jobs: Dict[str, JobState] = {}
        self._order: List[str] = []
        self._lock = threading.RLock()
        self.db = db
        self.keep = keep

    def create(self, kind: str, total: int = 0, owner: str = "",
               exclusive: Optional[bool] = None) -> JobState:
        """建一个任务。独占任务会先检查有没有别人在跑。

        `exclusive=None` 时按 kind 自动判定（EXCLUSIVE_KINDS 里的都独占）。
        已经有独占任务 → 抛 `JobBusy`，**不排队**（排队在文件操作里是危险的）。
        """
        want_excl = (kind in EXCLUSIVE_KINDS) if exclusive is None else bool(exclusive)
        jid = uuid.uuid4().hex[:12]
        st = JobState(id=jid, kind=kind, total=total, owner=owner,
                      exclusive=want_excl)
        # ★★ 「检查独占位」和「登记新任务」必须在**同一把锁**里。★★
        #
        #   原来分两次拿锁：先查 busy（释放锁）→ 再插入（重新拿锁）。
        #   中间那段空档里两个线程都能看到"没人占位"，于是**两个写任务
        #   同时拿到独占位** —— 而独占位的全部意义就是"同一时间只有一个
        #   写流程"。它们会同时改同一批文件，基于彼此过期的状态。
        #
        #   现在整段锁住：要么都成功一个，要么第二个看到第一个已经插入。
        with self._lock:
            self._reap_stale_locked()
            if want_excl:
                busy = self._busy_locked()
                if busy is not None:
                    raise JobBusy(busy)
                # ★ 跨进程独占：内存锁只管本进程。两个实例（或测试包 + 正常版）
                #   共享同一个数据目录时，各自的 _jobs 都是空的，
                #   于是**都认为自己独占**，同时改同一批文件 ——
                #   实测后果是两边都报 done，但目标只剩后写的那份。
                try:
                    st.lock_token = proclock.acquire()
                except proclock.LockBusy as exc:
                    raise JobBusy(JobState(id="proclock", kind="另一个进程",
                                           total=0, exclusive=True)) from exc
            self._jobs[jid] = st
            self._order.append(jid)
            while len(self._order) > self.keep:
                old = self._order.pop(0)
                self._jobs.pop(old, None)
        if self.db is not None:
            try:
                self.db.create_job(jid, kind, total)
            except Exception:
                pass
        return st

    def _busy_locked(self) -> Optional[JobState]:
        """当前占着独占位的任务（调用方要持有锁）。

        暂停中的任务**也算占用** —— 它随时可能继续，
        这时候让别的流程插进来，两边操作同一批文件就冲突了。

        ★ 判据是 **`st.exclusive`**（建任务时实际生效的那个），
        不是"kind 在不在 EXCLUSIVE_KINDS 里"。

        这个区别是实打实的 bug：`run_in_background` 一律用
        `exclusive=False` 建任务（它跑的是**只读计算**，本来就不该挡写操作），
        但这里按 kind 判断 —— 于是 `run_in_background("organize", …)`
        一边"没占独占位"、一边**真的把写操作全挡住了**，
        整整 6 秒（实测）里 `/api/tags/write` 之类的接口全返回 409。
        建任务时那次 `_busy_locked()` 检查反而是对的（用的是 want_excl），
        两处判据不一致，才会出现这种"看起来没占、实际占着"的怪状态。
        """
        for jid in reversed(self._order):
            st = self._jobs.get(jid)
            if st is not None and st.state in ("running", "paused") \
                    and st.exclusive:
                return st
        return None

    def _reap_stale_locked(self) -> None:
        """把心跳停了的任务标成"已中断"，释放独占位。

        没有这一步，一个被 kill 的任务会永久占着独占位，
        之后所有操作都提示"已有任务在执行"，而界面上看不到任何进度 ——
        等于把用户锁死了。
        """
        now = time.time()
        for st in self._jobs.values():
            if st.is_stale(now):
                st.state = "error"
                st.error = ("任务心跳中断，已判定为中断"
                            f"（超过 {int(STALE_JOB_SECONDS)} 秒没有进展）")
                st.finished = now
                st.message = "已中断"
                # ★ 必须落库。
                #   原来只改了内存里的 JobState —— 进程重启后从 DB 恢复时，
                #   这条任务仍然是 `running`，界面显示"还在跑"，
                #   而它其实永远不会再动了（用户只能干等或手动取消）。
                #   `finish()` 里做的就是这件事，这里照做一遍。
                if self.db is not None:
                    try:
                        # ★ 注意 `job` 表**没有** error 列，只有
                        #   detail/done/finished/id/kind/message/started/state/total。
                        #   传 `error=` 会 OperationalError，而下面那个 except
                        #   一吞，就变成"看着落库了、其实没有" ——
                        #   这正是这一条待办原本要修的东西，差点用同样的方式再犯一次。
                        #   中断原因写进 message（它是有列的那个）。
                        self.db.update_job(st.id, state=st.state, done=st.done,
                                           total=st.total, message=st.error,
                                           finished=st.finished)
                    except Exception as exc:                      # noqa: BLE001
                        # 不静默：落库失败意味着重启后这条任务会显示成"还在跑"
                        log.warning("陈旧任务落库失败 %s: %s: %s",
                                    st.id, type(exc).__name__, exc)

    def busy(self) -> Optional[Dict[str, Any]]:
        """给别人看的：现在有没有独占任务在跑。"""
        with self._lock:
            self._reap_stale_locked()
            b = self._busy_locked()
            return b.to_dict() if b else None

    def ensure_free(self) -> None:
        """有独占任务在跑就抛 `JobBusy`，否则直接返回。

        给"**只读但需要一致快照**"的操作用 —— 典型是方案预演：
        它不占独占位（不该挡别人），但也不能在别人写文件的时候算，
        否则算出来的方案基于正在变动的状态。
        """
        with self._lock:
            self._reap_stale_locked()
            b = self._busy_locked()
        if b is not None:
            raise JobBusy(b)

    def heartbeat(self, jid: str) -> bool:
        st = self.get(jid)
        if st is None:
            return False
        st.touch()
        return True

    def dismiss(self, jid: str) -> bool:
        """把一条**已结束**任务从记录里删掉（内存 + DB）。

        为什么需要：崩溃恢复出来的"已中断"任务（state=error）原来只有
        "接着做完"一个动作。用户不想做、或者早就在别处处理过了，
        它就会永远挂在操作记录里，重启还会回来 —— 因为 DB 里仍是
        running/paused，下次启动又被 load_unfinished 读出来。
        """
        st = self.get(jid)
        if st is not None and st.state in ("running", "paused"):
            return False                      # 正在跑的不能忽略
        with self._lock:
            self._jobs.pop(jid, None)
            if jid in self._order:
                self._order.remove(jid)
        if self.db is not None:
            try:
                self.db.delete_job(jid)
            except Exception:                                  # noqa: BLE001
                pass
        return True

    def dismiss_finished(self) -> int:
        """一键清掉所有已结束（done/error/cancelled）的任务记录。"""
        n = 0
        with self._lock:
            jids = [j for j, st in self._jobs.items()
                    if st.state in ("done", "error", "cancelled")]
        for jid in jids:
            if self.dismiss(jid):
                n += 1
        return n

    def ack(self, jid: str) -> bool:
        """标记结果已被前端读取（收尾完成，可以开始下一个操作）。"""
        st = self.get(jid)
        if st is None or st.state == "running":
            return False
        st.acked = True
        return True

    def skip(self, st: JobState, item: Dict[str, Any]) -> None:
        """记下一个被跳过的条目（超时 / 单点出错但整批继续）。"""
        with self._lock:
            st.skipped.append(item)
            if len(st.skipped) > 500:
                del st.skipped[:-500]

    def get(self, jid: str) -> Optional[JobState]:
        with self._lock:
            return self._jobs.get(jid)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._reap_stale_locked()
            return [self._jobs[j].to_dict() for j in reversed(self._order) if j in self._jobs]

    def active(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._reap_stale_locked()
            return [j.to_dict() for j in self._jobs.values() if j.state == "running"]

    def cancel(self, jid: str) -> bool:
        st = self.get(jid)
        if st and st.state in ("running", "paused"):
            st.cancel = True
            st.pause = False          # 取消优先：卡在暂停里的也要能撤
            st.message = "正在取消…"
            st.touch()
            # ★ 线程已经退出（finished 有值）或正卡在暂停里 → 没有实体会再调
            #   finish()，这里必须直接收尾，否则状态永远停在 running。
            if st.finished is not None or st.state == "paused":
                self.finish(st)
            return True
        return False

    def pause(self, jid: str) -> bool:
        """暂停：worker 在下一个检查点停下，**已经做完的不会回退**。

        暂停不等于取消 —— 恢复后从当前位置继续。
        """
        st = self.get(jid)
        if st and st.state == "running":
            st.pause = True
            st.message = "已暂停（可以继续）"
            st.touch()
            return True
        return False

    def resume(self, jid: str) -> bool:
        st = self.get(jid)
        if st and st.pause:
            st.pause = False
            # ★ `finished` 有值 = 工作线程已经退出了。
            #   这时把状态改回 running 就是造一个**没有线程的幽灵任务**，
            #   之后 cancel() 只挂标志位、没人再调 finish()，独占锁永久泄漏。
            if st.finished is not None:
                st.message = "任务已结束，无法继续"
                st.touch()
                return False
            if st.state == "paused":
                st.state = "running"
            st.message = "继续中…"
            st.touch()
            return True
        return False

    def wait_if_paused(self, st: JobState, timeout: Optional[float] = None) -> None:
        """worker 在每个检查点调用：暂停时就在这里等。

        注意保持心跳：暂停期间 `is_stale` 不判卡死，但界面上的
        "空闲多久"还是要有意义，所以这里照样 touch。

        ★ 默认**不超时**。原来默认 `timeout=3600.0` —— 暂停超过一小时后
          worker 会自己接着干，而界面上仍然显示"已暂停"：用户以为文件
          没被动，实际在改。暂停是用户的明确意图，**只有取消能打破它**。
          `timeout` 参数保留，仅供测试显式传入。
        """
        waited = 0.0
        while st.pause and not st.cancel:
            st.state = "paused"
            st.touch()
            time.sleep(0.2)
            waited += 0.2
            if timeout is not None and waited >= timeout:
                break
        if st.state == "paused" and not st.pause:
            st.state = "running"
            st.touch()

    # ---------------------------------------------------------- 崩溃恢复

    def record_steps(self, st: JobState, steps: List[Dict[str, Any]]) -> None:
        """把原始步骤记在任务上，供"程序异常退出后接着做完"。

        只记**可以安全重放**的操作：`skip` 策略下重放已经完成的移动
        会因为源文件不存在而被跳过（Executor 对缺源就是跳过），
        所以重放不会破坏数据 —— 这是能安全恢复的前提。

        同时落库：只放内存的话，进程一挂就没了，
        重启后用户既看不到"上次没做完"，也没法接着做完。
        """
        with self._lock:
            st.steps = list(steps or [])
        self.persist_steps(st, st.steps)

    def unfinished(self) -> List[Dict[str, Any]]:
        """上次运行**没跑完**的任务（程序异常退出留下的）。

        判据：状态是 running/paused，但心跳早就停了 —— 说明进程没了，
        不是真的还在跑。
        """
        now = time.time()
        out = []
        with self._lock:
            for st in self._jobs.values():
                if st.state in ("running", "paused") and \
                        (now - st.heartbeat) > STALE_JOB_SECONDS:
                    out.append(st)
        return [s.to_dict() for s in out]

    def mark_interrupted(self) -> int:
        """启动时调用：把上次留下的"运行中"任务标成中断。

        不这么做的话，重启后界面上会出现一个永远不动、也无法清理的
        "运行中"任务，还会一直占着独占位。
        """
        n = 0
        with self._lock:
            for st in self._jobs.values():
                if st.state in ("running", "paused"):
                    st.state = "error"
                    st.error = "程序上次异常退出，这个任务没有完成"
                    st.message = "已中断"
                    st.finished = time.time()
                    n += 1
        return n

    def load_unfinished(self) -> int:
        """启动时从数据库把上次**没做完**的运行恢复出来。

        为什么必须做：任务状态原来只活在内存里，程序一挂（崩溃 / 被
        任务管理器结束 / 断电）就什么都没了 —— 用户重启后既看不到
        "上次没做完"，也没法接着做完，只能自己回忆改了哪些文件。

        这里把 DB 里 state 仍是 running/paused 的记录读回来，标成
        "已中断"，并带上原始步骤，于是界面可以提示"要不要接着做完"。
        """
        if self.db is None:
            return 0
        n = 0
        try:
            rows = self.db.list_jobs(limit=self.keep)
        except Exception:
            return 0
        with self._lock:
            for r in rows:
                try:
                    jid = r["id"]
                    state = r["state"]
                except Exception:
                    continue
                if jid in self._jobs:
                    continue
                if state not in ("running", "paused"):
                    continue
                st = JobState(
                    id=jid, kind=(r["kind"] or "sync"), state="error",
                    total=int(r["total"] or 0), done=int(r["done"] or 0),
                    error="程序上次异常退出，这个任务没有完成",
                    message="已中断（可以尝试接着做完）",
                    started=float(r["started"] or time.time()),
                    finished=time.time(),
                    steps=[],
                    # `exclusive` 没落库，只能按 kind 推。
                    # 这是**唯一**允许按 kind 推的地方 —— 建任务时那个真实值
                    # 随着进程一起没了。按 kind 推对绝大多数情况是对的
                    # （独占任务是写操作，kind 在 EXCLUSIVE_KINDS 里）；
                    # 只在"用 exclusive=False 建的只读任务恰好 kind 也在表里"
                    # 时偏保守（报成独占），而恢复出来的任务是 error 状态、
                    # 本来就不占位，所以偏保守没有实际代价。
                    exclusive=((r["kind"] or "sync") in EXCLUSIVE_KINDS),
                )
                # 步骤存在 detail 里（update_job 的 detail 字段）
                try:
                    raw = r["detail"] or ""
                    if raw.startswith("[") or raw.startswith("{"):
                        import json as _json
                        obj = _json.loads(raw)
                        if isinstance(obj, dict) and isinstance(obj.get("steps"), list):
                            st.steps = obj["steps"]
                        elif isinstance(obj, list):
                            st.steps = obj
                except Exception:
                    pass
                self._jobs[jid] = st
                self._order.append(jid)
                n += 1
        return n

    def persist_steps(self, st: JobState, steps: List[Dict[str, Any]]) -> None:
        """把步骤写进数据库 —— 崩溃后才能恢复。

        用 `detail` 字段存 JSON（它就是给"任务明细"用的自由字段）。
        """
        if self.db is None:
            return
        import json as _json
        try:
            self.db.update_job(st.id, detail=_json.dumps(
                {"steps": steps, "recoverable": True}, ensure_ascii=False)[:200000])
        except Exception as exc:
            # 落库失败 → 重启后这条任务没有可恢复的步骤，用户点"接着做完"会没反应。
            log.warning("保存可恢复步骤失败（崩溃后无法续做）: %s", exc)

    def finish(self, st: JobState, result: Optional[Dict[str, Any]] = None,
               error: str = "") -> None:
        # 释放跨进程写锁（只有独占任务持有它）
        # ★ 只释放**自己那个 token**，并且释放后立刻清空 —— 二次 finish
        #   （被取消的工作线程醒来后又调一次）就不会再去拔新任务的锁。
        if getattr(st, "exclusive", False):
            tok = getattr(st, "lock_token", "")
            try:
                if tok:
                    proclock.release(tok)
                elif proclock.held_by_us():
                    # ★ 兜底：有些 JobState 是**手工构造**的（没走 create），
                    #   没有 token。只有本进程确实持锁时才释放，避免拔别人的锁。
                    proclock.release()
            except Exception:                          # noqa: BLE001
                pass
            finally:
                st.lock_token = ""
        st.finished = time.time()
        st.touch()
        # 用户主动取消 ≠ 出错：分开报，界面上别把"我取消了"显示成失败
        # ★ 真正做完的任务永远记 done，**哪怕期间被暂停过**。
        #   否则会出现："工作线程已经退出了，状态却是 paused" ——
        #   接着 resume() 把它改成 running，然后再 cancel() 只挂标志位，
        #   没有任何线程会再调 finish()：任务永久卡在 running、
        #   独占写锁（proclock）永不释放，全系统写接口全 409。
        _complete = (st.total > 0 and st.done >= st.total)
        if error:
            st.state = "error"
        elif st.cancel:
            st.state = "cancelled"
            st.message = st.message or "已取消"
        elif _complete:
            st.state = "done"
        elif st.pause:
            # 暂停收尾：只有**还剩活没干完**才标 paused。
            st.state = "paused"
            st.message = st.message or "已暂停（可以继续）"
        else:
            st.state = "done"
        st.error = error
        if result:
            st.result = result
        if self.db is not None:
            try:
                self.db.update_job(st.id, state=st.state, done=st.done, total=st.total,
                                   message=st.message, finished=st.finished, detail=str(st.result)[:4000])
            except Exception:
                pass


jobs: Optional[Jobs] = None


def _adopt(db) -> Jobs:
    """让注册表用上 `db`，并尽量**保住已有任务**。

    三条规则，都是踩出来的：

    · 还没有注册表 → 建一个。
    · 有注册表但它的 `db` 是 `None` → **只把 db 装上，不重建**。
      这是最关键的一条：`get_jobs()` 不带参数被调到时（`guard.py` 的
      独占检查、`scanner.py` 兜底、任何只读路径）会惰性建出 `db=None`
      的注册表；随后 `main.db()` 第一次建库时如果把它当成"换了库"重建，
      **正在跑的任务就凭空消失了** —— 列表空了、`/api/busy` 说没人忙，
      可任务还在跑。实测表现正是"扫描任务建好 0.3 秒后从列表里消失"。
    · `db` 指向**另一个库** → 重建（新库不该背旧库的任务）。
    """
    global jobs
    if db is None:
        return get_jobs()
    if jobs is None:
        jobs = Jobs(db)
    elif jobs.db is None:
        jobs.db = db                      # 装上，不重建
    elif _same_db(jobs.db, db):
        pass                              # 同一个库，什么都不用做
    elif jobs.busy() is not None:
        # ★ 安全网：**绝不为了换库而丢掉正在跑的任务**。
        #   正常情况下 rebind 都发生在建库那一刻（那时还没有任务），
        #   但万一有谁在任务运行中换了库，宁可让注册表暂时绑旧库
        #   （最多是任务记录写不进新库），也不能让"正在移动文件的任务"
        #   从界面上消失 —— 那样用户既看不到进度、也没法取消它。
        pass
    else:
        jobs = Jobs(db)                   # 真的换库了，且没有任务在跑
    return jobs


def init(db) -> Jobs:
    """初始化任务中心。幂等：同一个库（或尚未绑定库）时**保住已有任务**。"""
    return _adopt(db)


def rebind(db) -> None:
    """把注册表对齐到当前的数据库（`main.db()` 每次解析完都调它）。

    存在的理由：调用方（尤其是测试）换库的方式是改 `MSH_DB` 再让
    `main.DB = None`，但**没人会记得同时重置 `core.jobs` 的单例**。
    结果就是某个测试类忘了重置之后，后面所有类建的任务都进了
    **第一个测试那个库的注册表**，而那个库的连接早关了 ——
    表现是"任务建好就从列表里消失"，报错点离真正原因非常远。

    让它跟着 `main.db()` 走，就没有"记得重置"这个要求了。
    """
    _adopt(db)


def _same_db(a, b) -> bool:
    """两个 Database 是不是同一个（同一路径 / 同一对象）。"""
    if a is b:
        return True
    if a is None or b is None:
        return False
    try:
        return os.path.abspath(str(getattr(a, "path", ""))) == \
            os.path.abspath(str(getattr(b, "path", "")))
    except Exception:
        return False


def get_jobs(db=None) -> Jobs:
    """获取任务中心。

    不带参数调用时会惰性建出 `db=None` 的注册表；随后 `main.db()` 第一次
    建库时由 `_adopt` **把库装上而不是重建**，所以不会丢任务。
    """
    global jobs
    if jobs is None:
        jobs = Jobs(db)
    return jobs


def reset_for_tests() -> None:
    """丢掉注册表单例（只给测试用）。

    换库的测试类应当在 setUp / tearDown 里调它。因为**同一个进程里
    换 `MSH_DB` 是个不寻常的动作**：正常使用（一个进程一个库）永远不需要，
    所以不把它做成自动行为 —— 自动换库反而会引入"任务跑着跑着注册表被换掉"
    这类更难查的问题（见 `_adopt` 的说明）。
    """
    global jobs
    jobs = None
