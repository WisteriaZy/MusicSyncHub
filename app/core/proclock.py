"""跨进程独占锁：保证**同一时刻只有一个写流程**，跨进程也成立。

## 为什么内存锁不够

`core/jobs.py` 的独占位是一把 `threading.Lock` + 进程内字典。它只能管住
**同一个进程**。实际会出事的场景：

  · 用户开了两个服务实例（一个用默认端口、一个换了端口排查问题）
  · 打包成测试包后又跑了一份正常版
  · 计划任务/脚本在后台又起了一个

两个进程共享同一个 `~/.music-sync-hub` 和同一批数据源，各自的 `_jobs` 都是空的，
于是**都认为自己独占**，同时改同一批文件。实测后果：两边都报 `done`，
但目标只剩后写的那份（静默丢一个文件）。

## 实现

一个锁文件（`<data_dir>/msh-exclusive.lock`），用 `O_CREAT|O_EXCL` 原子创建：

    pid|<token>|<时间戳>

  · 创建成功 = 拿到锁，把 `token` 还给调用方（释放时要核对，免得删掉别人的锁）
  · 已存在 = 别人占着；如果文件**太旧**（默认 6 小时，超过任何单次搬运的合理时长）
    就认为是崩溃残留，删掉重试一次
  · 进程退出时 `atexit` 兜底释放

为什么用文件而不是命名管道/互斥体：跨平台、可观测（出问题能直接看到锁文件内容）、
不需要额外依赖。Windows 上 `os.open` 带 `O_EXCL` 同样是原子的。
"""
from __future__ import annotations

import atexit
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("msh.proclock")

LOCK_NAME = "msh-exclusive.lock"
# 超过这个时长还没释放 → 认为是崩溃残留（一次搬运再怎么慢也不该超过 6 小时）
STALE_SECONDS = 6 * 3600

_token: Optional[str] = None


class LockBusy(RuntimeError):
    """别的进程正占着写锁。"""

    def __init__(self, holder: str = "") -> None:
        self.holder = holder
        super().__init__(
            "另一个 Music Sync Hub 进程正在执行写操作"
            + (f"（{holder}）" if holder else "")
            + "。两个进程同时改同一批文件会丢数据 —— 请等它结束，"
              "或先关掉另一个窗口/实例。")


def lock_path() -> Path:
    from .config import data_dir
    return Path(data_dir()) / LOCK_NAME


def _read_holder(p: Path) -> str:
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return raw


def is_stale(p: Path) -> bool:
    try:
        return (time.time() - p.stat().st_mtime) > STALE_SECONDS
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    """这个进程还活着吗？（用于判定"锁的主人已死"）

    只按 mtime 判陈旧是不够的：进程被强杀之后锁文件还很新，
    下一个实例会被挡 6 小时。实测就踩到过（测试进程留下的锁 PID 早没了）。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:                                  # noqa: BLE001
            return True          # 拿不准就当活着，宁可保守
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def acquire(*, steal_stale: bool = True) -> str:
    """拿写锁。成功返回 token；被别的进程占着 → 抛 `LockBusy`。"""
    global _token
    if _token is not None:
        return _token                     # 本进程已经持有（可重入）
    p = lock_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    token = uuid.uuid4().hex[:12]
    body = f"{os.getpid()}|{token}|{int(time.time())}".encode("utf-8")
    for attempt in range(2):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 0 and steal_stale and is_stale(p):
                log.warning("写锁文件已过期（>%d 秒），按崩溃残留处理并接管", STALE_SECONDS)
                try:
                    p.unlink()
                except OSError:
                    pass
                continue
            _h = _read_holder(p)
            try:
                _pid = int(str(_h).split("|")[0])
            except Exception:                              # noqa: BLE001
                _pid = 0
            if _pid and not _pid_alive(_pid):
                # 主人已死 → 锁是残骸，直接接管
                try:
                    p.unlink()
                except OSError:
                    pass
                return acquire(steal_stale=steal_stale)
            raise LockBusy(_h) from None
        except OSError as exc:            # 目录不可写等：不阻塞用户，只记日志
            log.warning("无法创建写锁文件（%s）：%s；本次不做跨进程互斥", p, exc)
            _token = token
            return token
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        _token = token
        p.touch()
        return token
    raise LockBusy(_read_holder(p))


def release(token: Optional[str] = None) -> None:
    """释放写锁。只删**自己**的锁文件。"""
    global _token
    tok = token or _token
    _token = None
    p = lock_path()
    if not p.exists():
        return
    # ★ 没持锁就**什么都不做**。
    #   原来这里写的是 `if tok and tok not in holder: return` —— tok 为 None 时
    #   整个条件为假，于是继续往下 unlink，把**别人的锁删掉**。
    #   触发场景很真实：进程重启后「恢复未完成任务」，`Jobs.finish()` 会调
    #   `proclock.release()`；此时本进程并没有拿到锁，却把另一个正在运行的
    #   实例的锁解除了 → 两个进程同时写文件。
    if not tok:
        return
    holder = _read_holder(p)
    # 核对 token，避免把别人（比如接管后另一个进程）的锁删掉
    if tok not in holder:
        return
    try:
        p.unlink()
    except OSError:
        pass


def held_by_us() -> bool:
    return _token is not None


@atexit.register
def _release_at_exit() -> None:
    try:
        if _token is not None:
            release(_token)
    except Exception:                     # noqa: BLE001
        pass
