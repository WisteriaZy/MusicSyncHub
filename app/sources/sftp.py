"""SFTP（SSH）数据源，用于连接另一台 Windows / Linux 服务器。

支持密码与私钥两种认证；私钥口令在 config 里用 `extras.key_passphrase_ref` 指定。
"""
from __future__ import annotations

import io
import os
import stat as statmod
import time
from typing import Any, Callable, Dict, List, Optional

from .base import Entry, MusicSource, SourceError, register
from ..core import paths as P


@register
class SftpSource(MusicSource):
    kind = "sftp"
    label = "SFTP（SSH 文件传输）"
    hint = ("主机 + 端口(默认22) + 用户名 + 密码，根路径填远端目录。写入走 SFTP 协议，支持断点读")
    experimental = True
    verify_note = (
        "只做过代码实现，**没有**连过任何真实 SSH 服务器。上传、断点读、删除进回收站（改名）这几条路径都未验证。")
    _prefetch_max = 64 * 1024 * 1024

    # ------------------------------------------------------------ 连接
    def open(self) -> None:
        self._client()

    def _client(self):
        tid = _tid()
        cli = self._clients.get(tid)
        if cli is not None:
            try:
                transport = cli.get_transport()
                if transport is not None and transport.is_active():
                    return cli
            except Exception:
                pass
            try:
                cli.close()
            except Exception:
                pass
        try:
            import paramiko
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise SourceError("缺少 paramiko 依赖，请执行: uv sync 或 pip install paramiko") from exc

        cfg = self.cfg
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: Dict[str, Any] = dict(
            hostname=cfg.host,
            port=int(cfg.port or 22),
            username=cfg.username or None,
            timeout=float(cfg.extras.get("timeout", 25)),
            banner_timeout=float(cfg.extras.get("timeout", 25)),
            auth_timeout=float(cfg.extras.get("timeout", 25)),
            look_for_keys=bool(cfg.extras.get("look_for_keys", True)),
            allow_agent=bool(cfg.extras.get("allow_agent", True)),
        )
        key_file = cfg.extras.get("key_file") or ""
        if key_file:
            from ..core.config import get_secret

            passphrase = get_secret(cfg.extras.get("key_passphrase_ref", ""), "") or None
            kwargs["key_filename"] = os.path.expanduser(str(key_file))
            kwargs["passphrase"] = passphrase
            kwargs["look_for_keys"] = False
        if self.password:
            kwargs["password"] = self.password
        elif not key_file and not cfg.extras.get("use_agent_only"):
            kwargs["password"] = None
        try:
            cli.connect(**kwargs)
        except Exception as exc:
            raise SourceError(f"SFTP 连接失败 {cfg.host}:{cfg.port or 22} -> {exc}") from exc
        self._clients[tid] = cli
        return cli

    def _sftp(self):
        cli = self._client()
        tid = _tid()
        sftp = self._clients.get(tid + ":sftp")
        if sftp is not None:
            try:
                sftp.stat(".")
                return sftp
            except Exception:
                try:
                    sftp.close()
                except Exception:
                    pass
        sftp = cli.open_sftp()
        try:
            sftp.get_channel().settimeout(float(self.cfg.extras.get("io_timeout", 60)))
        except Exception:
            pass
        self._clients[tid + ":sftp"] = sftp
        return sftp

    def close(self) -> None:
        for k in list(self._clients):
            obj = self._clients.pop(k, None)
            try:
                obj.close()
            except Exception:
                pass

    # ------------------------------------------------------------ 枚举
    def stat(self, path: str) -> Optional[Entry]:
        sftp = self._sftp()
        ap = self.abs_path(path)
        try:
            st = sftp.stat(ap)
        except IOError:
            return None
        except Exception as exc:
            raise SourceError(f"stat 失败 {ap}: {exc}") from exc
        return Entry(
            path=ap,
            name=P.basename(ap),
            is_dir=statmod.S_ISDIR(st.st_mode),
            size=int(st.st_size or 0),
            mtime=float(st.st_mtime or 0),
            etag=f"{int(st.st_mtime or 0)}-{st.st_size}",
        )

    def listdir(self, path: str) -> List[Entry]:
        sftp = self._sftp()
        ap = self.abs_path(path)
        out: List[Entry] = []
        try:
            for a in sftp.listdir_attr(ap):
                is_dir = statmod.S_ISDIR(a.st_mode) or (
                    a.st_mode is None and statmod.S_ISDIR(a.st_mode or 0)
                )
                if a.st_mode is None:
                    # 某些 SFTP 服务端不返回 mode，用 lstat 兜底
                    try:
                        is_dir = statmod.S_ISDIR(sftp.stat(P.join(ap, a.filename)).st_mode)
                    except Exception:
                        is_dir = False
                if is_dir and a.filename in (".", ".."):
                    continue
                out.append(Entry(
                    path=P.join(ap, a.filename),
                    name=a.filename,
                    is_dir=bool(is_dir),
                    size=0 if is_dir else int(a.st_size or 0),
                    mtime=float(a.st_mtime or 0),
                    etag=f"{int(a.st_mtime or 0)}-{a.st_size}",
                ))
        except IOError as exc:
            raise SourceError(f"无法列出目录 {ap}: {exc}") from exc
        except Exception as exc:
            raise SourceError(f"无法列出目录 {ap}: {exc}") from exc
        return out

    # ------------------------------------------------------------ 读取
    def open_read(self, path: str, offset: int = 0, length: int = 0):
        sftp = self._sftp()
        ap = self.abs_path(path)
        fh = sftp.open(ap, "rb")
        try:
            fh.prefetch(file_size=self._prefetch_max)
        except Exception:
            pass
        if offset:
            fh.seek(offset)
        if length:
            from .local import _BoundedReader

            return _BoundedReader(fh, length)
        return fh

    # ------------------------------------------------------------ 写入
    def mkdirs(self, path: str) -> None:
        self._guard_write()
        sftp = self._sftp()
        ap = self.abs_path(path)
        parts = [p for p in ap.split("/") if p]
        cur = "/" if ap.startswith("/") else ""
        for part in parts:
            cur = (cur.rstrip("/") + "/" + part) if cur not in ("", "/") else ("/" + part if ap.startswith("/") else part)
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError as exc:
                    if "exists" not in str(exc).lower():
                        raise SourceError(f"创建目录失败 {cur}: {exc}") from exc

    def write_file(self, path: str, data, progress: Optional[Callable[[int], None]] = None,
                   expected_size: Optional[int] = None) -> None:
        self._guard_write()
        sftp = self._sftp()
        ap = self.abs_path(path)

        class _CB:
            def __init__(self, cb):
                self.cb = cb
                self.n = 0

            def __call__(self, transferred, total):
                self.n = transferred
                if self.cb:
                    self.cb(transferred)

            def get(self):
                return self.n

        cb = _CB(progress)
        if isinstance(data, (bytes, bytearray, memoryview)):
            fh = sftp.open(ap, "wb")
            try:
                fh.set_pipelined(True)
                fh.write(bytes(data))
                if progress:
                    progress(len(data))
            finally:
                fh.close()
            return
        sftp.putfo(data, ap, file_size=expected_size or 0, callback=cb)

    def replace(self, src_path: str, dst_path: str) -> None:
        sftp = self._sftp()
        a, b = self.abs_path(src_path), self.abs_path(dst_path)
        try:
            sftp.posix_rename(a, b)
            return
        except Exception:
            pass
        try:
            sftp.remove(b)
        except IOError:
            pass
        sftp.rename(a, b)

    def remove(self, path: str, hard: bool = False) -> str:
        self._guard_write()
        sftp = self._sftp()
        ap = self.abs_path(path)
        if hard:
            sftp.remove(ap)
            return ""
        trash = self._trash_dir()
        self.mkdirs(trash)
        target = P.join(trash, P.basename(ap))
        try:
            if self.stat(target):
                target = P.join(trash, f"{int(time.time())}-{P.basename(ap)}")
        except Exception:
            pass
        try:
            sftp.posix_rename(ap, target)
        except Exception:
            sftp.rename(ap, target)
        return target

    def remove_dir(self, path: str) -> None:
        self._guard_write()
        sftp = self._sftp()
        ap = self.abs_path(path)

        def _rm(p: str) -> None:
            for a in sftp.listdir_attr(p):
                child = P.join(p, a.filename)
                if statmod.S_ISDIR(a.st_mode):
                    _rm(child)
                else:
                    sftp.remove(child)
            sftp.rmdir(p)

        _rm(ap)

    def copy_from(self, src: MusicSource, src_path: str, dst_path: str, progress=None,
                  verify_hash: Optional[str] = None):
        # 同主机 SFTP -> 本地 直接走本地写，避免中转内存
        return super().copy_from(src, src_path, dst_path, progress, verify_hash)

    def _trash_dir(self) -> str:
        return P.join(self.root, ".music-sync-trash", time.strftime("%Y%m%d"))


def _tid() -> int:
    import threading

    return threading.get_ident()
