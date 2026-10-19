"""中转站：上传后暂时没想好放哪的文件，先存这里。

为什么要独立一个目录，而不是继续用 `~/.music-sync-hub/uploads`：

  · **容量可控**：`station_max_gb` 限制上限，满了直接拒绝上传。
    这是防"上传没注意把盘写满"的关键 —— 等到磁盘写不进去才报错就晚了。
  · **位置可预期**：默认在**程序目录**下（`station/`），用户能自己去看、
    去清理，而不是藏在用户目录的某个哈希目录里。
  · **能从库里临时挪进来**：整理时拿不准的歌可以先放中转站，想好了再归位。

设计上刻意和上传暂存区分开：
  · 上传的中间态（还没分析完）留在 `uploads/<batch>/`，是**一次性**的；
  · 中转站是**长期**存放，用户明确知道"这些歌我还没安排"。

与其他功能的联动：
  · 上传时若容量不够 → 直接拒绝并告知还差多少，不写坏数据；
  · 中转站里的文件可以走"智能分配去向"（复用 /api/organize/plan-paths）；
  · 库里的文件可以移入中转站（`move_in`），也可以从中转站归入某个目录（`move_out`）；
  · 删除仍然走回收站，绝不直接抹掉。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from .core import paths as P
from .core.guard import exclusive_guard, exclusive_slot

router = APIRouter()

_LOCK = threading.RLock()


def _deps():
    """延迟导入，避免与 main 循环依赖。"""
    from . import main
    return main


# 音频扩展名统一用 `core.paths.AUDIO_EXTS`（唯一真相）。
# 这里原来另有一份手写清单，比正式那份**少 10 个格式**
# （.ape/.wv/.wma/.dsf/.dff/.mpc/.tta/.mka/.caf…）——
# 结果那些格式的文件在中转站里会被当成"非音频"。
# 两份清单一并存本身就是 bug，删掉本地的，统一走 P.is_audio()。


# ---------------------------------------------------------------- 路径与配置

def _program_dir() -> str:
    """项目根目录（app/ 的上一层）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings():
    try:
        from .core.config import load_config
        return load_config().settings
    except Exception:
        return None


def station_dir() -> str:
    """中转站目录（绝对路径）。不存在就建。"""
    st = _settings()
    raw = str(getattr(st, "station_dir", "") or "").strip()
    d = raw or os.path.join(_program_dir(), "station")
    d = os.path.abspath(os.path.expanduser(d))
    os.makedirs(d, exist_ok=True)
    return d


def _manifest_path() -> str:
    """记录每个文件是**从哪来的**，用于"还原到原位置"。"""
    return os.path.join(station_dir(), ".station.json")


def _load_meta() -> Dict[str, Any]:
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_meta(d: Dict[str, Any]) -> None:
    p = _manifest_path()
    try:
        tmp = p + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        pass


def _record_origin(name: str, info: Dict[str, Any]) -> None:
    """记下来源，供"还原"和重试用。"""
    with _LOCK:
        d = _load_meta()
        d[name] = {**info, "at": time.time()}
        _save_meta(d)


def _forget_origin(names: List[str]) -> None:
    with _LOCK:
        d = _load_meta()
        for n in names:
            d.pop(n, None)
        _save_meta(d)


def _inside_station(rel: str) -> Optional[str]:
    """把中转站内的相对路径解析成绝对路径；越界/不存在返回 None。

    用 `commonpath` 而不是 `startswith` —— 后者对
    `.../station/../../x` 这类归一化后仍在父目录的情况判断不准，
    而且前缀比较容易被同名目录骗过（`station-evil` 也以 `station` 开头）。
    """
    d = os.path.abspath(station_dir())
    cand = os.path.abspath(os.path.join(d, str(rel).replace("\\", "/").lstrip("/")))
    try:
        if os.path.commonpath([d, cand]) != d:
            return None
    except ValueError:
        return None            # 不同盘符等
    return cand


def station_max_bytes() -> int:
    st = _settings()
    try:
        gb = float(getattr(st, "station_max_gb", 20.0) or 0)
    except (TypeError, ValueError):
        gb = 20.0
    return int(gb * 1024 ** 3) if gb > 0 else 0     # 0 = 不限制


