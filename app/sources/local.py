"""本地文件系统数据源（Windows 盘符 / Linux 挂载点 / UNC 路径）。"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from .base import Entry, MusicSource, SourceError, register
from ..core import paths as P

_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(?:/(.*))?$")

# 回收站里的「找空位 + 搬进去」必须原子完成：
# 多个 worker 在同一秒删同名文件（多张专辑的 `01 Track.mp3`）时，
# 会抢同一个目标名，后一个直接把前一个覆盖掉 —— 那份文件就永久没了。
_trash_lock = threading.Lock()


def _msys_drive(p: str) -> str:
    """把 MSYS/Git-Bash 风格路径 `/d/Music` 还原成 `D:/Music`。

    只在该盘符真的存在时才转换；因此 `/mnt/music`、`/Users/xx` 这类
    真正的 POSIX 路径（远端库、Mac 路径）不会被误改。
    """
    p = P.norm(p)
    m = _MSYS_DRIVE.match(p or "")
    if not m:
        return p
    drive = f"{m.group(1).upper()}:"
    if not os.path.exists(drive + "\\"):
        return p
    rest = m.group(2) or ""
    return f"{drive}/{rest}" if rest else drive


_FILE_ATTR_REPARSE = 0x400        # FILE_ATTRIBUTE_REPARSE_POINT


def _is_reparse(st) -> bool:
    """这个 stat 是不是 Windows 的 reparse point（junction / 符号链接）。

    ★ `os.path.islink()` 对 **junction 返回 False**，但 junction 会把读写
      带到完全不同的目录 —— 库里的一个 junction 就能让扫描/移动/删除
      全部跑到源根之外。必须单独判。
    """
    attrs = getattr(st, "st_file_attributes", 0) or 0
    return bool(attrs & _FILE_ATTR_REPARSE)


@register
class LocalSource(MusicSource):
    kind = "local"
    label = "本地 / 已挂载盘符"
    hint = ("Windows: D:\\Music　　Linux: /mnt/music　　U 盘 / 移动硬盘 / 网络盘映射（Y:）都算本地")
    experimental = False

    def __init__(self, cfg, password: str = ""):
        super().__init__(cfg, password)
        # root 也要做同样的盘符归一化，否则 `/d/Music` 这种写法下
        # rel_path 会剥不掉前缀、返回整条绝对路径。
        if os.name == "nt":
            self.root = _msys_drive(self.root)
        # 设备探测结果缓存。`detect()` 在 measure=False 时只是几次 Win32
        # 调用，但仍然会被每次计划/执行问到，缓存一下免得反复问系统。
        self._dev_cache: Optional[Dict[str, Any]] = None

    def device_info(self, measure: bool = False) -> Dict[str, Any]:
        """本地盘的真实性质（可移除 / 网络 / 慢速）。

        结果会缓存 —— 盘不会在执行过程中换性质（真换了就该重新扫描）。
        """
        if self._dev_cache is not None and not measure:
            return dict(self._dev_cache)
        try:
            from ..core import device as DEV
            info = DEV.detect(self.root, measure=measure)
            out = info.to_dict()
        except Exception:
            out = {"kind": "unknown", "removable": False, "network": False,
                   "slow": bool((self.cfg.extras or {}).get("slow_disk")),
                   "read_mbps": None, "label": "", "notes": []}
        # 用户手动勾的"慢速盘"永远优先 —— 他比系统更清楚那块盘是什么
        if (self.cfg.extras or {}).get("slow_disk"):
            out["slow"] = True
            notes = list(out.get("notes") or [])
            notes.append("已手动标记为慢速盘")
            out["notes"] = notes
        if not measure:
            self._dev_cache = dict(out)
        return out

    @property
    def is_removable(self) -> bool:
        try:
            return bool(self.device_info().get("removable"))
        except Exception:
            return False

    def abs_path(self, path: str) -> str:
        p = P.norm(path)
        if not p:
            return P.norm(self.root)
        r = P.norm(self.root)
        if r and (p == r or p.startswith(r.rstrip("/") + "/")):
            return p        # Windows 上形如 D:/Music/song.mp3 的路径本身就是绝对路径，
        # 不能再拼到 root 前面（否则会得到 C:/root/D:/Music/song.mp3）。
        if os.name == "nt" and P.is_windows_abs(p):
            return p
        # 其余带前导斜杠的路径在 Windows 上按"相对 root"处理
        # （远端 POSIX 路径会在落盘前由 _native 做映射）
        return P.join(r, p.lstrip("/")) if r else p

    def _native(self, path: str) -> str:
        p = self.abs_path(path)
        # ★ 路径里带 NUL 字节 → 直接拒。
        #   `os.scandir`/`os.stat` 遇到内嵌 NUL 会抛 **ValueError**（不是 OSError），
        #   而调用方普遍只 `except OSError` —— 于是它一路冒到框架层变成
        #   HTTP 500。实测 `GET /api/files/list?path=%00` 就是这样。
        #   这是**请求参数不合法**，该报 400 而不是"服务端出错了"。
        #   放在 `_native` 是因为所有落盘操作都经过它，一处收口。
        if "\x00" in p:
            raise SourceError("路径里不允许包含空字符（NUL）")
        if os.name == "nt":
            # 兼容文档里推荐的 MSYS/Git-Bash 写法 `/d/Music` → `D:\Music`
            out = _msys_drive(p).replace("/", "\\")
            # ★ 裸盘符 `Y:` 在 Windows 上是「**盘符相对路径**」
            #   （相对 Y: 盘自己的当前目录），**不是**根目录。
            #   于是 `os.path.join("Y:", "x.mp3")` 给出 `Y:x.mp3` —— 少一个分隔符。
            #   索引里的 path / rel_path 因此变成 `Y:节奏医生/…`，而
            #   `rel_path()`、`inside_root()`、目录树的相对路径都按 `Y:/…` 比较：
            #     · rel_path 带着盘符 → 本库与其他库的键格式不一致
            #     · inside_root 永远 False → 一旦该源可写，所有写操作被判越界
            #   界面之所以"看着正常"，是因为 Windows 恰好把裸盘符解析成根目录。
            #   补上分隔符，让盘符根变成真正的根。
            if len(out) == 2 and out[1] == ":":
                out += "\\"
            return out
        return p

    def stat(self, path: str) -> Optional[Entry]:
        n = self._native(path)
        try:
            st = os.stat(n)
        except (FileNotFoundError, NotADirectoryError, OSError):
            return None
        return Entry(
            path=P.norm(path) if path else P.norm(n),
            name=os.path.basename(n),
            is_dir=os.path.isdir(n),
            size=st.st_size,
            mtime=st.st_mtime,
            etag=f"{int(st.st_mtime)}-{st.st_size}",
        )

    def listdir(self, path: str) -> List[Entry]:
        n = self._native(path)
        out: List[Entry] = []
        try:
            with os.scandir(n) as it:
                for d in it:
                    try:
                        st = d.stat(follow_symlinks=self.cfg.follow_symlinks)
                    except OSError:
                        continue
                    if d.is_symlink() and not self.cfg.follow_symlinks:
                        continue
                    # ★ junction 不是 symlink，但一样会把路径带出源根
                    if not self.cfg.follow_symlinks and _is_reparse(st):
                        continue
                    is_dir = d.is_dir(follow_symlinks=self.cfg.follow_symlinks)
                    out.append(Entry(
                        path=P.norm(d.path),
                        name=d.name,
                        is_dir=is_dir,
                        size=0 if is_dir else st.st_size,
                        mtime=st.st_mtime,
                        etag=f"{int(st.st_mtime)}-{st.st_size}",
                    ))
        except (FileNotFoundError, NotADirectoryError):
            raise SourceError(f"目录不存在或不是目录: {n}")
        except PermissionError as e:
            raise SourceError(f"没有权限读取: {n} ({e})")
        return out

    # ---------------------------------------------------------- 读取
    def append_file(self, path: str, data, progress: Optional[Callable[[int], None]] = None) -> None:
        """把数据**追加**到文件末尾（断点续传用）。"""
        self._guard_write()
        n = self._native(path)
        os.makedirs(os.path.dirname(n) or ".", exist_ok=True)
        written = 0
        with open(n, "ab") as fh:
            if isinstance(data, (bytes, bytearray, memoryview)):
                fh.write(bytes(data))
                written = len(data)
                if progress:
                    progress(written)
            else:
                while True:
                    chunk = data.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written)

    def open_read(self, path: str, offset: int = 0, length: int = 0):
        n = self._native(path)
        fh = open(n, "rb")
        if offset:
            fh.seek(offset)
        if length:
            return _BoundedReader(fh, length)
        return fh

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        n = self._native(path)
        with open(n, "rb") as fh:
            if offset:
                fh.seek(offset)
            return fh.read(length) if length else fh.read()

    # ---------------------------------------------------------- 写入
    def mkdirs(self, path: str) -> None:
        self._guard_write()
        os.makedirs(self._native(path), exist_ok=True)

    def write_file(self, path: str, data, progress: Optional[Callable[[int], None]] = None,
                   expected_size: Optional[int] = None) -> None:
        self._guard_write()
        n = self._native(path)
        os.makedirs(os.path.dirname(n) or ".", exist_ok=True)
        written = 0
        with open(n, "wb") as fh:
            if isinstance(data, (bytes, bytearray, memoryview)):
                fh.write(bytes(data))
                written = len(data)
                if progress:
                    progress(written)
            else:
                while True:
                    chunk = data.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written)
        return None

    def replace(self, src_path: str, dst_path: str) -> None:
        os.replace(self._native(src_path), self._native(dst_path))

    def same_entry(self, a: str, b: str) -> bool:
        """两条路径是不是同一个物理条目（大小写不敏感文件系统上的"只改大小写"）。"""
        na, nb = self._native(a), self._native(b)
        if na == nb:
            return False                     # 完全相同不算"改名"，交给调用方的 cur==want
        try:
            return os.path.samefile(na, nb)
        except OSError:
            return False

    def move(self, src_path: str, dst_path: str) -> None:
        """同源内移动：优先用原子改名，跨盘（EXDEV）才退化成"复制→校验→删源"。

        底线是源文件绝不先删：只有目标确认落盘、体积一致之后才动源文件；
        中途任何失败都清掉半截文件并抛错，源文件保持原样。
        """
        self._guard_write()
        src, dst = self._native(src_path), self._native(dst_path)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            # 只有"跨设备链接"才需要退化成复制；其它错误（权限、占用）原样抛出，
            # 让上层报错，而不是绕道去复制出一个可能损坏的副本。
            if exc.errno != errno.EXDEV:
                raise
        size_before = os.path.getsize(src)
        tmp = dst + ".mshpart"
        try:
            shutil.copyfile(src, tmp)
            size_after = os.path.getsize(tmp)
            if size_before != size_after:
                raise SourceError(
                    f"跨盘移动校验失败：源 {size_before} 字节，目标 {size_after} 字节")
            os.replace(tmp, dst)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass
        os.remove(src)

    def remove(self, path: str, hard: bool = False) -> str:
        self._guard_write()
        n = self._native(path)
        if hard:
            os.remove(n)
            return ""
        trash = self._trash_dir(path)
        os.makedirs(self._native(trash), exist_ok=True)
        # ★ 同名去重必须**循环**，而且要在锁里完成「找空位 → 搬进去」。
        #
        #   原来只试一次：磁盘上已有 `x.mp3` 就改用 `{时间戳}-x.mp3`，
        #   但**同一秒里删三个同名文件**时，第 2 和第 3 个会算出同一个
        #   时间戳名字，`shutil.move` 直接把前一个覆盖掉 ——
        #   实测三份内容 b'AAAA'/b'BBBB'/b'CCCC'，最后回收站里只剩
        #   两份，`BBBB` 永久消失，而三次调用**全部返回成功**。
        #
        #   这和"绝不弄丢文件"直接冲突，所以：
        #     · 循环找空位（同名很多时也能找到）
        #     · 整段加锁，避免多 worker 并发删除时抢同一个名字
        base = os.path.basename(n)
        stem_, ext_ = os.path.splitext(base)
        stamp = int(time.time())
        with _trash_lock:
            target = P.join(trash, base)
            if os.path.exists(self._native(target)):
                for i in range(1, 10000):
                    tag = f"{stamp}-{stem_}" + ("" if i == 1 else f"-{i}") + ext_
                    cand = P.join(trash, tag)
                    if not os.path.exists(self._native(cand)):
                        target = cand
                        break
                else:
                    raise SourceError(
                        f"回收站里同名文件过多，无法为 {base} 找到空位")
            shutil.move(n, self._native(target))
            # 记下"它原来在哪"，否则回收站里的文件无法还原到原位
            self._record_origin(trash, os.path.basename(target), self.rel_path(path))
        return target

    def remove_dir(self, path: str) -> None:
        """永久删除整个目录（谨慎：不走回收站）。界面上的"删除文件夹"不走这里。"""
        self._guard_write()
        n = self._native(path)
        if os.path.isdir(n):
            shutil.rmtree(n, ignore_errors=True)

    # ---------------------------------------------------------- 目录级操作
    def dir_stat(self, path: str) -> Dict[str, int]:
        """统计一个目录下有多少文件/子目录/总字节。用于删除前的确认提示。"""
        n = self._native(path)
        out = {"files": 0, "dirs": 0, "bytes": 0, "audio": 0}
        if not os.path.isdir(n):
            return out
        for root, dirs, files in os.walk(n):
            out["dirs"] += len(dirs)
            for f in files:
                out["files"] += 1
                if self.kind == "local" and P.is_audio(f):
                    out["audio"] += 1
                try:
                    out["bytes"] += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return out

    def trash_dir(self, path: str, stamp: str = "") -> str:
        """把整个目录移进回收站（可恢复），返回它被移到了哪里。

        这是"删除文件夹"的实现方式：与删单个文件一致，先移入回收站，
        目录名带上时间戳，重名也不会互相覆盖。
        """
        self._guard_write()
        n = self._native(path)
        if not os.path.isdir(n):
            raise SourceError(f"目录不存在: {path}")
        trash = self._native(self._trash_dir(path))
        os.makedirs(trash, exist_ok=True)
        name = os.path.basename(n.rstrip("\\/")) or "folder"
        tag = stamp or time.strftime("%Y%m%d-%H%M%S")
        # ★ 「找空位 → 搬进去」必须原子。不加锁时两个同名目录并发删除：
        #   后者看到 target 已存在却仍可能算出同一个名字，`shutil.move`
        #   遇到已存在的目录会把源**塞进**目标里面 → 回收站结构嵌套、
        #   `_origin.json` 记录失效、还原不回去。
        # ★ 用 os.rename 的原子性解决跨进程竞态：目标已存在时内核抛
        #   FileExistsError，冲突方递增序号重试。`shutil.move` 在目标目录
        #   已存在时会把源**塞进**目标里面（嵌套），回收站结构就毁了。
        target = os.path.join(trash, f"{tag}-{name}")
        seq = 2
        while True:
            try:
                os.rename(n, target)
                break
            except OSError:
                target = os.path.join(trash, f"{tag}-{name} ({seq})")
                seq += 1
                if seq > 10000:
                    raise SourceError(f"回收站里同名目录过多: {name}")
        try:
            self._record_origin(trash, os.path.basename(target),
                                self.rel_path(path))
        except Exception:                                  # noqa: BLE001
            pass
        return target.replace("\\", "/") if os.name == "nt" else target

    # ---------------------------------------------------------- 回收站
    #
    # 为什么要有这一段：界面上一直写着"删除 = 移入回收站，可恢复"，
    # 但**应用里没有任何还原入口** —— 用户得自己去资源管理器翻
    # `.music-sync-trash\日期\`，还得记得它原来在哪。
    # 现在把"原路径"记在回收站目录里的 `_origin.json`：
    #   · 跟着回收站走（复制/搬走回收站也能还原）
    #   · 不依赖索引库（库重建了也不丢）
    #   · 老回收站没有这个文件 → 仍然列出，还原时落到指定目录

    def trash_root(self) -> str:
        """回收站根目录（不含日期子目录）。"""
        from ..core.config import load_config
        cfg_trash = load_config().settings.trash_dir
        if cfg_trash:
            return P.join(cfg_trash, self.cfg.id)
        return P.join(self.root, ".music-sync-trash")

    def _origin_file(self, trash: str) -> str:
        return P.join(trash, "_origin.json")

    def _load_origins(self, trash: str) -> Dict[str, Any]:
        try:
            with open(self._native(self._origin_file(trash)), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _record_origin(self, trash: str, name: str, rel: str) -> None:
        """把"回收站里的文件名 → 原相对路径"记下来（调用方持锁）。"""
        data = self._load_origins(trash)
        data[name] = {"rel": rel, "ts": int(time.time())}
        if len(data) > 20000:                      # 防止无限增长
            for k in sorted(data, key=lambda x: data[x].get("ts", 0))[:5000]:
                data.pop(k, None)
        p = self._native(self._origin_file(trash))
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError:
            pass

    def trash_list(self) -> List[Dict[str, Any]]:
        """回收站里的条目（文件 + 目录）。老回收站没有 _origin.json 也能列。"""
        root = self.trash_root()
        nroot = self._native(root)
        out: List[Dict[str, Any]] = []
        try:
            tops = sorted(os.listdir(nroot), reverse=True)
        except OSError:
            return out
        for top in tops:
            if top.startswith("_"):
                continue
            ntop = os.path.join(nroot, top)
            top_rel = P.join("", top)
            if os.path.isdir(ntop):
                if top.startswith(".") or top == "~$":
                    continue
                origins = self._load_origins(P.join(self.trash_root(), top))
                try:
                    names = sorted(os.listdir(ntop))
                except OSError:
                    continue
                for name in names:
                    if name.endswith(".tmp") or name.startswith("_origin"):
                        continue
                    np = os.path.join(ntop, name)
                    try:
                        st = os.stat(np)
                    except OSError:
                        continue
                    is_dir = os.path.isdir(np)
                    out.append({
                        "rel": P.join(top_rel, name), "date": top, "name": name,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                        "origin": str((origins.get(name) or {}).get("rel") or ""),
                    })
            else:
                try:
                    st = os.stat(ntop)
                except OSError:
                    continue
                origins = self._load_origins(self.trash_root())
                out.append({
                    "rel": top_rel, "date": "", "name": top, "is_dir": False,
                    "size": st.st_size, "mtime": st.st_mtime,
                    "origin": str((origins.get(top) or {}).get("rel") or ""),
                })
        return out

    def _free_name(self, folder_abs: str, name: str) -> str:
        """在 folder 里找一个不冲突的名字（x.mp3 → x (2).mp3）。"""
        base, ext = os.path.splitext(name)
        cand = name
        for i in range(2, 10000):
            if not os.path.exists(os.path.join(folder_abs, cand)):
                return cand
            cand = f"{base} ({i}){ext}"
        raise SourceError(f"目标目录里同名文件过多: {name}")

    def _trash_abs(self, rel: str) -> str:
        """回收站内条目的绝对路径；**越界直接拒绝**。

        ★ 安全修复（外部审查 NEW-02 / CRITICAL）：

          `trash_purge` 原来直接
              n = self._native(P.join(self.trash_root(), rel))
              shutil.rmtree(n, ignore_errors=True)
          完全没有校验 `rel`。构造 `{"rel": "../../Album"}` 就能把
          **回收站之外、甚至整个曲库/任意目录**递归删掉。
          `trash_restore` 同样能把库外任意文件搬进库里。

          现在两道闸：先拒绝含 `..` 的段，再用 `inside_root` 确认
          最终绝对路径确实在回收站根之下。
        """
        rel = P.norm(str(rel or "")).strip("/")
        if not rel:
            raise SourceError("缺少回收站条目路径")
        base = P.norm(self.trash_root())
        abs_p = P.norm(P.join(base, rel))
        if P.has_dotdot(abs_p) or not P.inside_root(base, abs_p):
            raise SourceError(f"非法回收站路径（越界）: {rel}")
        return abs_p

    def trash_restore(self, rel: str, dst_dir: str = "", policy: str = "skip") -> Dict[str, Any]:
        """把回收站里的一项还原回原路径（或指定目录）。**绝不覆盖**。"""
        self._guard_write()
        src_abs = self._trash_abs(rel)
        n = self._native(src_abs)
        if not os.path.exists(n):
            raise SourceError(f"回收站里找不到: {rel}")
        parts = P.norm(rel).strip("/").split("/")
        top_dir = (P.join(self.trash_root(), parts[0]) if len(parts) > 1
                   else self.trash_root())
        origins = self._load_origins(top_dir)
        name = parts[-1]
        origin = str((origins.get(name) or {}).get("rel") or "")
        if origin:
            target_rel = P.join(dst_dir, origin) if dst_dir else origin
        else:
            target_rel = P.join(dst_dir, name) if dst_dir else name
        target_rel = P.norm(target_rel).strip("/")
        target_abs = self.abs_path(target_rel)
        if not P.inside_root(self.root, target_abs):
            raise SourceError("还原目标超出数据源范围")
        existed = os.path.exists(self._native(target_abs))
        if existed:
            if policy == "skip":
                return {"state": "conflict", "detail": "目标已存在，已跳过",
                        "rel": rel, "target": target_rel}
            if policy == "rename":
                folder = os.path.dirname(self._native(target_abs)) or self._native(self.root)
                # ★ 用**目标应有的名字**做基准。回收站里的文件名是
                #   `{时间戳}-原名`，拿它生成新名会把时间戳永久写进曲库。
                target_rel = P.join(P.parent(target_rel),
                                    self._free_name(folder, P.basename(target_rel)))
                target_abs = self.abs_path(target_rel)
            elif policy != "overwrite":
                raise SourceError(f"不支持的同名冲突策略: {policy}")
        self.mkdirs(P.parent(target_rel) or "")
        if existed and policy == "overwrite":
            # 覆盖前先把现有文件移进回收站（和移动/传输一致）
            self.remove(target_rel, hard=False)
        shutil.move(n, self._native(target_abs))
        # 还原成功：清掉记录，之后同名文件不会被误认为同一份
        try:
            data = self._load_origins(top_dir)
            data.pop(name, None)
            p = self._native(self._origin_file(top_dir))
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except OSError:
            pass
        return {"state": "done", "rel": rel, "target": self.rel_path(target_abs),
                "overwrote": bool(existed and policy == "overwrite")}

    def trash_purge(self, rel: str) -> None:
        """永久删除回收站里的一项（不可恢复）。"""
        self._guard_write()
        n = self._native(self._trash_abs(rel))
        if os.path.isdir(n):
            shutil.rmtree(n, ignore_errors=True)
        elif os.path.exists(n):
            os.remove(n)

    def remove_empty_dir(self, path: str) -> bool:
        """只在目录确实为空时删掉它；非空返回 False（绝不递归删内容）。"""
        self._guard_write()
        n = self._native(path)
        if not os.path.isdir(n):
            return False
        try:
            if os.listdir(n):
                return False
        except OSError:
            return False
        try:
            os.rmdir(n)
            return True
        except OSError:
            return False

    def rename_dir(self, src_path: str, dst_path: str) -> None:
        """目录改名/移动：目标是空目录或不存在时才允许，避免静默覆盖已有内容。"""
        self._guard_write()
        src, dst = self._native(src_path), self._native(dst_path)
        if not os.path.isdir(src):
            raise SourceError(f"目录不存在: {src_path}")
        # ★ 只改大小写（`Album` → `album`）：Windows 上 dst "已存在"的就是 src 自己，
        #   原来会报"目标目录已存在且非空"。走 os.rename 原子改名即可。
        if self.same_entry(src_path, dst_path):
            os.rename(src, dst)
            return
        if os.path.exists(dst):
            if not os.path.isdir(dst):
                raise SourceError(f"目标已存在同名文件: {dst_path}")
            if os.listdir(dst):
                raise SourceError(f"目标目录已存在且非空: {dst_path}")
            os.rmdir(dst)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)

    def copy_from(self, src: MusicSource, src_path: str, dst_path: str, progress=None,
                  verify_hash: Optional[str] = None):
        self._guard_write()
        if isinstance(src, LocalSource):
            os.makedirs(os.path.dirname(self._native(dst_path)) or ".", exist_ok=True)
            src_n = src._native(src_path)
            tmp_n = self._native(dst_path) + ".mshpart"
            src_size = os.path.getsize(src_n)
            src_mtime = os.stat(src_n).st_mtime
            # ★ 断点续传：临时文件 + 元数据都对得上，才从已完成处接着写。
            #   对不上（换了另一个源文件 / 源被改过）就从头写，绝不拼错内容。
            offset = self._resume_offset(src, src_path, dst_path, src_size, src_mtime)
            # ★★ 元数据必须在**开始写数据之前**落盘 ★★
            #   原来写在复制完成之后：真实中断时根本执行不到，于是永远续不上。
            if not offset:
                self._write_part_meta(dst_path + ".mshpart.json", {
                    "src_id": getattr(src.cfg, "id", ""),
                    "src_path": str(src_path),
                    "src_size": src_size,
                    "src_mtime": src_mtime,
                    "ts": time.time(),
                })
            with open(src_n, "rb") as fs, open(tmp_n, "ab" if offset else "wb") as fd:
                if offset:
                    fs.seek(offset)
                shutil.copyfileobj(fs, fd, 4 * 1024 * 1024)
            got = os.path.getsize(tmp_n)
            if got != src_size:
                # 接错了或者源在复制途中变了 → 丢弃，下次从头来
                try:
                    os.remove(tmp_n)
                except OSError:
                    pass
                self._drop_part(dst_path)
                raise SourceError(
                    f"复制后大小不一致（源 {src_size} / 临时 {got}），已丢弃临时文件")
            os.replace(tmp_n, self._native(dst_path))
            self._drop_part(dst_path, keep_meta=False)
            try:
                shutil.copystat(src_n, self._native(dst_path))
            except OSError:
                pass
            st = os.stat(self._native(dst_path))
            if progress:
                progress(st.st_size, st.st_size)
            return {"size": st.st_size, "verified": None, "resumed_from": offset}
        return super().copy_from(src, src_path, dst_path, progress, verify_hash)

    def _trash_dir(self, path: str) -> str:
        from ..core.config import load_config

        cfg_trash = load_config().settings.trash_dir
        if cfg_trash:
            return P.join(cfg_trash, self.cfg.id)
        return P.join(self.root, ".music-sync-trash", time.strftime("%Y%m%d"))


class _BoundedReader:
    def __init__(self, fh, limit: int):
        self._fh = fh
        self._left = limit

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        if n is None or n < 0:
            n = self._left
        data = self._fh.read(min(n, self._left))
        self._left -= len(data)
        return data

    def seek(self, *a, **k):
        return self._fh.seek(*a, **k)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
