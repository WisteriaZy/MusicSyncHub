"""运行时状态与跨端点复用的 helper。

从 `main.py` 拆出来，目的是让路由模块能 import 它们而**不用 import main**
（那样会循环导入）。这里**不允许**出现任何 FastAPI app 级别的东西
（中间件、异常处理器、静态挂载）—— 那些属于 `main.py`。

模块级状态都在这里，改它之前先看 `AGENTS.md` 第 2 节。
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

from fastapi import HTTPException, Response


from .core import audio as A
from .core import device as DEV
from .core import matching as M
from .core import paths as P
from .core import plan as PLAN
from .core import scanner as S
from .core.config import (AppConfig, Settings, SourceConfig, SourceNotFound,
                          data_dir, default_db_path, get_secret, load_config,
                          resolve_source, save_config, save_secret, test_mode)
from .core.db import Database
from .core.jobs import JobBusy, get_jobs, init as init_jobs, rebind as rebind_jobs
from .core.ops import Executor
from .sources import available_types, manager
from .sources.base import ReadOnlyError, SourceError

log = logging.getLogger("msh")

# 版本号放在这里，而不是让路由去读 `main.app.version`
# —— 那样会形成 routes → main → routes 的循环导入。
APP_VERSION = "1.0.0"

DB: Optional[Database] = None

_cover_cache: "OrderedDict[str, Any]" = OrderedDict()

_cover_lock = threading.RLock()

FUZZY_MIN_SCORE = 0.45

_FILE_BROWSE_CACHE: Dict[str, Any] = {}

_FILE_BROWSE_TTL = 20.0

_FILE_BROWSE_LOCK = threading.RLock()

def _wanted_db_path() -> str:
    return str(os.environ.get("MSH_DB") or default_db_path())

def db() -> Database:
    """当前该用的索引库。

    ★ 会跟着环境变量走：`MSH_DB` 变了就换库（旧连接关掉）。

    为什么要这样：原来这里是 `if DB is None: 建库` —— `DB` 一旦建好就
    **认死了第一个路径**，之后再改 `MSH_DB` 也无效。对单个进程跑一个库的
    正常使用没影响，但它让"换库"这件事变得只能靠调用方记得手动
    `main.DB = None`（每个测试类都得记一次，漏一个就会串库）。
    实测表现：某个测试类忘了重置之后，后面所有类的请求都打在
    **第一个测试的临时库**上，于是"明明插了 8 首却只查到 5 首"，
    而且报错点离真正的原因非常远。
    """
    global DB
    want = _wanted_db_path()
    if DB is not None and os.path.abspath(str(DB.path)) != os.path.abspath(want):
        try:
            DB.close_all()
        except Exception:
            pass
        DB = None
    if DB is None:
        DB = Database(want)
        init_jobs(DB)
    else:
        # 库没变，但注册表可能是绑在旧库上的（测试换过 MSH_DB、
        # 或任何在 lifespan 之前先碰过 db() 的路径）。对齐一下。
        rebind_jobs(DB)
    return DB

def require_writable(*sids: Optional[str], what: str = "写操作") -> None:
    """这批操作的**全部**目标数据源都只读时，直接 403 拒绝。

    为什么要单独做这一层（执行器里已经有 `can_write` 检查了）：

    执行器的检查是**逐步骤**的 —— 它把每一步标成 `state="blocked"`，
    但整个请求仍然返回 **HTTP 200**。于是"只读数据源上做移动"这件事，
    接口层面看起来是**成功**的，只有仔细看 `results[].state` 才知道
    一步都没做。实测确认过：

        POST /api/files/move  → HTTP 200，results[0].state="blocked"
        POST /api/tags/write  → HTTP 200，ok=0 failed=1

    对用户来说这就是"点了一下、提示成功、文件没动"。宁可在这里明确
    报 403 并说清是哪个数据源被设成了只读，也不要给一个含糊的 200。

    注意：只在**全部**相关源都不可写时才拒绝。如果一个源可写、
    另一个只读（比如复制到只读目标），仍然交给执行器逐步骤处理 ——
    那样用户能看到"哪些步骤被阻断了"，信息更全。
    """
    seen: List[str] = []
    for sid in sids:
        if not sid or sid in seen:
            continue
        seen.append(sid)
    if not seen:
        return
    blocked: List[str] = []
    writable = 0
    for sid in seen:
        try:
            src = manager.get(sid)
        except Exception:
            src = None
        if src is None:
            continue
        if getattr(src, "can_write", True):
            writable += 1
        else:
            blocked.append(getattr(getattr(src, "cfg", None), "name", "") or sid)
    if blocked and writable == 0:
        names = "、".join(f"「{n}」" for n in blocked)
        raise HTTPException(
            403,
            f"{names} 被标记为只读，{what}已被拒绝。"
            f"如果确实要写入，请在「数据源」里取消它们的只读勾选。")

def _db_size() -> int:
    """索引库占用的真实磁盘空间。

    开了 WAL 之后，数据大部分时间在 -wal 文件里，只看主文件会显示得很小。
    """
    base = str(db().path)
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = base + suffix
        if os.path.exists(p):
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
    return total

def _source_view(sc: SourceConfig) -> Dict[str, Any]:
    row = db().query_one("SELECT last_scan, track_count FROM source WHERE id=?", (sc.id,))
    return {
        **sc.to_dict(),
        "has_secret": bool(sc.secret_ref and get_secret(sc.secret_ref, "")),
        "last_scan": row["last_scan"] if row else None,
        "indexed": db().counts_by_source().get(sc.id, 0),
        # 在线状态不在这里算：探活要走文件系统，放在列表接口里会让每次
        # 刷新都卡住。界面另外调 /api/sources/status 拿（见下）。
        "connected": None,
    }

def _validate_source(sc: SourceConfig) -> None:
    if not sc.id:
        raise HTTPException(400, "缺少 id")
    if sc.type not in {t["kind"] for t in available_types()}:
        raise HTTPException(400, f"不支持的类型: {sc.type}")
    if not sc.name:
        sc.name = sc.id

def _track_view(row, with_hash: bool = True) -> Dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["human_size"] = P.human_size(d.get("size"))
    d["quality_label"] = A.quality_label(d)
    d["cover_url"] = f"/api/cover/{d['id']}?size=160" if d.get("cover_hash") else None
    d["stream_url"] = f"/api/preview/{d['id']}"
    if not with_hash:
        d.pop("hash_quick", None)
        d.pop("hash_full", None)
    return d

def _device_view(sc: SourceConfig) -> Dict[str, Any]:
    """这个数据源所在盘的性质（测速失败不影响主流程）。"""
    try:
        if sc.type != "local" or not sc.root:
            return {}
        if not os.path.isdir(sc.root):
            return {}
        info = DEV.detect(sc.root, measure=False)
        return {"kind": info.kind, "removable": info.removable,
                "network": info.network, "slow": info.slow,
                "label": info.label, "mount": info.mount}
    except Exception:
        return {}

def _connect_tip(sc: SourceConfig) -> str:
    tips = {
        "local": "确认路径存在且进程有读权限（Windows 用 D:\\Music 形式，注意转义）。",
    }
    return tips.get(sc.type, "")

def _serve_cover_bytes(data: bytes, size: int, key: str) -> Response:
    if not data:
        raise HTTPException(404, "封面为空")
    ck = f"{key}@{size}"
    with _cover_lock:
        hit = _cover_cache.get(ck)
        if hit is not None:
            _cover_cache.move_to_end(ck)          # 命中就提到"最近使用"
    if hit:
        payload, mime = hit
    else:
        payload, mime, w, h = A.make_thumbnail(data, max(48, min(1024, int(size))))
        # ★ 上限读设置项 `cover_cache_size`，不要写死。
        #   原来这里硬编码 2000，而 `Settings.cover_cache_size` 默认是 800 ——
        #   于是用户在界面上改这个值**完全没效果**（改了也不报错，更难发现）。
        #   读失败就用默认值：封面服务不该因为配置读不出来就挂掉。
        try:
            cap = int(load_config().settings.cover_cache_size) or 800
        except Exception:
            cap = 800
        # ★ cap=0 应该禁用缓存，而不是"无上限"
        #   - cap <= 0 时直接返回，不写入缓存
        #   - 避免配置读取失败时缓存无限增长
        if cap <= 0:
            return Response(content=payload, media_type=mime,
                            headers={"Cache-Control": "public, max-age=86400",
                                     "ETag": f'"{key}-{size}"'})
        with _cover_lock:
            _cover_cache[ck] = (payload, mime)
            _cover_cache.move_to_end(ck)
            # 超上限只淘汰**最久未用**的几条，不要整体清空
            while len(_cover_cache) > cap:
                _cover_cache.popitem(last=False)
    return Response(content=payload, media_type=mime,
                    headers={"Cache-Control": "public, max-age=86400",
                             "ETag": f'"{key}-{size}"'})

def _tracks_for(sid: str, prefix: str = "", limit: int = 0) -> List[Dict[str, Any]]:
    rows = db().tracks(sid, prefix=prefix, limit=limit)
    return [M.rec(r) for r in rows]

def _tracks_capped(sid: str, prefix: str = "") -> tuple:
    """按配置上限取曲目，返回 (记录列表, 是否被截断, 总数)。

    超大库（几万首）一次性对比会吃内存也会让界面变卡，
    所以这里是"软截断"：取前 N 条并在结果里明确告诉用户被截断了，
    用户可以按目录分别对比（left_prefix / right_prefix）。
    """
    cap = max(100, int(load_config().settings.max_compare_tracks or 8000))
    total = db().counts_by_source().get(sid, 0)
    rows = db().tracks(sid, prefix=prefix, limit=cap if total > cap else 0)
    return [M.rec(r) for r in rows], total > len(rows), total

def _step_fingerprint(st: Dict[str, Any]) -> str:
    """单步指纹。

    用**单步**而不是整份方案的哈希，是因为对比页允许用户只勾选一部分步骤执行
    —— 如果只发整份方案的哈希，勾选子集时必然对不上，只能放弃校验。
    按步骤发，前端把选中那几步的指纹带回来，就能做到"既容忍取消勾选、
    又能发现步骤被改动"。
    """
    import hashlib
    h = hashlib.sha1()
    h.update("|".join(str(st.get(k) or "") for k in
                      ("id", "op", "src", "dst", "src_path", "dst_path", "size"))
             .encode("utf-8"))
    return h.hexdigest()[:16]

def _steps_fingerprint(steps: List[Dict[str, Any]]) -> str:
    """给一组执行步骤算指纹，用来校验"要执行的就是用户确认过的那份方案"。

    ★ 为什么必须有（对比页的 `/api/plan` → `/api/execute` 是两次独立请求）：

      用户看方案时库里是状态 A；点执行时可能已经是状态 B（另一个标签在扫描、
      同步工具刚跑过、文件被替换）。执行器会按**现在的**磁盘状态动手，
      而用户以为自己批准的是 A —— 最坏的情况是"批准的是复制，实际删掉了别的"。

    指纹里带上 `size`：同一路径的文件被替换（重下、换质量、损坏后修复）时
    path 不变，只有 size 能便宜地发现它变了。不用 mtime —— 重扫/同步工具
    碰一下就会变，会大量误报。

    与 `search_api._plan_hash` 同一套思路（那边管整理/收集，这边管对比）。
    """
    import hashlib
    h = hashlib.sha1()
    for st in steps:
        h.update(("|".join(str(st.get(k) or "") for k in
                           ("id", "op", "src", "dst", "src_path", "dst_path", "size")))
                 .encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]

def _stable_hash(r: Dict[str, Any]) -> str:
    """取这条记录可用的内容指纹。注意运算符优先级：必须显式判断，否则 "" 会短路。"""
    h = r.get("hash_full")
    if h:
        return str(h)
    if r.get("hash_state") in ("quick", "full") and r.get("hash_quick"):
        return str(r.get("hash_quick"))
    return ""

def _content_clusters(group: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """按内容指纹把一组相似文件细分成"字节完全一致"的子簇。

    没有指纹的文件各自成为一个子簇——也就是永远不敢自动判定为可删。
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for i, r in enumerate(group):
        h = _stable_hash(r)
        key = f"h:{h}" if h else f"?{i}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)
    return [buckets[k] for k in order]

