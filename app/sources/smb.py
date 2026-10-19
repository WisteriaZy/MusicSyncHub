"""SMB / CIFS（Windows 共享）数据源。

优先使用 smbclient（smbprotocol 库）；若未安装，则自动回退到
「操作系统已挂载的盘符路径」——例如 macOS 的 /Volumes/Music，Linux 的 /mnt/music。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .base import Entry, MusicSource, SourceError, register
from ..core import paths as P
from .local import LocalSource

try:  # pragma: no cover - 取决于运行环境
    import smbclient  # type: ignore
    import smbclient.shutil as _smb_shutil  # type: ignore

    _HAS_SMBCLIENT = True
except Exception:  # pragma: no cover
    smbclient = None  # type: ignore
    _smb_shutil = None  # type: ignore
    _HAS_SMBCLIENT = False


@register
class SmbSource(MusicSource):
    kind = "smb"
    label = "SMB / Windows 共享"
    hint = ("\\主机\共享名，可填域与用户名。Windows 共享文件夹用这个")
    experimental = True
    verify_note = (
        "只做过代码实现，**没有**连过任何真实 Windows 共享。依赖 pysmb，认证、域、共享内路径语义都未验证。")

    def __init__(self, cfg, password: str = ""):
        super().__init__(cfg, password)
        share = (cfg.share or "").strip("/\\")
        self.unc = f"//{cfg.host}/{share}" if cfg.host and share else ""
        # 回退模式：把 root 当作本地已挂载路径
        self._fallback: Optional[LocalSource] = None
        self._registered = False

    # ------------------------------------------------------------ 连接
    @property
    def using_fallback(self) -> bool:
        return not _HAS_SMBCLIENT or bool(self.cfg.extras.get("use_mounted_path"))

    def _local_fallback(self) -> LocalSource:
        if self._fallback is None:
            cfg = self.cfg
            root = cfg.root or ""
            if not root:
                raise SourceError(
                    "SMB 数据源需要 settings：未安装 smbclient 时请把 root 设为已挂载目录，"
                    "例如 /Volumes/Music 或 /mnt/music"
                )
            import copy

            c2 = copy.copy(cfg)
            c2.type = "local"
            self._fallback = LocalSource(c2, self.password)
        return self._fallback

    def open(self) -> None:
        if self.using_fallback:
            return
        cfg = self.cfg
        try:
            smbclient.register_session(
                cfg.host,
                username=cfg.username or None,
                password=self.password or None,
                port=int(cfg.port or 445),
                encrypt=bool(cfg.extras.get("encrypt", False)),
                connection_timeout=float(cfg.extras.get("timeout", 30)),
            )
        except Exception as exc:
            raise SourceError(f"SMB 连接失败 //{cfg.host}: {exc}") from exc
        self._registered = True

    def close(self) -> None:
        if _HAS_SMBCLIENT and self._registered:
            try:
                smbclient.delete_session(self.cfg.host, port=int(self.cfg.port or 445))
            except Exception:
                pass
            self._registered = False

    def _unc_path(self, path: str) -> str:
        ap = self.abs_path(path)
        rel = self.rel_path(ap)
        if self.root.startswith("//"):
            # root 就是完整 UNC
            return ap.replace("/", "\\") if not ap.startswith("//") else "\\\\" + ap.lstrip("/").replace("/", "\\")
        return self.unc + "\\" + rel.replace("/", "\\")

    # ------------------------------------------------------------ 枚举
    def stat(self, path: str) -> Optional[Entry]:
        if self.using_fallback:
            return self._local_fallback().stat(path)
        try:
            st = smbclient.stat(self._unc_path(path))
        except Exception:
            return None
        return Entry(path=self.abs_path(path), name=P.basename(path),
                     is_dir=bool(getattr(st, "st_mode", 0) & 0o040000),
                     size=int(getattr(st, "st_size", 0) or 0),
                     mtime=float(getattr(st, "st_mtime", 0) or 0))

    def listdir(self, path: str) -> List[Entry]:
        if self.using_fallback:
            return self._local_fallback().listdir(path)
        unc = self._unc_path(path)
        out: List[Entry] = []
        try:
            for name in smbclient.listdir(unc):
                if name in (".", ".."):
                    continue
                full = P.join(path or self.root, name)
                st = self.stat(full)
                if st is None:
                    continue
                st.name = name
                out.append(st)
        except Exception as exc:
            raise SourceError(f"无法列出 SMB 目录 {unc}: {exc}") from exc
        return out

    # ------------------------------------------------------------ 读取
    def open_read(self, path: str, offset: int = 0, length: int = 0):
        if self.using_fallback:
            return self._local_fallback().open_read(path, offset, length)
        fh = smbclient.open_file(self._unc_path(path), mode="rb")
        if offset:
            fh.seek(offset)
        if length:
            from .local import _BoundedReader

            return _BoundedReader(fh, length)
        return fh

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        if self.using_fallback:
            return self._local_fallback().read_range(path, offset, length)
        with smbclient.open_file(self._unc_path(path), mode="rb") as fh:
            if offset:
                fh.seek(offset)
            return fh.read(length) if length else fh.read()

    # ------------------------------------------------------------ 写入
    def mkdirs(self, path: str) -> None:
        self._guard_write()
        if self.using_fallback:
            return self._local_fallback().mkdirs(path)
        unc = self._unc_path(path)
        parts = unc.split("\\")
        cur = "\\\\".join(parts[:4]) if unc.startswith("\\\\") else parts[0]
        for seg in (parts[4:] if unc.startswith("\\\\") else parts[1:]):
            cur = cur.rstrip("\\") + "\\" + seg
            try:
                smbclient.mkdir(cur)
            except Exception:
                pass

    def write_file(self, path: str, data, progress: Optional[Callable[[int], None]] = None,
                   expected_size: Optional[int] = None) -> None:
        self._guard_write()
        if self.using_fallback:
            return self._local_fallback().write_file(path, data, progress, expected_size)
        self.mkdirs(P.parent(path))
        written = 0
        with smbclient.open_file(self._unc_path(path), mode="wb") as fh:
            if isinstance(data, (bytes, bytearray, memoryview)):
                fh.write(bytes(data))
                written = len(data)
                if progress:
                    progress(written)
            else:
                while True:
                    chunk = data.read(1024 * 512)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written)

    def replace(self, src_path: str, dst_path: str) -> None:
        if self.using_fallback:
            return self._local_fallback().replace(src_path, dst_path)
        a, b = self._unc_path(src_path), self._unc_path(dst_path)
        try:
            smbclient.remove(b)
        except Exception:
            pass
        smbclient.rename(a, b)

    def remove(self, path: str, hard: bool = False) -> str:
        self._guard_write()
        if self.using_fallback:
            return self._local_fallback().remove(path, hard)
        if hard:
            smbclient.remove(self._unc_path(path))
            return ""
        trash = P.join(self.root, ".music-sync-trash", time.strftime("%Y%m%d"))
        self.mkdirs(trash)
        target = P.join(trash, P.basename(path))
        if self.exists(target):
            target = P.join(trash, f"{int(time.time())}-{P.basename(path)}")
        smbclient.rename(self._unc_path(path), self._unc_path(target))
        return target

    def remove_dir(self, path: str) -> None:
        self._guard_write()
        if self.using_fallback:
            return self._local_fallback().remove_dir(path)
        smbclient.rmdir(self._unc_path(path))

    def copy_from(self, src: MusicSource, src_path: str, dst_path: str, progress=None,
                  verify_hash: Optional[str] = None):
        if self.using_fallback:
            return self._local_fallback().copy_from(src, src_path, dst_path, progress, verify_hash)
        return super().copy_from(src, src_path, dst_path, progress, verify_hash)
