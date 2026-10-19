"""音频元数据解析与封面处理。

* 用 mutagen 读取 MP3/FLAC/M4A/OGG/Opus/WAV/AIFF/WMA/DSF 等标签；
* 对大文件只读取头部若干字节，避免把整个文件拉过来；
* 标签缺失时从文件名推断（`01 - 周杰伦 - 晴天.mp3`）；
* 封面：优先内嵌图，其次同目录 cover/folder.jpg；统一缩放成 JPEG 缩略图存 SQLite。
"""
from __future__ import annotations

import hashlib
import io
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import paths as P

try:
    import mutagen
    from mutagen import File as MutagenFile

    _HAS_MUTAGEN = True
except Exception:  # pragma: no cover
    mutagen = None  # type: ignore
    MutagenFile = None  # type: ignore
    _HAS_MUTAGEN = False

try:
    from PIL import Image

    _HAS_PIL = True
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    _HAS_PIL = False


HEAD_BYTES_DEFAULT = 2 * 1024 * 1024      # 解析标签时最多先读这么多
HEAD_BYTES_LOSSLESS = 3 * 1024 * 1024
FULL_READ_LIMIT = 12 * 1024 * 1024        # 小文件直接整读

# 封面索引：source_id -> (扫描时间, {父目录: {小写主名: (path, size, mtime)}})
_cover_index: Dict[str, Tuple[float, Dict[str, Dict[str, Tuple[str, int, float]]]]] = {}
_cover_lock = threading.RLock()