def _hash_groups(groups: List[List[Dict[str, Any]]], mode: str, cfg,
                 job=None, on_progress=None) -> None:
    """对参与查重的文件补算哈希（只算一次，结果回写索引）。

    优化：同一组里体积占多数的那一档才值得哈希——体积唯一的文件必然是独一份，
    算了也只会浪费流量（大库/远端源上这一步省得最多）。

    `job` / `on_progress` 用来把进度报给外层（查重现在跑在后台任务里，
    这一步往往是最慢的，必须能看到"正在算哈希 120/430"）。
    """
    need: Dict[str, Dict[str, Any]] = {}
    for g in groups:
        counter: Dict[int, int] = {}
        for r in g:
            sz = int(r.get("size") or 0)
            counter[sz] = counter.get(sz, 0) + 1
        dominant = {sz for sz, c in counter.items() if c >= 2}
        for r in g:
            if _stable_hash(r):
                continue
            sz = int(r.get("size") or 0)
            if mode == "quick" and dominant and sz not in dominant:
                # 体积在这一组里是独一份 → 不可能是另一份的副本，跳过哈希
                continue
            key = f"{r.get('source_id')}|{r.get('path')}"
            need[key] = r
    if not need:
        return
    # 外层已经给了 job 就复用它报进度；否则自己建一个（独立调用的场景）
    hjob = job if job is not None else get_jobs().create("hash", total=len(need))
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for r in need.values():
        by_source.setdefault(r["source_id"], []).append(r)
    total = len(need)
    for sid, rows in by_source.items():
        if job is not None and job.cancel:
            return
        try:
            src = manager.get(sid)
        except Exception:
            continue
        try:
            S.ensure_hashes(
                sid, src, db(), rows, job=hjob, mode=mode,
                limit_mb=cfg.settings.hash_size_limit_mb,
                probe_kb=cfg.settings.quick_hash_probe_kb,
                on_progress=((lambda d, t: on_progress(d / max(1, t),
                                                       f"校验文件内容 {d}/{t}"))
                             if on_progress else None))
        except Exception:
            continue
    # 回填内存里的哈希值
    for r in need.values():
        row = db().track(r["source_id"], r["path"])
        if row is not None:
            r["hash_quick"] = row["hash_quick"]
            r["hash_full"] = row["hash_full"]
            r["hash_state"] = row["hash_state"]
    return

