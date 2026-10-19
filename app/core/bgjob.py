"""后台任务运行器：把"慢的只读计算"从同步请求里挪到后台线程。

为什么需要：查重 / 对比这类操作原来直接跑在 HTTP 请求里 ——
大库上要几十秒，期间：
  · 界面只有一个转圈，**不知道进度**，用户以为卡死就刷新甚至强杀；
  · **没法取消**：请求已经发出去了，关掉页面后端照样在跑；
  · 浏览器/代理还有超时，跑到一半可能被切断。

包成后台任务后：有进度、可取消、可暂停、能跨页面/刷新存活。

和 `Executor` 的分工：`Executor` 是"会改文件"的执行器（要独占、
要记步骤、要崩溃恢复）；这里是**只读计算**，不占独占位，
也不写任何文件，所以可以放心并行。
"""
from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable, Dict, Optional

from .jobs import JobState, get_jobs

log = logging.getLogger("msh.bgjob")


def run_in_background(kind: str, total: int, work: Callable[[JobState], Any],
                      on_done: Optional[Callable[[JobState, Any], None]] = None,
                      owner: str = "") -> JobState:
    """把 `work(job)` 丢到后台线程跑。

    `work` 里应该：
      · 定期设 `job.done` / `job.message` / `job.current`（进度）
      · 定期检查 `job.cancel` 并尽早返回（可取消）
      · 调 `job.touch()` 或直接写 message（心跳，防误判卡死）

    只读计算**不占独占位**（`exclusive=False`），
    所以用户可以在扫描的同时查看重结果。
    """
    job = get_jobs().create(kind, total=total, owner=owner, exclusive=False)

    def runner() -> None:
        try:
            out = work(job)
            if job.cancel:
                get_jobs().finish(job, error="")
            else:
                get_jobs().finish(job)
                job.result = out if isinstance(out, dict) else {"value": out}
            if on_done is not None:
                try:
                    on_done(job, out)
                except Exception:
                    pass
        except Exception as exc:                       # noqa: BLE001
            job.error = f"{type(exc).__name__}: {exc}"
            tb_txt = traceback.format_exc()
            # ★ 后台任务失败也要进日志文件。
            #   以前只塞进 job.error —— 用户看到的是界面上"任务失败"，
            #   但"为什么失败"（堆栈）只在那一刻的内存里，
            #   关掉页面就再也找不回来了。
            try:
                log.error("后台任务 %s(%s) 失败: %s\n%s",
                          kind, job.id, job.error, tb_txt)
            except Exception:
                pass
            try:
                job.result = {"traceback": tb_txt[:4000]}
            except Exception:
                pass
            get_jobs().finish(job, error=job.error)

    t = threading.Thread(target=runner, name=f"bg-{kind}-{job.id}",
                         daemon=True)
    t.start()
    return job


def cached_result(job: JobState) -> Optional[Dict[str, Any]]:
    """任务完成后取结果（前端轮询用）。"""
    if job.state == "running":
        return None
    if job.state == "error":
        return {"error": job.error or "任务失败"}
    return job.result or {}
