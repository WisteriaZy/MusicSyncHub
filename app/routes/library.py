"""library.py —— 见 `app/main.py` 顶部导航索引。

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
    FUZZY_MIN_SCORE,
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
    refresh_tracks,
    require_writable,
)

router = APIRouter()

@router.get("/api/library/{sid}")
def library(sid: str, q: str = "", prefix: str = "", sort: str = "path",
            order: str = "asc", limit: int = 0, offset: int = 0,
            lossless: Optional[bool] = None, min_bitrate: int = 0,
            max_bitrate: int = 0, min_duration: float = 0, max_duration: float = 0,
            has_cover: Optional[bool] = None, codec: str = "", fuzzy: int = 0,
            min_score: float = 0) -> Dict[str, Any]:
    resolve_source(sid)  # 校验存在

    # 把"筛选条件"单独攒出来：列表、总数、统计三处必须用同一套条件，
    # 否则会出现"选了某个文件夹，列表变了但总体积/平均码率还是整库的"这种歧义。
    where = ["source_id=?"]
    params: List[Any] = [sid]
    scope = "all"
    # fuzzy=1 时关键词交给下面的模糊打分，不在这里用 LIKE（LIKE 不认错别字/缩写）
    fq = (q or "").strip() if fuzzy else ""
    if prefix:
        where.append("(parent=? OR parent LIKE ?)")
        params += [P.norm(prefix), P.norm(prefix).rstrip("/") + "/%"]
        scope = "folder"
    if q and not fq:
        where.append("(name LIKE ? OR title LIKE ? OR artist LIKE ? OR album LIKE ?"
                     " OR path LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like, like]
        scope = "filtered"
    if fq:
        scope = "filtered"
    if lossless is not None:
        where.append("is_lossless=?")
        params.append(1 if lossless else 0)
        scope = "filtered"
    if min_bitrate:
        where.append("COALESCE(bitrate,0)>=?")
        params.append(int(min_bitrate))
        scope = "filtered"
    if max_bitrate:
        where.append("COALESCE(bitrate,0)<=?")
        params.append(int(max_bitrate))
        scope = "filtered"
    if min_duration:
        where.append("COALESCE(duration,0)>=?")
        params.append(float(min_duration))
        scope = "filtered"
    if max_duration:
        where.append("COALESCE(duration,0)<=?")
        params.append(float(max_duration))
        scope = "filtered"
    if has_cover is not None:
        where.append("has_cover=?")
        params.append(1 if has_cover else 0)
        scope = "filtered"
    if codec:
        where.append("UPPER(CODEC)=?")
        params.append(codec.upper())
        scope = "filtered"

    clause = " AND ".join(where)

    cols = {"path": "path", "name": "name", "title": "title", "artist": "artist",
            "album": "album", "size": "size", "duration": "duration",
            "bitrate": "bitrate", "quality": "quality", "mtime": "mtime"}
    col = cols.get(sort, "path")
    direction = "DESC" if order.lower() == "desc" else "ASC"

    # ---- 模糊搜索分支 ----
    # SQL 的 LIKE 只认连续子串，搜"青花词"找不到"青花瓷"、搜"zjl"找不到
    # "zhoujielun"。所以把这些交给 Python 打分，但要先把 SQL 能表达的条件
    # （目录、无损、码率、封面…）全部过滤掉，参与打分的集合才足够小。
    if fq:
        rows_all = db().query(f"SELECT * FROM track WHERE {clause}", params)
        scored = []
        for r in rows_all:
            sc = P.fuzzy_match(fq, r["name"], r["title"], r["artist"],
                               r["album"], r["rel_path"], r["path"])
            if sc > 0:
                scored.append((sc, r))
        # 默认丢掉"只沾一点点边"的弱命中：用户输入"青花词"时，
        # 打分为 0.17 的 `05 - 无题.flac` 也算命中，但它显然不是想找的东西。
        # 0.45 这条线是在中英文、错别字、缩写几种情况上实测出来的：
        # 正常的错别字(0.67)与缩写(0.5)都留着，纯属噪声的(0.17~0.4)被丢掉。
        cutoff = float(min_score or FUZZY_MIN_SCORE)
        scored = [x for x in scored if x[0] >= cutoff]
        if sort == "path":
            # 默认按相关度排；相关度相同再按文件名，保证结果稳定
            scored.sort(key=lambda x: (-x[0], str(x[1]["name"] or "").lower(),
                                       int(x[1]["id"] or 0)))
        else:
            rev = direction == "DESC"
            # 同样要加 id 决胜键，否则分页会漏行/重复行
            scored.sort(key=lambda x: (str(x[1][col] or ""), int(x[1]["id"] or 0)),
                        reverse=rev)
        hits = [r for _s, r in scored]
        total_count = len(hits)
        page = scored[offset:offset + limit] if limit else scored[offset:]
        tracks = [_track_view(r) for _s, r in page]
        n_br = [int(r["bitrate"]) for r in hits if r["bitrate"]]
        agg = {
            "c": total_count,
            "s": sum(int(r["size"] or 0) for r in hits),
            "b": (sum(n_br) / len(n_br)) if n_br else 0,
            "l": sum(1 for r in hits if r["is_lossless"]),
            "d": sum(float(r["duration"] or 0) for r in hits),
        }
        whole = db().query_one(
            "SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM track WHERE source_id=?", (sid,))
        return {
            "source": sid, "total": total_count, "returned": len(tracks),
            "tracks": tracks, "scope": scope, "fuzzy": True,
            "prefix": P.norm(prefix) if prefix else "",
            "min_score": float(min_score or 0.45),
            "aggregate": {
                "count": agg["c"], "bytes": agg["s"],
                "human_bytes": P.human_size(agg["s"]),
                "avg_bitrate": int(agg["b"] or 0), "lossless": agg["l"],
                "total_duration": round(agg["d"], 1),
            },
            "aggregate_all": {
                "count": whole["c"] if whole else 0,
                "bytes": whole["s"] if whole else 0,
                "human_bytes": P.human_size(whole["s"] if whole else 0),
            },
        }

    sql = f"SELECT * FROM track WHERE {clause}"
    # 分页参数只能加在"取列表"那条 SQL 上：总数与统计都要用同一份筛选参数，
    # 所以这里先留一份快照，之后再往 params 上追加 LIMIT/OFFSET，
    # 否则统计语句会收到多余的绑定参数而直接报错。
    filter_params = list(params)
    total_sql = sql.replace("SELECT *", "SELECT COUNT(*) AS c", 1)
    total = db().query_one(total_sql, filter_params)
    # 排序必须**稳定**，否则分页会漏行/重复行：
    # 库里大量曲目的排序键是相同的（比如按"作者"排时有几百首作者为空），
    # SQLite 对同键行的返回顺序不保证稳定，两次查询（offset 0 与 offset N）
    # 可能给出不同的排列，于是"加载更多"就会看到重复或缺失。
    # 追加 `id` 作为最终决胜键，顺序就唯一确定了。
    sql += f" ORDER BY {col} COLLATE NOCASE {direction}, id ASC"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    rows = db().query(sql, params)
    tracks = [_track_view(r) for r in rows]
    # 统计必须和列表用同一套筛选条件（见上面的 where），
    # 否则"选了文件夹/搜了关键词"之后数字还是整库的，看着就像坏掉了。
    agg = db().query_one(
        "SELECT COUNT(*) c, COALESCE(SUM(size),0) s, COALESCE(AVG(bitrate),0) b,"
        " COALESCE(SUM(is_lossless),0) l, COALESCE(SUM(duration),0) d"
        f" FROM track WHERE {clause}",
        filter_params,
    )
    # 同时给出"整库"数字，界面上可以对比着显示，避免误解
    whole = db().query_one(
        "SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM track WHERE source_id=?", (sid,))
    return {
        "source": sid, "total": total["c"] if total else len(tracks),
        "returned": len(tracks), "tracks": tracks,
        "scope": scope,
        "prefix": P.norm(prefix) if prefix else "",
        "aggregate": {
            "count": agg["c"] if agg else 0,
            "bytes": agg["s"] if agg else 0,
            "human_bytes": P.human_size(agg["s"] if agg else 0),
            "avg_bitrate": int(agg["b"]) if agg else 0,
            "lossless": agg["l"] if agg else 0,
            "total_duration": round(agg["d"], 1) if agg else 0,
        },
        "aggregate_all": {
            "count": whole["c"] if whole else 0,
            "bytes": whole["s"] if whole else 0,
            "human_bytes": P.human_size(whole["s"] if whole else 0),
        },
    }


@router.get("/api/folders/{sid}")
def folders(sid: str) -> Dict[str, Any]:
    """目录树 + 每目录曲目数，供左侧树使用。"""
    rows = db().query(
        "SELECT parent, COUNT(*) c, COALESCE(SUM(size),0) s FROM track "
        "WHERE source_id=? GROUP BY parent ORDER BY parent", (sid,))
    src = resolve_source(sid)
    root = P.norm(src.root)
    tree: List[Dict[str, Any]] = []
    for r in rows:
        parent = P.norm(r["parent"])
        rel = parent[len(root) + 1:] if root and parent.startswith(root + "/") else parent
        tree.append({"path": parent, "rel": rel, "count": r["c"], "bytes": r["s"],
                     "depth": len([x for x in rel.split("/") if x])})
    return {"source": sid, "root": root, "folders": tree}


@router.get("/api/track/{tid}")
def track_detail(tid: int) -> Dict[str, Any]:
    row = db().track_by_id(tid)
    if row is None:
        raise HTTPException(404, "记录不存在")
    d = _track_view(row)
    d["display_path"] = P.display_path(row["path"], "/")
    return d


@router.post("/api/track/{tid}/refresh")
def refresh_track(tid: int) -> Dict[str, Any]:
    """只重扫一个文件（改过标签后想立即看到效果时用）。"""
    row = db().track_by_id(tid)
    if row is None:
        raise HTTPException(404, "记录不存在")
    sid = row["source_id"]
    src = manager.get(sid)
    if src is None:
        raise HTTPException(404, "数据源不存在或未就绪")
    st = src.stat(row["path"])
    if st is None:
        db().remove_track(sid, row["path"])
        return {"ok": True, "removed": True}
    try:
        data, head_only = S._fetch_bytes(src, row["path"], st.size, P.ext(row["name"]))
        meta = A.parse_bytes(data, row["name"], head_only=head_only)
        base = {"source_id": sid, "path": P.norm(row["path"]),
                "rel_path": src.rel_path(row["path"]), "name": row["name"],
                "parent": P.parent(row["path"]), "ext": P.ext(row["name"]),
                "size": st.size or 0, "mtime": st.mtime or 0}
        new = S._row_from_meta(meta, base)
        if meta.cover_data:
            thumb, mime, w, h = A.make_thumbnail(meta.cover_data)
            hh = A.sha1_bytes(thumb)
            db().put_cover(hh, mime, 0, 0, thumb)
            new["cover_hash"] = hh
            new["cover_mime"] = mime
            new["has_cover"] = 1
        db().upsert_tracks([{k: new.get(k) for k in S._TRACK_FIELDS}])
        return {"ok": True, "track": _track_view(db().track(sid, row["path"]))}
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")


@router.get("/api/cover/{tid}")
def cover_by_track(tid: int, size: int = 160) -> Response:
    row = db().track_by_id(tid)
    if row is None:
        raise HTTPException(404, "记录不存在")
    h = row["cover_hash"]
    if not h:
        cv = A.find_cover_file(row["source_id"], row["path"])
        if not cv:
            raise HTTPException(404, "没有封面")
        src = manager.get(row["source_id"])
        if src is None:
            raise HTTPException(404, "数据源不存在或未就绪")
        return _serve_cover_bytes(src.read_range(cv, 0, 0), size, f"{row['source_id']}:{cv}")
    cached = db().get_cover(h)
    if cached is None:
        raise HTTPException(404, "封面缓存已丢失，请重新扫描该数据源")
    return _serve_cover_bytes(cached["data"], size, h)


@router.get("/api/cover-raw")
def cover_raw(sid: str, path: str, size: int = 160) -> Response:
    """直接按路径取封面（浏览未索引目录时用）。"""
    resolve_source(sid)
    src = manager.get(sid)
    if src is None:
        raise HTTPException(404, "数据源不存在或未就绪")
    # ★ 用户路径必须先过边界检查（否则能读库外任意文件）
    rel = in_source_rel(src, path, "封面路径")
    if not rel:
        raise HTTPException(400, "缺少封面路径")
    abs_p = src.abs_path(rel)
    if not os.path.exists(abs_p):
        raise HTTPException(404, "封面文件不存在")
    if os.path.isdir(abs_p):
        raise HTTPException(400, "这是一个目录，不是文件")
    try:
        data = src.read_range(rel, 0, 0)
    except (PermissionError, OSError) as exc:
        raise HTTPException(400, f"读不到这个文件：{type(exc).__name__}") from exc
    return _serve_cover_bytes(data, size, f"{sid}:{rel}")


@router.get("/api/preview/{tid}")
def preview(tid: int, request: Request) -> Response:
    """音频流：支持 Range，浏览器可直接拖动进度条试听。"""
    row = db().track_by_id(tid)
    if row is None:
        raise HTTPException(404, "记录不存在")
    src = manager.get(row["source_id"])
    if src is None:
        raise HTTPException(404, "数据源不存在或未就绪")
    st = src.stat(row["path"])
    if st is None:
        raise HTTPException(404, "源文件不存在（可能已被移动或删除）")
    total = st.size or 0
    range_header = request.headers.get("range") or request.headers.get("Range")
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"

    if range_header and total:
        try:
            spec = range_header.split("=", 1)[1]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
            end = min(end, total - 1)
            if start > end:
                raise ValueError
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        length = end - start + 1

        def gen():
            remaining = length
            fh = src.open_read(row["path"], start, length)
            try:
                while remaining > 0:
                    chunk = fh.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                try:
                    fh.close()
                except Exception:
                    pass

        headers = {
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "no-store",
        }
        return StreamingResponse(gen(), status_code=206, media_type=mime, headers=headers)

    def gen_full():
        fh = src.open_read(row["path"])
        try:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                fh.close()
            except Exception:
                pass

    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if total:
        headers["Content-Length"] = str(total)
    return StreamingResponse(gen_full(), media_type=mime, headers=headers)