def _browse_cache_clear() -> None:
    """写操作之后主动清掉浏览缓存（扫描完成、搬运回填索引时调用）。"""
    with _FILE_BROWSE_LOCK:
        _FILE_BROWSE_CACHE.clear()

def _folder_map(sid: str, root: str) -> Dict[str, Dict[str, Any]]:
    """目录 -> {count(本层), count_all(含子目录), bytes, bytes_all}。

    注意必须补齐"中间目录"：聚合是按 parent 分组的，只有**直接放文件**的目录
    才会出现在结果里。像 `D:/Music/歌手/专辑/` 这种，`歌手/` 自己不存文件，
    不补出来的话它在列表里就会显示成 0 首。
    """
    root_n = P.norm(root)
    _ck = ("folders", sid, root_n)
    _now = time.time()
    with _FILE_BROWSE_LOCK:
        _hit = _FILE_BROWSE_CACHE.get(_ck)
        if _hit is not None and _now - _hit[0] < _FILE_BROWSE_TTL:
            # ★ 必须返回**拷贝**：`files_folders` 会就地把 `rel` / `depth`
            #   改成相对子目录的版本，直接返回缓存里的对象会把缓存改脏。
            return {k: dict(v) for k, v in _hit[1].items()}
    rows = db().query(
        "SELECT parent AS p, COUNT(*) AS c, COALESCE(SUM(size),0) AS b FROM track "
        "WHERE source_id=? GROUP BY parent", (sid,))
    nodes: Dict[str, Dict[str, Any]] = {}

    def ensure(path: str) -> Dict[str, Any]:
        node = nodes.get(path)
        if node is None:
            rel = path[len(root_n) + 1:] if root_n and path.startswith(root_n + "/") else path
            node = {"path": path, "rel": rel, "count": 0, "count_all": 0,
                    "bytes": 0, "bytes_all": 0,
                    "depth": len([x for x in rel.split("/") if x])}
            nodes[path] = node
        return node

    for r in rows:
        parent = P.norm(r["p"] or "")
        if not parent:
            continue
        node = ensure(parent)
        node["count"] += r["c"]
        node["count_all"] += r["c"]
        node["bytes"] += r["b"]
        node["bytes_all"] += r["b"]
        # 补齐所有祖先目录
        cur = parent
        while True:
            nxt = P.parent(cur)
            if not nxt or nxt == cur or (root_n and len(nxt) <= len(root_n) and nxt != root_n):
                break
            if root_n and not (nxt == root_n or nxt.startswith(root_n + "/")):
                break
            ensure(nxt)
            cur = nxt

    # 自底向上把子目录的数量累加到祖先
    for node in sorted(nodes.values(), key=lambda x: -x["depth"]):
        par = nodes.get(P.parent(node["path"]))
        if par is None or par is node:
            continue
        par["count_all"] += node["count_all"]
        par["bytes_all"] += node["bytes_all"]
    # 写回缓存（存副本，避免调用方就地改脏）
    with _FILE_BROWSE_LOCK:
        _FILE_BROWSE_CACHE[_ck] = (time.time(),
                                  {kk: dict(vv) for kk, vv in nodes.items()})
        while len(_FILE_BROWSE_CACHE) > 32:
            _FILE_BROWSE_CACHE.pop(next(iter(_FILE_BROWSE_CACHE)))
    return nodes

