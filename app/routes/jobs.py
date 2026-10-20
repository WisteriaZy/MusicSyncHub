"""jobs.py —— 见 `app/main.py` 顶部导航索引。

路由函数体与拆分前**完全一致**，只是从 `@app.` 换成了 `@router.`。
共享 helper 在 `app/deps.py`。
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Body, Depends, Header, HTTPException,
                     Query, Request, Response)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)


from ..core import audio as A
from ..core import device as DEV
from ..core import matching as M
from ..core import paths as P
from ..core import plan as PLAN
from ..core import scanner as S
from ..core.config import (AppConfig, Settings, SourceConfig, SourceNotFound,
                          data_dir, default_db_path, get_secret, load_config,
                          resolve_source, save_config, save_secret, test_mode)
from ..core.db import Database
from ..core.jobs import JobBusy, get_jobs, init as init_jobs, rebind as rebind_jobs
from ..core.ops import Executor
from ..sources import available_types, manager
from ..sources.base import ReadOnlyError, SourceError

log = logging.getLogger("msh")

from ..core.guard import exclusive_guard

from ..deps import (
    _browse_cache_clear,
    _connect_tip,
    _content_clusters,
    _db_size,
    _device_view,
    _dir_summary,
    _disk_view,
    _folder_map,
    _folder_rows,
    _hash_groups,
    _reindex_after_transfer,
    _serve_cover_bytes,
    _source_view,
    _stable_hash,
    _step_fingerprint,
    _steps_fingerprint,
    _track_view,
    _tracks_capped,
    _tracks_for,
    _validate_source,
    _wanted_db_path,
    db,
    require_writable,
)


router = APIRouter()

@router.get("/api/jobs")
def list_jobs() -> List[Dict[str, Any]]:
    return get_jobs().list()


@router.get("/api/busy")
def busy_state() -> Dict[str, Any]:
    """当前有没有独占任务在跑。

    多页面用：每个页面轮询这个，就能知道"隔壁页面在忙什么"，
    从而**禁用会冲突的操作**，而不是等提交了才被 409 顶回来。
    """
    b = get_jobs().busy()
    return {"busy": b is not None, "job": b}


@router.post("/api/jobs/{jid}/ack")
def ack_job(jid: str) -> Dict[str, Any]:
    """标记任务结果已被读取。

    前端在任务结束后必须走一次：这样"一次只允许一个流程"才有明确的
    收尾点 —— 不允许堆积任务，是因为堆积的任务会基于**已经变化**的
    文件状态执行，容易出错。
    """
    return {"ok": get_jobs().ack(jid)}


@router.post("/api/jobs/{jid}/dismiss")
def dismiss_job(jid: str) -> Dict[str, Any]:
    """把一条已结束的任务从记录里删掉（"忽略"）。运行中的任务不允许。"""
    return {"ok": get_jobs().dismiss(jid)}


@router.post("/api/jobs/clear-finished")
def clear_finished_jobs() -> Dict[str, Any]:
    """一键清理所有已结束（完成/失败/取消）的任务记录。"""
    return {"removed": get_jobs().dismiss_finished()}


@router.get("/api/jobs/{jid}")
def get_job(jid: str) -> Dict[str, Any]:
    j = get_jobs().get(jid)
    if j is None:
        raise HTTPException(404, "任务不存在")
    return j.to_dict()


@router.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str) -> Dict[str, Any]:
    ok = get_jobs().cancel(jid)
    return {"ok": ok}


@router.post("/api/jobs/{jid}/pause")
def pause_job(jid: str) -> Dict[str, Any]:
    """暂停任务。已完成的步骤不动，恢复后从下一个未处理的继续。"""
    ok = get_jobs().pause(jid)
    return {"ok": ok, "job": (get_jobs().get(jid).to_dict()
                              if get_jobs().get(jid) else None)}


@router.post("/api/jobs/{jid}/resume")
def resume_job(jid: str) -> Dict[str, Any]:
    ok = get_jobs().resume(jid)
    return {"ok": ok, "job": (get_jobs().get(jid).to_dict()
                             if get_jobs().get(jid) else None)}


@router.get("/api/unfinished")
def unfinished_jobs() -> Dict[str, Any]:
    """上次运行没做完的任务（程序异常退出留下的）。

    前端启动时读这个，提示用户"要不要接着做完" —— 而不是让他
    自己回忆上一轮改了哪些文件。
    """
    lst = get_jobs().unfinished()
    recoverable = [j for j in lst if j.get("recoverable")]
    return {"count": len(lst), "recoverable": len(recoverable), "jobs": lst}


@router.post("/api/unfinished/recover")
def recover_job(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """接着做完一个被中断的任务。

    **安全前提**：重放用的是 `skip` 策略 —— 已经搬完的文件会因为
    源不存在而被跳过，目标已存在的也会被跳过，所以重放不会覆盖、
    不会重复，最多是"白跑一趟"。
    """
    jid = str(payload.get("job") or "")
    st = get_jobs().get(jid)
    if st is None:
        raise HTTPException(404, "任务不存在")
    steps = list(st.steps or [])
    if not steps:
        raise HTTPException(400, "这个任务没有留下可重放的步骤，无法自动恢复")
    if st.state in ("running", "paused"):
        raise HTTPException(409, "这个任务还在进行中，不需要恢复")
    sources: Dict[str, Any] = {}
    for s in steps:
        for key in ("src", "dst"):
            sid = s.get(key)
            if sid and sid not in sources:
                sources[sid] = manager.get(sid)
    try:
        job = get_jobs().create("sync", total=len(steps),
                                owner=str(payload.get("client") or ""))
    except JobBusy as exc:
        raise HTTPException(409, str(exc))
    ex = Executor(sources, db(), default_policy="skip")
    try:
        r = ex.run(steps, execute=True, policy="skip", verify=True,
                   verify_hash=False, hard_delete=False,
                   workers=int(payload.get("workers") or 3), job=job)
    finally:
        if job.state == "running":
            get_jobs().finish(job)
    # 恢复完成后把旧的"中断"记录标成已处理，免得反复提示。
    # ★ 必须落库：库里那行还是 running，只改内存的话下次启动又会弹一遍。
    get_jobs().settle_interrupted(st, "已接着做完（见新任务）")
    return {"ok": True, "job": job.to_dict(), "result": r}
