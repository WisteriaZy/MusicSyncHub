"""FTP / FTPS 数据源（**实验性**）。

## 为什么标"实验性"

FTP 是明文协议，而且各家服务器的目录列举格式、路径语义、被动/主动模式
差别很大。这里只用 Python 标准库的 `ftplib` 做**最保守**的实现：

  · 被动模式（PASV）—— 穿透 NAT/防火墙的常规做法
  · 一律用 MLSD 列目录（RFC 3659）；服务器不支持时**明确报错**，
    而不是去猜 `LIST` 的文本格式（那是过度合并/漏文件的经典来源）
  · 删除优先尝试把文件移到 `.music-sync-trash/`（保持全项目一致的
    "删除可恢复"约定）；服务器不支持改名就**只读报错**，不硬删

所以：局域网里连 NAS / 老设备能用；公网、怪异服务器上不保证。
界面上会标 [实验性]，配置里也可以先勾只读。

## 认证

密码走 `secret_ref` → secrets.toml（和 SFTP/WebDAV 一致）。
`extras.tls = true` 时用 FTPS（隐式 AUTH TLS）。
"""
from __future__ import annotations

import ftplib
import io
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from .base import Entry, MusicSource, ReadOnlyError, SourceError, register
from ..core import paths as P


def _tid() -> int:
    return threading.get_ident()