def _folder_rows(sid: str, root: str) -> List[Dict[str, Any]]:
    """把索引里的曲目按目录聚合，得到"每个文件夹有多少首"（含子目录汇总）。"""
    return [n for n in _folder_map(sid, root).values() if n["rel"]]

def _dir_summary(sid: str, src, path: str) -> Dict[str, int]:
    """一个目录下的子目录各有多少音频文件（读真实文件系统，用于未索引的目录）。"""
    out = {"dirs": 0, "audio": 0}
    try:
        entries = src.listdir(path)
    except Exception:
        return out
    for e in entries:
        if e.is_dir:
            out["dirs"] += 1
        elif P.is_audio(e.name):
            out["audio"] += 1
    return out

def _disk_view(path: str, src=None) -> Dict[str, Any]:
    """某个目录所在磁盘的余量。拿不到就返回空（不影响列表本身）。"""
    out: Dict[str, Any] = {"total": 0, "used": 0, "free": 0, "percent": None,
                           "writable": True, "dir": ""}
    try:
        probe = path or (getattr(src, "root", "") if src else "")
        if not probe:
            return out
        # 目录可能还不存在（新建目录的场景）→ 往上一层层找已存在的
        p = probe
        for _ in range(40):
            if os.path.isdir(p):
                break
            up = os.path.dirname(p)
            if up == p:
                break
            p = up
        if not os.path.isdir(p):
            return out
        u = shutil.disk_usage(p)
        out["total"] = int(u.total)
        out["used"] = int(u.used)
        out["free"] = int(u.free)
        out["percent"] = round(100.0 * u.used / u.total, 1) if u.total else None
        out["dir"] = P.norm(p)
        out["writable"] = bool(getattr(src, "can_write", True)) if src else True
    except Exception:
        pass
    return out