def station_usage() -> Dict[str, Any]:
    """当前占用：文件数、字节数、上限、剩余。

    ★ 口径是**物理占用**（含用户手扔进来的非音频）。
    配额管的是磁盘，杂物也真占盘；只算音频的话，
    往中转站扔一堆截图就能把盘填满而不被提示。

    同时单独报 `other_count` / `other_bytes`，界面能说清
    "其中 N 个不是音乐文件、不会归库"。
    """
    d = station_dir()
    count = 0
    size = 0
    other_count = 0
    other_size = 0
    for base, _dirs, files in os.walk(d):
        for f in files:
            # 元数据/临时文件不算占用
            if f.endswith(".mshpart") or f.startswith("."):
                continue
            try:
                sz = os.path.getsize(os.path.join(base, f))
            except OSError:
                sz = 0
            size += sz
            count += 1
            if not P.is_audio(f):
                other_count += 1
                other_size += sz
    cap = station_max_bytes()
    free_disk = 0
    try:
        free_disk = shutil.disk_usage(d).free
    except OSError:
        pass
    return {
        "dir": d,
        "count": count,
        # 其中不是音乐文件的（用户手扔的杂物）：算进 count/bytes（真占盘），
        # 但界面要说清楚"这 N 个不会归库"。
        "other_count": other_count, "other_bytes": other_size,
        "bytes": size,
        "cap_bytes": cap,
        "remaining": (max(0, cap - size) if cap else None),
        "free_disk": free_disk,
        "percent": (round(100.0 * size / cap, 1) if cap else None),
    }


def check_capacity(add_bytes: int) -> None:
    """放入前的容量检查。

    规则（按用户要求调整过）：
      · **物理磁盘**装不下 → 拒绝。这是硬约束，写不进去就是写不进去。
      · 用户设的配额已经**用超了**（比如改了更小的上限）→ **只警告不拦**，
        否则用户会被自己以前存的文件卡死，连清理都做不了。
      · 配额没超但这次会超 → 拒绝，并说清还差多少、怎么调。
    """
    u = station_usage()
    cap = u["cap_bytes"]
    if u["free_disk"] and add_bytes > u["free_disk"]:
        raise HTTPException(
            413,
            f"磁盘剩余空间不足：{u['dir']} 所在磁盘仅剩 "
            f"{u['free_disk'] / 1024 ** 3:.2f} GB，本次还需要 "
            f"{add_bytes / 1024 ** 3:.2f} GB。"
            f"请先清理磁盘，或在设置里把中转站改到别的盘。")
    if not cap:
        return
    if u["bytes"] > cap:
        return                    # 已经超了 → 放行，直到物理写满为止
    if u["bytes"] + add_bytes > cap:
        raise HTTPException(
            413,
            f"中转站空间不足：已用 {u['bytes'] / 1024 ** 3:.2f} GB / 上限 "
            f"{cap / 1024 ** 3:.2f} GB，本次还需要 "
            f"{add_bytes / 1024 ** 3:.2f} GB。"
            f"请先清理中转站，或把上限调大（填 0 表示不限额）。")


def capacity_notice() -> Dict[str, Any]:
    """给界面看的容量提示：超了就说清楚，但**不影响使用**。

    字段要和暂存区那份（`upload_api.staging_notice`）保持一致 ——
    两个区域语义相同，前端应该能用同一套判断，而不是一处靠
    `cap_bytes == 0` 猜"不限额"、另一处读 `unlimited`。
    """
    u = station_usage()
    cap = u["cap_bytes"]
    over = bool(cap and u["bytes"] > cap)
    return {
        "over_quota": over,
        "over_bytes": (u["bytes"] - cap) if over else 0,
        "unlimited": not cap,          # cap 为 0 = 用户选了不限额
        "message": (
            f"已超出设定上限 {((u['bytes'] - cap) / 1024 ** 3):.2f} GB，"
            f"仍可继续使用，直到磁盘写满"
        ) if over else "",
        **u,
    }


# ---------------------------------------------------------------- 文件操作

