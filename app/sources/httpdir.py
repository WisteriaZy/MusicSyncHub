"""HTTP 静态文件目录数据源（nginx / apache / caddy / python -m http.server 的目录索引）。

纯读取：适合把第三方 HTTP 音乐站点当"只读参考库"做对比与补齐。
支持 JSON 目录（nginx `autoindex on; autoindex_format json;`、Caddy browse 等）。
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse as up
from typing import Any, Callable, Dict, List, Optional

from .base import Entry, MusicSource, ReadOnlyError, SourceError, register
from ..core import paths as P

_HREF_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.I | re.S)
_SIZE_HINT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*([KMGTP])(?:i?B)?\b", re.I)
_SKIP = {"../", "..", "./", "/", "?", "#", ""}


@register
class HttpDirSource(MusicSource):
    kind = "http"
    label = "HTTP 目录（只读）"
    hint = ("一个能列出文件的 HTTP 目录（Apache/Nginx autoindex）。只能读，不能写")
    experimental = True
    verify_note = (
        "只做过代码实现，**没有**连过任何真实 HTTP 目录服务。各服务端 autoindex 的 HTML 格式差异较大，解析可能失效。")

    def __init__(self, cfg, password: str = ""):
        super().__init__(cfg, password)
        self.base_url = (cfg.base_url or cfg.root).rstrip("/")
        self._client_obj = None

    def _client(self):
        if self._client_obj is not None:
            return self._client_obj
        import httpx

        auth = (self.cfg.username, self.password or "") if self.cfg.username else None
        headers = {"User-Agent": "MusicSyncHub/1.0"}
        for k, v in (self.cfg.extras.get("headers") or {}).items():
            headers[str(k)] = str(v)
        self._client_obj = httpx.Client(
            auth=auth,
            verify=bool(self.cfg.extras.get("verify_tls", True)),
            headers=headers,
            timeout=float(self.cfg.extras.get("timeout", 45)),
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

    @property
    def can_write(self) -> bool:
        return False

    @property
    def can_delete(self) -> bool:
        return False

    # ------------------------------------------------------------ URL
    def url_for(self, path: str) -> str:
        ap = self.abs_path(path)
        rel = self.rel_path(ap)
        if rel == ap and ap.startswith("/"):
            rel = ap.lstrip("/")
        quoted = "/".join(up.quote(seg) for seg in rel.split("/") if seg and seg != ".")
        return self.base_url + ("/" + quoted if quoted else "")

    # ------------------------------------------------------------ 枚举
    def listdir(self, path: str) -> List[Entry]:
        cli = self._client()
        url = self.url_for(path)
        if not url.endswith("/"):
            url += "/"
        try:
            resp = cli.get(url)
        except Exception as exc:
            raise SourceError(f"HTTP 目录请求失败 {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} {url}")
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            return self._parse_json(resp.text, url, path)
        return self._parse_html(resp.text, url, path)

    def _parse_json(self, text: str, url: str, path: str) -> List[Entry]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceError(f"HTTP 目录 JSON 解析失败: {exc}") from exc
        items = data if isinstance(data, list) else data.get("items", [])
        out: List[Entry] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", ""))
            if not name or name in _SKIP:
                continue
            etype = str(it.get("type", "")).lower()
            is_dir = etype == "directory" or name.endswith("/")
            name = name.rstrip("/")
            mtime = 0.0
            mt = it.get("mtime") or it.get("modified") or ""
            if isinstance(mt, str) and mt:
                from email.utils import parsedate_to_datetime

                try:
                    mtime = parsedate_to_datetime(mt).timestamp()
                except Exception:
                    mtime = 0.0
            out.append(Entry(
                path=P.join(path or self.root, name),
                name=name,
                is_dir=is_dir,
                size=0 if is_dir else int(it.get("size") or 0),
                mtime=mtime,
            ))
        return out

    def _parse_html(self, text: str, url: str, path: str) -> List[Entry]:
        body = text
        m = _PRE_RE.search(text)
        if m:
            body = m.group(1)
        out: List[Entry] = []
        seen = set()
        for href, label in _HREF_RE.findall(body):
            raw = html.unescape(href).strip()
            if not raw or raw.startswith(("?", "#", "mailto:", "javascript:")):
                continue
            if raw.startswith("http://") or raw.startswith("https://"):
                # 绝对链接：只保留同目录下的
                pth = up.urlparse(raw).path
                if not pth.startswith(up.urlparse(url).path.rstrip("/")):
                    continue
                raw = pth.rsplit("/", 1)[-1]
            if raw in _SKIP:
                continue
            is_dir = raw.endswith("/")
            name = up.unquote(raw.rstrip("/")).split("/")[-1]
            if not name or name in seen:
                continue
            seen.add(name)
            label_txt = re.sub(r"<[^>]+>", " ", label or "")
            size = 0
            if not is_dir:
                mm = _SIZE_HINT.search(html.unescape(label_txt))
                if mm:
                    size = _to_bytes(float(mm.group(1).replace(",", ".")), mm.group(2))
            out.append(Entry(
                path=P.join(path or self.root, name),
                name=name,
                is_dir=is_dir,
                size=size,
            ))
        if not out:
            # 有些目录页没有 <pre>，直接全文扫
            for raw in re.findall(r"""href\s*=\s*["']([^"']+)["']""", text, re.I):
                raw = html.unescape(raw).strip()
                if raw in _SKIP or raw.endswith("/") is False and not raw:
                    continue
                name = up.unquote(raw.rstrip("/")).split("/")[-1]
                if not name or name in seen or name == P.basename(url.rstrip("/")):
                    continue
                seen.add(name)
                out.append(Entry(path=P.join(path or self.root, name), name=name,
                                 is_dir=raw.endswith("/")))
        return out

    def stat(self, path: str) -> Optional[Entry]:
        cli = self._client()
        url = self.url_for(path)
        try:
            resp = cli.head(url)
            if resp.status_code == 405:
                resp = cli.get(url, headers={"Range": "bytes=0-0"})
        except Exception:
            return None
        if resp.status_code >= 400:
            return None
        length = resp.headers.get("content-length")
        return Entry(
            path=self.abs_path(path),
            name=P.basename(path),
            is_dir=False,
            size=int(length) if length and length.isdigit() else 0,
            etag=(resp.headers.get("etag") or "").strip('"'),
        )

    # ------------------------------------------------------------ 读取
    def open_read(self, path: str, offset: int = 0, length: int = 0):
        cli = self._client()
        url = self.url_for(path)
        headers = {}
        if offset or length:
            headers["Range"] = f"bytes={offset}-{(offset + length - 1) if length else ''}"
        req = cli.build_request("GET", url, headers=headers)
        try:
            resp = cli.send(req, stream=True)
        except Exception as exc:
            raise SourceError(f"HTTP GET 失败 {url}: {exc}") from exc
        if resp.status_code >= 400:
            resp.close()
            raise SourceError(f"HTTP {resp.status_code} {url}")

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

            def close(self):
                try:
                    self._r.close()
                except Exception:
                    pass

        return _Stream(resp)

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        cli = self._client()
        url = self.url_for(path)
        headers = {"Range": f"bytes={offset}-{(offset + length - 1) if length else ''}"} if (offset or length) else {}
        resp = cli.get(url, headers=headers)
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} {url}")
        return resp.content

    # ------------------------------------------------------------ 写入（不支持）
    def mkdirs(self, path: str) -> None:
        raise ReadOnlyError("HTTP 目录源是只读的，不能作为写入目标")

    def write_file(self, path: str, data, progress=None, expected_size=None) -> None:
        raise ReadOnlyError("HTTP 目录源是只读的，不能作为写入目标")

    def replace(self, src_path: str, dst_path: str) -> None:
        raise ReadOnlyError("HTTP 目录源是只读的")

    def remove(self, path: str, hard: bool = False) -> str:
        raise ReadOnlyError("HTTP 目录源是只读的，不能删除文件")

    def remove_dir(self, path: str) -> None:
        raise ReadOnlyError("HTTP 目录源是只读的")


def _to_bytes(v: float, unit: str) -> int:
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
    return int(v * mult.get(unit.upper(), 1))