def _reindex_after_transfer(src_id: str, dst_id: str,
                            moved: List[str],
                            dst_dirs: Optional[List[str]] = None,
                            dst_files: Optional[List[str]] = None) -> None:
    """传输完把索引对齐：源侧删记录、目标侧重扫这几个文件所在的目录。

    不做的话会出现"文件已经不在源里了、搜索还能搜到"以及
    "新传过去的文件搜不到"。
    """
    db().delete_tracks_by_paths(src_id, moved)
    _browse_cache_clear()          # 索引变了，目录聚合缓存必须失效
    # ★ 重扫的必须是**目标源内**的相对目录，不能拿源侧的绝对路径当 subpath。
    #   原来 `dirs = {P.parent(p) for p in moved}` 拿到的是源库的绝对路径
    #   （如 `D:/Src/Artist`），却传给了 `dst_id` 去扫 —— 目标源根下根本没有
    #   这个路径，扫描必然抛异常并被 `except: pass` 吞掉，于是
    #   **目标库永远搜不到刚传过去的文件**（源记录还删了，等于凭空消失）。
    # ★ 根目录（`""`）不能被过滤掉。
    #   原来 `if str(d).strip("/")` 把空串当假值筛掉 —— 传到**目标库根目录**
    #   的文件因此永远不会进索引（源侧记录已删，等于凭空消失）。
    #   根目录没法用 subpath 扫（`subpath=""` 等于整库重扫），所以对根目录
    #   改用"按文件刷新"，只处理刚搬过去的那几个。
    dirs = {str(d).strip("/") for d in (dst_dirs or [])}
    for d in sorted(x for x in dirs if x)[:50]:
        try:
            S.scan_source(dst_id, manager.get(dst_id), db(), subpath=d,
                          max_files=0)
        except Exception as exc:
            log.warning("搬运后重扫目标目录 %s 失败: %s", d, exc)
    # 唯一调用方（routes/files.py::files_transfer）总会传 dst_files；
    # 根目录只能按文件刷新（subpath="" 等于整库重扫）。
    if "" in dirs and dst_files:
        try:
            refresh_tracks(dst_id, [p for p in dst_files if p])
        except Exception as exc:                              # noqa: BLE001
            log.warning("搬运到目标根目录后刷新索引失败: %s", exc)


