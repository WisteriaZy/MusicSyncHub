"""WebDAV 数据源（群晖、Nextcloud、ownCloud、坚果云、Alist 等）。

只依赖 PROPFIND / GET / PUT / MKCOL / MOVE / DELETE 这些通用动词，
用 httpx 直接实现，不引入额外的 webdav 客户端库。
"""
from __future__ import annotations

import time
import urllib.parse as up
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .base import Entry, MusicSource, SourceError, register
from ..core import paths as P

_NS = {
    "d": "DAV:",
    "lp1": "DAV:",
}
_PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getlastmodified/>
    <d:getetag/>
    <d:resourcetype/>
  </d:prop>
</d:propfind>"""


@register
class WebDavSource(MusicSource):
    kind = "webdav"
    label = "WebDAV"
    hint = ("基地址形如 https://host/dav/music（Nextcloud、群晖、坚果云都支持）")
    experimental = True
    verify_note = (
        "只做过代码实现，**没有**连过任何真实 WebDAV 服务端。PUT / MKCOL / PROPFIND / MOVE 的组合行为未验证。")

    def __init__(self, cfg, password: str = ""):
        super().__init__(cfg, password)
        base = (cfg.base_url or "").strip()
        if not base:
            # root 可能本身就是完整 URL
            base, _, rest = self.root.partition("/dav-root/")
            base = base or self.root
        self.base_url = base.rstrip("/")
        self._client_obj = None

    # ------------------------------------------------------------ 连接
    def _client(self):
        if self._client_obj is not None:
            return self._client_obj
        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise SourceError("缺少 httpx 依赖，请执行: uv sync") from exc
        auth = None
        if self.cfg.username:
            auth = (self.cfg.username, self.password or "")
        verify = bool(self.cfg.extras.get("verify_tls", True))
        headers = {"User-Agent": "MusicSyncHub/1.0"}
        for k, v in (self.cfg.extras.get("headers") or {}).items():
            headers[str(k)] = str(v)
        self._client_obj = httpx.Client(
            auth=auth,
            verify=verify,
            headers=headers,
            timeout=float(self.cfg.extras.get("timeout", 60)),
            follow_redirects=True,
        )
        return self._client_obj

    def close(self) -> None:
        if self._client_obj is not None:
            try:
                self._client_obj.close()
            except Exception:
                pass
            self._client_obj = None

    # ------------------------------------------------------------ URL
    def url_for(self, path: str) -> str:
        ap = self.abs_path(path)
        root = self.root
        if ap == root:
            rel = ""
        elif ap.startswith(root.rstrip("/") + "/"):
            rel = ap[len(root.rstrip("/")) + 1:]
        else:
            rel = ap.lstrip("/")
        quoted = "/".join(up.quote(seg) for seg in rel.split("/") if seg != "")
        return self.base_url + ("/" + quoted if quoted else "")

    # ------------------------------------------------------------ 枚举
    def listdir(self, path: str) -> List[Entry]:
        cli = self._client()
        url = self.url_for(path)
        try:
            resp = cli.request("PROPFIND", url, content=_PROPFIND_BODY,
                               headers={"Depth": "1", "Content-Type": "application/xml"})
        except Exception as exc:
            raise SourceError(f"WebDAV PROPFIND 失败 {url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise SourceError(f"WebDAV 认证/权限失败 ({resp.status_code}) {url}")
        if resp.status_code >= 400:
            raise SourceError(f"WebDAV PROPFIND {resp.status_code} {url}")
        return self._parse_multistatus(resp.text, url)

    def _parse_multistatus(self, xml: str, base_url: str,
                           include_self: bool = False) -> List[Entry]:
        """解析 PROPFIND 响应。

        `include_self=False` 时跳过"响应自身那一条"（`listdir` 用）；
        `stat` 必须传 `True` —— `Depth: 0` 时服务端**只返回自身**，
        跳过它就永远拿到空列表，`stat()` 一直返回 `None`
        （WebDAV 的"文件存不存在 / 多大"全部失效）。
        """
        out: List[Entry] = []
        try:
            root = ET.fromstring(xml.encode("utf-8"))
        except ET.ParseError as exc:
            raise SourceError(f"WebDAV 返回内容无法解析: {exc}") from exc
        base_path = up.urlparse(base_url).path.rstrip("/")
        self_path = self.abs_path("")
        for resp in root.findall("d:response", _NS):
            href = (resp.findtext("d:href", default="", namespaces=_NS) or "").strip()
            if not href:
                continue
            href_path = up.unquote(up.urlparse(href).path).rstrip("/")
            if href_path == base_path and not include_self:
                continue  # 自身（listdir 时要跳过，stat 时要保留）
            is_dir = resp.find(".//d:resourcetype/d:collection", _NS) is not None
            size = resp.findtext(".//d:getcontentlength", default="0", namespaces=_NS) or "0"
            mtime_raw = resp.findtext(".//d:getlastmodified", default="", namespaces=_NS) or ""
            etag = (resp.findtext(".//d:getetag", default="", namespaces=_NS) or "").strip('"')
            name = href_path.rsplit("/", 1)[-1]
            if not name:
                continue
            # 相对 base 的路径
            if href_path.startswith(base_path):
                rel = href_path[len(base_path):].lstrip("/")
            else:
                rel = name
            full = P.join(self.root, rel) if self.root else rel
            out.append(Entry(
                path=full,
                name=name,
                is_dir=is_dir,
                size=0 if is_dir else _to_int(size),
                mtime=_parse_http_date(mtime_raw),
                etag=etag,
            ))
        return out

    def stat(self, path: str) -> Optional[Entry]:
        cli = self._client()
        url = self.url_for(path)
        try:
            resp = cli.request("PROPFIND", url, content=_PROPFIND_BODY,
                               headers={"Depth": "0", "Content-Type": "application/xml"})
        except Exception as exc:
            raise SourceError(f"WebDAV PROPFIND 失败 {url}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            return None
        items = self._parse_multistatus(resp.text, url, include_self=True)
        return items[0] if items else None

    # ------------------------------------------------------------ 读取
    def open_read(self, path: str, offset: int = 0, length: int = 0):
        cli = self._client()
        url = self.url_for(path)
        headers = {}
        if offset or length:
            end = (offset + length - 1) if length else ""
            headers["Range"] = f"bytes={offset}-{end}"
        req = cli.build_request("GET", url, headers=headers)
        try:
            resp = cli.send(req, stream=True)
        except Exception as exc:
            raise SourceError(f"WebDAV GET 失败 {url}: {exc}") from exc
        if resp.status_code >= 400:
            resp.close()
            raise SourceError(f"WebDAV GET {resp.status_code} {url}")

        class _Stream:
            def __init__(self, r):
                self._r = r
                self._it = r.iter_bytes(1024 * 256)
                self._buf = b""

            def read(self, n: int = -1) -> bytes:
                if n is None or n < 0:
                    chunks = [self._buf]
                    self._buf = b""
                    chunks.extend(self._it)
                    return b"".join(chunks)
                while len(self._buf) < n:
                    try:
                        self._buf += next(self._it)
                    except StopIteration:
                        break
                out, self._buf = self._buf[:n], self._buf[n:]
                return out

            def seek(self, *a, **k):
                raise SourceError("WebDAV 流不支持 seek")

            def close(self):
                try:
                    self._r.close()
                except Exception:
                    pass

        return _Stream(resp)

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        cli = self._client()
        url = self.url_for(path)
        end = (offset + length - 1) if length else ""
        headers = {"Range": f"bytes={offset}-{end}"} if (offset or length) else {}
        try:
            resp = cli.get(url, headers=headers)
        except Exception as exc:
            raise SourceError(f"WebDAV GET 失败 {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise SourceError(f"WebDAV GET {resp.status_code} {url}")
        return resp.content

    # ------------------------------------------------------------ 写入
    def mkdirs(self, path: str) -> None:
        self._guard_write()
        cli = self._client()
        ap = self.abs_path(path)
        rel = self.rel_path(ap) if ap != self.root else ""
        cur = self.root
        for seg in [s for s in rel.split("/") if s]:
            cur = P.join(cur, seg)
            url = self.url_for(cur)
            r = cli.request("MKCOL", url)
            if r.status_code in (201, 405, 301):
                continue
            if r.status_code in (401, 403):
                raise SourceError(f"WebDAV 无权限创建目录 {url} ({r.status_code})")
            if r.status_code >= 400 and r.status_code not in (409,):
                raise SourceError(f"WebDAV MKCOL {r.status_code} {url}")

    def write_file(self, path: str, data, progress: Optional[Callable[[int], None]] = None,
                   expected_size: Optional[int] = None) -> None:
        self._guard_write()
        cli = self._client()
        url = self.url_for(path)
        if isinstance(data, (bytes, bytearray, memoryview)):
            body = bytes(data)
            resp = cli.put(url, content=body)
            if progress:
                progress(len(body))
        else:
            resp = cli.put(url, content=_iter_chunks(data, progress))
        if resp.status_code >= 400:
            raise SourceError(f"WebDAV PUT {resp.status_code} {url}")

    def replace(self, src_path: str, dst_path: str) -> None:
        cli = self._client()
        src_url, dst_url = self.url_for(src_path), self.url_for(dst_path)
        r = cli.request("MOVE", src_url, headers={"Destination": dst_url, "Overwrite": "T"})
        if r.status_code >= 400:
            raise SourceError(f"WebDAV MOVE {r.status_code} {src_url}")

    def remove(self, path: str, hard: bool = False) -> str:
        self._guard_write()
        cli = self._client()
        ap = self.abs_path(path)
        if hard:
            r = cli.delete(self.url_for(ap))
            if r.status_code >= 400:
                raise SourceError(f"WebDAV DELETE {r.status_code}")
            return ""
        trash = P.join(self.root, ".music-sync-trash", time.strftime("%Y%m%d"))
        self.mkdirs(trash)
        target = P.join(trash, P.basename(ap))
        if self.exists(target):
            target = P.join(trash, f"{int(time.time())}-{P.basename(ap)}")
        r = cli.request("MOVE", self.url_for(ap),
                        headers={"Destination": self.url_for(target), "Overwrite": "T"})
        if r.status_code >= 400:
            raise SourceError(f"WebDAV MOVE->trash {r.status_code}")
        return target

    def remove_dir(self, path: str) -> None:
        self._guard_write()
        cli = self._client()
        r = cli.delete(self.url_for(path))
        if r.status_code >= 400:
            raise SourceError(f"WebDAV DELETE {r.status_code}")


# ------------------------------------------------------------------ 工具

def _to_int(s: str) -> int:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return 0


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _parse_http_date(s: str) -> float:
    """解析 RFC1123 时间，避免依赖 email.utils 的本地化问题。"""
    if not s:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(s).timestamp()
    except Exception:
        return 0.0


def _iter_chunks(stream, progress=None, chunk: int = 1024 * 512):
    total = 0
    while True:
        data = stream.read(chunk)
        if not data:
            break
        total += len(data)
        if progress:
            progress(total)
        yield data
