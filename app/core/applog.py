"""后台日志：写到文件 + 留一份内存里的最近记录。

## 为什么需要

以前整个后端只有 `print()`（启动信息）和一处 `log.warning`。
出问题时用户在控制台里翻不到东西：启动窗口一关就没了，
而且真正想知道的是"刚才那个操作为什么失败" —— 那要靠**异常栈**，
而异常被 FastAPI 吃掉之后只在控制台出现一次，关掉窗口就消失。

所以这里做两件事：

1. **落文件**（带轮转）：`<数据目录>/logs/msh.log`，单个 4 MB、留 3 个。
   出问题可以让用户直接把文件发过来。
2. **内存环形缓冲**：最近 500 条，供 `/api/logs` 即时查看，
   不用去翻文件。条数写死在这个量级 —— 再多也只是占内存。

## 设计上的两个克制

* **不动请求处理路径**：只注册 handler，不加中间件。
  每个请求都写一条日志的话，日志会被刷屏，真正有用的那几行反而被埋掉。
  所以只记"错误 + 我们自己主动记的关键事件"。
* **不抛异常**：日志本身出问题（目录不可写、磁盘满）绝不能影响主流程，
  所有 handler 都吞掉自身错误。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# 最近多少条留在内存里（供 /api/logs）
MEMORY_LINES = 500
# 单个日志文件多大轮转 / 留几个
MAX_BYTES = 4 * 1024 * 1024
BACKUP_COUNT = 3

_ring: Deque[Dict[str, Any]] = deque(maxlen=MEMORY_LINES)
_ring_lock = threading.Lock()
_configured = False
_log_path: Optional[str] = None


class _RingHandler(logging.Handler):
    """把日志同时留一份在内存里，供界面即时查看。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {
                "t": round(record.created, 3),
                "level": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                item["exc"] = self.format(record).split("\n", 1)[-1][:4000]
            with _ring_lock:
                _ring.append(item)
        except Exception:
            pass


def setup(data_dir, level: int = logging.INFO) -> Optional[str]:
    """装好日志。返回日志文件路径（失败时返回 None，绝不影响启动）。

    可重复调用（幂等）—— 数据目录换了之后可以再调一次。
    """
    global _configured, _log_path
    try:
        d = os.path.join(str(data_dir), "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "msh.log")
    except Exception:
        return None

    root = logging.getLogger("msh")
    root.setLevel(level)
    # 幂等：清掉自己上次装的 handler，避免重复输出
    for h in list(root.handlers):
        if getattr(h, "_msh", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._msh = True          # type: ignore[attr-defined]
        root.addHandler(fh)
    except Exception:
        pass

    rh = _RingHandler()
    rh.setFormatter(fmt)
    rh._msh = True              # type: ignore[attr-defined]
    root.addHandler(rh)

    # 让库内部的 logger 也走同一套（uvicorn/httpx 太吵，不动它们）
    for name in ("msh.jobs", "msh.ops", "msh.transfer", "msh.scanner"):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.propagate = True

    _log_path = path
    _configured = True
    return path


def log_path() -> Optional[str]:
    return _log_path


def recent(limit: int = 200, level: str = "") -> List[Dict[str, Any]]:
    """内存里的最近若干条（新的在后）。`level` 传 ERROR/WARNING 可过滤。"""
    try:
        n = max(1, min(int(limit or 200), MEMORY_LINES))
    except (TypeError, ValueError):
        n = 200
    with _ring_lock:
        items = list(_ring)
    if level:
        want = level.upper()
        items = [x for x in items if str(x.get("level")) == want]
    return items[-n:]


def read_file(lines: int = 400, level: str = "") -> Dict[str, Any]:
    """读日志文件尾部（界面"查看完整日志"用）。"""
    out: Dict[str, Any] = {"path": _log_path, "lines": [], "size": 0}
    if not _log_path or not os.path.isfile(_log_path):
        return out
    try:
        out["size"] = os.path.getsize(_log_path)
        try:
            n = max(1, min(int(lines or 400), 5000))
        except (TypeError, ValueError):
            n = 400
        with open(_log_path, "r", encoding="utf-8", errors="replace") as fh:
            buf = deque(fh, maxlen=n)
        items = [x.rstrip("\n") for x in buf]
        if level:
            want = level.upper()
            items = [x for x in items if f" {want} " in x or f" {want:<7} " in x]
        out["lines"] = items
    except Exception as exc:                                  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def clear() -> None:
    """清空内存缓冲（不动文件）—— 便于"我只想看接下来的日志"。"""
    with _ring_lock:
        _ring.clear()