def refresh_tracks(sid: str, paths: List[str]) -> Dict[str, Any]:
    """按路径批量重扫（改完标签后刷新索引，否则搜索里还是旧值）。

    跟 refresh_track 一样的逻辑，只是按路径而不是 track id 定位 ——
    标签编辑那边拿到的就是路径。
    """
    if not paths:
        return {"ok": 0, "failed": 0}
    _browse_cache_clear()          # 索引要变了，目录聚合缓存立刻失效
    src = manager.get(sid)
    ok = failed = 0
    updates: List[Dict[str, Any]] = []
    covers: List[tuple] = []
    for p in paths:
        p = P.norm(p)
        try:
            st = src.stat(p)
            if st is None:
                db().remove_track(sid, p)
                continue
            name = P.basename(p)
            data, head_only = S._fetch_bytes(src, p, st.size, P.ext(name))
            meta = A.parse_bytes(data, name, head_only=head_only)
            base = {"source_id": sid, "path": p, "rel_path": src.rel_path(p),
                    "name": name, "parent": P.parent(p), "ext": P.ext(name),
                    "size": st.size or 0, "mtime": st.mtime or 0}
            new = S._row_from_meta(meta, base)
            if meta.cover_data:
                thumb, mime, w, h = A.make_thumbnail(meta.cover_data)
                hh = A.sha1_bytes(thumb)
                covers.append((hh, mime, 0, 0, thumb))
                new["cover_hash"] = hh
                new["cover_mime"] = mime
                new["has_cover"] = 1
            updates.append({k: new.get(k) for k in S._TRACK_FIELDS})
            ok += 1
        except Exception:
            failed += 1
    try:
        if covers:
            for c in covers:
                db().put_cover(*c)
        if updates:
            db().upsert_tracks(updates)
    except Exception:
        pass
    return {"ok": ok, "failed": failed, "covers": len(covers)}

# ---------------------------------------------------------------- 入参类型校验
#
# 为什么需要：所有写端点都是 `payload: Dict[str, Any]`，**FastAPI 不做类型校验**。
# 客户端把 `paths` 传成数字，就会 `for p in 123` → TypeError → 全局兜底变成
# **500「服务端出错」**，用户完全不知道是自己传错了。
# 实测（类型混淆模糊测试）一次就打出 17 种 500，分散在
# `/api/files/transfer`、`/api/folders/create|rename`、`/api/tags/preview|write`。
#
# 这两个 helper 把"取值 + 类型检查 + 清晰 400"收口到一处，新端点照着用即可。