def _safe_name(d: str, name: str) -> str:
    """同名不覆盖：自动加 (2)、(3)…

    同时挡掉 Windows 保留设备名（CON/NUL/COM1…）：
    写到这些名字上数据会被设备吞掉（得到 0 字节文件），
    而 `os.replace` 到它们会**永久挂起**。
    """
    # 带扩展名的形态也一样是设备名（`CON.flac` 同样出问题）
    _res = ({"CON", "PRN", "AUX", "NUL"}
            | {f"COM{i}" for i in range(1, 10)}
            | {f"LPT{i}" for i in range(1, 10)})
    name = str(name or "")
    if os.path.splitext(name)[0].rstrip(" .").upper() in _res:
        name = "_" + name
    base, ext = os.path.splitext(name)
    cand = name
    i = 1
    while os.path.exists(os.path.join(d, cand)):
        i += 1
        cand = f"{base} ({i}){ext}"
    return cand


def _list_files() -> List[Dict[str, Any]]:
    """中转站里的**音频**文件（待归位的那些）。

    ★ 只列音频。用户会往中转站目录里手动扔东西（截图、说明.txt、
    压缩包…），那些不是"待归位的音乐"：
      · 不该出现在待分配列表里（会被当成待归位项）
      · 不该被 `move-out` 归入音乐库
    非音频由 `_list_others()` 单独列出，看得见、删得掉，但不进归位流程。
    """
    d = station_dir()
    out: List[Dict[str, Any]] = []
    for base, _dirs, files in os.walk(d):
        for f in files:
            # 跳过临时文件和我们自己的元数据（`.station.json` 记来源用），
            # 否则它会当成一个"待分配的文件"显示出来
            if f.endswith(".mshpart") or f.startswith("."):
                continue
            if not P.is_audio(f):
                continue
            p = os.path.join(base, f)
            try:
                stt = os.stat(p)
            except OSError:
                continue
            out.append({
                "name": f,
                "path": p.replace("\\", "/"),
                "rel": os.path.relpath(p, d).replace("\\", "/"),
                "size": stt.st_size,
                "mtime": stt.st_mtime,
                "is_audio": True,
            })
    out.sort(key=lambda x: -(x.get("mtime") or 0))
    return out


def _list_others() -> List[Dict[str, Any]]:
    """中转站里的**非音频**文件（用户手扔的杂物）。

    列出来是为了让用户看得见、删得掉 —— 否则文件明明占着盘，
    界面里却完全没有入口。但它们**绝不参与归位**。
    """
    d = station_dir()
    out: List[Dict[str, Any]] = []
    for base, _dirs, files in os.walk(d):
        for f in files:
            if f.endswith(".mshpart") or f.startswith("."):
                continue
            if P.is_audio(f):
                continue
            p = os.path.join(base, f)
            try:
                stt = os.stat(p)
            except OSError:
                continue
            out.append({
                "name": f,
                "path": p.replace("\\", "/"),
                "rel": os.path.relpath(p, d).replace("\\", "/"),
                "size": stt.st_size,
                "mtime": stt.st_mtime,
                "is_audio": False,
                "reason": "不是音乐文件，不会归入曲库",
            })
    out.sort(key=lambda x: -(x.get("mtime") or 0))
    return out


@router.get("/api/station")
def station_info() -> Dict[str, Any]:
    """中转站概况 + **音频**清单（含"超了配额但照常可用"的提示）。

    `ignored` 是用户手扔进来的非音频文件：列出来让他看得见、删得掉，
    但不会出现在待归位列表里，也不会被 `move-out` 归入曲库。
    """
    n = capacity_notice()
    return {**n, "files": _list_files(), "ignored": _list_others(),
            "limit_gb": (n["cap_bytes"] / 1024 ** 3) if n["cap_bytes"] else 0}


