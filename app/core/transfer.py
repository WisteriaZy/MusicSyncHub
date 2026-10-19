"""跨库传输：把一个数据源里的文件搬到**另一个**数据源。

## 为什么单独做一层，而不是让调用方自己拼 copy + delete

`Executor` 已经能跨源复制（`dst.copy_from(src, ...)`），也能删文件，
所以"跨库移动"理论上等于 `copy` 之后再 `trash` 源。但两处细节让它不能
简单拼装：

1. **顺序错了会丢文件**。先删后拷、或者拷完不校验就删，一旦中途失败
   就是"源没了、目标也没有"。`ops.py` 的注释里也写着"跨源搬运请用
   copy（复制成功后再单独删源）" —— 也就是**靠人去记得分两步做**。
   这层把"复制 → 校验 → 再删源"固化成一个不可分割的操作。

2. **回滚要能说清**。万一目标写了一半，必须明确告诉用户"源还在，
   目标那份不完整，要不要清掉"，而不是留一个含糊的失败。

## 安全约定（和项目其它地方一致）

  · 目标是**先写临时文件再改名**（由各数据源的 `write_file` 保证），
    所以中断不会在目标留下"半截但看着像完整"的文件。
  · 删除源走 `trash`（回收站）而不是真删 —— 除非显式 `hard_delete`。
  · 校验默认按**大小**（便宜且够用）；`verify_hash=True` 时再比内容哈希。
  · 目标已存在时按 `policy`（skip / rename / overwrite）处理，默认 skip。
  · 任何一步失败 → **源不动**，如实报告卡在哪一步。
"""
from __future__ import annotations

import logging

import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import paths as P

log = logging.getLogger("msh.transfer")