def as_list(payload: Dict[str, Any], key: str, what: str = "",
            max_items: Optional[int] = None) -> List[Any]:
    """从 payload 里取数组。缺省 → `[]`；类型不对 → 400。"""
    v = payload.get(key)
    if v is None:
        return []
    if isinstance(v, (str, bytes)) or not isinstance(v, (list, tuple)):
        raise HTTPException(
            400, f"{what or key} 必须是数组（收到 {type(v).__name__}）")
    out = list(v)
    if max_items is not None and len(out) > max_items:
        raise HTTPException(
            400, f"{what or key} 一次最多 {max_items} 项（收到 {len(out)} 项）")
    return out


def as_text(payload: Dict[str, Any], key: str, what: str = "",
            default: str = "") -> str:
    """从 payload 里取字符串。缺省 → `default`；类型不对 → 400。"""
    v = payload.get(key)
    if v is None:
        return default
    if not isinstance(v, str):
        raise HTTPException(
            400, f"{what or key} 必须是字符串（收到 {type(v).__name__}）")
    return v

def as_dicts(payload: Dict[str, Any], key: str, what: str = "",
             max_items: Optional[int] = None) -> List[Dict[str, Any]]:
    """取"对象数组"。缺元素不是对象 → 400。

    ★ 为什么单独一个：`isinstance(v, list)` 只能挡住"不是数组"，
    挡不住"数组里塞了数字"。`steps=[1,2]` 会走到 `st.get("id")` 上抛
    AttributeError → 500。实测 `/api/execute`、`/api/files/move`、
    `/api/duplicates/keep` 都有这个问题。
    """
    out = as_list(payload, key, what, max_items=max_items)
    for i, x in enumerate(out, 1):
        if not isinstance(x, dict):
            raise HTTPException(
                400, f"{what or key} 第 {i} 项必须是对象（收到 {type(x).__name__}）")
    return out

def in_source_rel(src, raw: str, what: str = "路径") -> str:
    """把**用户传入**的路径规整成"源内相对路径"；越界直接 400。

    ★ 为什么必须有这一层：`src.abs_path()` 对"跨盘绝对路径"是**原样返回**的
      （早先为了修 `C:/Music/D:/Other` 那种拼接），于是用户传一个绝对路径
      就能读到/列到源根之外。实测：

        · `GET /api/cover-raw?sid=..&path=C:/.../config.toml`
          → 200，把库外文件当封面吐回来
        · `GET /api/cover-raw?sid=..&path=../outside/config.toml` → 同样 200
        · `GET /api/files/list?sid=..&path=<库外绝对目录>` → 200，列出库外文件

      所以"信任边界"必须在端点入口收口：绝对路径必须落在根内，
      相对路径不许出现 `..`。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    p = P.norm(raw)
    root_n = P.norm(getattr(src, "root", "") or "").rstrip("/")
    is_abs = bool(re.match(r"^[A-Za-z]:/", p)) or p.startswith("//") or p.startswith("/")
    if is_abs:
        if not root_n or not P.inside_root(root_n, p):
            raise HTTPException(400, f"{what}超出数据源范围")
        # junction / 符号链接：字符串在根内，实际指向根外
        if not P.inside_root_real(root_n, p):
            raise HTTPException(400, f"{what}经过目录联接指向了数据源之外")
        if p == root_n:
            return ""
        return p[len(root_n) + 1:].lstrip("/")
    if P.has_dotdot(p):
        raise HTTPException(400, f"{what}不允许包含 ..")
    # ★ 相对路径也必须检查 junction
    #   绕过路径：在源根内创建 junction evil_link → C:\Windows\System32
    #   请求 path=evil_link/drivers/etc/hosts
    #   - 相对路径，没有 ..，通过 has_dotdot 检查
    #   - inside_root("Y:\music", "Y:\music\evil_link\...") → True（字符串前缀）
    #   - 但 inside_root_real 未调用，实际读取到系统文件
    #   只对本地源做：junction 只存在于本机文件系统；网络源的 root 可能是
    #   URL 或空串，套 inside_root 会把所有相对路径误判为越界。
    if getattr(src, "kind", "") == "local" and root_n:
        if not P.inside_root_real(root_n, src.abs_path(p)):
            raise HTTPException(400, f"{what}经过目录联接指向了数据源之外")
    return p.strip("/")