@router.post("/api/station/move-in")
def station_move_in(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("station"))) -> Dict[str, Any]:
    """把库里的文件**移入**中转站（"这首我还没想好放哪"）。

    用移动而不是复制：中转站是"待安排"，留一份副本只会让两边都乱。
    依然不删任何东西 —— 只是换了位置。
    """
    from .core import paths as P
    sid = str(payload.get("source") or "")
    paths = payload.get("paths") or []
    
    if not sid:
        raise HTTPException(400, "缺少 source")
    _deps().require_writable(sid, what="移入中转站")
    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, "paths 不能为空")

    m = _deps()
    # 注意：`resolve_source` 返回的是**配置**（SourceConfig），
    # 要操作磁盘得用 `manager.get(sid)` 拿 MusicSource 实例
    m.resolve_source(sid)
    src = m.manager.get(sid)
    root = P.norm(src.root)
    d = station_dir()

    picked: List[str] = []
    total = 0
    for p in paths:
        s = P.norm(str(p))
        if not s:
            continue
        if not (s == root or s.startswith(root + "/")):
            continue
        try:
            stt = src.stat(s)
        except Exception:
            stt = None
        if not stt or stt.is_dir:
            continue
        picked.append(s)
        total += int(getattr(stt, "size", 0) or 0)
    if not picked:
        raise HTTPException(400, "没有可移动的文件（可能都不在数据源范围内）")
    check_capacity(total)

    moved: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for s in picked:
        try:
            name = _safe_name(d, P.basename(s))
            dst = os.path.join(d, name)
            _moved(s, dst)
            _record_origin(name, {"from": s, "source": sid})
            moved.append({"src": s, "name": name, "path": dst.replace("\\", "/")})
        except Exception as exc:
            failed.append({"src": s, "error": str(exc)})

    # 索引里这些路径已经不存在了 → 清掉记录，下次扫描按真实情况重建
    if moved:
        try:
            with m.db().write() as conn:
                conn.executemany(
                    "DELETE FROM track WHERE source_id=? AND path=?",
                    [(sid, m["src"]) for m in moved])
        except Exception:
            pass

    u = station_usage()
    return {"ok": True, "moved": moved, "failed": failed,
            "moved_count": len(moved), "failed_count": len(failed),
            "bytes": sum(_size_of(m["path"]) for m in moved),
            "station": u}


def adopt_file(src_path: str, origin: Optional[Dict[str, Any]] = None,
               move: bool = True) -> Dict[str, Any]:
    """把一个**外部文件**收进中转站（上传暂存区转过来时用）。

    和 `move_in` 的区别：`move_in` 处理的是数据源里的曲目（要按数据源根目录
    校验、要动索引）；这里处理的是暂存区里、本来就不属于任何数据源的文件，
    只需要搬运 + 记来源。
    """
    d = station_dir()
    src = os.path.abspath(src_path)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"文件不存在: {src}")
    # 已经在中转站里就不用搬了
    if os.path.commonpath([d, src]) == d:
        return {"name": os.path.basename(src), "path": src.replace("\\", "/"),
                "already": True}
    name = _safe_name(d, os.path.basename(src))
    dst = os.path.join(d, name)
    if move:
        _moved(src, dst)
    else:
        _copied(src, dst)
    _record_origin(name, dict(origin or {}))
    return {"name": name, "path": dst.replace("\\", "/"), "already": False}



def _moved(src: str, dst: str) -> None:
    """带超时的移动。

    文件系统调用会因为坏扇区 / 网络盘掉线 / 被独占而**永久挂住**，
    既不返回也不报错 —— 请求就停在那儿，界面看着像卡死。
    超时后放弃等待并报错，让调用方把这一条标成失败、继续处理其他文件。
    """
    from .core.ops import call_with_timeout
    return call_with_timeout(lambda: shutil.move(src, dst), what=f"移动 {os.path.basename(src)}")


def _copied(src: str, dst: str) -> None:
    from .core.ops import call_with_timeout
    return call_with_timeout(lambda: shutil.copy2(src, dst), what=f"复制 {os.path.basename(src)}")