@dataclass
class AudioMeta:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_no: str = ""
    year: str = ""
    genre: str = ""
    duration: Optional[float] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    bits_per_sample: Optional[int] = None
    codec: str = ""
    is_lossless: bool = False
    cover_data: Optional[bytes] = None
    cover_mime: str = ""
    status: str = "ok"
    error: str = ""

    def to_row(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("cover_data", None)
        d.pop("cover_mime", None)
        return d


def parse_filename(name: str) -> Dict[str, str]:
    """从文件名兜底推断 艺术家 / 标题 / 音轨号。"""
    s = P.stem(name).strip()
    s = re.sub(r"^\s*(?:cd\s*\d+\s*[-_ ]?)?(\d{1,3})\s*[-_.、 ]\s*", "", s, flags=re.I)
    out = {"track_no": "", "artist": "", "title": s}
    m = re.match(r"^(\d{1,3})\s*[-_.、]\s*(.+)$", P.stem(name).strip())
    if m:
        out["track_no"] = m.group(1).lstrip("0") or "0"
    # ★ 裸 `-` / `_` 不能当分隔符：`T-Square.flac`、`Jay-Z.flac` 会被切成
    #   艺术家 T / Jay。音乐工业里无空格的连字符属于词素连接。
    for sep in (" - ", " – ", " — ", " _ "):
        if sep in s:
            left, _, right = s.partition(sep)
            left, right = left.strip(), right.strip()
            if left and right:
                out["artist"] = left
                out["title"] = right
                break
    return out


def _tag(tags, *names: str) -> str:
    if not tags:
        return ""
    for n in names:
        try:
            v = tags.get(n)
        except Exception:
            v = None
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            if not v:
                continue
            v = v[0]
        s = str(v).strip()
        if s:
            return s
    return ""


def _num(v: Any) -> Optional[float]:
    try:
        if isinstance(v, (list, tuple)):
            v = v[0]
        if v is None:
            return None
        f = float(str(v).split("/")[0])
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def parse_bytes(data: bytes, filename: str, head_only: bool = False) -> AudioMeta:
    """解析内存中的音频数据。`head_only` 表示数据只是文件头部。"""
    meta = AudioMeta()
    if not _HAS_MUTAGEN:
        meta.status = "error"
        meta.error = "缺少 mutagen 依赖"
        return _fallback(meta, filename)

    try:
        f = MutagenFile(io.BytesIO(data), filename=filename)
    except Exception as exc:
        meta.status = "error"
        meta.error = f"解析失败: {type(exc).__name__}: {exc}"
        return _fallback(meta, filename)

    if f is None:
        meta.status = "partial"
        meta.error = "无法识别的音频格式（可能文件损坏或被截断）"
        return _fallback(meta, filename)

    info = getattr(f, "info", None)
    if info is not None:
        meta.duration = _num(getattr(info, "length", None))
        br = getattr(info, "bitrate", None)
        meta.bitrate = int(br) if br else None
        sr = getattr(info, "sample_rate", None)
        meta.sample_rate = int(sr) if sr else None
        ch = getattr(info, "channels", None)
        meta.channels = int(ch) if ch else None
        bps = getattr(info, "bits_per_sample", None)
        if bps:
            meta.bits_per_sample = int(bps)
    meta.codec = type(f).__name__.replace("Easy", "").replace("Info", "") or P.ext(filename).lstrip(".").upper()

    tags = None
    try:
        tags = getattr(f, "tags", None)
    except Exception:
        tags = None
        meta.status = "partial"
        meta.error = "标签读取异常"

    if tags:
        meta.title = _tag(tags, "TIT2", "title", "\xa9nam", "Title")
        meta.artist = _tag(tags, "TPE1", "artist", "\xa9ART", "Artist")
        meta.album = _tag(tags, "TALB", "album", "\xa9alb", "Album")
        meta.album_artist = _tag(tags, "TPE2", "albumartist", "album_artist", "aART", "AlbumArtist")
        meta.track_no = _tag(tags, "TRCK", "tracknumber", "trkn", "TrackNumber")
        meta.year = _tag(tags, "TDRC", "date", "year", "\xa9day", "originaldate", "Year")
        meta.genre = _tag(tags, "TCON", "genre", "\xa9gen", "Genre")

    # 封面
    try:
        cover = _extract_cover(f, head_only)
        if cover:
            meta.cover_data, meta.cover_mime = cover
    except Exception:
        pass

    meta.is_lossless = _infer_lossless(f, filename, meta)
    return _fallback(meta, filename)


def _fallback(meta: AudioMeta, filename: str) -> AudioMeta:
    """标签为空时用文件名补全 title/artist。"""
    inferred = parse_filename(filename)
    if not meta.title:
        meta.title = inferred["title"] or P.stem(filename)
    if not meta.artist:
        meta.artist = inferred["artist"]
    if not meta.track_no:
        meta.track_no = inferred["track_no"]
    if not meta.album:
        meta.album = P.basename(P.parent(filename))
    if not meta.codec:
        meta.codec = P.ext(filename).lstrip(".").upper()
    return meta


def _infer_lossless(f: Any, filename: str, meta: AudioMeta) -> bool:
    name = type(f).__name__.lower()
    if any(k in name for k in ("flac", "aiff", "wav", "ape", "wavpack", "dsf", "dff", "tta", "monkeysaudio")):
        return True
    if "mp4" in name or "alac" in name:
        # M4A 可能是 ALAC 也可能是 AAC
        try:
            if getattr(f, "info", None) is not None and getattr(f.info, "codec", "") == "alac":
                return True
        except Exception:
            pass
        return "alac" in str(getattr(f, "info", "")).lower()
    if P.ext(filename) in P.LOSSLESS_EXTS:
        return True
    if meta.codec.upper().startswith(("FLAC", "ALAC", "PCM", "WAV", "AIFF")):
        return True
    return False


def _extract_cover(f: Any, head_only: bool) -> Optional[Tuple[bytes, str]]:
    cls = type(f).__name__.lower()
    if "flac" in cls or "ogg" in cls or "opus" in cls:
        pics = getattr(f, "pictures", None) or []
        if pics:
            p = pics[0]
            return bytes(p.data), (p.mime or "image/jpeg")
    if "mp4" in cls:
        covr = (f.tags or {}).get("covr") if f.tags else None
        if covr:
            item = covr[0]
            fmt = getattr(item, "imageformat", None)
            mime = "image/png" if fmt == 14 else "image/jpeg"
            return bytes(item), mime
    if "id3" in cls or "mp3" in cls or "aiff" in cls or "wav" in cls or "ape" in cls:
        tags = getattr(f, "tags", None)
        if tags:
            apics = tags.getall("APIC") if hasattr(tags, "getall") else []
            if apics:
                ap = apics[0]
                return bytes(ap.data), (ap.mime or "image/jpeg")
    if "asf" in cls:
        tags = getattr(f, "tags", None)
        if tags and "WM/Picture" in tags:
            pic = tags["WM/Picture"][0]
            return bytes(pic.value), "image/jpeg"
    return None


# ------------------------------------------------------------------ 封面缩略

def make_thumbnail(data: bytes, max_size: int = 320) -> Tuple[bytes, str, int, int]:
    """生成缩略图。没有 Pillow 时原样返回。"""
    if not _HAS_PIL:
        return data, "image/jpeg", 0, 0
    try:
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_size:
            scale = max_size / float(max(w, h))
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=82, optimize=True)
        return out.getvalue(), "image/jpeg", im.size[0], im.size[1]
    except Exception:
        return data, "image/jpeg", 0, 0


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# ------------------------------------------------------------------ 目录封面索引

