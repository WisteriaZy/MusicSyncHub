"""数据源注册与实例管理。"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from .base import (Entry, MusicSource, ReadOnlyError, SourceError,  # noqa: F401
                   available_types, build_source, register)
# 导入即注册
from .local import LocalSource  # noqa: F401
from .sftp import SftpSource  # noqa: F401
from .webdav import WebDavSource  # noqa: F401
from .httpdir import HttpDirSource  # noqa: F401
from .smb import SmbSource  # noqa: F401
from .ftp import FtpSource  # noqa: F401


class SourceManager:
    """按 id 复用数据源实例（含连接池），配置变更时重置。"""

    def __init__(self):
        self._instances: Dict[str, MusicSource] = {}
        self._lock = threading.RLock()

    def get(self, sid: str, cfg=None) -> MusicSource:
        from ..core.config import get_secret, load_config, resolve_source

        cfg = cfg or load_config()
        sc = resolve_source(sid, cfg)
        with self._lock:
            inst = self._instances.get(sid)
            if inst is not None and getattr(inst, "_cfg_signature", None) == _sig(sc):
                return inst
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
            password = get_secret(sc.secret_ref, "") if sc.secret_ref else ""
            inst = build_source(sc, password)
            inst._cfg_signature = _sig(sc)  # type: ignore[attr-defined]
            self._instances[sid] = inst
            return inst

    def reset(self, sid: Optional[str] = None) -> None:
        with self._lock:
            keys = [sid] if sid else list(self._instances)
            for k in keys:
                inst = self._instances.pop(k, None)
                if inst is None:
                    continue
                try:
                    inst.close()
                except Exception:
                    pass

    def close_all(self) -> None:
        self.reset()


def _sig(sc) -> str:
    return f"{sc.type}|{sc.root}|{sc.host}|{sc.port}|{sc.username}|{sc.secret_ref}|{sc.base_url}|{sc.share}"


manager = SourceManager()