class _Heartbeat:
    """搬运途中替我们按心跳的看门狗。

    为什么需要：`copy_from` 有可能是 `shutil.copyfile`（本地→本地），
    它**中途不给任何回调** —— 一个 6 GB 的文件拷到 U 盘要 ~300 秒，
    这段时间里 `job.heartbeat` 一次都不动。`STALE_JOB_SECONDS` 默认 180 秒，
    于是任务会被 `_reap_stale_locked` 判成"心跳中断"：
    **标成 error 并释放独占位，而复制其实还在跑** ——
    之后任何写操作都会被放进来，两边同时改文件。

    所以起一个小线程，每隔几秒 `touch()` 一次；搬运结束就停。
    出错一律吞掉：心跳失败绝不能影响搬运本身。
    """

    def __init__(self, job, interval: float = 20.0):
        self._job = job
        self._interval = max(5.0, float(interval))
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def __enter__(self):
        if self._job is None:
            return self
        def run():
            while not self._stop.wait(self._interval):
                try:
                    self._job.touch()
                except Exception:
                    return
        self._t = threading.Thread(target=run, name="msh-heartbeat", daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False


# ---------------------------------------------------------------- 传输模式
#
# 用户要求"每个库可选标准/快速传输"（有点像 U 盘的"快速删除"）。
# 两种模式的差别**只有一个**：复制完要不要先量一下大小再删源。
#
#   standard（默认）—— 复制 → **校验大小一致** → 才删源。
#                      慢一点，但"目标写歪了"能被发现并中止。
#   fast            —— 复制 → 校验（默认关）→ 删源。少一次 stat 往返，
#                      网络盘 / U 盘上批量搬运能省下可观的往返开销。
#
# ★ 两种模式下**源都进回收站**（不是永久删除），所以"快速"最坏也只是
#   "目标那份可能不完整，但源还在回收站里"，随时能拿回来。
#   这一点是"快速模式可以存在"的前提 —— 如果它会真删源，就不该提供。
VALID_MODES = ("standard", "fast")


def mode_of(src, explicit: str = "") -> str:
    """这次搬运用什么模式。

    优先级：请求里显式指定的 > 数据源自己的设置 > standard。
    数据源设置放在 `cfg.extras["transfer_mode"]`（跟着数据源走，
    因为它描述的是"这块盘/这个网络上适合怎么传"）。
    """
    m = str(explicit or "").strip().lower()
    if m in VALID_MODES:
        return m
    try:
        ex = getattr(getattr(src, "cfg", None), "extras", None) or {}
        m2 = str(ex.get("transfer_mode") or "").strip().lower()
        if m2 in VALID_MODES:
            return m2
    except Exception:
        pass
    return "standard"


def _err(msg: str, kind: str = "") -> Dict[str, Any]:
    out = {"state": "error", "detail": msg}
    if kind:
        out["error_kind"] = kind
    return out


def _dedupe_name(src, path: str) -> str:
    """目标重名时找一个不冲突的名字（`a.flac` → `a (2).flac`）。"""
    stem, ext = os.path.splitext(path)
    for i in range(2, 200):
        cand = f"{stem} ({i}){ext}"
        if not src.exists(cand):
            return cand
    return f"{stem} ({os.getpid()}){ext}"


def _quarantine(src, path: str) -> str:
    """把一份**不可信**的目标文件改成 `.bad` 名字保留，而不是直接删掉。

    为什么不删：删掉是不可逆的，而"半截文件"至少还能让用户判断
    （比如从别处再拷一份时对比大小）。也不留在正式名下 —— 否则
    它会被当成一首正常的歌入库。

    ★ 用 uuid 替代时间戳避免同一秒内多文件校验失败时的名字冲突
    """
    import uuid
    tgt = f"{path}.bad-{uuid.uuid4().hex[:8]}"
    try:
        src.move(path, tgt)
        return tgt
    except Exception:                                     # noqa: BLE001
        try:
            src.remove(path, hard=True)
            return ""
        except Exception:                                 # noqa: BLE001
            return ""


def transfer_one(src, dst, src_path: str, dst_path: str, *,
                 policy: str = "skip",
                 verify: Optional[bool] = None,
                 verify_hash: bool = False,
                 hard_delete: bool = False,
                 job=None,
                 mode: str = "") -> Dict[str, Any]:
    """把 `src` 上的 `src_path` 搬到 `dst` 上的 `dst_path`。

    返回 `{state, detail, ...}`；`state` 取值与 `Executor` 保持一致：
    `done` / `skipped` / `conflict` / `blocked` / `error`。

    `mode`：`standard`（默认，复制后校验大小再删源）或 `fast`
    （跳过校验，少一次往返）。留空则按源数据源的 `extras.transfer_mode`。
    **两种模式下源都进回收站**，所以 fast 最坏也只是"目标可能不完整、
    但源还在回收站"，可恢复。

    `verify`：**默认 `None` = 跟着 mode 走**。
    显式传 `True`/`False` 就是更强的意图，会盖过 mode ——
    "快速模式"只是默认省一步，不该让调用方**没法**要求校验。

    **调用方必须保证 `src is not dst`** —— 同源移动走 `_do_move`
    （原子改名，更快更安全）。
    """
    if src is None or dst is None:
        return _err("数据源不存在")
    if src is dst or (getattr(src, "cfg", None) is not None
                      and getattr(dst, "cfg", None) is not None
                      and src.cfg.id == dst.cfg.id):
        return _err("源和目标在同一个数据源里，请用「移动到…」而不是跨库传输")

    if job is not None:
        try:
            job.touch()
        except Exception as exc:
            # ★ 不能静默。心跳续不上的后果不是"少记一笔账"：
            #   超过 STALE_JOB_SECONDS 没心跳，任务会被判成僵尸、**独占位被释放**，
            #   于是别的写流程就能进来改同一批文件 —— 而这次搬运还在进行中。
            #   打日志至少能在事后解释"为什么两个任务撞在了一起"。
            log.warning("搬运心跳续期失败（可能导致独占位被误回收）: %s", exc)

    # ---- 权限：源要能删、目标要能写 ----
    if not dst.can_write:
        return {"state": "blocked", "error_kind": "readonly",
                "detail": f"目标数据源「{dst.cfg.name}」是只读的"}
    if not src.can_delete:
        return {"state": "blocked", "error_kind": "readonly",
                "detail": f"源数据源「{src.cfg.name}」不允许删除，"
                          f"无法完成「复制后删源」的传输（可以只做复制）"}

    # ★ 源也必须在自己的根内。否则客户端给一个库外绝对路径，
    #   就能把库外文件复制出去、再把库外文件删掉。
    if not P.inside_root(src.root, src.abs_path(src_path)):
        return _err("源路径超出源数据源根目录，已拒绝传输")
    src_stat = src.stat(src_path)
    if src_stat is None:
        return _err("源文件不存在（可能已经被移动或删除）")
    if src_stat.is_dir:
        return _err("源路径是目录；跨库传输目前只支持单个文件")

    # ---- 目标落点 ----
    dst_path = dst.abs_path(dst_path)
    # ★ 纵深防御：入口层（`/api/files/transfer`）已经校验过，但
    #   `transfer_one` 也可能被别的调用方直接用，这里再判一次。
    #   判定统一走 `paths.inside_root`（含 `..` 与 Windows 大小写处理）。
    if not P.inside_root(dst.root, dst_path):
        return _err("目标路径超出目标数据源根目录，已拒绝传输")
    _had_old = dst.exists(dst_path)
    if _had_old:
        if policy == "skip":
            return {"state": "conflict", "error_kind": "conflict",
                    "detail": "目标已存在同名文件，已跳过（源**没有**被动过）",
                    "target": dst_path}
        if policy == "rename":
            dst_path = _dedupe_name(dst, dst_path)
        if policy == "overwrite":
            # ★ 覆盖前先把**原件移进回收站**（可恢复），再写新的。
            #   以前是直接覆盖：一旦复制中断/校验失败，原件和副本**都没了**
            #   （复制把原件写坏，校验失败又把目标删掉）。
            #   现在最坏情况是"原件躺在回收站里"，用户能还原。
            try:
                dst.remove(dst_path, hard=False)
            except Exception as exc:                          # noqa: BLE001
                return _err(f"覆盖前备份原件失败：{type(exc).__name__}: {exc}", "copy")
        # overwrite：原件已进回收站，下面正常写新文件

    # ---- 1) 复制（先写临时文件再改名，由 write_file 保证）----
    before = dst.stat(dst_path)
    try:
        # 搬运途中持续按心跳 —— 见 `_Heartbeat` 的说明：
        # 单文件复制可能远超 `STALE_JOB_SECONDS`，不按心跳会被误判中断
        # 并释放独占位，而复制还在跑。
        with _Heartbeat(job):
            info = dst.copy_from(src, src_path, dst_path, progress=None)
    except Exception as exc:                                      # noqa: BLE001
        return _err(f"复制失败：{type(exc).__name__}: {exc}", "copy")

    _resumed = int((info or {}).get("resumed_from") or 0)
    out: Dict[str, Any] = {
        "state": "done", "detail": "已传输",
        "target": dst_path,
        "bytes": (info or {}).get("size") or src_stat.size,
        "overwrote": bool(before) or _had_old,
        "resumed_from": _resumed,
        "steps_done": ["copy"],
    }
    if _resumed:
        # 让用户知道这一段是"接着上次的"而不是从头传的
        out["detail"] = f"已传输（断点续传：从 {_resumed} 字节处继续）"

    # ---- 2) 校验：**没验过就不许删源** ----
    #
    #   `verify=None`（默认）→ 跟着 mode：standard 校验、fast 跳过。
    #   `verify=True/False`（调用方显式给的）→ 听调用方的，mode 让位。
    _mode = mode_of(src, mode)
    out["mode"] = _mode
    if verify is None:
        do_verify = (_mode != "fast")
    else:
        do_verify = bool(verify)
    if not do_verify:
        out["verified"] = False
        if hard_delete:
            # ★ 没校验就**永久**删源 —— 一旦复制是短写，源没了、目标也是坏的。
            #   这条组合必须硬拒：要永久删就必须先校验。
            return {**out, "state": "error",
                    "detail": ("永久删除（hard_delete）必须先校验；"
                               "当前是快速模式或显式 verify=false，已拒绝。"
                               "请改成 verify=true，或让源进回收站（默认）"),
                    "error_kind": "verify", "steps_done": ["copy"]}
    if do_verify:
        got = dst.stat(dst_path)
        if got is None:
            return {**out, "state": "error",
                    "detail": "复制后目标文件不见了 —— 源**未**删除，请重试",
                    "error_kind": "verify", "steps_done": ["copy"]}
        if src_stat.size and got.size and int(got.size) != int(src_stat.size):
            _bad = _quarantine(dst, dst_path)
            return {**out, "state": "error",
                    "detail": (f"大小不一致（源 {src_stat.size} / "
                               f"目标 {got.size}），源**未**删除；"
                               f"不完整的那份已改名保留：{_bad or '（改名失败）'}"),
                    "error_kind": "verify", "steps_done": ["copy"]}
        # ★ 断点续传必须强制全量哈希：中断常伴随尾部/中段脏块，只比大小
        #   会把坏文件当成功，然后删源 —— 源没了、目标还是坏的。
        _resumed_bytes = int((info or {}).get("resumed_from") or 0)
        if bool(verify_hash) or _resumed_bytes > 0:
            try:
                a = _hash_of(src, src_path)
                b = _hash_of(dst, dst_path)
                if a and b and a != b:
                    _bad = _quarantine(dst, dst_path)
                    return {**out, "state": "error",
                            "detail": (f"内容哈希不一致，源**未**删除；"
                                       f"不完整的那份已改名保留：{_bad or '（改名失败）'}"),
                            "error_kind": "verify", "steps_done": ["copy"]}
            except Exception as exc:                              # noqa: BLE001
                return {**out, "state": "error",
                        "detail": f"校验哈希时出错：{exc}（源**未**删除）",
                        "error_kind": "verify", "steps_done": ["copy"]}
        out["steps_done"].append("verify")

    if job is not None:
        try:
            job.touch()
        except Exception as exc:
            # ★ 不能静默。心跳续不上的后果不是"少记一笔账"：
            #   超过 STALE_JOB_SECONDS 没心跳，任务会被判成僵尸、**独占位被释放**，
            #   于是别的写流程就能进来改同一批文件 —— 而这次搬运还在进行中。
            #   打日志至少能在事后解释"为什么两个任务撞在了一起"。
            log.warning("搬运心跳续期失败（可能导致独占位被误回收）: %s", exc)

    # ---- 3) 删源（进回收站；只有显式 hard_delete 才真删）----
    try:
        trashed = src.remove(src_path, hard=hard_delete)
        out["trashed"] = trashed
        out["steps_done"].append("delete-src")
        _tail = "（源已永久删除）" if hard_delete else "（源已移入回收站）"
        if not do_verify:
            # 没校验就如实说，别让用户以为验过了
            _tail += ("｜快速模式：未校验大小"
                      if _mode == "fast" else "｜未校验大小")
        _r = ""
        if out.get("resumed_from"):
            _r = f"（断点续传：从 {out['resumed_from']} 字节处继续）"
        out["detail"] = "已传输" + _r + _tail
    except Exception as exc:                                      # noqa: BLE001
        # 这一步失败**不能**回滚目标 —— 目标那份是好的，
        # 回滚反而会把它删掉。如实报告：文件现在两边都有。
        return {**out, "state": "error",
                "detail": (f"文件已复制到目标，但删除源失败：{exc}。"
                           f"现在**两边都有**这个文件，请手动删掉源那份"),
                "error_kind": "delete-src"}
    return out


def _hash_of(src, path: str, limit: int = 0) -> str:
    """取文件哈希（走数据源自己的读接口，网络源也能用）。"""
    import hashlib
    h = hashlib.sha1()
    fh = src.open_read(path)
    try:
        n = 0
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
            if limit and n >= limit:
                break
    finally:
        try:
            fh.close()
        except Exception:
            pass
    return h.hexdigest()


def plan_transfer(src_id: str, dst_id: str, items: List[str],
                  dst_dir: str = "") -> List[Dict[str, Any]]:
    """把"要传哪些文件 + 落到哪"翻译成执行器认识的步骤。

    `items` 是源数据源内部的**绝对路径**；`dst_dir` 是目标数据源内部的
    目录（空 = 目标根目录）。文件名保持不变 —— 跨库搬运不该顺手改名。
    """
    steps: List[Dict[str, Any]] = []
    for i, sp in enumerate(items):
        name = P.basename(sp)
        rel = P.join(dst_dir, name) if dst_dir else name
        steps.append({
            "id": f"xfer-{i}",
            "op": "transfer",
            "src": src_id, "src_path": sp,
            "dst": dst_id, "dst_path": rel,
            "size": 0,
        })
    return steps


__all__ = ["transfer_one", "plan_transfer"]