def _size_of(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


@router.post("/api/station/move-out")
def station_move_out(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("station"))) -> Dict[str, Any]:
    """把中转站里的文件归入某个目录（回到库里）。"""
    from .core import paths as P
    sid = str(payload.get("source") or "")
    names = payload.get("files") or []
    dest = str(payload.get("dest") or "").strip()
    
    if not sid:
        raise HTTPException(400, "缺少 source")
    _deps().require_writable(sid, what="从中转站归位")
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "files 不能为空")

    m = _deps()
    m.resolve_source(sid)
    src = m.manager.get(sid)
    root = P.norm(src.root)
    d = station_dir()

    # 目标目录：空 = 数据源根目录
    if not dest:
        dest_abs = root
    else:
        dest_abs = P.norm(dest)
        # ★ `norm()` 不折叠 `..`：`root + "/../../别处"` 能通过前缀判断。
        #   曾经这里就是这样被绕过去的（会真的在库外建目录、把文件搬出去）。
        if P.has_dotdot(dest_abs):
            raise HTTPException(400, "目标目录不能包含 ..")
        if not P.inside_root(root, dest_abs):
            raise HTTPException(400, "目标目录超出数据源范围")
    try:
        os.makedirs(dest_abs, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"无法创建目标目录: {exc}")

    moved: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for n in names:
        rel = str(n).replace("\\", "/").lstrip("/")
        srcp = _inside_station(rel)
        if not srcp:
            failed.append({"name": str(n), "error": "路径越界，已拒绝"})
            continue
        if not os.path.isfile(srcp):
            failed.append({"name": str(n), "error": "文件不存在"})
            continue
        # ★ 最后一道闸：**非音频绝不归入曲库**。
        #   列表那边已经过滤，但客户端可以直接构造 files 绕过去。
        #   归位是"写进用户音乐库"的动作，把 .txt/.jpg 搬进去不可接受 ——
        #   索引会忽略它们，库里就多出一堆谁也看不见的垃圾。
        if not P.is_audio(srcp):
            failed.append({
                "name": os.path.basename(srcp),
                "error": f"不是音乐文件（{P.ext(srcp) or '无扩展名'}），已跳过",
            })
            continue
        try:
            tgt = _safe_name(dest_abs, os.path.basename(srcp))
            _moved(srcp, os.path.join(dest_abs, tgt))
            moved.append({"name": os.path.basename(srcp), "dest": tgt,
                          "path": os.path.join(dest_abs, tgt).replace("\\", "/")})
        except Exception as exc:
            failed.append({"name": str(n), "error": str(exc)})

    # ★ 归位后必须刷新索引，否则文件已在库目录里、曲库却查不到（审查 NEW-05）
    _paths = [x["path"] for x in moved if x.get("path")]
    if _paths:
        try:
            _deps().refresh_tracks(sid, _paths)
        except Exception:                                     # noqa: BLE001
            pass
    return {"ok": True, "moved": moved, "failed": failed,
            "moved_count": len(moved), "failed_count": len(failed),
            "dest": dest_abs.replace("\\", "/"), "station": station_usage()}


@router.post("/api/station/remove")
def station_remove(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("station"))) -> Dict[str, Any]:
    """删除中转站里的文件（**移入数据源的回收站**，不是直接抹掉）。"""
    from .core import paths as P
    sid = str(payload.get("source") or "")
    names = payload.get("files") or []
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "files 不能为空")
    if not payload.get("confirm"):
        raise HTTPException(400, "删除需要 confirm=true")
    if sid:
        # ★ 只读源不许动（中转站删除会在数据源里建 .music-sync-trash 并写文件）
        _deps().require_writable(sid, what="删除中转站文件")

    m = _deps()
    d = station_dir()
    trash = None
    if sid:
        try:
            m.resolve_source(sid)
            trash = _trash_dir_for(m.manager.get(sid).root)
        except Exception:
            trash = None
    if not trash:
        trash = os.path.join(d, "_trash", time.strftime("%Y%m%d"))

    os.makedirs(trash, exist_ok=True)
    moved: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for n in names:
        rel = str(n).replace("\\", "/").lstrip("/")
        srcp = _inside_station(rel)
        if not srcp:
            failed.append({"name": str(n), "error": "路径越界，已拒绝"})
            continue
        if not os.path.isfile(srcp):
            failed.append({"name": str(n), "error": "文件不存在"})
            continue
        try:
            tgt = _safe_name(trash, os.path.basename(srcp))
            _moved(srcp, os.path.join(trash, tgt))
            moved.append({"name": os.path.basename(srcp), "trash": tgt})
        except Exception as exc:
            failed.append({"name": str(n), "error": str(exc)})

    return {"ok": True, "trashed": moved, "failed": failed,
            "trashed_count": len(moved), "failed_count": len(failed),
            "trash_dir": trash.replace("\\", "/"), "station": station_usage()}


def _trash_dir_for(root: str) -> str:
    """复用数据源的回收站约定（`<root>/.music-sync-trash/YYYYMMDD/`）。"""
    try:
        from .core import paths as P
        r = P.norm(root)
        return os.path.join(r, ".music-sync-trash", time.strftime("%Y%m%d"))
    except Exception:
        return ""


def _sid_for_abs(path: str) -> str:
    """一个绝对路径属于哪个数据源（取最长的根匹配）。"""
    from .core.config import load_config
    from .core import paths as P

    best = ("", -1)
    for sc in load_config().sources:
        try:
            r = P.norm(sc.root)
            if P.inside_root(r, P.norm(path)) and len(r) > best[1]:
                best = (sc.id, len(r))
        except Exception:                                     # noqa: BLE001
            pass
    return best[0]


