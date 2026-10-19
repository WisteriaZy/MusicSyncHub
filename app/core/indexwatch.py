"""索引自动刷新 —— 让"索引和磁盘不一致"这件事自己收敛。

## 为什么需要

没有它，索引**只在你手动点扫描时**更新。库被别的程序改过（播放器写标签、
同步工具搬文件、你在资源管理器里删了东西）之后，索引会一直停在旧状态：
搜得到但打不开、删了还能看见（幽灵）、统计数字对不上。

这个项目里"索引和磁盘不一致"是最贵的一类问题 —— 它不报错，只是让界面说谎。

## 三个机制

1. **目录 mtime 探测**：新增 / 删除 / 改名都会改父目录的 mtime。
   只 stat **已有曲目所在的目录**（几百个），变了就只重扫那个子目录。
2. **文件指纹探测**：内容被替换（大小 / mtime 变）不会改目录 mtime。
   对索引里的每一行做 `stat` 比对（本地盘 2301 个文件约几十毫秒）。
3. **陈旧兜底**：某个源的 `last_scan` 超过 `auto_full_rescan_hours` 小时，
   没有任何变化也整源扫一次 —— 兜住"新目录里新增了文件"这种目录级
   探测看不见的情况。

## 三条硬约束

* **绝不在有写任务时抢跑**：每轮先看 `get_jobs().busy`，忙就直接跳过。
  扫描本身走同一个 `scan_source`，所以"写入索引 / 清缓存 / 清理失效行"
  的规则和手动扫描**完全一致**，不另起一套。
* **首次运行只播种**：先把目录 mtime 记下来、不触发重扫，避免一启动就把
  整个库扫一遍。
* **单轮有上限**：最多重扫 `max_dirs` 个目录，剩下的下一轮继续，
  避免一次性卡住后台。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_lock = threading.Lock()
_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_dir_mtimes: Dict[Tuple[str, str], float] = {}
_last_result: Dict[str, Any] = {}
_last_run: float = 0.0


# ------------------------------------------------------------------ 探测

def _parent_rel(path: str) -> str:
    p = str(path or "").replace("\\", "/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def probe_source(sid: str, src: Any, dbh: Any,
                 max_files: int = 50000) -> List[str]:
    """返回"需要重扫"的子目录（相对路径，'' 表示根）。

    只做 stat，不读文件内容。首次运行只记录目录 mtime，不触发重扫。
    """
    changed: Dict[str, bool] = {}
    dirs_seen: Dict[str, float] = {}
    try:
        rows = dbh.tracks(sid)
    except Exception:                                      # noqa: BLE001
        return []
    for r in rows[:max_files]:
        path = str(r["path"])
        prel = _parent_rel(src.rel_path(path))
        try:
            st = src.stat(path)
        except Exception:                                  # noqa: BLE001
            st = None
        if st is None:
            changed[prel] = True          # 文件没了 → 重扫它所在目录来清理
            continue
        if (int(st.size or 0) != int(r["size"] or 0)
                or abs(float(st.mtime or 0) - float(r["mtime"] or 0)) > 1.0):
            changed[prel] = True
    # 目录 mtime：新增/删除/改名都会改它
    for d in {_parent_rel(src.rel_path(str(r["path"]))) for r in rows[:max_files]}:
        try:
            st = src.stat(d) if d else src.stat("")
        except Exception:                                  # noqa: BLE001
            continue
        if st is None:
            continue
        dirs_seen[d] = float(st.mtime or 0)
        old = _dir_mtimes.get((sid, d))
        # ★ 目录 mtime 的变化量常常只有几十毫秒（新建/删除文件瞬间完成），
        #   用 1 秒阈值会漏判 —— 实测加一个文件后 diff 只有 0.06 秒。
        #   目录只要 mtime 变了就值得看一眼（重扫一个目录很便宜）。
        if old is not None and abs(old - dirs_seen[d]) > 1e-6:
            changed[d] = True
    _dir_mtimes.update({(sid, d): m for d, m in dirs_seen.items()})
    return sorted(changed)


# ------------------------------------------------------------------ 单轮

def _stale(dbh: Any, sid: str, hours: float) -> bool:
    if not hours:
        return False
    try:
        row = dbh.query_one(
            "SELECT last_scan FROM source WHERE id=?", (sid,))
    except Exception:                                      # noqa: BLE001
        return False
    last = float((row["last_scan"] if row else 0) or 0)
    return (time.time() - last) > hours * 3600.0


def tick_once(force: bool = False, max_dirs: int = 8) -> Dict[str, Any]:
    """跑一轮自动刷新。返回统计信息（也给测试用）。"""
    global _last_result, _last_run
    from . import jobs as J

    if not force:
        try:
            if J.get_jobs().busy():
                return {"skipped": "busy"}       # 有写任务在跑，这轮让路
        except Exception:                        # noqa: BLE001
            pass

    from .config import load_config
    from .. import deps as _d                 # 延迟导入，避免循环依赖

    cfg = load_config()
    st = cfg.settings
    out: Dict[str, Any] = {"at": time.time(), "sources": {}}
    for sc in cfg.sources:
        info: Dict[str, Any] = {}
        if getattr(sc, "type", "") != "local":
            info["skipped"] = "非本地源（网络盘 stat 太贵，交给手动扫描）"
            out["sources"][sc.id] = info
            continue
        try:
            src = _d.manager.get(sc.id)
            dbh = _d.db()
        except Exception as exc:                           # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"
            out["sources"][sc.id] = info
            continue
        try:
            changed = probe_source(sc.id, src, dbh)
        except Exception as exc:                           # noqa: BLE001
            info["error"] = f"探测失败 {type(exc).__name__}: {exc}"
            out["sources"][sc.id] = info
            continue
        info["changed_dirs"] = changed[:40]
        scanned = []
        # ★ 巡检期间持**独占 Job**：只查一次 busy() 挡不住"查完到开扫"之间的
        #   窗口。外部 Job 交给 scan_source 托管（它不 finish），这里 finally 释放。
        _wjob = None
        try:
            from . import jobs as _J
            _wjob = _J.get_jobs().create("scan", total=0, exclusive=True)
        except Exception:                                  # noqa: BLE001
            _wjob = None
        for d in changed[:max_dirs]:
            try:
                _d.S.scan_source(sc.id, src, dbh, subpath=d, max_files=0,
                                 workers=2, job=_wjob)
                scanned.append(d)
            except Exception as exc:                       # noqa: BLE001
                info.setdefault("errors", []).append(
                    f"{d or '(根)'}: {type(exc).__name__}: {exc}")
        if not changed and _stale(dbh, sc.id, float(st.auto_full_rescan_hours or 0)):
            try:
                _d.S.scan_source(sc.id, src, dbh, subpath="", max_files=0,
                                 workers=2)
                info["full_rescan"] = True
            except Exception as exc:                       # noqa: BLE001
                info.setdefault("errors", []).append(
                    f"整源重扫失败: {type(exc).__name__}: {exc}")
        info["scanned"] = scanned
        if _wjob is not None:
            try:
                from . import jobs as _J2
                _J2.get_jobs().finish(_wjob)
            except Exception:                              # noqa: BLE001
                pass
        out["sources"][sc.id] = info
    out["finished"] = time.time()
    with _lock:
        _last_result = out
        _last_run = out["finished"]
    return out


# ------------------------------------------------------------------ 线程

def _loop() -> None:
    while True:
        try:
            from .config import load_config
            iv = int(getattr(load_config().settings,
                             "auto_index_refresh_seconds", 0) or 0)
        except Exception:                                  # noqa: BLE001
            iv = 0
        if iv <= 0:
            # 关掉了：仍然每 30 秒回来看一眼，设置改了不用重启
            if _stop.wait(30):
                return
            continue
        if _stop.wait(iv):
            return
        try:
            tick_once()
        except Exception:                                  # noqa: BLE001
            # 后台线程绝不因为单轮异常退出（否则功能静默失效）
            pass


def start() -> bool:
    """启动后台线程（幂等）。返回是否真的启动了。"""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="msh-indexwatch", daemon=True)
        _thread.start()
        return True


def stop() -> None:
    _stop.set()


def status() -> Dict[str, Any]:
    with _lock:
        alive = bool(_thread is not None and _thread.is_alive())
        return {"running": alive, "last_run": _last_run or None,
                "last": dict(_last_result) if _last_result else None}
