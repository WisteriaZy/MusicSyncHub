"""数据源连接器。

每个音乐库位置（本地目录 / SFTP / WebDAV / HTTP 目录 / SMB）实现同一套接口，
上层扫描器、对比器、执行器只依赖这套接口，因此添加新协议很轻松。

设计要点：
* `listdir` 只做一次目录枚举，返回轻量的 Entry，元数据解析交给扫描器；
* `read_range` 支持只读文件头部（大文件解析标签时避免全量下载）；
* 删除一律走 `remove`，实现里默认是"移动到回收站目录"，而不是真删。
"""
from __future__ import annotations

import fnmatch
import io
import json
import os
import posixpath
import threading
import time
from dataclasses import dataclass, field
from typing import Union, Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..core import paths as P


@dataclass
class Entry:
    path: str
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    etag: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class SourceError(RuntimeError):
    pass


class ReadOnlyError(SourceError):
    pass


class MusicSource:
    """所有数据源的基类。"""

    kind = "base"
    # 给界面用的显示名与提示（`available_types()` 从这里读，
    # 所以新增一种源只要实现类 + `@register`，界面下拉自动就有了）
    label = "未命名数据源"
    hint = ""
    # 实验性：**没有对着真实服务器完整验证过**。界面上会明确标注。
    # 判定标准要从严 —— 只是"代码写完了"不算验证过。
    experimental = False
    # 可选：一句话讲清"没验证到什么程度"，界面会显示在警告里
    verify_note = ""

    def __init__(self, cfg, password: str = ""):
        self.cfg = cfg
        self.password = password
        self.root = P.norm(cfg.root)
        self._lock = threading.RLock()
        self._clients: Dict[str, Any] = {}

    # ------------------------------------------------------------ 生命周期
    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------ 能力
    @property
    def can_write(self) -> bool:
        return not getattr(self.cfg, "read_only", False)

    @property
    def can_delete(self) -> bool:
        return self.can_write

    @property
    def supports_range_read(self) -> bool:
        return True

    def _guard_write(self) -> None:
        if not self.can_write:
            raise ReadOnlyError(f"数据源 {self.cfg.name} 被标记为只读，已拒绝写操作")

    # ------------------------------------------------------------ 设备特性
    def device_info(self, measure: bool = False) -> Dict[str, Any]:
        """这个数据源所在存储设备的性质。

        基类返回"未知"，**只有本地源会真的探测** —— 远端源（SFTP/WebDAV/
        SMB）的"慢"和"可移除"是另一回事，不能套用本地盘那套判断。

        `measure=False` 时不做读速测试（默认）—— 这个方法会被频繁调用
        （每次计划/执行都要问一次），测速太贵。
        """
        return {"kind": "unknown", "removable": False, "network": False,
                "slow": bool(getattr(self.cfg, "extras", {}).get("slow_disk")),
                "read_mbps": None, "label": "", "notes": []}

    @property
    def is_slow(self) -> bool:
        """慢速盘（含用户手动标记）。"""
        if bool(getattr(self.cfg, "extras", {}).get("slow_disk")):
            return True
        try:
            return bool(self.device_info().get("slow"))
        except Exception:
            return False

    @property
    def is_removable(self) -> bool:
        """可移除存储（U 盘 / 移动硬盘 / SD 卡）。

        基类保守返回 False：远端源即使"看起来可移除"也不该套用
        "拔盘可能留半截文件"那套逻辑（它的失败模式完全不同）。
        """
        return False

    # ------------------------------------------------------------ 路径
    def abs_path(self, path: str) -> str:
        path = P.norm(path)
        if not self.root:
            return path
        if path == self.root or path.startswith(self.root.rstrip("/") + "/"):
            return path
        if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
            # ★ 已经是绝对路径，但**不在 root 内**。
            #   原来无条件 `P.join(root, path.lstrip("/"))`，于是
            #   root=C:/Music、path=D:/Other/x.mp3 会拼成
            #   `C:/Music/D:/Other/x.mp3`（路径中间带冒号）→ WinError 123 崩溃。
            #   现在原样返回：让上层的 `paths.inside_root` 明确拒绝，
            #   报"超出数据源根目录"而不是抛系统异常。
            if len(path) > 1 and path[1] == ":":
                drv = path[0].lower()
                if not self.root or self.root[0].lower() != drv:
                    return path
            elif self.root and (len(self.root) > 1 and self.root[1] == ":"):
                # root 是盘符路径、path 是 POSIX 绝对路径 → 同样不该硬拼
                return path
            return P.join(self.root, path.lstrip("/"))
        return P.join(self.root, path)

    def rel_path(self, path: str) -> str:
        p = P.norm(path)
        r = self.root.rstrip("/")
        if r and p.startswith(r + "/"):
            return p[len(r) + 1:]
        return p

    def stat(self, path: str) -> Optional[Entry]:
        raise NotImplementedError

    def listdir(self, path: str) -> List[Entry]:
        raise NotImplementedError

    def walk(self, path: str = "", on_dir: Optional[Callable[[str, List[Entry]], None]] = None,
             should_descend: Optional[Callable[[str], bool]] = None) -> Iterable[Entry]:
        """深度优先遍历，按需回调。`path` 为空表示从根开始。"""
        start = self.abs_path(path) if path else self.root
        stack = [start]
        seen_dirs = set()
        while stack:
            cur = stack.pop()
            if cur in seen_dirs:
                continue
            seen_dirs.add(cur)
            try:
                entries = self.listdir(cur)
            except SourceError:
                if cur == start:
                    raise
                continue
            except Exception:
                continue
            if on_dir:
                on_dir(cur, entries)
            for e in entries:
                if e.is_dir:
                    if should_descend and not should_descend(e.path):
                        continue
                    stack.append(e.path)
                else:
                    yield e

    # ------------------------------------------------------------ 读取
    def open_read(self, path: str, offset: int = 0, length: int = 0):
        """返回一个可读的二进制流（offset/length 为 0 表示全量）。"""
        raise NotImplementedError

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        fh = self.open_read(path, offset, length)
        try:
            if hasattr(fh, "_msh_closer"):
                data = fh.read(length) if length else fh.read()
                return data
            return fh.read(length) if length else fh.read()
        finally:
            try:
                fh.close()
            except Exception:
                pass

    def read_head(self, path: str, nbytes: int) -> bytes:
        return self.read_range(path, 0, nbytes)

    # ------------------------------------------------------------ 写入
    def mkdirs(self, path: str) -> None:
        raise NotImplementedError

    def write_file(self, path: str, data: Union[bytes, Iterable[bytes]],
                   progress: Optional[Callable[[int], None]] = None,
                   expected_size: Optional[int] = None) -> None:
        raise NotImplementedError

    # ---------------------------------------------------------- 断点续传
    #
    # 为什么要做：跨盘/跨网络搬一个大文件（几十 GB 的母带、整张专辑镜像）
    # 中途断了（网络抖动、U 盘被拔、笔记本休眠）以前**必须从头再来**。
    # `.mshpart` 只保证"不污染库"，不保证"省时间"。
    #
    # 安全前提（很重要）：回收站/临时文件是**按目标名**放的，如果上一次
    # 中断的是**另一个源文件**，直接接着写就会拼出一个内容错乱的文件。
    # 所以临时文件旁边放一份 `_meta`：
    #     {src_id, src_path, src_size, ts}
    # 只有三者都和**当前这次复制**一致才续传，否则从头写。
    # 续传完成后再校验总大小；不一致就丢弃临时文件（下次从头来）。

    PART_SUFFIX = ".mshpart"

    def _part_paths(self, dst_path: str) -> tuple:
        part = dst_path + self.PART_SUFFIX
        return part, part + ".json"

    def _part_size(self, part: str) -> int:
        try:
            st = self.stat(part)
        except Exception:                                  # noqa: BLE001
            return 0
        if st is None or st.is_dir:
            return 0
        return int(st.size or 0)

    def _read_part_meta(self, meta: str) -> Dict[str, Any]:
        try:
            fh = self.open_read(meta)
            try:
                raw = fh.read(64 * 1024)
            finally:
                try:
                    fh.close()
                except Exception:                          # noqa: BLE001
                    pass
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:                                  # noqa: BLE001
            return {}

    def _write_part_meta(self, meta: str, info: Dict[str, Any]) -> None:
        try:
            self.write_file(meta, json.dumps(info, ensure_ascii=False).encode("utf-8"))
        except Exception:                                  # noqa: BLE001
            pass

    def _drop_part(self, dst_path: str, keep_meta: bool = False) -> None:
        part, meta = self._part_paths(dst_path)
        for p in ((part,) if keep_meta else (part, meta)):
            try:
                self.remove(p, hard=True)
            except Exception:                              # noqa: BLE001
                pass

    def _resume_offset(self, src: "MusicSource", src_path: str,
                       dst_path: str, src_size: Optional[int],
                       src_mtime: Optional[float] = None) -> int:
        """能安全续传就返回"已经写好的字节数"，否则 0。"""
        if not src_size or not getattr(src, "supports_range_read", True):
            return 0
        part, meta = self._part_paths(dst_path)
        have = self._part_size(part)
        if have <= 0 or have >= src_size:
            return 0
        info = self._read_part_meta(meta)
        if not info:
            return 0
        try:
            same = (str(info.get("src_id")) == str(getattr(src.cfg, "id", ""))
                    and str(info.get("src_path")) == str(src_path)
                    and int(info.get("src_size") or 0) == int(src_size))
            # ★ 源文件在中断期间被改过（换标签 / 换成另一首同样大小的曲子）
            #   时光比大小是不够的：后半段会接在旧内容上，拼出损坏音频。
            #   记录并比对修改时间；老 meta 没有这个字段时按"能续"处理（向后兼容）。
            if same and src_mtime is not None and info.get("src_mtime") is not None:
                if abs(float(info["src_mtime"]) - float(src_mtime)) > 1e-6:
                    same = False
        except (TypeError, ValueError):
            return 0
        return have if same else 0

    def append_file(self, path: str, data: Union[bytes, Iterable[bytes]],
                    progress: Optional[Callable[[int], None]] = None) -> None:
        """把数据**追加**到文件末尾。默认不支持（子类实现）。"""
        raise NotImplementedError("这个数据源不支持追加写入，无法续传")

    def copy_from(self, src: "MusicSource", src_path: str, dst_path: str,
                  progress: Optional[Callable[[int, Optional[int]], None]] = None,
                  verify_hash: Optional[str] = None) -> Dict[str, Any]:
        """从另一个数据源复制过来。子类可覆盖以加速（例如本地到本地、同主机 SFTP）。

        支持**断点续传**：临时文件 + 元数据都对得上时，从已完成的字节继续写。
        """
        self._guard_write()
        st = src.stat(src_path)
        if st is None:
            raise SourceError(f"源文件不存在: {src_path}")
        total = st.size
        self.mkdirs(P.parent(dst_path))
        part, meta = self._part_paths(dst_path)
        src_mtime = getattr(st, "mtime", None)
        offset = self._resume_offset(src, src_path, dst_path, total, src_mtime)
        # ★★ 元数据必须在**开始写数据之前**落盘。★★
        #
        #   原来是在整份复制成功之后才写 —— 真实中断（拔盘 / 掉网 / 杀进程）
        #   发生在复制途中，执行流根本走不到那行，于是磁盘上只有半截
        #   `.mshpart`、没有 `.mshpart.json`；下次重试读不到 meta，
        #   `_resume_offset` 返回 0，直接 `"wb"` 清空重来。
        #   换句话说：**续传在现实中永远不会生效**，只有测试里人工预置 meta 才过。
        if not offset:
            self._write_part_meta(meta, {
                "src_id": getattr(src.cfg, "id", ""),
                "src_path": str(src_path),
                "src_size": total,
                "src_mtime": src_mtime,
                "ts": time.time(),
            })
        try:
            if offset:
                try:
                    stream = src.open_read(src_path, offset)
                except TypeError:                          # 适配器不支持 offset
                    stream = src.open_read(src_path)
                    offset = 0
                if offset:
                    try:
                        self.append_file(
                            part, stream,
                            progress=(lambda n: progress and progress(offset + n, total))
                            if progress else None)
                    finally:
                        try:
                            stream.close()
                        except Exception:                  # noqa: BLE001
                            pass
                else:
                    stream = None
            if not offset:
                stream = src.open_read(src_path)
                try:
                    self.write_file(part, stream,
                                    progress=(lambda n: progress and progress(n, total))
                                    if progress else None)
                finally:
                    try:
                        stream.close()
                    except Exception:                      # noqa: BLE001
                        pass
            # 续传可能接错（比如临时文件是别的源留下的）—— 用总大小兜底
            got = self._part_size(part)
            if total and got != total:
                self._drop_part(dst_path)
                raise SourceError(
                    f"复制后大小不一致（源 {total} / 临时 {got}），已丢弃临时文件，下次会重新复制")
            self.replace(part, dst_path)
            self._drop_part(dst_path, keep_meta=False)
        except Exception:
            # ★ 失败时**保留** part + meta（下次能续传）。
            #   过期残留由启动时的 `cleanup_stale_parts` 清（>24h）。
            raise
        st2 = self.stat(dst_path)
        return {"size": st2.size if st2 else 0, "verified": None,
                "resumed_from": offset}

    def replace(self, src_path: str, dst_path: str) -> None:
        """把同目录下的临时文件改名为最终文件。"""
        raise NotImplementedError

    def move(self, src_path: str, dst_path: str) -> None:
        """同源内移动文件（换目录）。

        默认实现退化成"改名"：同一存储上的原子操作，失败时源文件原样保留。
        跨设备的源（不同挂载点、SFTP 换目录等）应覆盖此方法，自己保证
        "先确认目标落盘、再删源文件"，绝不允许先删源。
        """
        self._guard_write()
        self.replace(src_path, dst_path)

    def remove(self, path: str, hard: bool = False) -> str:
        """删除文件。默认移动到回收站目录，返回"移到哪里了"。"""
        raise NotImplementedError

    def remove_dir(self, path: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------ 便利
    def exists(self, path: str) -> bool:
        try:
            return self.stat(path) is not None
        except Exception:
            return False

    def display(self, path: str) -> str:
        return P.display_path(path, "\\" if self.kind == "local" and os.name == "nt" else "/")


# ------------------------------------------------------------------ 注册表

_REGISTRY: Dict[str, type] = {}


def register(cls: type) -> type:
    _REGISTRY[cls.kind] = cls
    return cls


def available_types() -> List[Dict[str, str]]:
    """界面上"数据源类型"下拉能选的东西。

    ★ 必须**从注册表推导**，不能手写一份。

    踩过：这里原来写死只有 `local` 一项，而 SFTP / WebDAV / HTTP 目录 / SMB
    四个源**早就实现并注册好了** —— 结果是后端的网络数据源能力
    **在界面上根本选不到**，等于白做。用户只能加本地路径。

    现在遍历 `_REGISTRY`，从每个类上取 `label` / `hint`，
    新增一个源类型只要实现类并 `@register`，界面自动就有了。
    排在最前的是 `local`（最常用），其余按 kind 排序。
    """
    out: List[Dict[str, str]] = []
    for kind, cls in _REGISTRY.items():
        exp = bool(getattr(cls, "experimental", False))
        label = str(getattr(cls, "label", "") or kind)
        # ★ "（实验性）"后缀在这里统一加，不写进各源类的 label ——
        #   否则改标记时容易只改一处，界面和代码说法不一致。
        #
        #   什么算实验性：**没有对着真实服务器完整验证过**。按这个标准，
        #   除了本地源，**所有网络协议都算** —— SFTP / WebDAV / HTTP 目录 /
        #   SMB / FTP 全都没在真实服务端上跑过一遍，只是代码实现了。
        #   这个标记必须诚实：标成"稳定"而实际没测过，等于把风险转嫁给用户。
        if exp and "实验性" not in label:
            label = f"{label}（实验性）"
        out.append({
            "kind": kind,
            "label": label,
            "hint": str(getattr(cls, "hint", "") or ""),
            "experimental": exp,
            # 给界面用的一句话说明，讲清"没验证到什么程度"
            "verify_note": str(getattr(cls, "verify_note", "") or ""),
        })
    out.sort(key=lambda x: (x["kind"] != "local", x["kind"]))
    return out


def build_source(cfg, password: str = "") -> MusicSource:
    if cfg.type not in _REGISTRY:
        raise SourceError(f"不支持的数据源类型: {cfg.type}")
    return _REGISTRY[cfg.type](cfg, password)