@router.post("/api/station/restore")
def station_restore(payload: Dict[str, Any] = Body(...), job=Depends(exclusive_slot("station"))) -> Dict[str, Any]:
    """把中转站里的文件**还原到它原来的位置**。

    这是"容错 + 可重试"的关键一环：执行过程中如果某个文件搬错了、
    或者用户后悔了，只要能还原就不至于把库搞乱。
    来源信息在移入时就记下来了（`.station.json`）。
    """
    names = payload.get("files") or []
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "files 不能为空")
    meta = _load_meta()
    # ★ Pre-flight：把要还原的文件按"归属源"分组，**循环前**统一校验写权限。
    #   放在循环里时，第二首属于只读源就抛 403 中断 —— 第一首文件已经搬走、
    #   但后面的 refresh_tracks 永远不执行：用户看到 403，实际半边生效。
    _owners = set()
    for _n in names:
        _rel = str(_n).replace("\\", "/").lstrip("/")
        _info = meta.get(os.path.basename(_rel)) or {}
        _o = _sid_for_abs(str(_info.get("from") or ""))
        if _o:
            _owners.add(_o)
    for _o in sorted(_owners):
        _deps().require_writable(_o, what="从中转站还原")

    restored: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for n in names:
        rel = str(n).replace("\\", "/").lstrip("/")
        srcp = _inside_station(rel)
        if not srcp or not os.path.isfile(srcp):
            failed.append({"name": str(n), "error": "文件不存在或路径越界"})
            continue
        base = os.path.basename(srcp)
        info = meta.get(base) or {}
        origin = str(info.get("from") or "")
        if not origin:
            failed.append({"name": base,
                           "error": "没有记录来源，无法还原（可用「归入目录」手动指定）"})
            continue
        # ★ 来源必须是**当前存在的**数据源里的路径。meta 的 `from` 可以是
        #   任意绝对路径（旧源已删除、甚至 C:/Windows/Temp）—— 不校验就是
        #   往库外任意位置建目录 + 搬文件。
        if not _sid_for_abs(origin):
            failed.append({"name": base,
                           "error": "来源路径不属于任何可用数据源，禁止自动还原"})
            continue
        try:
            dest_dir = os.path.dirname(origin)
            os.makedirs(dest_dir, exist_ok=True)
            tgt = _safe_name(dest_dir, os.path.basename(origin))
            _moved(srcp, os.path.join(dest_dir, tgt))
            restored.append({"name": base,
                             "to": os.path.join(dest_dir, tgt).replace("\\", "/")})
            _forget_origin([base])
        except Exception as exc:
            failed.append({"name": base, "error": str(exc)})

    # ★ 还原后刷新索引（文件已回到库里，不刷新就搜不到）
    _groups: Dict[str, List[str]] = {}
    for _x in restored:
        _s = _sid_for_abs(_x.get("to") or "")
        if _s:
            _groups.setdefault(_s, []).append(_x["to"])
    for _s, _ps in _groups.items():
        try:
            _deps().refresh_tracks(_s, _ps)
        except Exception:                                     # noqa: BLE001
            pass
    return {"ok": True, "restored": restored, "failed": failed,
            "restored_count": len(restored), "failed_count": len(failed),
            "station": station_usage()}