@register
class FtpSource(MusicSource):
    kind = "ftp"
    label = "FTP / FTPS"
    hint = ("主机 + 端口(默认21) + 用户名 + 密码。明文 FTP，"
            "局域网内可用；公网建议先勾「只读」。勾 extras.tls 走 FTPS")
    experimental = True
    verify_note = (
        "只做过代码实现，**没有**连过任何真实 FTP 服务器。MLSD 列目录、REST 断点读、回收站改名都未验证。")

    def __init__(self, cfg, password: str = ""):
        super().__init__(cfg, password)
        self._conns: Dict[int, ftplib.FTP] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 连接
    def open(self) -> None:
        self._ftp()

    def close(self) -> None:
        with self._lock:
            for c in list(self._conns.values()):
                try:
                    c.quit()
                except Exception:
                    try:
                        c.close()
                    except Exception:
                        pass
            self._conns.clear()

    def _ftp(self) -> ftplib.FTP:
        tid = _tid()
        c = self._conns.get(tid)
        if c is not None:
            try:
                c.voidcmd("NOOP")          # 轻量探活
                return c
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
        cfg = self.cfg
        host = str(cfg.host or "").strip()
        if not host:
            raise SourceError("FTP 数据源缺少主机地址")
        timeout = float(cfg.extras.get("timeout", 25) or 25)
        try:
            use_tls = bool(cfg.extras.get("tls"))
            cls = ftplib.FTP_TLS if use_tls else ftplib.FTP
            c = cls()
            c.connect(host, int(cfg.port or 21), timeout=timeout)
            c.login(cfg.username or "anonymous", self.password or "")
            if use_tls:
                try:
                    c.prot_p()             # 数据通道也加密
                except Exception:
                    pass
            try:
                c.set_pasv(True)           # 被动模式：穿透 NAT 的常规选择
            except Exception:
                pass
        except SourceError:
            raise
        except Exception as exc:                                  # noqa: BLE001
            raise SourceError(f"连接 FTP 失败（{host}）：{type(exc).__name__}: {exc}") from exc
        self._conns[tid] = c
        return c

    def _abs(self, path: str) -> str:
        """把各种写法收敛成 FTP 上的绝对路径（都以 `/` 开头）。

        FTP 各家服务器的 cwd 语义不一致，所以内部一律用绝对路径。
        这里要处理三种输入（**并且不能把 root 重复拼上去**）：

            ``（空）/`.`      → 就是 root 本身
            `a/b.mp3`         → root 下的相对路径
            `/music/a/b.mp3`  → 已经是绝对路径，**原样返回**

        踩过：最初对"以 / 开头但不是 root 开头"的输入直接当相对路径处理，
        于是已经带 root 的绝对路径会被再拼一次，
        `/music/a/b.mp3` 变成 `/music/music/a/b.mp3`（列表/写入全找不到文件）。
        """
        p = P.norm(path or "").strip("/")
        # `"."` 也是"就在这里"的意思（界面上列举根目录时会传它）
        if p in (".", "./"):
            p = ""
        root = P.norm(self.cfg.root or "").strip("/")
        if not root:
            return "/" + p if p else "/"
        if not p:
            return "/" + root
        # 已经带 root 前缀 → 原样（不再拼）
        if p == root:
            return "/" + root
        if p.startswith(root + "/"):
            return "/" + p
        return "/" + root + "/" + p

    def _call(self, fn: Callable[[ftplib.FTP], Any]) -> Any:
        """执行 FTP 操作；连接掉了就重连一次。"""
        for attempt in (0, 1):
            c = self._ftp()
            try:
                return fn(c)
            except (ftplib.error_temp, ftplib.error_proto, EOFError, OSError) as exc:
                try:
                    c.close()
                except Exception:
                    pass
                self._conns.pop(_tid(), None)
                if attempt == 1:
                    raise SourceError(f"FTP 操作失败：{type(exc).__name__}: {exc}") from exc
            except ftplib.error_perm as exc:
                # 权限/不存在这类**不要重连重试** —— 重试也一样
                raise SourceError(f"FTP 拒绝：{exc}") from exc
        return None

    # ------------------------------------------------------------ 列举
    def _mlsd(self, path: str) -> List[Dict[str, Any]]:
        """MLSD 列目录（RFC 3659）。

        ★ 不支持就**明确报错**，不去解析 `LIST` 的文本输出 ——
        各家格式不同，猜错的后果是文件被漏掉或名字被切坏。
        """
        rows: List[Dict[str, Any]] = []

        def go(c: ftplib.FTP) -> None:
            try:
                for name, facts in c.mlsd(path):
                    rows.append({"name": name, "facts": facts})
            except ftplib.error_perm as exc:
                raise SourceError(
                    f"这台 FTP 服务器不支持 MLSD（RFC 3659）列目录：{exc}。"
                    f"实验性的 FTP 源只支持标准 MLSD，"
                    f"可以改用 SFTP / WebDAV / SMB 连同一台机器。") from exc
        self._call(go)
        return rows

    def stat(self, path: str) -> Optional[Entry]:
        ap = self._abs(path)
        if ap == "/":
            return Entry(path="/", name="/", is_dir=True, size=0,
                         mtime=time.time(), etag="")
        parent = P.parent(ap) or "/"
        name = P.basename(ap)
        for row in self._mlsd(parent):
            if row["name"] != name:
                continue
            f = row["facts"]
            typ = str(f.get("type", "")).lower()
            is_dir = typ in ("dir", "cdir", "pdir")
            size = int(f.get("size") or 0)
            mt = f.get("modify")
            mtime = 0.0
            if mt and len(str(mt)) >= 14:
                try:
                    mtime = time.mktime(time.strptime(str(mt)[:14], "%Y%m%d%H%M%S"))
                except Exception:
                    mtime = 0.0
            return Entry(path=ap, name=name, is_dir=is_dir, size=size,
                         mtime=mtime, etag=f"{int(mtime)}-{size}")
        return None

    def listdir(self, path: str) -> List[Entry]:
        ap = self._abs(path)
        out: List[Entry] = []
        for row in self._mlsd(ap):
            name = row["name"]
            if name in (".", ".."):
                continue
            f = row["facts"]
            typ = str(f.get("type", "")).lower()
            is_dir = typ in ("dir", "cdir", "pdir")
            size = int(f.get("size") or 0)
            mt = f.get("modify")
            mtime = 0.0
            if mt and len(str(mt)) >= 14:
                try:
                    mtime = time.mktime(time.strptime(str(mt)[:14], "%Y%m%d%H%M%S"))
                except Exception:
                    mtime = 0.0
            out.append(Entry(path=P.join(ap, name), name=name, is_dir=is_dir,
                             size=size, mtime=mtime, etag=f"{int(mtime)}-{size}"))
        out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return out

    # ------------------------------------------------------------ 读
    def open_read(self, path: str, offset: int = 0, length: int = 0):
        ap = self._abs(path)
        st = self.stat(ap)
        if st is None:
            raise SourceError(f"FTP 上找不到文件：{ap}")
        total = int(st.size or 0)
        end = total - 1 if not length else min(total - 1, offset + length - 1)
        buf = io.BytesIO()

        def go(c: ftplib.FTP) -> None:
            # REST 指定起始偏移（不是所有服务器都支持；不支持就让上层报错）
            if offset:
                c.voidcmd(f"REST {int(offset)}")
            c.retrbinary("RETR " + ap, buf.write, rest=offset or None)
        self._call(go)
        buf.seek(0)
        if length:
            return io.BytesIO(buf.read(length))
        return buf

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        fh = self.open_read(path, offset, length)
        try:
            return fh.read(length)
        finally:
            try:
                fh.close()
            except Exception:
                pass

    # ------------------------------------------------------------ 写
    def mkdirs(self, path: str) -> None:
        self._guard_write()
        ap = self._abs(path)
        parts = [x for x in ap.split("/") if x]
        cur = ""
        for seg in parts:
            cur = cur + "/" + seg
            def go(c: ftplib.FTP, _d: str = cur) -> None:
                try:
                    c.mkd(_d)
                except ftplib.error_perm as exc:
                    # 已存在是正常的（MKD 已存在多数服务器报 550）
                    if "exist" not in str(exc).lower() and "550" not in str(exc):
                        raise
            self._call(go)

    def write_file(self, path: str, data: Union[bytes, Iterable[bytes]],
                   progress: Optional[Callable[[int], None]] = None,
                   expected_size: Optional[int] = None) -> None:
        self._guard_write()
        ap = self._abs(path)
        parent = P.parent(ap)
        if parent and parent != "/":
            self.mkdirs(parent)
        tmp = ap + ".mshpart"
        sent = 0

        def go(c: ftplib.FTP) -> None:
            nonlocal sent
            if isinstance(data, (bytes, bytearray, memoryview)):
                c.storbinary("STOR " + tmp, io.BytesIO(bytes(data)))
                sent = len(data)
            else:
                class _Reader(io.RawIOBase):
                    def readable(self): return True
                    def readinto(self, b):
                        chunk = data.read(len(b))
                        if not chunk:
                            return 0
                        n = len(chunk)
                        b[:n] = chunk
                        return n
                    def read(self, n=-1):     # ftplib 用 read
                        chunk = data.read(n if n and n > 0 else 65536)
                        if progress and chunk:
                            progress(len(chunk))
                        return chunk
                c.storbinary("STOR " + tmp, _Reader())
        self._call(go)
        # 先传临时名、再改名 —— 中断不会在目标留下"半截但看着完整"的文件
        try:
            self._call(lambda c: c.rename(tmp, ap))
        except SourceError:
            try:
                self._call(lambda c: c.delete(tmp))
            except Exception:
                pass
            raise

    def replace(self, src_path: str, dst_path: str) -> None:
        self._guard_write()
        self._call(lambda c: c.rename(self._abs(src_path), self._abs(dst_path)))

    def move(self, src_path: str, dst_path: str) -> None:
        self._guard_write()
        a, b = self._abs(src_path), self._abs(dst_path)
        parent = P.parent(b)
        if parent and parent != "/":
            self.mkdirs(parent)
        self._call(lambda c: c.rename(a, b))

    # ------------------------------------------------------------ 删
    def trash_dir(self, path: str, stamp: str = "") -> str:
        """回收站目录（`<root>/.music-sync-trash/日期/`）。"""
        root = P.norm(self.cfg.root or "").strip("/")
        day = stamp or time.strftime("%Y%m%d")
        base = f"/{root}/.music-sync-trash/{day}" if root else f"/.music-sync-trash/{day}"
        self.mkdirs(base)
        return P.norm(base)

    def remove(self, path: str, hard: bool = False) -> str:
        """删除。

        ★ 默认**移到回收站**（改名），和本地源保持一致的可恢复语义。
        服务器不允许改名时**报错而不是硬删** —— 宁可让用户手动处理，
        也不做"以为进回收站了其实永久删了"这种事。
        """
        self._guard_write()
        ap = self._abs(path)
        if hard:
            self._call(lambda c: c.delete(ap))
            return ""
        trash = self.trash_dir(ap)
        name = P.basename(ap)
        target = P.join(trash, name)
        i = 2
        while True:
            try:
                self._call(lambda c, _s=ap, _d=target: c.rename(_s, _d))
                return target
            except SourceError:
                # 目标重名就换一个；连续失败则判定为"不支持改名"
                nxt = P.join(trash, f"{P.stem(name)} ({i}){P.ext(name)}")
                if i > 20 or nxt == target:
                    raise SourceError(
                        f"这台 FTP 服务器不支持把文件移到回收站（改名失败）：{ap}。"
                        f"为避免永久删除，已中止。可在「数据源」里把它设为只读，"
                        f"或改用 SFTP / WebDAV。")
                target, i = nxt, i + 1

    def remove_dir(self, path: str) -> None:
        self._guard_write()
        ap = self._abs(path)
        # 递归先清空（FTP 的 RMD 只删空目录）
        for e in self.listdir(ap):
            if e.is_dir:
                self.remove_dir(e.path)
            else:
                self.remove(e.path, hard=True)
        self._call(lambda c: c.rmd(ap))

    def remove_empty_dir(self, path: str) -> bool:
        try:
            if self.listdir(self._abs(path)):
                return False
            self._call(lambda c: c.rmd(self._abs(path)))
            return True
        except Exception:
            return False

    def rename_dir(self, src_path: str, dst_path: str) -> None:
        self.replace(src_path, dst_path)

    # ------------------------------------------------------------ 复制
    def copy_from(self, src: MusicSource, src_path: str, dst_path: str,
                  progress: Optional[Callable[[int], None]] = None,
                  verify_hash: Optional[str] = None) -> Dict[str, Any]:
        """从另一个源复制过来。

        FTP **不能**服务端直连别的协议，所以一律走本地中转：
        读一块、写一块。对"从本地/U 盘搬到 FTP"这个常见场景够用。
        """
        self._guard_write()
        ap = self._abs(dst_path)
        parent = P.parent(ap)
        if parent and parent != "/":
            self.mkdirs(parent)
        tmp = ap + ".mshpart"
        total = 0
        try:
            fh = src.open_read(src_path)
        except Exception as exc:                                  # noqa: BLE001
            raise SourceError(f"读取源文件失败：{type(exc).__name__}: {exc}") from exc
        try:
            it = iter(lambda: fh.read(1024 * 1024), b"")
            self.write_file(tmp, it, progress=progress)
            total = int(getattr(src.stat(src_path), "size", 0) or 0)
        finally:
            try:
                fh.close()
            except Exception:
                pass
        self._call(lambda c: c.rename(tmp, ap))
        st = self.stat(ap)
        return {"size": (st.size if st else total), "verified": None}
