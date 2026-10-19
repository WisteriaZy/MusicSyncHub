"""服务端独占守卫：保证**同一时刻只有一个写流程**在改文件。

放在独立模块里而不是 `main.py`，是为了避免循环导入：
各个 router（`folders_api` / `station_api` / `upload_api` / `tags_api` /
`search_api`）都要用它，而 `main.py` 又要 import 这些 router。
`main.py` 反过来 import 本模块没有环，因为这里只依赖 `core.jobs`。

为什么需要服务端这一层：
前端的 `runExclusive` 是**体验**（提前禁用按钮、跨页面提示），
而这里是**约束**。任何客户端 —— 另一个标签页、脚本、curl、重放请求 ——
都能绕过前端。两个写流程同时跑会**基于过期的文件状态操作**，
这正是必须禁止的。
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from .jobs import get_jobs

__all__ = ["exclusive_guard", "exclusive_slot", "current_busy"]


def current_busy() -> dict | None:
    return get_jobs().busy()


def exclusive_guard(x_msh_client: str = Header(default="", alias="X-MSH-Client"),
                    x_msh_job: str = Header(default="", alias="X-MSH-Job")) -> None:
    """挂到每个会改文件的端点上。

    例外只有一种：**正在执行的那个任务自己**回来做后续调用
    （带 `X-MSH-Job: <自己的 id>`）。否则任务跑到一半会被自己挡住。
    """
    jb = current_busy()
    if jb is None:
        return
    if x_msh_job and str(jb.get("id")) == x_msh_job:
        return                       # 是当前任务自己
    kind = jb.get("kind") or "任务"
    done = jb.get("done")
    total = jb.get("total")
    prog = f"（{done}/{total}）" if total else ""
    raise HTTPException(
        409,
        f"已有任务在执行：{kind}{prog}。"
        f"同时运行两个写操作会基于过期的文件状态改动文件 —— "
        f"请等它结束，或在任务中心取消它。")


def exclusive_slot(kind: str):
    """返回一个**真正占位**的独占依赖（请求期间持有，响应后释放）。

    ★ 为什么需要它（真实漏洞，外部审计定为 Critical）：

      `exclusive_guard` 只做一件事 —— 进门时看一眼 `busy()` 是不是空的。
      **它自己不占位。** 而 `station_api` / `tags_api` / `sources/folders_api`
      的写端点通篇没有一处 `get_jobs().create()`，于是：

        · 用户批量改 500 首标签（可能几十秒）→ 期间 busy() 仍是 None
        · 另一个页面/定时扫描可以合法开始扫描、重命名、传输
        · 两个写流程同时基于**过期的文件状态**改同一批文件

      这就是"假守卫"：守卫的函数名和历史注释都写着"同一时刻只有一个写流程"，
      但实际没有任何互斥。

    用法（每个端点只改一行，不用重排函数体）：

        def tags_write(..., job=Depends(exclusive_slot("tags"))):
            ...

    实现要点：
      · 用 FastAPI 的 `yield` 依赖 —— 请求进来时建任务，响应结束后自动 finish，
        业务函数体一行都不用动；
      · `kind` 必须在 `EXCLUSIVE_KINDS` 里（`create()` 会据此自动独占）；
      · 保留 `exclusive_guard` 原有的"同一个任务自己回调"例外
        （带 `X-MSH-Job: <自己的 id>`），否则任务跑到一半会被自己挡住；
      · 端点抛异常时把任务记成 error，不要留一个永远 running 的僵尸。
    """

    def _dep(x_msh_client: str = Header(default="", alias="X-MSH-Client"),
             x_msh_job: str = Header(default="", alias="X-MSH-Job")):
        jb = current_busy()
        if jb is not None:
            if x_msh_job and str(jb.get("id")) == x_msh_job:
                yield None          # 是当前任务自己回来的后续调用，放行但不重复占位
                return
            kind_label = jb.get("kind") or "任务"
            done, total = jb.get("done"), jb.get("total")
            prog = f"（{done}/{total}）" if total else ""
            raise HTTPException(
                409,
                f"已有任务在执行：{kind_label}{prog}。"
                f"同时运行两个写操作会基于过期的文件状态改动文件 —— "
                f"请等它结束，或在任务中心取消它。")
        job = get_jobs().create(kind, owner=x_msh_client)
        try:
            yield job
        except BaseException as exc:                      # noqa: BLE001
            try:
                get_jobs().finish(job, error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            raise
        else:
            try:
                get_jobs().finish(job)
            except Exception:
                pass

    # 给依赖函数一个可识别的名字：`test_audit_fixes` 里的路由表检查
    # 靠 `dep.call.__name__` 判断端点有没有挂守卫。
    _dep.__name__ = f"exclusive_slot[{kind}]"
    return _dep
