"""读写音频文件的元数据（标签 / 封面）。

设计要点
--------
* **只走 mutagen 的官方接口**，按扩展名挑具体类（FLAC/ID3/MP4/Vorbis…），
  不用 `mutagen.File()` 的自动嗅探 —— 那个返回的是只读视图，写不回去。
* **原子写**：先写临时文件、重新解析确认没问题、再 `os.replace` 覆盖。
  直接在原文件上 `save()` 一旦中途失败，留下的就是半个损坏文件。
* **备份**：改之前把原始标签存一份 JSON 到应用数据目录，
  界面上可以一键还原 —— 标签写错不像文件删了能进回收站，
  没有备份就只能靠记忆手工敲回来。
* 图片按格式各处安放：ID3 用 APIC 帧，MP4 用 `covr`，
  FLAC/Vorbis/APE 用 `metadata_block_picture`。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import mutagen
    from mutagen.aiff import AIFF
    from mutagen.apev2 import APEv2
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError, PictureType
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
    HAVE_MUTAGEN = True
except Exception:                                   # pragma: no cover
    HAVE_MUTAGEN = False

# 可编辑的字段（键名是界面用的统一名，跟具体格式无关）
FIELDS = ("title", "artist", "album", "albumartist", "year", "genre",
          "track", "disc", "comment")

FIELD_LABELS = {
    "title": "标题", "artist": "作者", "album": "专辑",
    "albumartist": "专辑作者", "year": "年份", "genre": "流派",
    "track": "音轨号", "disc": "碟号", "comment": "备注",
}



def _guarded(fn, what: str):
    """给文件系统调用套超时。

    坏扇区 / 网络盘掉线 / 被独占 都会让 syscall **永久不返回**：
    既不报错也不结束，HTTP 请求就挂在那儿。超时后放弃等待并报错，
    调用方能把这一个文件标成失败、继续处理其他的。
    """
    from .ops import StepTimeout, call_with_timeout
    try:
        return call_with_timeout(fn, what=what)
    except StepTimeout as exc:
        raise IOError(str(exc))


def _backup_dir() -> str:
    # ★ `load_config()` 是 **AppConfig**，它没有 `data_dir` 属性。
    #   原来写 `load_config().data_dir` 会 AttributeError，被下面的
    #   `except Exception` 吞掉 → 整段 fallback 永远走 else 分支，
    #   "让备份跟着配置的数据目录走"这个意图从来没生效过。
    #   正确的东西是 `config.data_dir()` 这个**函数**（返回 Path）。
    from .config import data_dir
    base = ""
    try:
        base = str(data_dir() or "")
    except Exception:
        base = ""
    if not base:
        # 兜底也要走 `data_dir()` —— 自己拼 `~/.music-sync-hub` 会在
        # "MSH_DATA_DIR 被 pop 掉"的空窗期里落到**用户真实数据目录**
        # （config.toml 已经因为这个问题被覆盖过一次）。
        try:
            base = str(data_dir() or "")
        except Exception:
            base = ""
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".music-sync-hub")
    d = os.path.join(base, "tag-backups")
    os.makedirs(d, exist_ok=True)
    return d


def _kind(path: str) -> str:
    """按扩展名判断文件格式。

    `MP4` 能同时处理 m4a 和 mp4 —— 浏览器给的 `audio/mp4` 就是 .m4a，
    这两种都要走 MP4 分支。
    """
    e = os.path.splitext(path)[1].lower()
    # 原子写用的临时名 `xxx.flac.mshtag`：往回退一层看真实扩展名
    if e == ".mshtag":
        e = os.path.splitext(os.path.splitext(path)[0])[1].lower()
    if e == ".flac":
        return "flac"
    if e == ".mp3":
        return "id3"
    # WAV/AIFF 用的是 RIFF/AIFF 容器里挂 ID3 块，**必须**走 mutagen 的
    # WAVE/AIFF 类；直接对它们用 ID3(path).save(path) 会把 RIFF 头覆盖成
    # ID3 头，文件当场损坏（实测踩过这个坑）。
    if e in (".wav", ".wave"):
        return "wav"
    if e in (".aiff", ".aif", ".aifc"):
        return "aiff"
    if e in (".m4a", ".mp4", ".m4b", ".m4v", ".alac", ".aac"):
        return "mp4"
    if e in (".ogg", ".oga"):
        return "vorbis"
    if e == ".opus":
        return "opus"
    # WavPack / TrueAudio 用 APEv2 标签（键值对，没有容器结构风险）。
    # 这两种格式本来就能被扫描、能整理、能播放，但标签编辑原来直接
    # 报"暂不支持" —— 用户在曲库里看到它们、点编辑却撞墙。
    if e in (".wv", ".tta"):
        return "apev2"
    return ""


def can_edit(path: str) -> bool:
    return HAVE_MUTAGEN and bool(_kind(path))


def _open(path: str, kind: str = ""):
    """按格式打开成**可写**的 mutagen 对象。

    `kind` 可以显式指定 —— 原子写用的是 `xxx.flac.mshtag` 这种临时名，
    光看扩展名会认不出来。
    """
    k = kind or _kind(path)
    if not k:
        raise ValueError(f"这种格式暂不支持改标签: {os.path.splitext(path)[1]}")
    if k == "flac":
        return FLAC(path)
    if k == "id3":
        try:
            return ID3(path)
        except ID3NoHeaderError:
            return ID3()
    if k == "wav":
        return WAVE(path)
    if k == "aiff":
        return AIFF(path)
    if k == "mp4":
        return MP4(path)
    if k == "vorbis":
        return OggVorbis(path)
    if k == "opus":
        return OggOpus(path)
    if k == "apev2":
        t = APEv2(path)
        # APEv2 对 .wv 是"内嵌"标签，文件没有标签时构造会抛 —— 补一个空的
        return t
    raise ValueError(f"不支持的格式: {k}")


# ------------------------------------------------------------------ 读

def read_tags(path: str, kind: str = "") -> Dict[str, Any]:
    """读回统一的字段集合（读不到就是空串）。"""
    out: Dict[str, Any] = {k: "" for k in FIELDS}
    out["cover"] = None
    if not HAVE_MUTAGEN:
        out["error"] = "没装 mutagen，无法读写标签"
        return out
    try:
        f = _open(path, kind=kind)
    except Exception as exc:                        # noqa: BLE001
        out["error"] = str(exc)
        return out
    try:
        k = kind or _kind(path)
        if k in ("flac", "vorbis", "opus"):
            g = f.tags or {}
            def first(*names):
                for n in names:
                    v = g.get(n)
                    if v:
                        return str(v[0]) if isinstance(v, list) else str(v)
                return ""
            out["title"] = first("title")
            out["artist"] = first("artist")
            out["album"] = first("album")
            out["albumartist"] = first("albumartist", "album artist")
            out["year"] = first("date", "year")
            out["genre"] = first("genre")
            out["track"] = first("tracknumber", "track")
            out["disc"] = first("discnumber", "disc")
            out["comment"] = first("comment", "description")
            pics = getattr(f, "pictures", None) or []
            out["cover"] = _pic_to_data_url(pics[0].data, pics[0].mime) if pics else None
            out["cover_count"] = len(pics)
        elif k in ("id3", "wav", "aiff"):
            # MP3 和 WAV/AIFF 都是 ID3 帧，读法一样
            # （WAV/AIFF 的 ID3 挂在容器对象的 .tags 上，_open 已经取到了）
            def tag(*names):
                for n in names:
                    t = f.get(n)
                    if t:
                        return str(t.text[0]) if getattr(t, "text", None) else str(t)
                return ""
            out["title"] = tag("TIT2")
            out["artist"] = tag("TPE1")
            out["album"] = tag("TALB")
            out["albumartist"] = tag("TPE2")
            out["year"] = tag("TDRC", "TYER")
            out["genre"] = tag("TCON")
            out["track"] = tag("TRCK")
            out["disc"] = tag("TPOS")
            out["comment"] = tag("COMM::eng", "COMM")
            apics = [v for v in f.values() if isinstance(v, APIC)]
            out["cover"] = _pic_to_data_url(apics[0].data, apics[0].mime) if apics else None
            out["cover_count"] = len(apics)
        elif k == "mp4":
            def m(key):
                v = f.tags.get(key) if f.tags else None
                if not v:
                    return ""
                return str(v[0]) if isinstance(v, list) else str(v)
            out["title"] = m("\xa9nam")
            out["artist"] = m("\xa9ART")
            out["album"] = m("\xa9alb")
            out["albumartist"] = m("aART")
            out["year"] = m("\xa9day")
            out["genre"] = m("\xa9gen")
            tr = (f.tags.get("trkn") if f.tags else None) or []
            out["track"] = str(tr[0][0]) if tr and tr[0] else ""
            dk = (f.tags.get("disk") if f.tags else None) or []
            out["disc"] = str(dk[0][0]) if dk and dk[0] else ""
            out["comment"] = m("\xa9cmt")
            cov = (f.tags.get("covr") if f.tags else None) or []
            out["cover"] = _pic_to_data_url(bytes(cov[0]), None) if cov else None
            out["cover_count"] = len(cov)
        elif k == "apev2":
            # APEv2 是大小写不敏感的键值对；WavPack / TrueAudio 都用它。
            # 键名不统一（有的写 Artist 有的写 ARTIST），所以按小写找。
            g = f.tags if hasattr(f, "tags") and f.tags is not None else f
            low = {}
            try:
                for key in list(g.keys()):
                    v = g[key]
                    low[str(key).lower()] = (
                        str(v[0]) if isinstance(v, list) and v else str(v))
            except Exception:
                low = {}
            out["title"] = low.get("title", "")
            out["artist"] = low.get("artist", "")
            out["album"] = low.get("album", "")
            out["albumartist"] = low.get("album artist",
                                         low.get("albumartist", ""))
            out["year"] = low.get("year", low.get("date", ""))
            out["genre"] = low.get("genre", "")
            out["track"] = low.get("track", "")
            out["disc"] = low.get("disc", "")
            out["comment"] = low.get("comment", "")
            # APEv2 里封面通常存成 "Cover Art (Front)" 二进制项，
            # 值是 `文件名\0图像数据`。
            #
            # ★ 注意取的是 `.value`：`g[key]` 返回的是 `APEBinaryValue` /
            #   `APETextValue` 这类**包装对象**，不是裸 bytes。
            #   直接 `isinstance(raw, bytes)` 永远为假 —— 实测封面明明写在
            #   磁盘上、读回来却是"无"，就是这么漏的。
            def _raw(v):
                if isinstance(v, (bytes, str)):
                    return v
                inner = getattr(v, "value", None)
                if isinstance(inner, (bytes, str)):
                    return inner
                if isinstance(v, list) and v:
                    return _raw(v[0])
                return None

            cover = None
            for key in list(g.keys()):
                if not str(key).lower().startswith("cover art"):
                    continue
                raw = _raw(g[key])
                if isinstance(raw, bytes):
                    cover = raw.split(b"\x00", 1)[1] if b"\x00" in raw else raw
                elif isinstance(raw, str):
                    cover = raw.encode("latin-1", "ignore")
                break
            out["cover"] = _pic_to_data_url(cover, None) if cover else None
            out["cover_count"] = 1 if cover else 0
    except Exception as exc:                        # noqa: BLE001
        out["error"] = f"读取失败: {exc}"
    try:
        info = f.info
        out["duration"] = getattr(info, "length", None)
        out["bitrate"] = getattr(info, "bitrate", None)
        out["codec"] = type(f).__name__
    except Exception:
        pass
    return out


def _pic_to_data_url(data: Optional[bytes], mime: Optional[str]) -> Optional[str]:
    if not data:
        return None
    m = (mime or "").lower()
    if "png" in m:
        mt = "image/png"
    elif "webp" in m:
        mt = "image/webp"
    elif "gif" in m:
        mt = "image/gif"
    else:
        mt = "image/jpeg"
        if not m:
            # 没写 mime，按文件头猜
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                mt = "image/png"
    return f"data:{mt};base64," + base64.b64encode(data).decode("ascii")


# ------------------------------------------------------------------ 写

def write_tags(path: str, changes: Dict[str, Any], *,
               cover_data: Optional[bytes] = None,
               cover_mime: str = "image/jpeg",
               remove_cover: bool = False,
               delete_empty: bool = False,
               make_backup: bool = True) -> Dict[str, Any]:
    """写入标签。返回 {ok, backup, applied}。

    `changes` 只包含**要改的字段**，没提到的保持原样 —— 批量编辑时
    这一点很重要：只想给 20 首歌补个"专辑"，不能把它们的标题抹掉。
    值为空串表示"清空这个字段"。
    """
    if not HAVE_MUTAGEN:
        raise ValueError("没装 mutagen，无法写标签")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    k = _kind(path)
    if not k:
        raise ValueError(f"这种格式暂不支持改标签: {os.path.splitext(path)[1]}")

    backup_name = ""
    if make_backup:
        before = read_tags(path)
        backup_name = _save_backup(path, before)

    tmp = path + ".mshtag"
    shutil.copy2(path, tmp)
    try:
        f = _open(tmp, kind=k)
        _apply(f, k, changes, cover_data, cover_mime, remove_cover, tmp)
        if not isinstance(f, ID3):
            f.save()
        # 保存后重新解析一遍，确认文件没被写坏
        after = read_tags(tmp, kind=k)
        if after.get("error"):
            raise IOError(f"写完后回读失败: {after['error']}")
        if delete_empty and not after.get("title") and not after.get("artist"):
            raise ValueError("写入后标题和作者都是空的，已放弃（避免写坏文件）")
        _guarded(lambda: os.replace(tmp, path), f"写回 {os.path.basename(path)}")
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "backup": backup_name, "applied": sorted(changes.keys())}


def _apply_id3(t, changes: Dict[str, Any],
               cover_data: Optional[bytes], cover_mime: str,
               remove_cover: bool) -> None:
    """往一个 ID3 标签对象里写字段。

    MP3 和 WAV/AIFF 都用 ID3 帧，所以这段逻辑两边共用 ——
    区别只在"谁来 save"：MP3 是 ID3 自己，WAV/AIFF 是外层容器对象。
    """
    def val(key: str) -> Optional[str]:
        if key not in changes:
            return None
        v = changes[key]
        return "" if v is None else str(v).strip()

    from mutagen.id3 import (APIC as _APIC, COMM, TALB, TCON, TDRC, TIT2,
                             TPE1, TPE2, TPOS, TRCK)
    mapping = {
        "title": ("TIT2", TIT2), "artist": ("TPE1", TPE1),
        "album": ("TALB", TALB), "albumartist": ("TPE2", TPE2),
        "year": ("TDRC", TDRC), "genre": ("TCON", TCON),
        "track": ("TRCK", TRCK), "disc": ("TPOS", TPOS),
    }
    for key, (frame, cls) in mapping.items():
        v = val(key)
        if v is None:
            continue
        if v:
            t.setall(frame, [cls(encoding=3, text=[v])])
        elif frame in t:
            del t[frame]
    cv = val("comment")
    if cv is not None:
        t.delall("COMM")
        if cv:
            t.add(COMM(encoding=3, lang="eng", desc="", text=[cv]))
    if remove_cover:
        t.delall("APIC")
    elif cover_data:
        t.delall("APIC")
        t.add(_APIC(encoding=3, mime=cover_mime or "image/jpeg",
                    type=3, desc="Cover", data=cover_data))


def _apply(f, kind: str, changes: Dict[str, Any],
           cover_data: Optional[bytes], cover_mime: str,
           remove_cover: bool, path: str) -> None:
    """把改动写进已经打开的 mutagen 对象。

    ID3 是特例：它需要知道"往哪个文件保存"，所以由调用方把 `path` 传进来
    （以前用过一个模块级变量捎带路径，那种隐式状态很容易出问题）。
    """
    def val(key: str) -> Optional[str]:
        if key not in changes:
            return None
        v = changes[key]
        return "" if v is None else str(v).strip()

    if kind in ("flac", "vorbis", "opus"):
        if f.tags is None:
            try:
                f.add_tags()
            except Exception:
                pass
        g = f.tags
        mapping = {
            "title": ["title"], "artist": ["artist"], "album": ["album"],
            "albumartist": ["albumartist"],
            "year": ["date"], "genre": ["genre"],
            "track": ["tracknumber"], "disc": ["discnumber"],
            "comment": ["comment"],
        }
        for key, names in mapping.items():
            v = val(key)
            if v is None:
                continue
            for n in names:
                if n in g:
                    del g[n]
            if v:
                g[names[0]] = [v]
        if kind == "flac":
            if remove_cover:
                f.clear_pictures()
            elif cover_data:
                pic = Picture()
                pic.type = 3                       # front cover
                pic.mime = cover_mime or "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_data
                f.clear_pictures()
                f.add_picture(pic)
        else:
            if remove_cover:
                for key in ("metadata_block_picture", "coverart", "coverartmime"):
                    if key in g:
                        del g[key]
            elif cover_data:
                pic = Picture()
                pic.type = 3
                pic.mime = cover_mime or "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_data
                g["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")]

    if kind in ("wav", "aiff"):
        # RIFF/AIFF 容器里挂 ID3 块：走 WAVE/AIFF 的 tags（是个 ID3 对象），
        # 保存时必须让**容器对象**自己 save()，不能拿内层 ID3 去 save 路径。
        if getattr(f, "tags", None) is None:
            try:
                f.add_tags()
            except Exception:
                pass
        t = f.tags
        if t is None:
            raise ValueError("这个文件加不上标签")
        _apply_id3(t, changes, cover_data, cover_mime, remove_cover)
        # （保存统一由 write_tags 外层的 `if not isinstance(f, ID3)` 收口）
        return

    if kind == "id3":
        # ID3 对象本身就能直接塞帧，不需要 add_tags()（那是 FLAC/Vorbis 的接口）；
        # 文件原本没有 ID3 标签时，`ID3(path)` 会抛 ID3NoHeaderError，
        # `_open()` 已经把它兜成一个空 ID3 了。
        if not isinstance(f, ID3):
            raise ValueError("内部错误：ID3 分支收到非 ID3 对象")
        _apply_id3(f, changes, cover_data, cover_mime, remove_cover)
        f.save(path)
        return

    if kind == "apev2":
        # APEv2：纯键值对，没有容器结构可以写坏（这也是它安全的原因）。
        # 键名统一用首字母大写的规范写法，读取时按小写匹配。
        g = f.tags if hasattr(f, "tags") and f.tags is not None else f
        mapping = {
            "title": "Title", "artist": "Artist", "album": "Album",
            "albumartist": "Album Artist", "year": "Year", "genre": "Genre",
            "track": "Track", "disc": "Disc", "comment": "Comment",
        }
        for key, name in mapping.items():
            v = val(key)
            if v is None:
                continue
            # 老键名可能有不同大小写写法，先清干净再写
            for old in [k for k in list(g.keys())
                        if str(k).lower() == name.lower()]:
                if old != name:
                    try:
                        del g[old]
                    except Exception:
                        pass
            if v:
                g[name] = v
            elif name in g:
                del g[name]
        # 封面：APEv2 约定俗成用 "Cover Art (Front)"，值是 `文件名\0二进制`
        if remove_cover:
            for k in [x for x in list(g.keys())
                      if str(x).lower().startswith("cover art")]:
                del g[k]
        elif cover_data:
            for k in [x for x in list(g.keys())
                      if str(x).lower().startswith("cover art")]:
                del g[k]
            fname = "cover.jpg" if "png" not in (cover_mime or "") else "cover.png"
            g["Cover Art (Front)"] = fname.encode("utf-8") + b"\x00" + cover_data
        # （保存统一由 write_tags 外层的 `if not isinstance(f, ID3)` 收口）
        return

    if kind == "mp4":
        if f.tags is None:
            f.add_tags()
        t = f.tags
        mapping = {
            "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
            "albumartist": "aART", "year": "\xa9day", "genre": "\xa9gen",
            "comment": "\xa9cmt",
        }
        for key, atom in mapping.items():
            v = val(key)
            if v is None:
                continue
            if v:
                t[atom] = [v]
            elif atom in t:
                del t[atom]
        for key, atom in (("track", "trkn"), ("disc", "disk")):
            v = val(key)
            if v is None:
                continue
            if v:
                try:
                    n = int(str(v).split("/")[0])
                except ValueError:
                    n = 0
                tot = 0
                if "/" in str(v):
                    try:
                        tot = int(str(v).split("/")[1])
                    except ValueError:
                        tot = 0
                t[atom] = [(n, tot, 0, 0)]
            elif atom in t:
                del t[atom]
        if remove_cover:
            if "covr" in t:
                del t["covr"]
        elif cover_data:
            fmt = (MP4Cover.FORMAT_PNG if (cover_mime or "").lower().find("png") >= 0
                   else MP4Cover.FORMAT_JPEG)
            t["covr"] = [MP4Cover(cover_data, imageformat=fmt)]


def _save_backup(path: str, tags: Dict[str, Any]) -> str:
    """把原始标签存成 JSON，便于一键还原。

    标签写错不像删文件能进回收站 —— 没有备份就只能靠记忆手工敲回来，
    所以每次写入前都留一份。

    ★ 文件名必须**逐文件唯一**。
    踩过：原来用 `base64(path)[:48]`，而 base64 是按 3 字节一组编码的，
    48 个字符只覆盖完整路径的前 **36 字节** —— 对专辑目录里的歌来说，
    这 36 字节正好就是**目录前缀**。于是同一秒内批量写一张专辑的标签，
    30 首歌算出同一个名字，`open(..., "w")` 直接截断前一份：
    实测 8 个文件只剩 **1 份**备份（丢 7 份）。
    而"批量给一张专辑改标签"恰恰是最常见的用法 ——
    等用户写坏了想还原时，另外 29 首根本没有备份。

    现在用整条路径的 sha1 摘要（定长、无前缀重叠），并保留时间戳，
    仍然撞名（同一文件同一秒写两次）时再加序号，绝不覆盖已有备份。
    """
    try:
        h = hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        d = _backup_dir()
        os.makedirs(d, exist_ok=True)
        name = f"{stamp}-{h}.json"
        # 同一文件在同一秒被写两次（例如"写入 → 撤销 → 再写入"）：
        # 加序号，保住前一份，而不是把它截断掉。
        n = 1
        while os.path.exists(os.path.join(d, name)):
            n += 1
            name = f"{stamp}-{h}-{n}.json"
        payload = {
            "path": path, "when": time.time(),
            "tags": {k: v for k, v in tags.items() if k != "cover"},
            "cover": tags.get("cover"),
            "size": os.path.getsize(path) if os.path.isfile(path) else 0,
        }
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return name
    except Exception:
        return ""


def backup_target(name: str) -> str:
    """取某个备份要还原到哪个文件（**先查再写**，用来做只读门禁）。

    `restore_backup` 会直接把标签写回原文件；调用方需要在写之前知道目标路径，
    才能判断它属于哪个数据源、是不是只读。
    """
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("备份名不合法")
    p = os.path.join(_backup_dir(), name)
    if not os.path.isfile(p):
        raise FileNotFoundError(f"备份不存在: {name}")
    with open(p, encoding="utf-8") as f:
        return str((json.load(f) or {}).get("path") or "")


def restore_backup(name: str) -> Dict[str, Any]:
    """按备份还原标签。"""
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("备份名不合法")
    p = os.path.join(_backup_dir(), name)
    if not os.path.isfile(p):
        raise FileNotFoundError(f"备份不存在: {name}")
    with open(p, encoding="utf-8") as f:
        payload = json.load(f)
    target = payload.get("path") or ""
    if not os.path.isfile(target):
        raise FileNotFoundError(f"原文件已不在: {target}")
    tags = payload.get("tags") or {}
    changes = {k: tags.get(k, "") for k in FIELDS}
    cover = None
    if str(payload.get("cover") or "").startswith("data:"):
        try:
            cover = base64.b64decode(payload["cover"].split(",", 1)[1])
        except Exception:
            cover = None
    res = write_tags(target, changes, cover_data=cover,
                     remove_cover=not cover, make_backup=False)
    return {**res, "path": target}


def list_backups(limit: int = 100) -> List[Dict[str, Any]]:
    d = _backup_dir()
    out = []
    for n in sorted(os.listdir(d), reverse=True)[:limit]:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                p = json.load(f)
            out.append({"name": n, "path": p.get("path"), "when": p.get("when"),
                        "tags": {k: v for k, v in (p.get("tags") or {}).items() if v}})
        except Exception:
            continue
    return out