@router.post("/api/station/suggest")
def station_suggest(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """为中转站里选中的文件算**建议去向**（纯计算，不动文件）。

    复用「智能整理」的规则：库里已有同名专辑/作者目录就归进去，
    否则够门槛就新建，不够就落到收纳区。用户看过建议再决定要不要真搬。
    """
    from .core import organize as OG
    from .core import paths as P
    sid = str(payload.get("source") or "")
    files = payload.get("files") or []
    if not isinstance(files, list) or not files:
        raise HTTPException(400, "files 不能为空")

    m = _deps()
    d = station_dir()
    # all_tracks 返回的是 sqlite3.Row，没有 .get()；规划器要的是 dict
    rows = [dict(r) for r in m.db().all_tracks(sid)] if sid else []
    root = ""
    if sid:
        try:
            root = P.norm(m.manager.get(sid).root)
        except Exception:
            root = ""

    # 用中转站里的文件造一批"待分配"行：读它们的标签（便宜、不解码）
    out_rows: List[Dict[str, Any]] = []
    for rel in files:
        rel = str(rel).replace("\\", "/").lstrip("/")
        p = _inside_station(rel)
        if not p or not os.path.isfile(p):
            continue
        artist = album = title = ""
        try:
            from .core import audio as A
            with open(p, "rb") as fh:
                meta = A.parse_bytes(fh.read(512 * 1024), os.path.basename(p))
            artist = str(getattr(meta, "artist", "") or "")
            album = str(getattr(meta, "album", "") or "")
            title = str(getattr(meta, "title", "") or "")
        except Exception:
            pass
        out_rows.append({
            "path": p.replace("\\", "/"), "name": os.path.basename(p),
            "artist": artist, "album": album, "title": title,
            "size": _size_of(p), "rel": rel, "source_id": "",
        })
    if not out_rows:
        raise HTTPException(400, "选中的文件都读不到（可能已被移走）")

    plan: List[Dict[str, Any]] = []
    if root and rows:
        try:
            res = OG.plan_by_album(
                out_rows, root, min_group=5, group_by="artist_album",
                keep_flat_below=True,
                small_policy="keep", folder_pool_rows=rows)
            dst_of = {}
            for a in res.get("actions") or []:
                for it in a.get("items") or []:
                    dst_of[it["path"]] = (a.get("dst") or "", a.get("why") or "")
            for r in out_rows:
                dst, why = dst_of.get(r["path"], ("", ""))
                # dst 为空 = 规划器认为**不需要移动**，或没有更合适的现成目录。
                # 绝不能当成"放到根目录" —— 那等于把歌从专辑目录里拽出来扔到
                # 根目录，是破坏性的误操作。如实标成"暂不移动"。
                if not dst:
                    why = why or "按当前规则不需要移动（库里没有更合适的现成目录）"
                plan.append({"rel": r["rel"], "name": r["name"],
                             "dest": dst, "keep": not dst, "why": why,
                             "artist": r["artist"], "album": r["album"]})
        except Exception as exc:
            raise HTTPException(500, f"计算去向失败: {exc}")
    else:
        for r in out_rows:
            plan.append({"rel": r["rel"], "name": r["name"], "dest": "",
                         "keep": True,
                         "why": "没有库内信息可比对，需要手动指定目录",
                         "artist": r["artist"], "album": r["album"]})
    return {"ok": True, "plan": plan, "count": len(plan),
            "movable": len([x for x in plan if x.get("dest")])}


@router.get("/api/station/check")
def station_check(add_mb: float = 0) -> Dict[str, Any]:
    """预检查容量（上传前问一下还能不能放下）。

    ★ 判定必须和真正执行时的 `check_capacity` **一致**。
    踩过：这里原来"超配额就判放不下"，而 `check_capacity` 在**已经超配额**
    时是放行的（配额是软的，物理盘才是硬约束）。于是已经超配额的用户
    看到"空间不足"，真去搬却成功 —— 自相矛盾的提示，
    界面若按 `ok` 禁用按钮，反而把用户拦住了。
    """
    add = int(max(0.0, add_mb) * 1024 * 1024)
    u = station_usage()
    cap = u["cap_bytes"]
    ok = True
    reason = ""
    # 硬约束：物理磁盘
    if u["free_disk"] and add > u["free_disk"]:
        ok = False
        reason = f"磁盘剩余空间不足：仅剩 {u['free_disk'] / 1024 ** 3:.2f} GB"
    # 软约束：用户配额。**已经超了**就放行（只提示）；这次会超才拒
    elif cap and u["bytes"] <= cap and u["bytes"] + add > cap:
        ok = False
        reason = (f"超出中转站上限：已用 {u['bytes'] / 1024 ** 3:.2f} GB / "
                  f"上限 {cap / 1024 ** 3:.2f} GB"
                  f"（清理中转站，或把上限调大；填 0 表示不限额）")
    elif cap and u["bytes"] > cap:
        reason = (f"已超出设定的上限 {((u['bytes'] - cap) / 1024 ** 3):.2f} GB，"
                  f"仍可继续使用直到磁盘写满")
    return {"ok": ok, "reason": reason, "need_bytes": add, **u}
