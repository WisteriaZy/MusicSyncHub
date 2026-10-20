"""files.py —— 见 `app/main.py` 顶部导航索引。

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
    in_source_rel,
    as_dicts,
    as_list,
    as_text,
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

@router.post("/api/files/transfer")
def files_transfer(payload: Dict[str, Any] = Body(...),
                   exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """**跨库传输**：把文件从一个数据源搬到另一个数据源。

    语义是「移动」而不是「复制」：成功之后源那份会进回收站。
    实现上必须是 **复制 → 校验 → 再删源** —— 顺序反了或者跳过校验，
    一旦中途失败就是"源没了、目标也没有"。

    入参：
      `{"from": sid, "to": sid, "items": [源内绝对路径...],
        "dst_dir": 目标内目录(空=根), "policy": skip|rename|overwrite,
        "verify": true, "verify_hash": false, "execute": false, "confirm": false}`

    `execute=false` 时只做预检（检查两边权限、目标是否已存在），不搬任何东西。
    """
    src_id = str(payload.get("from") or "")
    dst_id = str(payload.get("to") or "")
    items = [str(x) for x in as_list(payload, "items", "要传输的文件") if x]
    if not src_id or not dst_id:
        raise HTTPException(400, "需要 from / to 两个数据源 id")
    if not items:
        raise HTTPException(400, "没有要传输的文件")
    if len(items) > 2000:
        raise HTTPException(400, "一次传输的文件过多，请分批（上限 2000）")
    if src_id == dst_id:
        raise HTTPException(400, "源和目标不能是同一个数据源；"
                                 "同库内移动请用「移动到…」")
    resolve_source(src_id)
    resolve_source(dst_id)
    execute = bool(payload.get("execute", False))
    if execute and not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")
    policy = str(payload.get("policy") or "skip")
    if policy not in ("skip", "rename", "overwrite"):
        raise HTTPException(400, f"不支持的同名冲突策略: {policy}")

    src = manager.get(src_id)
    dst = manager.get(dst_id)
    if src is None or dst is None:
        raise HTTPException(404, "数据源不可用")
    # ★ 不同源目录里的同名文件会被压平到同一个目标名（`rel = dst_dir/basename`）。
    #   如果 policy=overwrite，第二个会**把刚传过去的第一个覆盖掉**，
    #   而两边的源文件都已进回收站 —— 第一个文件彻底没了。
    #   skip/rename 不会丢（一个报冲突、一个自动改名），所以只在 overwrite 时硬拒。
    # ★ Windows 的文件名大小写不敏感：`Track.mp3` 与 `track.mp3` 是同一个
    #   物理文件。按原样统计会漏判，覆盖策略下后一个直接把前一个冲掉。
    _names = [P.basename(x) for x in items]
    _keys = [n.lower() if os.name == "nt" else n for n in _names]
    _dup = sorted({n for n, k in zip(_names, _keys) if _keys.count(k) > 1})
    if _dup and str(payload.get("policy") or "skip") == "overwrite":
        raise HTTPException(
            400,
            f"这批里有 {len(_dup)} 个同名文件（{'、'.join(_dup[:5])}"
            f"{'…' if len(_dup) > 5 else ''}），会落到同一个目标名。"
            f"用「覆盖」会导致前一个刚传过去的文件被覆盖丢失；"
            f"请改用「同名时改名」或分批传输。")
    if not dst.can_write:
        raise HTTPException(403, f"目标数据源「{dst.cfg.name}」是只读的，"
                                 f"不能作为传输目标")
    if not src.can_delete:
        raise HTTPException(403, f"源数据源「{src.cfg.name}」不允许删除，"
                                 f"无法完成「复制后删源」的传输")

    dst_dir = P.norm(str(payload.get("dst_dir") or "")).strip("/")
    # ★ dst_dir 是"目标源内的目录"，必须落在目标根内。
    #   `norm()` 不折叠 `..`，所以 `../../别处` 会通过前缀判断 —— 必须显式拒。
    if P.has_dotdot(dst_dir):
        raise HTTPException(400, "目标目录不能包含 ..")
    if dst_dir and not P.inside_root(dst.root, dst.abs_path(dst_dir)):
        raise HTTPException(400, "目标目录超出目标数据源范围")
    # ★ 每个 item 也必须在**源根内**。
    #   原来这里是直接拿客户端给的字符串去 stat / 复制 / 删源的：
    #   传一个库外的绝对路径，就会把库外文件复制到库外目录、再把"源"
    #   （还是库外文件）删掉，接口照样返回 done。
    _outside = [x for x in items
                if not P.inside_root(src.root, src.abs_path(x))]
    if _outside:
        _names = "、".join(str(x) for x in _outside[:5])
        raise HTTPException(
            400, f"有 {len(_outside)} 个路径超出源数据源范围：{_names}"
                 + ("…" if len(_outside) > 5 else ""))
    # 传输模式：默认 None → 让 `transfer_one` 按源数据源的 `extras.transfer_mode`
    # 决定（standard / fast）。请求里显式传了就听请求的。
    _tmode = str(payload.get("transfer_mode") or "").strip().lower()
    verify: Optional[bool] = None
    if payload.get("verify") is not None:
        verify = bool(payload.get("verify"))
    elif _tmode in ("standard", "fast"):
        verify = (_tmode == "standard")
    verify_hash = bool(payload.get("verify_hash"))
    hard_delete = bool(payload.get("hard_delete"))

    # ★ 跨库传输**只接音频**。
    #
    #   规则（用户定的）：库里的非音乐文件可以做基础文件操作
    #   （重命名 / 移动 / 删除 / 查看），但**不能跨库传输** ——
    #   跨库传输带校验和"复制后删源"，是给曲目用的；把 .nfo/.jpg/.cue
    #   这些捎带过去，到了对面就成了没人认识的孤儿文件，
    #   而且源那份已经被删掉了（进回收站），用户很难联想到是这次操作干的。
    #
    #   之前这里**完全没有检查**：`P.is_audio` 在 files_transfer 里一次都没出现，
    #   传什么路径都收。现在显式挡掉，并说清"这些要在文件浏览里单独移动"。
    _non_audio = [x for x in items
                  if not P.is_audio(P.basename(x) or x)]
    if _non_audio:
        _names = "、".join(P.basename(x) for x in _non_audio[:5])
        raise HTTPException(
            400,
            f"跨库传输只支持音频文件；这 {len(_non_audio)} 个不是音频：{_names}"
            + ("…" if len(_non_audio) > 5 else "")
            + "。非音频文件请用「移动到…」在同一个库内整理。")

    if not execute:
        # 预检：只读、目标落点、同名冲突都先报出来
        preview = []
        for sp in items[:500]:
            name = P.basename(sp)
            rel = P.join(dst_dir, name) if dst_dir else name
            target = dst.abs_path(rel)
            st = src.stat(sp)
            preview.append({
                "src_path": sp, "name": name, "dst_rel": rel,
                "dst_path": target,
                "exists": dst.exists(target),
                "size": (st.size if st else 0),
                "found": st is not None,
            })
        return {"dry_run": True, "from": src_id, "to": dst_id,
                "dst_dir": dst_dir, "policy": policy,
                "items": preview,
                "would_conflict": sum(1 for x in preview if x["exists"]),
                "missing": sum(1 for x in preview if not x["found"])}

    # ---- 真正执行 ----
    job = None
    try:
        job = get_jobs().create("transfer", total=len(items),
                                owner=str(payload.get("client") or ""))
    except JobBusy as exc:
        raise HTTPException(409, str(exc))

    results: List[Dict[str, Any]] = []
    summary = {"total": len(items), "done": 0, "conflict": 0, "blocked": 0,
               "error": 0, "bytes": 0}
    done_src: List[str] = []
    done_dst_dirs: set = set()
    try:
        from ..core import transfer as X
        for i, sp in enumerate(items):
            if getattr(job, "cancel", False):
                break
            name = P.basename(sp)
            rel = P.join(dst_dir, name) if dst_dir else name
            job.current = name
            job.touch()
            try:
                r = X.transfer_one(src, dst, sp, rel, policy=policy,
                                   verify=verify, verify_hash=verify_hash,
                                   hard_delete=hard_delete, job=job,
                                   mode=_tmode)
            except Exception as exc:                              # noqa: BLE001
                r = {"state": "error",
                     "detail": f"{type(exc).__name__}: {exc}"}
            r["src_path"] = sp
            r["dst"] = dst_id
            results.append(r)
            st = r.get("state") or "error"
            if st == "done":
                summary["done"] += 1
                summary["bytes"] += int(r.get("bytes") or 0)
                done_src.append(sp)
                # ★ 记下**目标侧**的相对目录，稍后要按它去重扫目标库。
                #   以前拿源侧绝对路径当 subpath 传给目标源，永远扫不到。
                _tgt = r.get("target")
                if _tgt:
                    try:
                        _dr = P.parent(dst.rel_path(_tgt))
                        done_dst_dirs.add(_dr.strip("/") if _dr.strip("/") else "")
                    except Exception:
                        pass
            elif st == "conflict":
                summary["conflict"] += 1
            elif st == "blocked":
                summary["blocked"] += 1
            else:
                summary["error"] += 1
            job.done = i + 1
            job.touch()
        get_jobs().finish(job, result=summary)
    except Exception as exc:                                      # noqa: BLE001
        try:
            get_jobs().finish(job, error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise

    # 索引：源那边删掉的文件要从索引里移除，目标那边新增的要能搜到
    if done_src:
        try:
            _done_dst_files = [r.get("target") for r in results
                               if r.get("state") == "done" and r.get("target")]
            _reindex_after_transfer(src_id, dst_id, done_src,
                                    sorted(done_dst_dirs), _done_dst_files)
        except Exception as exc:
            # ★ 不能静默。这一步失败意味着"磁盘上文件已经搬走了，索引还是旧的"：
            #   用户会看到源库里还挂着已经不存在的条目、目标库搜不到新文件，
            #   而且完全不知道原因（界面上一切正常）。
            log.warning("搬运后回填索引失败（磁盘已变、索引未同步）: %s", exc)

    return {"dry_run": False, "from": src_id, "to": dst_id,
            "results": results, "summary": summary,
            "moved": summary["done"], "conflict": summary["conflict"],
            "blocked": summary["blocked"], "failed": summary["error"],
            "bytes": summary["bytes"]}


@router.post("/api/files/exists")
def files_exists(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """批量问"这些文件还在不在"。

    给候选区用：候选区的条目是存在浏览器里的，可能已经放了好几天 ——
    期间文件被移走、删掉、或者 U 盘拔了。原来这些**过期项**照样能点
    「开始移动」，要等执行器报错才知道，白等一趟。

    这里只做 `stat`（不读内容、不建文件）。放不进索引判断是因为
    候选区要的就是"磁盘上真实情况"，而不是"索引里有没有记录"。
    """
    sid = str(payload.get("source") or "")
    if not sid:
        raise HTTPException(400, "缺少 source")
    raw = payload.get("paths") or []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "paths 不能为空")
    if len(raw) > 5000:
        raise HTTPException(400, "一次最多查 5000 条")
    src = resolve_source(sid)
    srcobj = manager.get(sid)
    if srcobj is None:
        raise HTTPException(404, f"数据源不可用: {sid}")

    missing: List[str] = []
    present: List[str] = []
    for p in raw:
        sp = str(p or "")
        if not sp:
            continue
        try:
            st = srcobj.stat(sp)
        except Exception:
            st = None
        (present if st is not None else missing).append(sp)
    return {"source": sid, "present": present, "missing": missing,
            "missing_count": len(missing), "total": len(present) + len(missing)}


@router.get("/api/files/folders")
def files_folders(sid: str, path: str = "") -> Dict[str, Any]:
    """列出某个数据源（或某个子目录）下的文件夹，带曲目数，供目录树/排序使用。"""
    sc = resolve_source(sid)
    src = manager.get(sid)
    path = in_source_rel(src, path, "目录路径")
    cur = src.abs_path(path) if path else P.norm(src.root)
    rows = _folder_rows(sid, src.root)
    if path:
        prefix = cur.rstrip("/") + "/"
        rows = [r for r in rows if r["path"].startswith(prefix)]
        base_len = len(cur) + 1
        for r in rows:
            r["rel"] = r["path"][base_len:]
            r["depth"] = len([x for x in r["rel"].split("/") if x])
    return {
        "source": sid, "name": sc.name, "root": P.norm(src.root),
        "path": cur, "folders": rows,
        "total_tracks": db().counts_by_source().get(sid, 0),
    }


@router.get("/api/files/list")
def files_list(sid: str, path: str = "", sort: str = "name", order: str = "asc",
               limit: int = 0) -> Dict[str, Any]:
    """列出一个真实目录的内容：子文件夹（带文件数）+ 音频文件（带索引里的元数据）。"""
    resolve_source(sid)
    src = manager.get(sid)
    path = in_source_rel(src, path, "目录路径")
    cur = src.abs_path(path) if path else P.norm(src.root)

    try:
        entries = src.listdir(cur)
    except SourceError as exc:
        raise HTTPException(400, str(exc))

    # 索引里该目录下的曲目（有元数据、封面、时长）
    indexed: Dict[str, Any] = {}
    # ★ 根目录原来走 `SELECT * FROM track WHERE source_id=?`，把**整库**几万行
    #   拉进 Python 再逐行 `P.parent()` 过滤。`track` 表本来就有 `parent` 列，
    #   直接用等值查询即可（根目录也走同一个口径）。
    rows = db().query(
        "SELECT * FROM track WHERE source_id=? AND parent=? ORDER BY path", (sid, cur))
    for r in rows:
        indexed[P.basename(r["path"])] = r

    folder_counts = {r["path"]: r for r in _folder_rows(sid, src.root)}

    dirs = []
    for e in entries:
        if not e.is_dir or e.name.startswith("."):
            continue
        if e.name.lower() in ("system volume information",):
            continue
        sub = P.norm(e.path)
        fs = None
        agg = folder_counts.get(sub)
        if agg is None:
            # 索引里没有这个目录（可能没扫过）→ 只数一层，避免深挖整个磁盘
            fs = _dir_summary(sid, src, sub)
        dirs.append({
            "name": e.name, "path": sub, "mtime": e.mtime,
            "count": (agg or {}).get("count_all", (fs or {}).get("audio", 0)),
            "count_here": (agg or {}).get("count", (fs or {}).get("audio", 0)),
            "subdirs": (fs or {}).get("dirs", 0),
            "bytes": (agg or {}).get("bytes_all", 0),
            "indexed": agg is not None,
        })

    files = []
    for e in entries:
        if e.is_dir or not P.is_audio(e.name):
            continue
        row = indexed.get(e.name)
        if row is not None:
            d = _track_view(row)
            d["indexed"] = True
        else:
            d = {
                "id": None, "source_id": sid, "path": P.norm(e.path), "name": e.name,
                "ext": P.ext(e.name), "size": e.size, "mtime": e.mtime,
                "human_size": P.human_size(e.size), "cover_url": None,
                "stream_url": None, "indexed": False,
            }
        d["parent"] = cur
        files.append(d)

    # 排序：大小 / 名称 / 时间 / 时长；文件夹在下面单独排
    def key_size(x): return (x.get("size") or 0)
    def key_name(x): return str(x.get("name") or "").lower()
    def key_time(x): return (x.get("mtime") or 0)
    def key_dur(x): return (x.get("duration") or 0)
    keymap = {"name": key_name, "size": key_size, "mtime": key_time, "duration": key_dur,
              "count": key_name, "quality": lambda x: (x.get("quality") or 0)}
    fkey = keymap.get(sort, key_name)
    files.sort(key=fkey, reverse=(order.lower() == "desc"))

    # 文件夹默认按"文件数"从多到少排（这正是用户要的：先看大文件夹）
    dkey = {"name": lambda x: x["name"].lower(), "count": lambda x: x["count"],
            "mtime": lambda x: x["mtime"] or 0, "size": lambda x: x["bytes"]}
    if sort in dkey:
        dirs.sort(key=dkey[sort], reverse=(order.lower() == "desc"))
    else:
        dirs.sort(key=lambda x: x["count"], reverse=True)

    total = len(files)
    if limit:
        files = files[:limit]
    parent_path = P.parent(cur)
    if parent_path == cur:
        parent_path = ""
    return {
        "source": sid, "path": cur, "parent": parent_path,
        "rel": src.rel_path(cur) if cur != P.norm(src.root) else "",
        "root": P.norm(src.root), "dirs": dirs, "files": files,
        "file_count": total, "shown": len(files),
        "dir_audio_total": sum(d["count"] for d in dirs),
        # 磁盘余量：让用户能预判"还能不能往里放"。
        # 放在目录列表里一起返回，免得前端再发一次请求。
        "disk": _disk_view(cur, src),
    }


@router.get("/api/state")
def state_revision(sid: str = "") -> Dict[str, Any]:
    """一个很轻的"数据变了没有"信号。

    前端每几秒问一次：数字没变就不重载，变了才刷新 ——
    这样用户不用手动 F5 也能看到别人（或另一个页面）刚做完的操作。

    只读索引 + 一个 stat，开销可以忽略；不会再触发磁盘遍历。
    """
    # rev 是**库里的单调计数器**（每次提交 +1）：前端只认它，
    # 不再依赖"曲目数 + WAL mtime"那套不可靠的判据。
    rev = {"tracks": 0, "deleted": 0, "sources": 0, "mtime": 0.0, "rev": 0}
    try:
        rows = db().query(
            "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS m FROM track"
            + (" WHERE source_id=?" if sid else ""),
            (sid,) if sid else ())
        if rows:
            rev["tracks"] = int(rows[0]["n"] or 0)
        try:
            rev["rev"] = db().revision()
            rev["deleted"] = db().deleted_total()
        except Exception:                              # noqa: BLE001
            pass
        # WAL 文件的修改时间能反映"索引刚被写过"
        p = str(db().path) + "-wal"
        if os.path.exists(p):
            rev["mtime"] = round(os.path.getmtime(p), 1)
    except Exception:
        pass
    try:
        rev["sources"] = len(load_config().sources)
    except Exception:
        pass
    # 任务的完成数：任务结束时前端要刷新一次（结果落盘了）
    try:
        js = get_jobs().list()
        rev["jobs_done"] = sum(1 for j in js if j.get("state") == "done")
        rev["busy"] = bool(get_jobs().busy())
    except Exception:
        rev["jobs_done"] = 0
        rev["busy"] = False
    return rev


@router.post("/api/files/under")
def files_under(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """列出若干目录（含子目录）下的**全部音频文件**。

    给"目录树多选 → 文件列表联动"用：用户在树上勾了几个目录，
    期望看到的就是这些目录里的文件被选上，而不是只让「整理这些」能点。

    只用**索引**（不遍历磁盘）：勾选是即时交互，遍历真实目录会卡住界面；
    索引本来就是唯一的曲目真相来源。
    """
    sid = str(payload.get("source") or "")
    if not sid:
        raise HTTPException(400, "缺少 source")
    raw = payload.get("paths") or []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "paths 不能为空")
    src = resolve_source(sid)
    root = P.norm(src.root)

    prefixes: List[str] = []
    for p in raw:
        s = P.norm(str(p))
        if not s:
            continue
        if not s.startswith("/") and not re.match(r"^[A-Za-z]:", s):
            s = P.join(root, s)
        s = s.rstrip("/")
        # 只接受根目录范围内，避免越界查询
        if s == root or s.startswith(root + "/"):
            prefixes.append(s)
    if not prefixes:
        raise HTTPException(400, "paths 里没有有效路径")

    files: List[Dict[str, Any]] = []
    seen: set = set()
    for pref in prefixes:
        for r in db().tracks(sid, prefix=pref):
            p = P.norm(r["path"])
            if p in seen:
                continue
            seen.add(p)
            d = _track_view(r)
            d["indexed"] = True
            files.append(d)

    files.sort(key=lambda x: str(x.get("path") or "").lower())
    return {"source": sid, "root": root, "folders": prefixes,
            "count": len(files), "files": files}


@router.post("/api/files/select")
def files_select(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """按条件快速集合曲目（同作者 / 同专辑 / 同格式 / 整目录…）。

    给"批量移动/批量处理"用：前端把筛选条件发过来，后端用 SQL 选出 path 列表。
    """
    sid = payload.get("source")
    if not sid:
        raise HTTPException(400, "缺少 source")
    resolve_source(sid)
    src = manager.get(sid)
    prefix = payload.get("prefix") or ""
    cur = src.abs_path(prefix) if prefix else P.norm(src.root)

    sql = "SELECT path, name, artist, album FROM track WHERE source_id=?"
    params: List[Any] = [sid]
    if prefix:
        sql += " AND (parent=? OR parent LIKE ?)"
        params += [cur, cur.rstrip("/") + "/%"]
    where = []
    by = payload.get("by")          # artist | album | ext | codec
    value = payload.get("value")
    if by == "artist":
        where.append("COALESCE(artist,'')=?")
        params.append(value or "")
    elif by == "album":
        where.append("COALESCE(album,'')=?")
        params.append(value or "")
    elif by == "ext":
        where.append("UPPER(ext)=?")
        params.append(str(value or "").upper())
    elif by == "codec":
        where.append("UPPER(codec)=?")
        params.append(str(value or "").upper())
    if where:
        sql += " AND " + " AND ".join(where)
    rows = db().query(sql, params)
    # 用 source::path 作为唯一键，和查重/移动里的身份写法保持一致
    items = [{"did": f"{sid}::{r['path']}", "path": r["path"], "name": r["name"],
              "artist": r["artist"], "album": r["album"]} for r in rows]
    return {"source": sid, "path": cur, "by": by, "value": value,
            "count": len(items), "items": items}


@router.get("/api/files/search")
def files_search(sid: str, q: str = "", limit: int = 60) -> Dict[str, Any]:
    """按名字搜文件夹（在已建索引的目录里找）。

    "移动文件"时要翻几十层目录太累，所以给一个搜索框：
    输入片段就能直接跳到目标目录，不用一层层点。
    只搜索引里出现过的目录——没扫描过的目录本来也没有曲目数。
    """
    resolve_source(sid)
    src = manager.get(sid)
    needle = P.normalize_text(q or "")
    if not needle:
        return {"source": sid, "query": q, "folders": [], "total": 0}
    rows = _folder_rows(sid, src.root)
    hits = []
    for r in rows:
        rel = r.get("rel") or ""
        # 同时匹配完整路径与最后一段目录名：输 "雪融" 也能找到长路径里的目录
        if needle in P.normalize_text(rel):
            leaf = rel.rsplit("/", 1)[-1]
            hits.append({**r, "leaf": leaf,
                         "leaf_hit": needle in P.normalize_text(leaf)})
    # 目录名本身命中的排在前面，其次按曲目数多的优先
    hits.sort(key=lambda x: (not x["leaf_hit"], -x["count_all"], len(x["rel"])))
    cap = max(1, min(300, int(limit)))
    return {"source": sid, "query": q, "total": len(hits), "folders": hits[:cap]}


@router.get("/api/files/facets")
def files_facets(sid: str, path: str = "", field: str = "artist",
                 limit: int = 60) -> Dict[str, Any]:
    """按某一维度统计曲目分布，供"按作者快速集合"用。"""
    resolve_source(sid)
    src = manager.get(sid)
    path = in_source_rel(src, path, "目录路径")
    cur = src.abs_path(path) if path else P.norm(src.root)
    col = {"artist": "artist", "album": "album", "codec": "codec", "ext": "ext"}.get(field)
    if not col:
        raise HTTPException(400, f"不支持的维度: {field}")
    sql = (f"SELECT COALESCE(NULLIF({col},''),'(未知)') AS v, COUNT(*) AS c,"
           f" COALESCE(SUM(size),0) AS b FROM track WHERE source_id=?")
    params: List[Any] = [sid]
    if path:
        sql += " AND (parent=? OR parent LIKE ?)"
        params += [cur, cur.rstrip("/") + "/%"]
    sql += " GROUP BY v ORDER BY c DESC, v LIMIT ?"
    params.append(max(1, min(500, int(limit))))
    rows = db().query(sql, params)
    return {"source": sid, "field": field,
            "facets": [{"value": r["v"], "count": r["c"], "bytes": r["b"]} for r in rows]}


@router.post("/api/files/move")
def files_move(payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """把文件移动到同一数据源内的另一个目录（界面"移动到…"用）。

    入参：`{"steps": [{"src": sid, "src_path": 绝对内部路径, "dst_path": 绝对内部路径}],
           "execute": false, "confirm": false, "policy": "skip"}`

    只做同源移动，走的还是 `Executor`：dry-run 与真正执行共用同一套安全检查，
    "目标已存在"也按现有 policy 处理（skip / rename / overwrite）。
    跨源搬运请走 /api/execute 的复制计划——那才是"先复制成功、再删源"的安全顺序。
    """
    steps = as_dicts(payload, "steps", "移动步骤")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "没有要执行的步骤")
    # 上限由服务端定；客户端只能往小了调（原来 max_steps 完全由客户端给）
    if len(steps) > min(int(payload.get("max_steps") or 20000), 20000):
        raise HTTPException(400, "步骤过多，请分批执行")
    execute = bool(payload.get("execute", False))
    if execute and not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")

    # 全部相关源都只读 → 直接 403，而不是返回一个"看着成功"的 200
    if execute:
        sids: List[str] = []
        for st in steps:
            if not isinstance(st, dict):
                continue
            for key in ("src", "dst"):
                sid = st.get(key)
                if sid and sid not in sids:
                    sids.append(sid)
        require_writable(*sids, what="移动文件")

    # 翻译成执行器认识的 move 步骤：移动只认一个源，dst 缺省就等于 src
    plan: List[Dict[str, Any]] = []
    sources: Dict[str, Any] = {}
    for i, st in enumerate(steps):
        sid = st.get("src")
        if not sid:
            raise HTTPException(400, "步骤缺少数据源 id（src）")
        if sid not in sources:
            sources[sid] = manager.get(sid)
        plan.append({
            # id 每条都要不同：_sync_index 靠它把结果对回步骤，
            # 否则一次移动多首时只有最后一条会被写进索引
            "id": st.get("id") or f"move-{i}",
            "op": "move",
            "kind": st.get("kind"),
            "src": sid,
            "src_path": st.get("src_path"),
            "dst": st.get("dst") or sid,
            "dst_path": st.get("dst_path"),
            "size": st.get("size") or 0,
        })
    ex = Executor(sources, db() if execute else None,
                  default_policy=payload.get("policy") or "skip")
    return ex.run(plan, execute=execute,
                  policy=payload.get("policy") or "skip",
                  workers=int(payload.get("workers") or 3))
