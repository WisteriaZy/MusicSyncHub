"""文件上传 + 按库内结构自动分配文件夹。

场景：手上一堆散装音乐文件，想按你现有的音乐库结构归位。

流程刻意分成三步，和"智能整理"一个套路（先看方案再动手）：
  1. `POST /api/upload`          文件落到暂存区（不碰音乐库）
  2. `GET  /api/upload/inspect`  读标签 + 跟音乐库现有目录比对，给出建议去向
  3. `POST /api/upload/commit`   按你确认的去向搬进库，并刷新索引

为什么先暂存而不是直接写进音乐库：上传的文件名/标签常常是乱的，
直接落到库里就是制造垃圾。暂存区让你先看清"它打算放哪、凭什么"，
不满意可以改或者直接丢掉。

安全：暂存区在应用数据目录下（`<MSH_DATA_DIR>/uploads/<批次>/`），
不属于任何数据源；只有 `commit` 那一步会往音乐库里写，而且走的是同一套
Executor（跳过同名、移动后校验、绝不覆盖）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile

from .core import organize as OG
from .core import paths as P
from .core.jobs import get_jobs
from .core.ops import call_with_timeout
from .core.guard import exclusive_guard

from .deps import as_dicts, as_list, as_text

router = APIRouter()

# 音频扩展名统一用 `core.paths.AUDIO_EXTS`（唯一真相），
# 判据统一用 `P.is_audio()`。
#
# 这里原来另抄了一份 16 个扩展名的清单，比正式那份（22 个）**少 6 个**：
#   .caf .m4b .oga .mpc .tta .wv
# 于是这些格式上传时会被判"不支持的格式"直接拒掉 —— 但扫描器认它们，
# 也就是说"手动拷进库能被扫到，走上传反而传不上去"，自相矛盾。
# 两份清单一并存本身就是 bug；删掉本地的，统一走 P.is_audio()。
# 单文件上限（防止误传电影文件）—— 1.5 GB
MAX_FILE_BYTES = 1536 * 1024 * 1024


def _deps():
    from . import main
    return main


def _uploads_root() -> str:
    """暂存区根目录。

    默认在应用数据目录下的 `uploads/`，可以用配置里的 `staging_dir` 改到别处
    （比如换到大盘上）。**不属于任何数据源**，所以扫描器永远碰不到它。
    """
    from .core.config import data_dir, load_config
    base = ""
    custom = ""
    try:
        cfg = load_config()
        # ★ `AppConfig` **没有** `data_dir` 属性 —— 原来写
        #   `getattr(cfg, "data_dir", "")` 永远拿到空串（和 tags.py 里
        #   `load_config().data_dir` 是同一个错，见台账 B16）。
        #   正确的东西是 `config.data_dir()` 这个**函数**。
        #   走它还有额外好处：测试进程里的兜底会生效，
        #   暂存文件不会再落到用户真实的数据目录。
        base = str(data_dir() or "")
        custom = str(getattr(cfg.settings, "staging_dir", "") or "").strip()
    except Exception:
        base = ""
    if custom:
        root = os.path.abspath(os.path.expanduser(custom))
    else:
        if not base:
            base = str(data_dir() or "")
        root = os.path.join(base, "uploads")
    os.makedirs(root, exist_ok=True)
    return root


def staging_max_bytes() -> int:
    """暂存区容量上限（字节）。0 = 不限制。"""
    try:
        from .core.config import load_config
        gb = float(getattr(load_config().settings, "staging_max_gb", 5.0) or 0)
    except Exception:
        gb = 5.0
    return int(gb * 1024 ** 3) if gb > 0 else 0


def staging_usage() -> Dict[str, Any]:
    """暂存区占用情况。

    ★ 统计口径：**物理占用**（所有文件都算，包括用户手扔进来的非音频）。
    为什么不像列表那样只算音频：配额管的是磁盘，杂物也真占盘；
    只算音频的话，用户往里面扔一堆截图就能把盘填满而不被提示。

    同时单独报 `other_count` / `other_bytes`，让界面能说清
    "其中 N 个不是音乐文件"。
    """
    root = _uploads_root()
    count = 0
    size = 0
    other_count = 0
    other_size = 0
    batches = 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        batches += 1
        for base_dir, _dirs, files in os.walk(d):
            for f in files:
                if f.endswith(".mshpart") or f == "manifest.json":
                    continue
                p = os.path.join(base_dir, f)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                size += sz
                count += 1
                if not P.is_audio(f):
                    other_count += 1
                    other_size += sz
    cap = staging_max_bytes()
    free_disk = 0
    try:
        free_disk = shutil.disk_usage(root).free
    except OSError:
        pass
    return {
        # 注意别用 `count`/`batches` 这种会和返回结构撞车的键名：
        # `/api/upload/batches` 里 `batches` 是**列表**，
        # 这里如果也叫 `batches`（数字），`**u` 展开会把它覆盖掉，
        # 前端拿到 int 就会 `len()` 报错。
        "dir": root, "file_count": count, "bytes": size, "batch_count": batches,
        # 其中不是音乐文件的那些（用户手扔的杂物）。它们算进 bytes/count
        # （因为真占盘），但界面要说清楚"其中 N 个不会入库"。
        "other_count": other_count, "other_bytes": other_size,
        "cap_bytes": cap,
        "remaining": (max(0, cap - size) if cap else None),
        "free_disk": free_disk,
        "percent": (round(100.0 * size / cap, 1) if cap else None),
    }


def check_staging_capacity(add_bytes: int) -> None:
    """上传前检查暂存区够不够。

    规则（按用户要求调整过）：
      · **物理磁盘**装不下 → 拒绝。这是硬约束。
      · 用户设的配额已经**用超了**（比如把上限改小了）→ **只警告不拦**。
        否则用户会被自己以前上传的文件卡死：既不能上传，清理也要先能看见。
      · 配额没超但这次会超 → 拒绝，并说清还差多少、怎么调。
        填 0 表示不限额。
    """
    u = staging_usage()
    if u["free_disk"] and add_bytes > u["free_disk"]:
        raise HTTPException(
            413,
            f"磁盘剩余空间不足：{u['dir']} 所在磁盘仅剩 "
            f"{u['free_disk'] / 1024 ** 3:.2f} GB，本次还需要 "
            f"{add_bytes / 1024 ** 3:.2f} GB。请先清理磁盘，"
            f"或在设置里把上传暂存区改到别的盘。")
    cap = u["cap_bytes"]
    if not cap:
        return                    # 0 = 不限额
    if u["bytes"] > cap:
        return                    # 已经超了 → 放行，直到物理写满为止
    if u["bytes"] + add_bytes > cap:
        raise HTTPException(
            413,
            f"上传暂存区空间不足：已用 {u['bytes'] / 1024 ** 3:.2f} GB / 上限 "
            f"{cap / 1024 ** 3:.2f} GB，本次还需要 "
            f"{add_bytes / 1024 ** 3:.2f} GB。可把上限调大（填 0 = 不限额），"
            f"或到「中转站」页清理滞留批次。")


def staging_notice() -> Dict[str, Any]:
    """给界面看的暂存区容量提示：超了就说清楚，但**不影响使用**。"""
    u = staging_usage()
    cap = u["cap_bytes"]
    over = bool(cap and u["bytes"] > cap)
    return {
        "over_quota": over,
        "over_bytes": (u["bytes"] - cap) if over else 0,
        "message": (
            f"已超出设定上限 {((u['bytes'] - cap) / 1024 ** 3):.2f} GB，"
            f"仍可继续上传，直到磁盘写满"
        ) if over else "",
        "unlimited": not cap,
        **u,
    }


def check_commit_capacity(target_dir: str, total_bytes: int) -> None:
    """入库前检查**目标盘**装不装得下这一批。

    暂存区够用不代表目标盘够用 —— 入库是把文件搬到数据源目录，
    那可能是另一块盘（甚至另一台机器）。这里提前判断，
    避免搬到一半失败、库里多出一半文件。
    """
    if total_bytes <= 0:
        return
    try:
        probe = target_dir
        for _ in range(40):
            if os.path.isdir(probe):
                break
            up = os.path.dirname(probe)
            if up == probe:
                break
            probe = up
        free = shutil.disk_usage(probe).free
    except Exception:
        return                     # 判断不了就不拦，交给执行阶段报错
    # 留 200 MB 余量：入库过程本身要临时文件
    if total_bytes > max(0, free - 200 * 1024 * 1024):
        raise HTTPException(
            413,
            f"目标磁盘空间不足：{probe} 仅剩 {free / 1024 ** 3:.2f} GB，"
            f"这一批需要 {total_bytes / 1024 ** 3:.2f} GB"
            f"（另需约 200 MB 余量）。请先清理目标盘，"
            f"或少选一些文件分批入库。")


def batch_size(batch: str) -> Dict[str, Any]:
    """一个批次的总大小 / 文件数（**按磁盘实际**，不信清单）。"""
    d = _batch_dir(batch)
    files = _disk_batch_files(d)
    return {"batch": batch, "count": len(files),
            "bytes": sum(int(f.get("size") or 0) for f in files),
            "files": files}


def _batch_dir(bid: str, must_exist: bool = True) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", bid or ""):
        raise HTTPException(400, "批次号不合法")
    d = os.path.join(_uploads_root(), bid)
    if must_exist and not os.path.isdir(d):
        raise HTTPException(404, f"批次不存在: {bid}")
    return d


# Windows 保留设备名。写这些名字**不会报错**，但数据会被设备吞掉，
# 更糟的是 `os.replace()` 到它们会**永久挂起**（实测 8 秒无返回）。
# CON/NUL/COM1 这些即使带扩展名也仍然是设备（`CON.flac` 一样出问题）。
_WIN_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _is_win_reserved(name: str) -> bool:
    """这个名字在 Windows 上是不是保留设备名（含带扩展名的形态）。"""
    stem = os.path.splitext(str(name or ""))[0].rstrip(" .").upper()
    return stem in _WIN_RESERVED


def _safe_name(name: str) -> str:
    """上传来的文件名不可信：只取最后一段，去掉路径分隔符。

    还要挡掉 Windows 保留设备名 —— 否则用户上传 `CON.flac` 会得到
    一个 0 字节的"文件"，而入库时 `os.replace` 到它会直接挂住整个请求。
    """
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip().strip(".")
    if base and _is_win_reserved(base):
        # 加前缀而不是替换：保留原名可读，同时不再是设备名
        base = "_" + base
    return base[:200] or f"unnamed-{uuid.uuid4().hex[:8]}"


def _unique_in(dirpath: str, name: str) -> str:
    """在目录里找一个不冲突的文件名。"""
    stem, ext = os.path.splitext(name)
    cand, i = name, 1
    while os.path.exists(os.path.join(dirpath, cand)):
        cand = f"{stem} ({i}){ext}"
        i += 1
    return cand


def _manifest_path(d: str) -> str:
    return os.path.join(d, "manifest.json")


def _load_manifest(d: str) -> Dict[str, Any]:
    p = _manifest_path(d)
    if not os.path.isfile(p):
        return {"batch": os.path.basename(d), "created": time.time(), "files": []}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"batch": os.path.basename(d), "created": time.time(), "files": []}


def _save_manifest(d: str, m: Dict[str, Any]) -> None:
    tmp = _manifest_path(d) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _manifest_path(d))


def _read_tags(path: str, name: str) -> Dict[str, Any]:
    """读音频标签；读不到就退化成"从文件名猜"。

    用 `parse_bytes` 而不是别的入口，是因为它跟扫描器完全同一条代码路径 ——
    暂存区判定的去向和入库之后扫描出来的结果才不会打架。
    文件名解析同样重要：上传的文件常常没有标签，但文件名里往往写着
    `艺术家 - 曲名`，那是唯一能用来归类的线索。
    """
    out: Dict[str, Any] = {
        "title": "", "artist": "", "album": "", "duration": 0.0,
        "codec": "", "bitrate": 0, "sample_rate": 0, "is_lossless": False,
        "cover": None,
    }
    try:
        from .core import audio as A
        with open(path, "rb") as f:
            head = f.read(1024 * 1024)          # 够标签和封面用，不必读整个文件
        meta = A.parse_bytes(head, name, head_only=True)
        out["title"] = (meta.title or "").strip()
        out["artist"] = (meta.artist or meta.album_artist or "").strip()
        out["album"] = (meta.album or "").strip()
        out["duration"] = float(meta.duration or 0)
        out["codec"] = (meta.codec or "").strip()
        out["bitrate"] = int(meta.bitrate or 0)
        out["sample_rate"] = int(meta.sample_rate or 0)
        out["is_lossless"] = bool(meta.is_lossless)
        # 缩略图存文件，前端能直接显示"即将入库的是什么"
        if getattr(meta, "cover_data", None):
            try:
                thumb, _mime, _w, _h = A.make_thumbnail(meta.cover_data, 96)
                cn = os.path.splitext(name)[0] + ".cover"
                with open(os.path.join(os.path.dirname(path), "." + cn), "wb") as cf:
                    cf.write(thumb)
                out["cover"] = "." + cn
            except Exception:
                out["cover"] = None
    except Exception:
        pass

    stem = os.path.splitext(name)[0]
    if not out["title"] or not out["artist"]:
        parsed = {}
        try:
            from .core import audio as A
            parsed = A.parse_filename(name) or {}
        except Exception:
            parsed = {}
        if not out["title"]:
            out["title"] = (parsed.get("title") or "").strip()
        if not out["artist"]:
            out["artist"] = (parsed.get("artist") or "").strip()
        if not out["title"]:
            t = re.sub(r"^\s*\d{1,3}\s*[.\-_)．、]\s*", "", stem)
            out["title"] = t or stem
    if not out["artist"]:
        # `艺术家 - 曲名`：作者常常是短的那一边
        parts = re.split(r"\s+[-–—]\s+", stem)
        if len(parts) >= 2 and 1 < len(parts[-1]) <= 60:
            out["artist"] = parts[-1].strip()
    if not out["album"]:
        out["album"] = ""
    return out


# ==================================================================== 上传

@router.post("/api/upload")
async def upload(files: List[UploadFile] = File(...),
                 source: str = Form(""),
                 subdir: str = Form("")) -> Dict[str, Any]:
    """把文件收进暂存区。

    一次请求可以带多个文件（前端按批切分，避免单个请求过大）。
    同名文件自动改名，**绝不覆盖**暂存区里已有的文件。
    """
    if not files:
        raise HTTPException(400, "没有收到文件")
    # 容量预检：上传前先确认**暂存区**放得下。
    # 用暂存区自己的配额（文件落在这里），不是中转站的 ——
    # 拿错的尺子量会出现"中转站满了却拦上传、暂存区涨爆了没人管"。
    need = 0
    for uf in files:
        try:
            sz = getattr(uf, "size", None)
            if sz:
                need += int(sz)
        except Exception:
            pass
    check_staging_capacity(need)
    # 想放进的子目录（可选）：默认按"批次"分开放，避免混在一起
    bid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    base = _batch_dir(bid, must_exist=False)
    dest = base
    sub = str(subdir or "").strip().strip("/")
    if sub:
        parts = [x for x in P.norm(sub).split("/") if x and x != ".."]
        dest = os.path.join(base, *parts) if parts else base
    os.makedirs(dest, exist_ok=True)

    m = _load_manifest(base)
    saved: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    total_bytes = 0

    for uf in files:
        raw_name = _safe_name(uf.filename or "")
        ext = os.path.splitext(raw_name)[1].lower()
        # 判据统一走 P.is_audio（唯一真相），别再用本地副本
        if not P.is_audio(raw_name):
            skipped.append({"name": raw_name,
                            "reason": f"不是音乐文件（{ext or '无扩展名'}），已跳过"})
            continue
        name = _unique_in(dest, raw_name)
        target = os.path.join(dest, name)
        size = 0
        # 分块写盘：一次性 read() 大文件会占满内存
        try:
            with open(target, "wb") as f:
                while True:
                    chunk = await uf.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("文件超过 1.5 GB 上限")
                    f.write(chunk)
        except Exception as exc:
            try:
                call_with_timeout(lambda: os.remove(target), what="删除暂存文件")
            except OSError:
                pass
            skipped.append({"name": raw_name, "reason": str(exc)})
            continue
        finally:
            try:
                await uf.close()
            except Exception:
                pass

        rel = os.path.relpath(target, base).replace("\\", "/")
        tags = _read_tags(target, name)
        rec = {"name": name, "rel": rel, "size": size, **tags,
               "uploaded": time.time()}
        m["files"].append(rec)
        saved.append(rec)
        total_bytes += size

    m["source"] = str(source or "")
    m["updated"] = time.time()
    _save_manifest(base, m)

    return {
        "batch": bid, "saved": len(saved), "skipped": skipped,
        "bytes": total_bytes, "human_bytes": P.human_size(total_bytes),
        "files": saved,
        "total_in_batch": len(m["files"]),
    }


def _disk_batch_files(d: str) -> List[Dict[str, Any]]:
    """按**磁盘实际情况**列出批次里的**音频**文件。

    以前这里是"只信 manifest"：`files = [f for f in m['files'] if isfile(...)]`，
    一旦 manifest 丢失或没记全（上传中途出错、旧版本留下的批次），
    整个批次会被 `if not files: continue` 跳过 ——
    文件明明占着盘，界面上却完全没有入口能看到或删掉它。

    现在以磁盘为准：manifest 只用来补充"原始文件名 / 上传时的来源"这类信息。

    ★ 只返回**音频**。用户会往暂存区目录里手动扔东西（截图、说明.txt、
    压缩包、`.DS_Store`…），那些不是待入库的音乐：
      · 不该出现在"待处理文件"列表里（会被当成待入库项）
      · 不该被 `upload_commit` 搬进音乐库
    非音频由 `_disk_batch_others()` 单独列出，让用户看得见、删得掉，
    但**不进入**入库流程。
    """
    out: List[Dict[str, Any]] = []
    for base_dir, _dirs, files in os.walk(d):
        for fn in files:
            if fn.endswith(".mshpart") or fn == "manifest.json":
                continue
            if not P.is_audio(fn):
                continue
            p = os.path.join(base_dir, fn)
            rel = os.path.relpath(p, d).replace("\\", "/")
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({
                "rel": rel,
                "name": fn,
                "path": p.replace("\\", "/"),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "is_audio": True,
            })
    out.sort(key=lambda x: x["rel"].lower())
    return out


def _disk_batch_others(d: str) -> List[Dict[str, Any]]:
    """批次里的**非音频**文件（用户手扔进来的杂物）。

    单独列出来是为了：
      · 容量统计如实（它们确实占着盘）
      · 用户能看见并删掉，而不是"文件明明在、界面里找不到"
    但绝不参与入库。
    """
    out: List[Dict[str, Any]] = []
    for base_dir, _dirs, files in os.walk(d):
        for fn in files:
            if fn.endswith(".mshpart") or fn == "manifest.json":
                continue
            if P.is_audio(fn):
                continue
            p = os.path.join(base_dir, fn)
            rel = os.path.relpath(p, d).replace("\\", "/")
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({
                "rel": rel,
                "name": fn,
                "path": p.replace("\\", "/"),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "is_audio": False,
                "reason": "不是音乐文件，不会入库",
            })
    out.sort(key=lambda x: x["rel"].lower())
    return out


@router.get("/api/upload/batches")
def upload_batches() -> Dict[str, Any]:
    """列出暂存区里的所有批次（**以磁盘为准**，孤儿文件也会列出来）。"""
    root = _uploads_root()
    out = []
    stray: List[Dict[str, Any]] = []      # 根目录下直接散落的文件（不属于任何批次）
    stale_h = 0.0
    try:
        from .core.config import load_config
        stale_h = float(getattr(load_config().settings, "staging_stale_hours", 48) or 0)
    except Exception:
        stale_h = 48.0

    for name in sorted(os.listdir(root), reverse=True):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            # 直接放在 uploads/ 下的文件（历史遗留）：也算"孤儿"，不能装作没看见
            if os.path.isfile(d) and name != "manifest.json":
                try:
                    st = os.stat(d)
                    stray.append({"rel": name, "name": name,
                                  "path": d.replace("\\", "/"), "size": st.st_size,
                                  "mtime": st.st_mtime})
                except OSError:
                    pass
            continue
        m = _load_manifest(d)
        files = _disk_batch_files(d)
        others = _disk_batch_others(d)
        if not files and not others:
            # 空批次目录本身也占一个"批次"位置，但没内容就不展示，
            # 免得列表里全是空壳（真要看可以用清理孤儿目录）
            continue
        total = sum(int(f.get("size") or 0) for f in files)
        other_bytes = sum(int(f.get("size") or 0) for f in others)
        # 最后活动时间：优先 manifest，其次磁盘上的 mtime
        stamps = [(f.get("mtime") or 0) for f in files + others]
        updated = m.get("updated") or m.get("created") or 0
        if not updated and stamps:
            updated = max(stamps)
        age_h = ((time.time() - float(updated)) / 3600.0) if updated else 0.0
        audio_n = len(files)
        out.append({
            "batch": name,
            "source": m.get("source") or "",
            "created": m.get("created"),
            "updated": updated,
            # count/bytes 只算**音频**：那才是"待入库的东西"。
            # 非音频单独用 ignored_* 报告，避免用户看到"5 个文件"却只能入库 3 个。
            "count": audio_n,
            "audio_count": audio_n,
            "other_count": len(others),
            "bytes": total,
            "human_bytes": P.human_size(total),
            # 非音频占的空间也要如实报（它们确实占着盘）
            "ignored_bytes": other_bytes,
            "ignored_human_bytes": P.human_size(other_bytes),
            # manifest 丢了 / 没记全 → 明确标出来，别让用户以为数据完好
            "orphan": not m.get("files"),
            "age_hours": round(age_h, 1),
            "stale": bool(stale_h and age_h >= stale_h),
            "files": files[:200],
            # ★ 用户手扔进来的杂物：列出来让他看得见、删得掉，
            #   但**绝不进入**入库流程（upload_commit 会拒绝它们）。
            "ignored": others[:200],
            "only_ignored": bool(not files and others),
        })
    out.sort(key=lambda x: x.get("updated") or 0, reverse=True)
    u = staging_notice()
    return {"root": u["dir"], "batches": out, "stray": stray,
            "stale_hours": stale_h,
            "stale_count": sum(1 for b in out if b["stale"]),
            "orphan_count": sum(1 for b in out if b["orphan"]),
            **u}


@router.delete("/api/upload/batch")
def upload_delete(batch: str, exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """丢掉一个批次（**只在暂存区里删**，不会碰音乐库）。"""
    d = _batch_dir(batch)
    if not os.path.abspath(d).startswith(os.path.abspath(_uploads_root())):
        raise HTTPException(400, "批次路径异常，已拒绝")
    call_with_timeout(lambda: shutil.rmtree(d, ignore_errors=True),
                          what="清理暂存目录")
    return {"ok": True, "batch": batch, "staging": staging_usage()}


@router.post("/api/upload/batch-to-station")
def upload_batch_to_station(payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """把暂存区里的批次**转进中转站**（长期存放），而不是丢掉。

    存在的意义：上传完发现"还没想好放哪"，又不该一直占着暂存区
    （暂存区是过路区，会算进上传配额）。转进中转站后就是明确的待办，
    有独立的上限，也能从「中转站」页随时归位。
    """
    from . import station_api as _ST
    batches = payload.get("batches") or []
    if not isinstance(batches, list) or not batches:
        raise HTTPException(400, "batches 不能为空")

    moved: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for bid in batches:
        try:
            d = _batch_dir(str(bid))
        except HTTPException as exc:
            failed.append({"batch": str(bid), "error": str(exc.detail)})
            continue
        files = _disk_batch_files(d)
        if not files:
            failed.append({"batch": str(bid), "error": "批次里没有文件"})
            continue
        total = sum(int(f.get("size") or 0) for f in files)
        try:
            _ST.check_capacity(total)
        except HTTPException as exc:
            failed.append({"batch": str(bid), "error": str(exc.detail)})
            continue
        for f in files:
            try:
                _ST.adopt_file(f["path"], origin={"from_staging": str(bid)})
                moved.append({"batch": str(bid), "name": f["name"]})
            except Exception as exc:
                failed.append({"batch": str(bid), "name": f["name"],
                               "error": str(exc)})
        # 文件都搬走了，批次目录（含 manifest）留着没意义
        try:
            call_with_timeout(lambda: shutil.rmtree(d, ignore_errors=True),
                          what="清理暂存目录")
        except Exception:
            pass

    return {"ok": True, "moved": moved, "failed": failed,
            "moved_count": len(moved), "failed_count": len(failed),
            "staging": staging_usage(), "station": _ST.station_usage()}


@router.post("/api/upload/cleanup")
def upload_cleanup(payload: Dict[str, Any] = Body(default={}), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """清理暂存区：删掉滞留批次 / 孤儿文件 / 空批次目录。

    `mode`:
      · `stale`  —— 只删超过 staging_stale_hours 的批次
      · `empty`  —— 只删空批次目录和散落的孤儿文件
      · `all`    —— 全清（需要 confirm=true）
    """
    mode = str((payload or {}).get("mode") or "empty")
    if mode == "all" and not (payload or {}).get("confirm"):
        raise HTTPException(400, "全部清理需要 confirm=true")
    root = _uploads_root()
    keep = set(str(x) for x in as_list(payload or {}, "keep", "要保留的批次"))
    stale_h = 48.0
    try:
        from .core.config import load_config
        stale_h = float(getattr(load_config().settings, "staging_stale_hours", 48) or 0)
    except Exception:
        pass

    removed: List[Dict[str, Any]] = []
    freed = 0
    for name in list(os.listdir(root)):
        if name in keep:
            continue
        d = os.path.join(root, name)
        if os.path.isfile(d) and name != "manifest.json":
            if mode in ("empty", "all"):        # 散落的孤儿文件
                try:
                    sz = os.path.getsize(d)
                    call_with_timeout(lambda: os.remove(d), what="删除暂存文件")
                    removed.append({"item": name, "kind": "孤儿文件", "bytes": sz})
                    freed += sz
                except OSError:
                    pass
            continue
        if not os.path.isdir(d):
            continue
        files = _disk_batch_files(d)
        if not files:
            try:
                call_with_timeout(lambda: shutil.rmtree(d, ignore_errors=True),
                          what="清理暂存目录")
                removed.append({"item": name, "kind": "空批次目录", "bytes": 0})
            except Exception:
                pass
            continue
        if mode == "empty":
            continue
        if mode in ("stale", "all"):
            total = sum(int(f.get("size") or 0) for f in files)
            if mode == "all":
                do = True
            else:
                m = _load_manifest(d)
                upd = m.get("updated") or m.get("created") or 0
                if not upd:
                    upd = max((f.get("mtime") or 0) for f in files)
                age_h = ((time.time() - float(upd)) / 3600.0) if upd else 0.0
                do = bool(stale_h and age_h >= stale_h)
            if do:
                try:
                    call_with_timeout(lambda: shutil.rmtree(d, ignore_errors=True),
                          what="清理暂存目录")
                    removed.append({"item": name, "kind": "批次",
                                    "bytes": total, "files": len(files)})
                    freed += total
                except Exception:
                    pass
    return {"ok": True, "mode": mode, "removed": removed,
            "removed_count": len(removed), "freed": freed,
            "staging": staging_usage()}


# ==================================================================== 分析去向

def _index_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把库内曲目按"内容哈希"和"文件名"建索引，供入库查重用。"""
    by_hash: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        brief = {"id": r.get("id"), "name": r.get("name"),
                 "path": r.get("path"), "size": r.get("size"),
                 "artist": r.get("artist"), "album": r.get("album")}
        h = r.get("hash_quick")
        if h:
            by_hash.setdefault(str(h), brief)
        n = str(r.get("name") or "").strip().lower()
        if n:
            by_name.setdefault(n, []).append(brief)
    return {"by_hash": by_hash, "by_name": by_name}


