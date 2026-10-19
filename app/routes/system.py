"""system.py —— 见 `app/main.py` 顶部导航索引。

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
    APP_VERSION,
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

@router.get("/api/system")
def system_info() -> Dict[str, Any]:
    cfg = load_config()
    return {
        "app": "Music Sync Hub",
        "version": APP_VERSION,
        # ★ 测试模式标记（`MSH_TEST_MODE=1` 时为 True）。
        #   前端据此再显示一条顶部横幅；脚本/自动化也用它判断"这是演示环境"。
        #   正常使用永远是 False —— 不改变任何业务行为。
        "test_mode": test_mode(),
        "data_dir": str(data_dir()),
        "db": str(db().path),
        "db_size": _db_size(),
        "counts": db().counts_by_source(),
        "source_count": len(cfg.sources),
        "jobs": get_jobs().active(),
        "capabilities": {
            "mutagen": A._HAS_MUTAGEN,
            "pillow": A._HAS_PIL,
        },
        # ★ 音频扩展名由**后端唯一来源**提供给前端。
        #   前端要判断"这个文件是不是音频"（候选区只收音频、跨库传输只收音频），
        #   如果在 JS 里抄一份清单，两边迟早会走偏 ——
        #   而且已经有一条测试专门禁止在 Python 侧抄第二份（`test_non_audio.py`）。
        #   让前端从这里拿，就永远不会分叉。
        # ★ 版本类别（伴奏 / 重制 / Remix …）也只有这一个来源。
        #   踩过的坑：前端 views.duplicates.js 里自己抄了一份 12 项的
        #   VERSION_LABEL 映射，注释还写着"与后端一致"；后端其实是 14 项，
        #   漏了 clip 和 speed —— 这两类在查重页显示成英文而不是中文。
        #   两份清单一定会分叉，和音频扩展名是同一个病。
        #   这里把 VERSION_KINDS（长描述）和 VERSION_LABELS（短标签）
        #   一起带出去，那两个常量终于都有消费者。
        "audio_exts": sorted(P.AUDIO_EXTS),
        "version_kinds": [
            {"kind": k, "label": P.VERSION_LABELS.get(k, k), "desc": d}
            for k, d in P.VERSION_KINDS
        ],
    }


@router.get("/api/source-types")
def source_types() -> List[Dict[str, Any]]:
    # 注意是 `Any` 不是 `str`：`available_types()` 里除了 kind/label/hint
    # 还有 `experimental`（布尔）。声明成 `Dict[str, str]` 会让 FastAPI
    # 做响应校验时抛 ResponseValidationError → 500。
    # （实测就是这么炸的，靠新加的全局异常日志一眼看到原因。）
    return available_types()


@router.get("/api/sources")
def list_sources() -> List[Dict[str, Any]]:
    return [_source_view(s) for s in load_config().sources]


@router.get("/api/sources/status")
def sources_status() -> List[Dict[str, Any]]:
    """轻量探活：只 stat 一下每个数据源的根目录，不做目录遍历。

    侧栏的"在线 / 离线"小圆点靠它——只测根目录是否可读，
    所以即使库里有几万首也是毫秒级。
    """
    out: List[Dict[str, Any]] = []
    for sc in load_config().sources:
        item: Dict[str, Any] = {"id": sc.id, "enabled": sc.enabled, "ok": False,
                                "error": "", "checked_at": time.time()}
        if sc.enabled is False:
            item["error"] = "已停用"
            out.append(item)
            continue
        if not (sc.root or "").strip():
            item["error"] = "未设置路径"
            out.append(item)
            continue
        t0 = time.time()
        try:
            src = manager.get(sc.id)
            st = src.stat("")
            if st is None:
                item["error"] = f"路径不存在: {sc.root}"
            else:
                item["ok"] = True
                # 根路径被配置成一个文件是明显的配置错误，单独提示
                if st.is_dir is False:
                    item["ok"] = False
                    item["error"] = "根路径是文件，不是目录"
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        item["latency"] = round(time.time() - t0, 3)
        out.append(item)
    return out


@router.post("/api/sources")
def create_source(payload: Dict[str, Any] = Body(...),
                   exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    cfg = load_config()
    # ★ 缺必填字段要 400，不能让它 TypeError 变 500。
    #   `SourceConfig.from_dict({})` 会抛 `missing required positional
    #   arguments: 'id' and 'name'` —— 用户看到的是"服务端出错"，
    #   完全不知道是自己少填了东西。（空 body 打这个端点就能复现）
    if not str(payload.get("id") or "").strip() and not str(payload.get("root") or "").strip():
        raise HTTPException(400, "缺少数据源信息：至少要给 root（库根目录）")
    try:
        sc = SourceConfig.from_dict(payload)
    except TypeError as exc:
        raise HTTPException(400, f"数据源参数不完整：{exc}") from exc
    if not sc.id:
        sc.id = f"src{int(time.time())}"
    if any(s.id == sc.id for s in cfg.sources):
        raise HTTPException(409, f"数据源 id 已存在: {sc.id}")
    _validate_source(sc)
    password = payload.get("password")
    if password:
        sc.secret_ref = sc.secret_ref or f"{sc.id}_password"
        save_secret(sc.secret_ref, password)
    cfg.sources.append(sc)
    try:
        save_config(cfg)
        db().upsert_source(sc.id, sc.name, sc.type, sc.root)
    except Exception as exc:                          # noqa: BLE001
        # ★ 以前先写 config 再写 DB。DB 被锁/出错时，config 里已经有这个源、
        #   索引表里没有 —— 用户看到数据源出现但一首歌都没有，
        #   再点"新建"还会 409「id 已存在」。这里把配置回滚掉。
        try:
            cfg.sources = [x for x in cfg.sources if x.id != sc.id]
            save_config(cfg)
        except Exception:                             # noqa: BLE001
            pass
        raise HTTPException(
            503, f"数据源没能保存（{type(exc).__name__}）：{exc}。"
                 f"索引库可能正忙，请稍后重试。") from exc
    manager.reset(sc.id)
    return _source_view(sc)


@router.put("/api/sources/{sid}")
def update_source(sid: str, payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    cfg = load_config()
    idx = next((i for i, s in enumerate(cfg.sources) if s.id == sid), None)
    if idx is None:
        raise HTTPException(404, "数据源不存在")
    merged = {**cfg.sources[idx].to_dict(), **{k: v for k, v in payload.items() if k != "password"}}
    sc = SourceConfig.from_dict(merged)
    _validate_source(sc)
    if payload.get("password"):
        sc.secret_ref = sc.secret_ref or f"{sid}_password"
        save_secret(sc.secret_ref, payload["password"])
    cfg.sources[idx] = sc
    save_config(cfg)
    db().upsert_source(sc.id, sc.name, sc.type, sc.root)
    manager.reset(sid)
    return _source_view(sc)


@router.delete("/api/sources/{sid}")
def delete_source(sid: str,
                  purge: bool = Query(False, description="同时清空该源的索引记录"),
                  exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    cfg = load_config()
    before = len(cfg.sources)
    cfg.sources = [s for s in cfg.sources if s.id != sid]
    if len(cfg.sources) == before:
        raise HTTPException(404, "数据源不存在")
    save_config(cfg)
    manager.reset(sid)
    # ★ 索引记录**必须**一起删：源已经从配置里去掉，这些 track 行再也
    #   访问不到（/api/library/<sid> 直接 404），只会让 /api/state 的
    #   tracks 数字对不上、让查重扫到幽灵数据。
    #   `purge` 参数保留只为兼容老客户端，不再影响索引清理（文件从不删除）。
    try:
        db().delete_source(sid)
    except Exception as exc:                          # noqa: BLE001
        log.warning("删除数据源 %s 的索引记录失败: %s", sid, exc)
    return {"ok": True, "purged": True}


@router.get("/api/logs")
def get_logs(limit: int = 200, level: str = "", full: bool = False,
             lines: int = 400) -> Dict[str, Any]:
    """后台日志：默认返回**内存里的最近若干条**（即时、不用翻文件）。

    `full=true` 时读日志文件尾部（`lines` 控制行数）——
    用于"我要把完整日志发给别人看"。
    `level` 可过滤（ERROR / WARNING / INFO）。
    """
    from ..core import applog
    if full:
        out = applog.read_file(lines=lines, level=level)
        out["memory"] = applog.recent(limit=limit, level=level)
        return out
    return {"path": applog.log_path(),
            "items": applog.recent(limit=limit, level=level)}


@router.post("/api/logs/clear")
def clear_logs() -> Dict[str, Any]:
    """清空**内存**里的日志缓冲（不动文件）。

    用途：复现一个问题之前先清一下，之后看到的就全是这次操作的日志。
    """
    from ..core import applog
    applog.clear()
    logging.getLogger("msh").info("内存日志缓冲已清空")
    return {"ok": True}


@router.get("/api/drives")
def list_drives() -> Dict[str, Any]:
    """这台机器上的盘（给「添加数据源」挑选，含可移除设备标记）。

    `measure=False` —— 批量列盘时不做读速测试，否则打开对话框要等好几秒。
    `portable` 是**便携设备（MTP）**：只列出来 + 说明为什么用不了，
    界面据此显示成灰色、不允许添加为数据源。
    """
    return {"drives": DEV.list_drives(),
            "portable": DEV.list_portable_devices()}


@router.get("/api/device/probe")
def probe_device(path: str = Query("", description="要探测的目录")) -> Dict[str, Any]:
    """探测一个目录落在什么盘上（可移除 / 网络 / 内置 / 慢速）。

    供「添加数据源」在用户填完路径后给出**针对性建议**：
    U 盘默认只读 + 低并发，网络位置默认只读。
    """
    if not path:
        raise HTTPException(400, "缺少 path")
    expanded = os.path.expanduser(path)
    if not os.path.isdir(expanded):
        raise HTTPException(404, f"目录不存在或读不到: {path}")
    info = DEV.detect(expanded, measure=True)
    return {"info": info.to_dict(), "suggested": DEV.suggested_defaults(info),
            "notes": list(info.notes), "label": info.label}


@router.post("/api/sources/{sid}/test")
def test_source(sid: str) -> Dict[str, Any]:
    t0 = time.time()
    # ★ 必须在 try **外面**解析：这个调用自己就会抛（数据源不存在 → 404）。
    #   放进 try 里的话会被下面的 `except Exception` 吞掉，
    #   变成一个"连接失败"的 200 响应，用户看不出真正原因。
    sc = resolve_source(sid)
    try:
        src = manager.get(sid)
        if src is None:
            raise SourceError(f"数据源不可用: {sid}")
        items = src.listdir("")
        audio = [e for e in items if (not e.is_dir) and P.is_audio(e.name)]
        dirs = [e for e in items if e.is_dir]
        return {
            "ok": True, "latency": round(time.time() - t0, 3),
            "entries": len(items), "dirs": len(dirs), "audio": len(audio),
            "sample": [e.name for e in items[:12]],
            "readable": True,
            # 顺带把盘的性质带回去：U 盘 / 网络位置要给出针对性提示
            "device": _device_view(sc),
        }
    except (SourceError, ReadOnlyError) as exc:
        return {"ok": False, "latency": round(time.time() - t0, 3),
                "error": str(exc), "tip": _connect_tip(sc),
                "device": _device_view(sc)}
    except Exception as exc:
        return {"ok": False, "latency": round(time.time() - t0, 3),
                "error": f"{type(exc).__name__}: {exc}", "tip": _connect_tip(sc),
                "device": _device_view(sc)}


@router.post("/api/sources/{sid}/browse")
def browse(sid: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """浏览远端目录树（不含元数据，速度快），用于配置路径 / 手动挑选。"""
    src = manager.get(sid)
    path = (payload or {}).get("path", "")
    try:
        items = src.listdir(path)
    except SourceError as exc:
        raise HTTPException(400, str(exc))
    dirs = sorted([e.name for e in items if e.is_dir and not e.name.startswith(".")])
    # ★ 这里原来是 `sorted([{...}])` —— 直接给**字典列表**排序会抛
    #   `TypeError: '<' not supported between instances of 'dict' and 'dict'`。
    #   于是任何含 2 首以上音频的目录，浏览接口都 500（实测）。
    #   必须显式给 key。
    files = sorted([{"name": e.name, "size": e.size, "mtime": e.mtime}
                    for e in items if not e.is_dir and P.is_audio(e.name)],
                   key=lambda d: str(d["name"]).lower())
    return {"source": sid, "path": path, "abs": src.abs_path(path) if path else src.root,
            "dirs": dirs, "files": files[:500], "file_count": len(files)}


@router.post("/api/scan/{sid}")
def start_scan(sid: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    opts = payload or {}
    # ★ 顺序很重要：**先校验，再占独占位**。
    #   原来这里先 create 任务再 resolve_source，如果 sid 不存在，
    #   resolve_source 抛异常 → 那个任务的独占位就永远占着，
    #   之后所有写操作都提示"已有任务在执行"，用户被锁死，
    #   而任务列表里那条记录还停在 running（心跳也不动）。
    sc = resolve_source(sid)          # 不存在 → 404
    src = manager.get(sid)            # 拿不到实例 → 也不该占位
    if src is None:
        raise HTTPException(404, f"数据源不可用: {sid}")
    cfg = load_config()
    # 扫描会写索引，和文件移动冲突（移动后索引对不上）→ 也要独占
    try:
        job = get_jobs().create("scan", owner=str(opts.get("client") or ""))
    except JobBusy as exc:
        raise HTTPException(409, str(exc))
    workers = int(opts.get("workers") or cfg.settings.scan_workers)
    # 慢速盘 / 可移除设备：压并发 + 跳过按内容的全量哈希。
    # 不这样做的话，U 盘上开 8 个 worker 只会互相抢带宽，
    # 总耗时反而更长，进度条几乎不动（用户以为卡死）。
    extras = dict(sc.extras or {})
    slow_disk = bool(extras.get("slow_disk"))
    if slow_disk:
        workers = DEV.effective_workers(extras, workers)
    skip_full_hash = bool(extras.get("skip_full_hash")) or slow_disk

    def run() -> None:
        try:
            S.scan_source(
                sid, src, db(), job=job,
                subpath=opts.get("subpath", ""),
                workers=workers,
                compute_hash=bool(opts.get("compute_hash")),
                hash_limit_mb=int(opts.get("hash_limit_mb") or cfg.settings.hash_size_limit_mb),
                quick_probe_kb=int(opts.get("quick_probe_kb") or cfg.settings.quick_hash_probe_kb),
                max_files=int(opts.get("max_files") or 0),
                skip_full_hash=skip_full_hash,
            )
        except Exception as exc:                              # noqa: BLE001
            get_jobs().finish(job, error=f"{type(exc).__name__}: {exc}")
        else:
            # ★ `scan_source` 现在只负责"自己创建的 job"；这个 job 是路由创建的，
            #   成功路径必须由路由自己 finish —— 否则任务永远 running、
            #   busy() 恒真，所有写接口 409。
            get_jobs().finish(job)
        finally:
            # ★ 扫描改动了索引 → 目录聚合缓存必须失效，
            #   否则扫完之后 20 秒内界面还显示旧的"每目录多少首"。
            _browse_cache_clear()

    threading.Thread(target=run, name=f"scan-{sid}", daemon=True).start()
    return {"job": job.to_dict(), "source": sc.to_dict()}


@router.get("/api/settings")
def get_settings() -> Dict[str, Any]:
    cfg = load_config()
    return {"settings": cfg.settings.__dict__, "config_path": str(data_dir() / "config.toml")}


@router.put("/api/settings")
def put_settings(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """保存设置。**每个值都要校验**，不合法就拒绝并说清原因。

    没有校验的话，一个天文数字（scan_workers = 99999999）会直接开
    同等数量的线程把机器打爆；NaN / inf 会让后面的比较和格式化全失效；
    路径里混进控制字符会让写文件时莫名其妙失败 —— 而且这些都要等到
    **下次启动**才炸，极难查。所以宁可现在拒绝。
    """
    from ..core.config import SettingsError, validate_setting
    cfg = load_config()
    # ★ 改变"文件往哪去"的设置，在有任务跑的时候不许改。
    #
    #   为什么单独挑这三个：`trash_dir` / `station_dir` / `staging_dir` 决定
    #   **文件最终落在哪个目录**。如果一个删除或中转任务正在跑，中途把这几个
    #   改掉，同一个任务的前后几步就会落到不同目录 —— 文件确实还在（都在
    #   回收站/中转站里），但用户按新路径去找会找不到，属于"看着成功、结果
    #   对不上"的那类问题。
    #
    #   其余设置（线程数、时长容差、缓存条数…）都是"下一次操作用到"的，
    #   随时可改，不该因为有个任务在跑就把整个设置页锁死。
    _PATH_SETTINGS = {"trash_dir", "station_dir", "staging_dir"}
    _touched = _PATH_SETTINGS & set((payload or {}).keys())
    if _touched:
        get_jobs().ensure_free()          # 有独占任务在跑 → 抛 JobBusy（409）
    applied: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for k, v in (payload or {}).items():
        if not hasattr(cfg.settings, k):
            # ★ 以前是 `continue` —— 客户端拼错字段名会得到 200，
            #   以为存上了，其实什么都没发生（实测 max_workers / scan_batch
            #   这类"看着该有"的名字都会被静默吞掉）。
            errors[k] = "不是可识别的设置项"
            continue
        try:
            applied[k] = validate_setting(k, v)
        except SettingsError as exc:
            errors[k] = exc.reason
        except Exception as exc:                      # noqa: BLE001
            errors[k] = f"{type(exc).__name__}: {exc}"
    if errors and not applied:
        raise HTTPException(400, "设置不合法：" + "；".join(
            f"{k} {v}" for k, v in errors.items()))
    for k, v in applied.items():
        setattr(cfg.settings, k, v)
    try:
        save_config(cfg)
    except Exception as exc:                          # noqa: BLE001
        # ★ 写盘失败（比如 config.toml 被设成只读）时，内存里的配置
        #   已经被改了。不回滚的话 GET /api/settings 会显示新值，
        #   而磁盘上还是旧值 —— 重启后又变回去，极难解释。
        try:
            load_config(force=True)
        except Exception:                             # noqa: BLE001
            pass
        raise HTTPException(
            500, f"设置没能写入磁盘（{type(exc).__name__}: {exc}）。"
                 f"已恢复为磁盘上的配置。") from exc
    out: Dict[str, Any] = {"settings": cfg.settings.__dict__}
    if errors:
        # 部分成功：把不合法的那些明确报出来，别让用户以为全存上了
        out["errors"] = errors
        out["warning"] = "部分设置未保存：" + "；".join(
            f"{k} {v}" for k, v in errors.items())
    return out


@router.post("/api/reload")
def reload_config() -> Dict[str, Any]:
    cfg = load_config(force=True)
    manager.close_all()
    return {"ok": True, "sources": len(cfg.sources)}
