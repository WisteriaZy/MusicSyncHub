"""目录扫描与索引。

策略：
* 单次目录遍历（网络源一次 listdir 拿全，避免 N+1 请求）；
* 4~16 线程并发拉取元数据，重复文件走 SQLite 缓存（按 size+mtime+etag 判定）；
* 大文件只读头部解析标签，避免越洋下载；
* 顺便登记同目录的封面图，供列表页显示；
* 扫描结束清理索引里已不存在的记录（增量、可审计）。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import audio as A
from . import matching as M
from . import paths as P
from .config import load_config
from .jobs import JobState, get_jobs
from ..sources.base import Entry, MusicSource, SourceError

CACHE_ETAG_TOLERANCE = 1.0


@dataclass
class ScanResult:
    source_id: str
    seen: int = 0
    parsed: int = 0
    cached: int = 0
    errors: int = 0
    removed: int = 0
    covers: int = 0
    truncated: bool = False
    duration: float = 0.0
    samples: List[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["samples"] = (self.samples or [])[:20]
        return d


def _fetch_bytes(src: MusicSource, path: str, size: int, ext: str) -> Tuple[bytes, bool]:
    """读取解析标签所需的数据。返回 (data, head_only)。"""
    if size and size <= A.FULL_READ_LIMIT:
        return src.read_range(path, 0, 0), False
    head = A.HEAD_BYTES_LOSSLESS if ext in (".flac", ".ape", ".wv", ".dsf", ".dff") else A.HEAD_BYTES_DEFAULT
    return src.read_range(path, 0, head), True


def _row_from_meta(meta: A.AudioMeta, base: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(base)
    row.update(meta.to_row())
    row["is_lossless"] = 1 if meta.is_lossless else 0
    row["name_key"] = P.name_key(row.get("name") or "")
    row["dup_key"] = M.dup_key({**row, "title": row.get("title"), "artist": row.get("artist"),
                                "duration": row.get("duration")})
    row["quality"] = A.quality_score(row)
    row["has_cover"] = 1 if meta.cover_data else 0
    row.pop("status", None)
    row.pop("error", None)
    row["meta_status"] = meta.status
    row["meta_error"] = (meta.error or "")[:500]
    row["scanned_at"] = time.time()
    return row


def scan_source(sid: str, source: MusicSource, db, job: Optional[JobState] = None,
                subpath: str = "", workers: int = 8,
                compute_hash: bool = False, hash_limit_mb: int = 512,
                quick_probe_kb: int = 256, progress: Optional[Callable[[str], None]] = None,
                max_files: int = 0, skip_full_hash: bool = False) -> ScanResult:
    """遍历并解析一个数据源。

    `skip_full_hash` 供**慢速盘 / 可移除设备**使用：U 盘上按内容做全量哈希
    会让扫描从几分钟变成几十分钟，而且中途一拔盘就白干。此时只保留
    "大小 + 时长 + 标签"这套轻量判据（查重仍然可用，只是少了字节级铁证）。

    `workers` 应当由调用方按盘的性质压过（见 `core.device.effective_workers`），
    这里只负责不再往上抬。
    """
    res = ScanResult(source_id=sid, samples=[])
    t0 = time.time()
    # ★ 记录所有权：job 是**外部传进来的**（比如 indexwatch 的巡检独占任务）时，
    #   scan_source 只负责更新进度/发心跳，**绝不 finish** —— 否则第一个子目录
    #   扫完就把调用方的锁释放了，后续目录直接 JobBusy / 状态错乱。
    created_job = (job is None)
    job = job or get_jobs().create("scan")
    # ★ 所有权标记挂在 job 上而不是用局部变量：`scan_source` 里有嵌套函数
    #   （ensure_hashes 等）也会收尾 job，闭包看不到局部变量会悬空。
    job._owned_here = created_job          # type: ignore[attr-defined]
    job.message = f"正在遍历 {source.cfg.name} …"
    job.touch()
    stop = threading.Event()

    def say(msg: str) -> None:
        job.message = msg
        job.current = msg
        # 这里顺便发心跳：say() 是扫描过程中最频繁的进度出口，
        # 挂在这儿能保证"只要还在动就不会被判定成卡死"。
        job.touch()
        if progress:
            progress(msg)

    # ---------------------------------------------------------- 1. 遍历
    entries: List[Entry] = []
    cover_files: List[Tuple[str, str, int, float]] = []
    dirs_seen = 0

    # ★ 用户配置的「忽略的文件名/目录名」。
    #
    #   这个设置项以前**界面能填、存进 config.toml、后端零消费** ——
    #   填了完全没效果，也不报错。和 `cover_cache_size` 是同一类缺陷：
    #   设置项看起来存在，实际没接线，比压根没有这个选项更误导。
    #
    #   只读一次（不要放进 `should_descend` 里逐目录读配置：一次扫描会调用
    #   上万次，那是实打实的性能问题）。比较时统一小写 —— Windows 文件名
    #   大小写不敏感，`@EADIR` 和 `@eaDir` 必须算同一个。
    try:
        ignored = {str(n).strip().lower()
                   for n in (load_config().settings.ignored_names or [])
                   if str(n).strip()}
    except Exception:
        ignored = set()

    def on_dir(cur: str, items: List[Entry]) -> None:
        nonlocal dirs_seen
        dirs_seen += 1
        if dirs_seen % 25 == 0:
            say(f"遍历目录中… 已发现 {len(entries)} 个音频文件（{dirs_seen} 个目录）")

    def should_descend(path: str) -> bool:
        base = P.basename(path).lower()
        if ignored and base in ignored:
            return False
        return base not in (".music-sync-trash", ".msh", "$recycle.bin", "system volume information")

    try:
        for e in source.walk(subpath, on_dir=on_dir, should_descend=should_descend):
            if job.cancel:
                stop.set()
                break
            if max_files and len(entries) >= max_files:
                res.truncated = True
                break
            if ignored and P.basename(e.path).lower() in ignored:
                continue
            if P.is_audio(e.name):
                entries.append(e)
            elif P.is_cover(e.name):
                cover_files.append((e.path, e.name, e.size, e.mtime))
    except SourceError as exc:
        if getattr(job, "_owned_here", True):
            get_jobs().finish(job, error=str(exc))
        raise

    if cover_files:
        # ★ 整库扫描时先把旧的封面索引清掉。
        #   封面索引 `A._cover_index` **不会自失效** —— `register_cover_files`
        #   只会往里加/覆盖，时间戳记了但没人读。于是封面文件被删掉之后，
        #   索引里还留着那个路径，取封面时会指向已不存在的文件。
        #   `A.clear_cover_index()` 本来就为这件事写的，但一直没人调用
        #   （属于「函数写好了、没接线」，和 ignored_names 同病）。
        #   只在整库扫描（subpath 为空）时清：扫子目录只覆盖那一块。
        if not subpath:
            A.clear_cover_index(sid)
        A.register_cover_files(sid, cover_files)
        res.covers = len(cover_files)
    elif not subpath:
        # 整库扫完零个封面 → 旧索引里的确实都不存在了
        A.clear_cover_index(sid)

    job.total = len(entries)
    say(f"共发现 {len(entries)} 个音频文件，开始解析元数据…")

    # ---------------------------------------------------------- 2. 元数据（并发）
    lock = threading.RLock()
    rows: List[Dict[str, Any]] = []
    covers_to_store: List[Tuple[str, bytes, str]] = []

    def handle(e: Entry) -> None:
        if stop.is_set():
            return
        ext = P.ext(e.name)
        rel = source.rel_path(e.path)
        base = {
            "source_id": sid, "path": P.norm(e.path), "rel_path": rel, "name": e.name,
            "parent": P.parent(e.path), "ext": ext, "size": e.size or 0, "mtime": e.mtime or 0,
        }
        try:
            cached = db.track(sid, base["path"])
            if cached is not None and not _stale(cached, e):
                row = dict(cached)
                row["size"] = e.size or row.get("size") or 0
                row["mtime"] = e.mtime or row.get("mtime") or 0
                # 保留已算好的哈希，避免缓存命中后被覆盖成 NULL
                row["hash_quick"] = cached["hash_quick"]
                row["hash_full"] = cached["hash_full"]
                row["hash_state"] = cached["hash_state"] or "none"
                with lock:
                    rows.append(row)
                    res.cached += 1
                    _tick(job, res)
                return
            data, head_only = _fetch_bytes(source, e.path, e.size or 0, ext)
            meta = A.parse_bytes(data, e.name, head_only=head_only)
            row = _row_from_meta(meta, base)
            if meta.cover_data:
                thumb, mime, w, h = A.make_thumbnail(meta.cover_data)
                hh = A.sha1_bytes(thumb)
                row["cover_mime"] = mime
                row["cover_hash"] = hh
                row["has_cover"] = 1
                with lock:
                    covers_to_store.append((hh, thumb, mime))
            else:
                cv = A.find_cover_file(sid, e.path)
                if cv:
                    row["_cover_path"] = cv
            # 慢速盘（U 盘 / 网络位置）不按内容做全量哈希：
            # 读一遍整盘太慢，而且中途拔盘就白干。轻量判据够查重用。
            if (compute_hash and not skip_full_hash
                    and (e.size or 0) <= hash_limit_mb * 1024 * 1024
                    and not head_only):
                row["hash_quick"] = quick_hash_bytes(data)
                row["hash_state"] = "quick"
            with lock:
                rows.append(row)
                res.parsed += 1
                _tick(job, res)
                if len(res.samples) < 20:
                    res.samples.append({"path": row["path"], "title": row.get("title"),
                                        "artist": row.get("artist"), "duration": row.get("duration"),
                                        "codec": row.get("codec"), "bitrate": row.get("bitrate")})
        except Exception as exc:  # 单个文件失败不影响整体
            with lock:
                res.errors += 1
                rows.append(_error_row(base, exc))
                _tick(job, res)

    def _tick(j, r) -> None:
        j.done = res.parsed + res.cached + res.errors
        if j.done % 20 == 0:
            j.message = f"已解析 {j.done}/{j.total}（缓存命中 {r.cached}，失败 {r.errors}）"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(handle, entries))

    if stop.is_set():
        if getattr(job, "_owned_here", True):
            get_jobs().finish(job, error="已取消")
        raise SourceError("扫描已取消")

    # ---------------------------------------------------------- 3. 封面落库（外挂封面）
    job.message = "处理封面…"
    for row in rows:
        cvp = row.pop("_cover_path", None)
        if not cvp or row.get("cover_hash"):
            continue
        try:
            raw = source.read_range(cvp, 0, 0)
            thumb, mime, w, h = A.make_thumbnail(raw)
            hh = A.sha1_bytes(thumb)
            row["cover_hash"] = hh
            row["cover_mime"] = mime
            row["has_cover"] = 1
            covers_to_store.append((hh, thumb, mime))
        except Exception:
            row["has_cover"] = 0
    for hh, data, mime in covers_to_store:
        try:
            db.put_cover(hh, mime, 0, 0, data)
        except Exception:
            pass

    # 统一成表字段
    clean: List[Dict[str, Any]] = []
    for r in rows:
        clean.append({k: r.get(k) for k in _TRACK_FIELDS})
    job.message = f"写入索引（{len(clean)} 条）…"
    db.upsert_tracks(clean)

    # ---------------------------------------------------------- 4. 清理失效记录
    if not res.truncated and not subpath:
        alive = [r.get("path") for r in clean]
        res.removed = db.remove_missing(sid, alive)
    elif subpath:
        # 局部扫描只清理该子目录下的消失项
        alive = {r.get("path") for r in clean}
        prefix = source.abs_path(subpath)
        dead = [r["path"] for r in db.tracks(sid, prefix=prefix) if r["path"] not in alive]
        if dead:
            res.removed = db.remove_tracks([(sid, p) for p in dead])

    res.seen = len(entries)
    res.duration = round(time.time() - t0, 1)
    db.set_scan_meta(sid, time.time(), len(rows))
    db.upsert_source(sid, source.cfg.name, source.cfg.type, source.cfg.root)
    if getattr(job, "_owned_here", True):
        get_jobs().finish(job, result=res.to_dict())
    return res


def _stale(cached, e: Entry) -> bool:
    """判断索引记录是否过期。大小变了、或 mtime 变了都算过期。"""
    try:
        csize = int(cached["size"] or 0)
        cmtime = float(cached["mtime"] or 0)
    except Exception:
        return True
    if e.size and csize and e.size != csize:
        return True
    if e.mtime and cmtime and abs(e.mtime - cmtime) > CACHE_ETAG_TOLERANCE:
        return True
    if not e.mtime and not e.size and cached["meta_status"] in (None, "pending"):
        return True
    return False


def _error_row(base: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
    row = dict(base)
    row.update({
        "title": P.stem(base["name"]), "artist": "", "album": P.basename(base["parent"]),
        "album_artist": "", "track_no": "", "year": "", "genre": "",
        "duration": None, "bitrate": None, "sample_rate": None, "channels": None,
        "bits_per_sample": None, "codec": P.ext(base["name"]).lstrip(".").upper(),
        "is_lossless": 0, "has_cover": 0, "cover_mime": None, "cover_hash": None,
        "meta_status": "error", "meta_error": f"{type(exc).__name__}: {exc}"[:500],
        "name_key": P.name_key(base["name"]), "dup_key": "", "quality": 0.0,
        "hash_quick": None, "hash_full": None, "hash_state": "none", "scanned_at": time.time(),
        "rel_path": base.get("rel_path", ""),
    })
    return row


_TRACK_FIELDS = [
    "source_id", "path", "rel_path", "name", "parent", "ext", "size", "mtime",
    "title", "artist", "album", "album_artist", "track_no", "year", "genre",
    "duration", "bitrate", "sample_rate", "channels", "bits_per_sample", "codec",
    "is_lossless", "has_cover", "cover_mime", "cover_hash", "meta_status", "meta_error",
    "name_key", "dup_key", "quality", "hash_quick", "hash_full", "hash_state", "scanned_at",
]


# ------------------------------------------------------------------ 哈希

def quick_hash_bytes(data: bytes, probe_kb: int = 256) -> str:
    """抽样哈希：头 + 尾各取一段。比全量哈希快几十倍，足够区分不同音质/不同录音。"""
    import hashlib

    probe = max(16 * 1024, probe_kb * 1024)
    if len(data) <= 2 * probe:
        return hashlib.sha1(b"F" + data).hexdigest()
    h = hashlib.sha1(b"Q")
    h.update(data[:probe])
    h.update(data[-probe:])
    h.update(str(len(data)).encode())
    return h.hexdigest()


# 小于这个体积就整文件哈希：抽样在"头尾相同、中间不同"的文件上会误判
# （例如同一首歌的高/低采样率版本，头尾的标签区一致）。多读几 MB 换准确性很值。
QUICK_FULL_THRESHOLD = 6 * 1024 * 1024


def quick_hash(source: MusicSource, path: str, size: int, probe_kb: int = 256) -> str:
    probe = max(16 * 1024, probe_kb * 1024)
    if not size or size <= QUICK_FULL_THRESHOLD:
        # 小文件：一次读完，直接全量哈希（本地/远端都只要一次请求）
        data = source.read_range(path, 0, 0)
        import hashlib

        return hashlib.sha1(b"F" + data).hexdigest()
    head = source.read_range(path, 0, probe)
    tail = source.read_range(path, max(0, size - probe), probe)
    import hashlib

    h = hashlib.sha1(b"Q")
    h.update(head)
    h.update(tail)
    h.update(str(size or 0).encode())
    return h.hexdigest()


def quick_hash_local(path: str) -> str:
    """本地文件的 `hash_quick`，用于入库查重。

    **必须和索引里存的值逐位一致**，否则查重永远匹配不上、而且不报错，
    是那种最安静的 bug。所以这里复刻的是扫描器的**实际做法**：
    扫描时 `data` 是整份文件（本地一定是整份），哈希就是
    `quick_hash_bytes(整份内容)`。所以这里也整份读、整份哈希。

    （不要写成"只读头尾"的抽样版：`quick_hash()` 那个抽样函数服务于
      Range 请求的远端源，跟索引里存的值不是一回事。）
    """
    with open(path, "rb") as fh:
        return quick_hash_bytes(fh.read())


def full_hash(source: MusicSource, path: str) -> str:
    import hashlib

    h = hashlib.sha1()
    fh = source.open_read(path)
    try:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    finally:
        try:
            fh.close()
        except Exception:
            pass
    return h.hexdigest()


def ensure_hashes(sid: str, source: MusicSource, db, rows: Iterable[Dict[str, Any]],
                  job: Optional[JobState] = None, mode: str = "quick",
                  limit_mb: int = 512, probe_kb: int = 256,
                  workers: int = 4,
                  on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, int]:
    """按需补算哈希。只会处理 hash_state 不满足要求的记录。

    `on_progress(done, total)` 让调用方（比如后台查重任务）能显示
    "校验文件内容 120/430" —— 这一步在大库上最慢，没有进度用户会以为卡死。
    """
    job = job or get_jobs().create("hash")
    todo = []
    for r in rows:
        st = r.get("hash_state") or "none"
        if mode == "quick" and st in ("quick", "full"):
            continue
        if mode == "full" and st == "full":
            continue
        if mode == "full" and (r.get("size") or 0) > limit_mb * 1024 * 1024:
            continue
        if mode == "quick" and (r.get("size") or 0) > limit_mb * 1024 * 1024:
            continue
        todo.append(r)
    job.total = len(todo)
    done = 0
    lock = threading.RLock()
    out = {"hashed": 0, "failed": 0, "skipped": len(list(rows)) - len(todo)}

    def work(r: Dict[str, Any]) -> None:
        nonlocal done
        if job.cancel:
            return
        try:
            if mode == "full":
                h = full_hash(source, r["path"])
                vals = {"hash_full": h, "hash_quick": h, "hash_state": "full"}
            else:
                h = quick_hash(source, r["path"], r.get("size") or 0, probe_kb)
                vals = {"hash_quick": h, "hash_state": "quick"}
            with lock:
                db.update_computed(r["source_id"] or sid, r["path"], **vals)
                out["hashed"] += 1
                done += 1
                job.done = done
                job.message = f"计算哈希 {done}/{job.total}"
                job.touch()
                if on_progress is not None:
                    try:
                        on_progress(done, len(todo))
                    except Exception:
                        pass
        except Exception:
            with lock:
                out["failed"] += 1
                done += 1
                job.done = done
                if on_progress is not None:
                    try:
                        on_progress(done, len(todo))
                    except Exception:
                        pass
        if lock and done % 5 == 0:
            job.current = r.get("name") or ""

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(work, todo))
    # 复用外层任务时**不能**在这里 finish —— 外层（比如查重）还没结束，
    # 提前标完成会让界面显示"已完成"但结果还没出来。
    if job.kind == "hash":
        if getattr(job, "_owned_here", True):
            get_jobs().finish(job, result=out)
    return out