def register_cover_files(source_id: str, covers: List[Tuple[str, str, int, float]]) -> None:
    """把一次扫描中发现的封面文件登记进索引。

    covers: [(path, name, size, mtime)]
    """
    with _cover_lock:
        _, idx = _cover_index.get(source_id, (0.0, {}))
        for path, name, size, mtime in covers:
            parent = P.parent(path)
            idx.setdefault(parent, {})[P.stem(name).lower()] = (path, size, mtime)
        _cover_index[source_id] = (__import__("time").time(), idx)


def cover_index(source_id: str) -> Dict[str, Dict[str, Tuple[str, int, float]]]:
    with _cover_lock:
        return _cover_index.get(source_id, (0.0, {}))[1]


def clear_cover_index(source_id: Optional[str] = None) -> None:
    with _cover_lock:
        if source_id:
            _cover_index.pop(source_id, None)
        else:
            _cover_index.clear()


_COVER_PREFS = ("cover", "folder", "front", "album", "albumart", "artwork", "thumb")
_COVER_UP_LEVELS = 2


def find_cover_file(source_id: str, file_path: str, up_levels: int = _COVER_UP_LEVELS) -> Optional[str]:
    """为某个音频文件找封面图。

    查找顺序：
      1. 同目录的 cover.jpg / folder.jpg（最常见）
      2. 逐级向上最多 `up_levels` 层（很多库把封面放在专辑父目录）
      3. 库根目录下的 covers/<哈希名>.jpg（自建封面库的常见组织方式）
    找不到就返回 None，界面显示占位图标。
    """
    idx_all = cover_index(source_id)
    if not idx_all:
        return None

    def best_in(folder: str) -> Optional[str]:
        idx = idx_all.get(folder)
        if not idx:
            return None
        for pref in _COVER_PREFS:
            if pref in idx:
                return idx[pref][0]
        return next(iter(idx.values()))[0]

    cur = P.parent(file_path)
    for _ in range(max(0, up_levels) + 1):
        if not cur:
            break
        hit = best_in(cur)
        if hit:
            return hit
        parent = P.parent(cur)
        if parent == cur:
            break
        cur = parent
    return None


# ------------------------------------------------------------------ 质量评分

_LOSSLESS_BONUS = {
    "FLAC": 30, "ALAC": 30, "APE": 28, "WAV": 20, "AIFF": 20, "WV": 26,
    "DSF": 26, "DFF": 26, "TTA": 24, "PCM": 22,
}


def quality_score(rec: Dict[str, Any]) -> float:
    """给"哪份更好"打分。分数越高越好。

    维度：是否无损 > 码率 > 采样率 > 位深 > 时长完整度。
    """
    score = 0.0
    bitrate = rec.get("bitrate") or 0
    lossless = bool(rec.get("is_lossless"))
    codec = (rec.get("codec") or "").upper()
    if lossless:
        score += 100.0
        score += _LOSSLESS_BONUS.get(codec, 15)
    else:
        # 有损：按码率线性给分（128k→12分，320k→32分）
        score += min(40.0, (bitrate or 128000) / 10000.0)
    sr = rec.get("sample_rate") or 0
    if sr:
        if sr >= 352800:
            score += 12
        elif sr >= 176400:
            score += 9
        elif sr >= 96000:
            score += 6
        elif sr >= 48000:
            score += 3
    bps = rec.get("bits_per_sample") or 0
    if bps >= 24:
        score += 4
    elif bps >= 16:
        score += 2
    dur = rec.get("duration") or 0
    if dur:
        score += min(6.0, dur / 60.0)
    return round(score, 3)


def quality_label(rec: Dict[str, Any]) -> str:
    parts: List[str] = []
    if rec.get("is_lossless"):
        parts.append("无损")
    codec = (rec.get("codec") or "").upper()
    if codec:
        parts.append(codec)
    if rec.get("bitrate"):
        parts.append(f"{int(rec['bitrate'] / 1000)}kbps")
    if rec.get("sample_rate"):
        parts.append(f"{rec['sample_rate'] / 1000:g}kHz")
    if rec.get("bits_per_sample"):
        parts.append(f"{rec['bits_per_sample']}bit")
    return " · ".join(parts)
