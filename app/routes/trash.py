"""回收站：浏览 / 还原 / 彻底删除。

## 为什么要有这一页

界面上一直写着"删除 = 移入回收站，**可恢复**"，但应用里从来没有还原入口 ——
用户得自己打开资源管理器，翻到 `<库根>/.music-sync-trash/<日期>/`，
还得记得它原来在哪个目录。**承诺了可恢复，却没给恢复的办法**，这是安全闭环上
唯一的缺口。

现在：
  · 删除时把"原相对路径"记进回收站目录里的 `_origin.json`（跟着回收站走，
    索引库重建也不丢）；
  · 这里按日期列出回收站内容，能一键还原回原位；
  · 老回收站没有 `_origin.json` 也能列出，还原时落回原目录或你指定的目录；
  · 还原**绝不覆盖**：默认跳过，可改名，选覆盖则先把现有文件移进回收站。

只支持本地/映射盘数据源：网络源的 `remove()` 语义由各自适配器决定，
那边没有统一的回收站约定。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..core.guard import exclusive_slot
from ..deps import (as_dicts, as_text, db, manager, refresh_tracks,
                    require_writable, resolve_source)

router = APIRouter()

log = logging.getLogger("msh.trash")


def _local(sid: str):
    resolve_source(sid)
    src = manager.get(sid)
    if src is None:
        raise HTTPException(404, "数据源不可用")
    if getattr(src, "kind", "") != "local" or not hasattr(src, "trash_list"):
        raise HTTPException(400, "回收站只支持本地 / 映射盘数据源")
    return src


@router.get("/api/trash")
def trash_list(sid: str = Query(..., description="数据源 id")) -> Dict[str, Any]:
    """列出回收站里的文件与目录（按时间倒序）。"""
    src = _local(sid)
    try:
        items = src.trash_list()
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(400, f"读回收站失败：{type(exc).__name__}: {exc}") from exc
    items.sort(key=lambda x: (x.get("mtime") or 0), reverse=True)
    return {
        "source": sid,
        "root": src.trash_root(),
        "items": items,
        "total": len(items),
        "bytes": sum(int(i.get("size") or 0) for i in items),
    }


@router.post("/api/trash/restore")
def trash_restore(payload: Dict[str, Any] = Body(...),
                  job=Depends(exclusive_slot("trash"))) -> Dict[str, Any]:
    """把选中的条目还原。`items` 是 `[{rel}]`，rel 来自 `/api/trash`。

    `dst_dir` 为空 → 还原回**原路径**（`_origin.json` 里记着的）；
    指定了 → 还原到那个目录下（保留原来的相对结构）。
    """
    sid = as_text(payload, "source", "数据源")
    items = as_dicts(payload, "items", "要还原的条目", max_items=2000)
    if not items:
        raise HTTPException(400, "没有要还原的条目")
    dst_dir = as_text(payload, "dst_dir", "还原到").strip()
    policy = str(payload.get("policy") or "skip")
    if policy not in ("skip", "rename", "overwrite"):
        raise HTTPException(400, f"不支持的同名冲突策略: {policy}")
    require_writable(sid, what="还原回收站文件")
    src = _local(sid)

    results: List[Dict[str, Any]] = []
    done = 0
    for it in items:
        rel = str(it.get("rel") or "")
        if not rel:
            results.append({"rel": "", "state": "error", "detail": "缺少 rel"})
            continue
        try:
            r = src.trash_restore(rel, dst_dir, policy)
            results.append({"rel": rel, **r})
            if r.get("state") == "done":
                done += 1
        except Exception as exc:                      # noqa: BLE001
            results.append({"rel": rel, "state": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
    # 还原出来的东西要回到索引里,否则曲库和目录树里看不到。
    # ★ 文件用 `refresh_tracks`(按路径重扫),**目录要用局部重扫**:
    #   `refresh_tracks` 是"按文件"的,把一个目录路径丢进去什么都不会索引 ——
    #   于是文件夹确实回到了磁盘,界面上却查不到(用户以为还原失败,
    #   或者过一会儿重扫后它才突然出现,顺序和别的目录对不上)。
    _files: List[str] = []
    _dirs: List[str] = []
    for r in results:
        if r.get("state") != "done" or not r.get("target"):
            continue
        ap = src.abs_path(r["target"])
        try:
            (_dirs if os.path.isdir(ap) else _files).append(r["target"])
        except OSError:
            _files.append(r["target"])
    if _files:
        try:
            refresh_tracks(sid, [src.abs_path(p) for p in _files])
        except Exception:                             # noqa: BLE001
            pass
    if _dirs:
        try:
            from ..core import scanner as S
            src_obj = manager.get(sid)
            for p in _dirs:
                S.scan_source(sid, src_obj, db(), subpath=src.rel_path(src.abs_path(p)),
                              max_files=0)
        except Exception as exc:                      # noqa: BLE001
            log.warning("还原目录后重扫失败 %s: %s", _dirs, exc)
    return {"ok": True, "done": done, "total": len(items), "results": results}


@router.post("/api/trash/purge")
def trash_purge(payload: Dict[str, Any] = Body(...),
                job=Depends(exclusive_slot("trash"))) -> Dict[str, Any]:
    """**永久删除**回收站里的条目。必须 `confirm=true`，不可恢复。"""
    sid = as_text(payload, "source", "数据源")
    items = as_dicts(payload, "items", "要删除的条目", max_items=2000)
    if not items:
        raise HTTPException(400, "没有要删除的条目")
    if not payload.get("confirm"):
        raise HTTPException(400, "永久删除需要 confirm=true")
    require_writable(sid, what="清空回收站")
    src = _local(sid)
    results: List[Dict[str, Any]] = []
    done = 0
    for it in items:
        rel = str(it.get("rel") or "")
        try:
            src.trash_purge(rel)
            results.append({"rel": rel, "state": "done"})
            done += 1
        except Exception as exc:                      # noqa: BLE001
            results.append({"rel": rel, "state": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
    return {"ok": True, "done": done, "total": len(items), "results": results}