def _suggest_for(files: List[Dict[str, Any]], rows: List[Dict[str, Any]],
                 root: str, min_group: int, sinks: List[str],
                 custom: Dict[str, str],
                 indexed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给每个上传文件定一个建议去向。

    **直接复用「智能整理」的规划器**（`OG.plan_by_album`），不再自己写一套
    匹配逻辑。这里曾经是一份手抄的副本，结果两边的判定规则慢慢跑偏 ——
    按专辑分的时候会拿专辑名去匹配作者目录、相似目录自问自答等等，
    整理页修好了这里还是错的。规则只该有一份。

    与整理页的唯一区别是**范围**：
      · 分组的对象是这批**上传的文件**；
      · 目录画像用**全量库内曲目**（`folder_pool_rows`），
        这样才能复用库里已有的目录，而不是只能新建。

    上传文件还没有真正的路径，所以用 `_up/<rel>` 造一个占位路径参与规划，
    最后再映射回原来的相对路径。
    """
    folders_now = OG.build_folders(rows, root) if rows else {}
    sink_set = {str(s) for s in (sinks or [])}

    # 收纳区兜底（优先用用户指定的，其次自动识别的）
    sink_dest = ""
    if custom.get("__sink__"):
        sink_dest = OG._safe_seg(custom["__sink__"])
    if not sink_dest and sink_set:
        sink_dest = sorted(sink_set, key=lambda f: (len(f.split("/")),
                                                    -folders_now.get(f, {}).get("count", 0)))[0]
    if not sink_dest:
        sink_dest = "单曲"

    # 造占位行参与规划：路径唯一、能反查回 rel
    PLACE = "_up/"
    fake_rows: List[Dict[str, Any]] = []
    for i, f in enumerate(files):
        rel = str(f.get("rel") or f.get("name") or f"f{i}")
        fake_rows.append({
            "id": None,
            "path": f"{root}/{PLACE}{i}/{rel}",
            "name": f.get("name") or rel.rsplit("/", 1)[-1],
            "artist": f.get("artist") or "",
            "album": f.get("album") or "",
            "size": int(f.get("size") or 0),
            "source_id": "",
        })
    for r in fake_rows:
        r["_key"] = r["path"]

    by_path = {r["path"]: f for r, f in zip(fake_rows, files)}

    plan = OG.plan_by_album(
        fake_rows, root,
        min_group=max(2, int(min_group)),
        sinks=sorted(sink_set),
        custom={k: v for k, v in (custom or {}).items() if k and v},
        group_by="artist_album",
        keep_flat_below=True,
        small_policy="sink" if sink_dest else "keep",
        folder_pool_rows=rows,
    )

    # 规划结果 → 每个上传文件的建议
    dst_of: Dict[str, Dict[str, str]] = {}
    for a in plan.get("actions") or []:
        for it in a.get("items") or []:
            dst_of[it["path"]] = {"dst": a.get("dst") or "",
                                  "kind": a.get("kind") or "",
                                  "why": a.get("why") or ""}
    for m in plan.get("moves") or []:
        dst_of.setdefault(m["path"], {"dst": m.get("dst_dir") or "",
                                      "kind": "move",
                                      "why": m.get("reason") or ""})

    out: List[Dict[str, Any]] = []
    for i, f in enumerate(files):
        rel = str(f.get("rel") or f.get("name") or f"f{i}")
        key = fake_rows[i]["path"]
        hit = dst_of.get(key)
        if hit and hit["dst"]:
            cand, kind, why = hit["dst"], hit["kind"], hit["why"]
        else:
            # 规划器没给出移动（说明它在"保留原位"或已达门槛但无需搬动）：
            # 上传文件本来就不在库里，所以退回收纳区，不能"留在原地"。
            cand, kind = sink_dest, "sink"
            why = f"未匹配到更合适的目录，收进收纳区「{sink_dest}」"
        # 自定义映射优先级最高，且要按**映射后的相对路径**展示
        dup = _cheap_dup(f, indexed) if indexed else None
        out.append({**f, "dst": cand, "why": why, "kind": kind, "dup": dup,
                    "cover_url": (f"/api/upload/cover?batch={urlquote(str(f.get('_batch') or ''))}"
                                  f"&rel={urlquote(rel)}") if f.get("cover") else None})
    return {"items": out, "sinks": sorted(sink_set), "sink_dest": sink_dest}
def _cheap_dup(f: Dict[str, Any], indexed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """零成本粗筛：只比文件名与体积，不读文件内容。

    分析阶段用它给个初步印象；精确判定（读内容算哈希）走后台任务。
    """
    size = int(f.get("size") or 0)
    name_l = str(f.get("name") or "").strip().lower()
    if not size or not name_l:
        return None
    for hit in indexed["by_name"].get(name_l, []):
        if int(hit.get("size") or 0) == size:
            return {"level": "likely", "match": hit,
                    "why": f"库内已有同名同体积的曲目「{hit['name']}」"
                           f"（{hit.get('path', '')}）"}
    return None


def _dup_check(f: Dict[str, Any], indexed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """判断暂存文件是否已经在库里（**读内容**，较慢，只用于后台查重）。

    分两档，**都只在能给出证据时才报**，避免误报吓人：
      `exact`   — 内容哈希一致，基本可以确定是同一首歌（可信度最高）
      `likely`  — 文件名与体积都一致（哈希还没算过时的近似判据）

    返回 None 表示没发现重复。
    """
    size = int(f.get("size") or 0)
    name_l = str(f.get("name") or "").strip().lower()

    # 1) 内容哈希：最硬的证据（和索引里同一套抽样算法，保证能对上）
    if f.get("_abs"):
        try:
            from app.core.scanner import quick_hash_local
            h = quick_hash_local(f["_abs"])
            hit = indexed["by_hash"].get(h)
            if hit:
                return {"level": "exact", "hash": h, "match": hit,
                        "why": f"库内已有内容相同的曲目「{hit['name']}」"}
        except Exception:
            pass
    # 2) 文件名 + 体积都一致：作为近似提示
    if size:
        for hit in indexed["by_name"].get(name_l, []):
            if int(hit.get("size") or 0) == size:
                return {"level": "likely", "match": hit,
                        "why": f"库内已有同名同体积的曲目「{hit['name']}」"
                               f"（{hit.get('path', '')}）"}
    return None


def _dedup_files(d: Path, batch: str, sid: str, files: List[Dict[str, Any]],
                 job) -> Dict[str, Any]:
    """后台查重：把暂存文件逐个算哈希，跟库内索引比对。

    单独做成任务而不是塞进"分析"，是因为它要把每个文件**完整读一遍**：
    300 首的批次就是几 GB 的磁盘 IO，放在同步请求里会让界面卡死。
    这里按文件推进 `job.done`，界面能显示进度、也能取消。
    """
    rows: List[Dict[str, Any]] = []
    if sid:
        mgr = _deps()
        try:
            rows = [dict(r) for r in mgr.db().all_tracks(sid)]
        except Exception:
            rows = []
    indexed = _index_rows(rows)

    job.total = len(files)
    results: Dict[str, Any] = {}
    for f in files:
        if job.cancel:
            job.state = "cancelled"
            job.message = "已取消"
            break
        job.current = f.get("name") or f.get("rel") or ""
        try:
            dup = _dup_check(f, indexed)
        except Exception as exc:                       # 单个文件出错不该中断整批
            dup = None
            job.message = f"部分文件无法校验（{exc}）"
        if dup:
            results[f["rel"]] = dup
        job.done += 1
        # ★ 必须打心跳。`is_stale()` 只看 `heartbeat`，而只有 `touch()`
        #   会写它 —— 光改 `done` 不算进度"活着"。
        #   这一步是把每个文件完整读一遍（几 GB 的磁盘 IO），几百首轻易
        #   超过 STALE_JOB_SECONDS(180s)；不打心跳就会被判成"已中断"、
        #   **释放独占位**，于是另一个写流程能插进来。
        job.touch()
    if job.state == "running":
        job.message = (f"发现 {len(results)} 个疑似重复"
                       if results else "未发现重复")
    return {"dups": results, "checked": job.done}


@router.post("/api/upload/dedup")
def upload_dedup(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """启动入库查重任务（用户主动触发）。

    **只提示、不拦截**：结果交给界面展示，入库与否完全由用户决定。
    """
    batch = as_text(payload, "batch", "批次号")
    if not batch:
        raise HTTPException(400, "缺少 batch")
    d = _batch_dir(batch)
    m = _load_manifest(d)
    raw = []
    for f in m.get("files", []):
        ap = os.path.join(d, f["rel"].replace("/", os.sep))
        if os.path.isfile(ap):
            raw.append({**f, "_batch": batch, "_abs": ap})
    if not raw:
        raise HTTPException(400, "这个批次里没有文件")

    sid = str(payload.get("source") or m.get("source") or "")
    job = get_jobs().create("upload-dedup", total=len(raw))
    job.message = "正在比对库内曲目…"

    def work():
        try:
            res = _dedup_files(Path(d), batch, sid, raw, job)
            # ★ 必须走 finish()：写库、补 finished 时间戳、通知等待方。
            #   直接改 job.state 只改内存 —— 重启后这条任务会被当成
            #   "未完成的陈旧任务"，界面也收不到完成信号。
            get_jobs().finish(job, result=res)
            # 结果留在 manifest 里，重新分析时还能看到
            try:
                m2 = _load_manifest(d)
                m2["dups"] = res["dups"]
                _save_manifest(d, m2)
            except Exception:
                pass
        except Exception as exc:
            get_jobs().finish(job, error=str(exc))

    threading.Thread(target=work, daemon=True).start()
    return {"job": job.id, "total": len(raw)}


@router.get("/api/upload/dedup")
def upload_dedup_status(job: str = "") -> Dict[str, Any]:
    """查询查重任务进度 / 取结果。"""
    if not job:
        raise HTTPException(400, "缺少 job")
    st = get_jobs().get(job)
    if not st:
        raise HTTPException(404, "任务不存在或已过期")
    return st.to_dict()


@router.get("/api/upload/dups")
def upload_dups(batch: str) -> Dict[str, Any]:
    """取某个批次上次查重的结果（没有就返回空）。"""
    d = _batch_dir(batch)
    m = _load_manifest(d)
    return {"dups": m.get("dups") or {}}


@router.get("/api/upload/inspect")
def upload_inspect(batch: str, source: str = "", min_group: int = 5,
                   custom: str = "") -> Dict[str, Any]:
    """分析一个批次：读标签、跟音乐库现有目录比对、给出建议去向。"""
    d = _batch_dir(batch)
    m = _load_manifest(d)
    files = []
    for f in m.get("files", []):
        ap = os.path.join(d, f["rel"].replace("/", os.sep))
        if os.path.isfile(ap):
            files.append({**f, "_batch": batch, "_abs": ap})
    if not files:
        raise HTTPException(400, "这个批次里没有文件")

    try:
        custom_map = json.loads(custom) if custom else {}
        if not isinstance(custom_map, dict):
            custom_map = {}
    except Exception:
        custom_map = {}

    rows: List[Dict[str, Any]] = []
    root = ""
    sid = source or m.get("source") or ""
    if sid:
        _src, root = _resolve_src_soft(sid)
        if root:
            mgr = _deps()
            try:
                rows = [dict(r) for r in mgr.db().all_tracks(sid)]
            except Exception:
                rows = []
    res = _suggest_for(files, rows, root, max(1, min(100, int(min_group))),
                       OG.find_sinks(rows, root) if rows else [], custom_map,
                       indexed=_index_rows(rows))
    # 按建议去向分个组，界面上直接按组显示
    by_dst: Dict[str, List[str]] = {}
    for it in res["items"]:
        by_dst.setdefault(it["dst"], []).append(it["rel"])
    return {
        "batch": batch, "source": sid, "root": root,
        "count": len(res["items"]),
        "bytes": sum(int(f.get("size") or 0) for f in res["items"]),
        "items": res["items"],
        "sinks": res["sinks"], "sink_dest": res["sink_dest"],
        "by_dst": [{"dst": k, "count": len(v)} for k, v in
                   sorted(by_dst.items(), key=lambda x: -len(x[1]))],
        "library_tracks": len(rows),
        "library_folders": len([f for f in OG.build_folders(rows, root) if f]) if rows else 0,
    }


def _resolve_src_soft(sid: str) -> Tuple[Any, str]:
    """拿数据源和它的根；拿不到就返回 (None, "")，而不是抛异常。

    这里刻意不抛异常：上传分析允许"还没选数据源"这种状态，
    界面上仍然可以看标签、只是给不出"跟库比对"的建议。
    """
    try:
        from .core.config import resolve_source
        resolve_source(sid)                 # 校验存在
        src = _deps().manager.get(sid)
        return src, P.norm(src.root)
    except Exception:
        return None, ""


@router.get("/api/upload/cover")
def upload_cover(batch: str, rel: str) -> Any:
    """暂存区里某个文件的封面缩略图。"""
    from fastapi.responses import Response
    d = _batch_dir(batch)
    if ".." in rel.split("/") or not rel:
        raise HTTPException(400, "路径不合法")
    ap = os.path.join(d, rel.replace("/", os.sep))
    stem, _ext = os.path.splitext(ap)
    cp = os.path.join(os.path.dirname(ap), "." + os.path.basename(stem) + ".cover")
    if not os.path.isfile(cp):
        raise HTTPException(404, "没有封面")
    with open(cp, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.post("/api/upload/commit")
def upload_commit(payload: Dict[str, Any] = Body(...), exclusive: None = Depends(exclusive_guard)) -> Dict[str, Any]:
    """把暂存区的文件按确认后的去向搬进音乐库。

    `moves` 是 `[{rel, dst}]`；`dst` 是相对数据源根的目录（空 = 根目录）。
    走 Executor，所以继承"跳过同名 / 移动后校验 / 绝不覆盖"的全部保护。
    """
    batch = payload.get("batch")
    sid = payload.get("source")
    if not batch or not sid:
        raise HTTPException(400, "缺少 batch / source")
    moves = as_dicts(payload, "moves", "入库清单", max_items=5000)
    if not moves:
        raise HTTPException(400, "没有要入库的文件")
    if len(moves) > 5000:
        raise HTTPException(400, "一次入库的文件过多，请分批")

    src, root = _resolve_src_soft(sid)
    if src is None or not root:
        raise HTTPException(404, f"数据源不存在: {sid}")
    if not src.can_write:
        raise HTTPException(403, f"数据源「{src.cfg.name}」已设为只读，拒绝写入")
    if not payload.get("confirm"):
        raise HTTPException(400, "执行写操作需要 confirm=true")
    policy = str(payload.get("policy") or "skip")
    if policy not in ("skip", "rename", "overwrite"):
        raise HTTPException(400, f"不支持的同名冲突策略: {policy}")
    do_scan = bool(payload.get("scan", True))

    d = _batch_dir(batch)
    # ★ 入库前先算这一批到底多大，并检查**目标盘**装不装得下。
    #   暂存区够用 ≠ 目标盘够用（可能是另一块盘），
    #   搬到一半失败会让库里多出一半文件，很难收拾。
    batch_files = {f["rel"]: f for f in _disk_batch_files(d)}
    # 按目标目录分组算：不同目标盘要各自判断
    per_dir: Dict[str, int] = {}
    for mv in moves:
        rel = str(mv.get("rel") or "").replace("\\", "/")
        f = batch_files.get(rel)
        if not f:
            continue
        dst = str(mv.get("dst") or "").strip().strip("/")
        # ★ `..` 必须在这里挡掉：后面的 startswith(root) 检查**挡不住**
        #   `root/../outside` —— 字符串确实以 root 开头，但文件系统会解析 ..
        if P.has_dotdot(dst) or re.match(r"^[A-Za-z]:", dst):
            raise HTTPException(400, f"目标目录不合法: {dst}")
        tgt = os.path.join(root, dst.replace("/", os.sep)) if dst else root
        per_dir[tgt] = per_dir.get(tgt, 0) + int(f.get("size") or 0)
    for tgt, sz in per_dir.items():
        check_commit_capacity(tgt, sz)

    plan: List[Dict[str, Any]] = []
    skipped_fmt: List[Dict[str, Any]] = []
    for i, mv in enumerate(moves):
        # ★ 反斜杠必须先归一：`rel.split("/")` 对 `..\\outside` 只会得到一段，
        #   `".." in [...]` 判不出来，随后 os.path.join 就穿出批次目录了。
        rel = str(mv.get("rel") or "").replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            continue
        ap = os.path.join(d, rel.replace("/", os.sep))
        if not os.path.isfile(ap):
            continue
        # ★ 最后一道闸：**非音频绝不进音乐库**。
        #   列表那边已经过滤了，但客户端可以直接构造 moves 绕过去
        #   （脚本、旧版页面、手工请求）。入库是"写进用户音乐库"的动作，
        #   让它把 .txt/.jpg 搬进去是不可接受的 —— 索引会忽略它们，
        #   结果是库里多出一堆谁也看不见、谁也管不着的垃圾文件。
        if not P.is_audio(ap):
            skipped_fmt.append({
                "rel": rel, "name": os.path.basename(ap),
                "reason": f"不是音乐文件（{P.ext(ap) or '无扩展名'}），已跳过",
            })
            continue
        dst_rel = P.norm(str(mv.get("dst") or "")).strip("/")
        if P.has_dotdot(dst_rel):
            raise HTTPException(400, f"目标目录不合法: {dst_rel}")
        name = os.path.basename(ap)
        dst_abs = P.join(root, dst_rel, name) if dst_rel else P.join(root, name)
        # 目标必须仍在数据源根目录内
        if not (dst_abs == root or dst_abs.startswith(root.rstrip("/") + "/")):
            continue
        plan.append({
            "id": f"up-{i}", "op": "move", "kind": "upload",
            # 上传的文件在本地磁盘上，所以"源"用本地路径直接搬
            "src_path_abs": ap, "src": sid,
            "dst": sid, "dst_path": dst_abs,
            "size": os.path.getsize(ap),
        })
    if not plan:
        if skipped_fmt:
            raise HTTPException(
                400, "选中的文件都不是音乐文件，没有可入库的内容："
                     + "；".join(f"{x['name']}（{x['reason']}）"
                                for x in skipped_fmt[:5]))
        raise HTTPException(400, "选中的文件都找不到了")

    mainmod = _deps()
    # 给这次入库建一个任务：这样界面能看到进度、能取消，
    # 循环里也能打心跳（否则大批次会被误判"已中断"并释放独占位）。
    job = get_jobs().create("upload-commit", total=len(plan))
    try:
        ok = _move_local_to_source(plan, src, root, policy, job=job)
        summary = ok["summary"]
        get_jobs().finish(job, result=summary)
    except Exception as exc:                                  # noqa: BLE001
        get_jobs().finish(job, error=f"{type(exc).__name__}: {exc}")
        raise

    # 入库后刷新索引（否则要等下一次全量扫描才看得到）
    scan_started = False
    if do_scan and summary.get("done"):
        try:
            # 复用接口层那套（带任务状态、可取消）。
            # ★ 签名是 `start_scan(sid, body)`，body 是**接口请求体**的形状。
            #   之前这里写的是 `m.start_scan(...)` —— 而 `m` 是同函数里的
            #   **manifest 字典**（`_load_manifest` 的返回值），
            #   于是必然抛 `AttributeError: module 'app.main' has no attribute 'get'`…
            #   不，更准确地说：deps 对象被 manifest 覆盖了，`m.start_scan` 时
            #   `m` 已经是 dict，抛 AttributeError 被下面的 except 吞掉 ——
            #   结果"入库后自动刷新索引"**从来没生效过**。
            #   变量重名是根因，这里把两边都改成不会撞的名字。
            mainmod.start_scan(sid, {})
            scan_started = True
        except Exception:
            scan_started = False

    # 成功的文件从暂存区删掉；失败的留着让用户重试。
    # `man` = 这个批次的 manifest（磁盘上的 manifest.json 内容）。
    man = _load_manifest(d)
    remain = list(man.get("files") or [])
    done_rels = {r for r in ok["done_rels"]}
    if done_rels:
        remain = [f for f in remain if f.get("rel") not in done_rels]
        man["files"] = remain
        man["updated"] = time.time()
        _save_manifest(d, man)
        for rel in done_rels:
            try:
                call_with_timeout(
                    lambda r=rel: os.remove(
                        os.path.join(d, r.replace("/", os.sep))),
                    what="删除暂存文件")
            except OSError:
                pass

    return {
        "ok": True, "batch": batch, "source": sid,
        "moved": summary.get("done", 0),
        "skipped": summary.get("skipped", 0),
        "failed": summary.get("error", 0),
        "bytes": summary.get("bytes_moved", 0),
        "results": ok["results"],
        "remaining_in_batch": len(remain),
        "scan_started": scan_started,
        # ★ 结果项的键是 `dst`（见 `_move_local_to_source` 里的 item 结构），
        #   不是 `dst_path` —— 后者是 **Executor 步骤**的字段名。
        #   这里原来写 `r["dst_path"]`，必然 KeyError。
        #   和上面那处 `m.start_scan` 一样，都是"成功路径从没被执行过"
        #   才能留到今天的缺陷：一旦有人真的入库成功就 500。
        "target_dirs": sorted({os.path.dirname(str(r.get("dst") or ""))
                               for r in ok["results"]
                               if r.get("state") == "done"
                               and r.get("dst")})[:200],
        "not_music": skipped_fmt,
    }


def _move_local_to_source(plan: List[Dict[str, Any]], src, root: str,
                          policy: str, job=None) -> Dict[str, Any]:
    """把本地暂存文件搬进数据源。

    这里不能直接用 Executor —— 它的两端都是"数据源内的路径"，而上传文件的
    源在暂存区（不在任何数据源里）。所以自己搬，但保持同一套安全约定：
    先建目录、同名按策略处理、搬完校验大小、失败不删源文件。

    `job` 传进来是为了**打心跳 + 响应取消**：大批次入库会超过
    `STALE_JOB_SECONDS`，不打心跳会被判"已中断"并释放独占位。
    """
    results: List[Dict[str, Any]] = []
    done_rels: List[str] = []
    summary = {"done": 0, "skipped": 0, "error": 0, "bytes_moved": 0,
               "cancelled": 0}

    for st in plan:
        if job is not None:
            if job.cancel:
                # 剩下的没搬 —— 如实记下来，别假装成功
                summary["cancelled"] = len(plan) - len(results)
                break
            job.touch()
            job.current = st.get("dst_path") or ""
            job.done = min(job.total or len(plan), job.done + 1)
        ap = st["src_path_abs"]
        dst_abs = st["dst_path"]
        rel = os.path.relpath(ap, os.path.dirname(os.path.dirname(ap)))
        item = {"id": st["id"], "src": ap, "dst": dst_abs, "state": "done",
                "detail": "", "bytes": st.get("size", 0)}
        try:
            dst_dir = os.path.dirname(dst_abs.replace("/", os.sep))
            os.makedirs(dst_dir, exist_ok=True)
            target = dst_abs
            # ★ 只有真的走 overwrite 才备份：其它策略下这里的 target
            #   会被保留/跳过，把原件丢进回收站反而是丢失。
            if policy == "overwrite" and os.path.exists(target):
                try:
                    src.remove(src.rel_path(target), hard=False)
                except Exception:                      # noqa: BLE001
                    pass
            if os.path.exists(target.replace("/", os.sep)):
                if policy == "skip":
                    item["state"] = "skipped"
                    item["detail"] = "目标已存在同名文件，已跳过（未覆盖）"
                    summary["skipped"] += 1
                    results.append(item)
                    continue
                if policy == "rename":
                    stem, ext = os.path.splitext(dst_abs)
                    i = 1
                    while os.path.exists(f"{stem} ({i}){ext}".replace("/", os.sep)):
                        i += 1
                    target = f"{stem} ({i}){ext}"
                    item["dst"] = target
                # overwrite：直接覆盖（走下面 replace）
            size_before = os.path.getsize(ap)
            # 入库的最后一跳。用带超时的包装：坏扇区 / 网络盘掉线 /
            # Windows 保留设备名（CON、NUL.flac…）都会让 os.replace
            # **永久不返回**（实测 8 秒无返回），HTTP 线程就挂死了。
            # ★ overwrite 前必须把库内原文件移进回收站；备份失败就中止，
            #   绝不无备份物理覆盖（宁可不传，也不能毁掉用户原版）。
            if policy == "overwrite" and os.path.exists(target.replace("/", os.sep)):
                try:
                    src.remove(src.rel_path(target), hard=False)
                except Exception as exc:                      # noqa: BLE001
                    raise IOError(
                        f"覆盖前备份原文件失败，已拒绝写入: {exc}") from exc
            call_with_timeout(
                lambda: os.replace(ap.replace("/", os.sep),
                                   target.replace("/", os.sep)),
                what=f"入库 {os.path.basename(target)}")
            size_after = os.path.getsize(target.replace("/", os.sep))
            if size_after != size_before:
                raise IOError(f"搬完大小不一致（{size_before} → {size_after}）")
            summary["done"] += 1
            summary["bytes_moved"] += size_after
            # 记下相对暂存区的路径，便于从暂存区清理
            try:
                base = _uploads_root()
                done_rels.append(os.path.relpath(
                    ap.replace("/", os.sep), base).split(os.sep, 2)[-1].replace(os.sep, "/"))
            except Exception:
                pass
            results.append(item)
        except Exception as exc:
            item["state"] = "error"
            item["detail"] = str(exc)
            summary["error"] += 1
            results.append(item)

    return {"results": results, "summary": summary, "done_rels": done_rels}
