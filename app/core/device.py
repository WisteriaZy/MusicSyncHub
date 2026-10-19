"""外置存储（USB 盘 / 移动硬盘 / SD 卡）的识别与针对性优化。

## 为什么要单独识别

U 盘和移动硬盘和内置盘的行为差别很大，用同一套默认参数会出问题：

1. **慢**。顺序读通常只有内置盘的 1/5 ~ 1/20，随机读更差。
   用内置盘的 8 个扫描 worker 去啃 U 盘，只会互相抢带宽、
   总耗时反而更长，而且进度条几乎不动 —— 用户以为卡死了。
2. **随时会消失**。拔盘、接口松动、供电不足都会让盘在半途掉线。
   这时候如果在写文件，轻则写坏一个文件，重则留下一个"半个文件"
   而索引里记着它是完整的。
3. **容易被手滑写坏**。很多人把 U 盘当"备份盘"插上来只读，
   结果误操作把里面的东西改了。

所以这里提供三件事：
  · `detect(path)` —— 判断一个路径落在什么盘上（可移除 / 慢速 / 网络）
  · 依据探测结果给**默认策略**（只读、少 worker、跳过全量哈希）
  · `is_available(path)` —— 写操作前后探活，用于"拔出检测"

## 探测手段（Windows 优先，其它平台降级）

  · `GetDriveTypeW` 返回 `DRIVE_REMOVABLE(2)` / `DRIVE_FIXED(3)` /
    `DRIVE_REMOTE(4)` —— 这是判断"可移除"最可靠的信号。
  · 读速：读一个 1 MB 的块并计时。**只有**明显慢（< 20 MB/s）才判为
    `slow_disk`，避免把正常的内置盘误判。
  · 拿不到任何信息时一律**按内置盘处理**（保守：不改变默认行为），
    但 `removable` 允许用户在界面上手动勾。
"""
from __future__ import annotations

import os
import shutil
import string
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# GetDriveTypeW 的返回值
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

# 低于这个顺序读速就认为"慢盘"（MB/s）。
#
# ★ 阈值定在 15 是**实测调出来的**，而且必须配 `measure_read_mbps` 里的
#   "取冷读 + 大样本"策略一起用，否则这个判定完全不可信：
#
#   最初版本随便挑目录里第一个文件读 1 MB，结果把内置的 D 盘判成了
#   7.7 MB/s 的"慢盘"（紧接着再测就是 2500~3800 MB/s）。原因是：
#     · 小样本 + 冷缓存 → 测到的是寻道和缓存未命中，不是顺序读速
#     · 热缓存 → 测到的是内存速度（4000+ MB/s），也不是磁盘
#   一个**误报**的慢盘标记会静默改掉 worker 数和哈希策略，比不检测更糟。
#
#   实测参考：内置 SSD 2000~4000（热）/ 200~500（冷），内置机械盘 80~160，
#   USB3 移动硬盘 60~120，USB2 移动硬盘 25~35，U 盘 8~20。
#   15 能干净地分出"真正的 U 盘"，又不会误伤内置盘。
SLOW_MBPS = 15.0

# 采样下限：小于这个的文件不做速度判定（小文件的耗时不代表顺序读）
MIN_SAMPLE_BYTES = 4 << 20          # 4 MB

# 慢盘上的默认并发：U 盘随机读写差，多线程只会互相拖累
SLOW_WORKERS = 2
SLOW_HASH_WORKERS = 1


@dataclass
class DriveInfo:
    """一个路径落在什么盘上。"""
    path: str = ""
    mount: str = ""              # 盘符 / 挂载点，如 `E:\` 或 `/media/usb`
    kind: str = "unknown"        # removable | fixed | remote | cdrom | ramdisk | unknown
    removable: bool = False
    network: bool = False
    slow: bool = False
    read_mbps: Optional[float] = None
    fs: str = ""
    notes: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "mount": self.mount, "kind": self.kind,
            "removable": self.removable, "network": self.network,
            "slow": self.slow,
            "read_mbps": (round(self.read_mbps, 1)
                          if self.read_mbps is not None else None),
            "fs": self.fs, "notes": list(self.notes),
        }

    @property
    def label(self) -> str:
        return {"removable": "可移除存储（U 盘 / 移动硬盘 / SD 卡）",
                "fixed": "内置磁盘", "remote": "网络位置",
                "cdrom": "光驱", "ramdisk": "内存盘"}.get(self.kind, "未知")


# ------------------------------------------------------------------ 盘符

def mount_of(path: str) -> str:
    """这个路径所在的挂载点（Windows 是盘符，其它平台是根）。"""
    p = os.path.abspath(str(path or ""))
    if os.name == "nt":
        drive, _ = os.path.splitdrive(p)
        return (drive + "\\") if drive else ""
    # POSIX：往上找到挂载点。没有 mtab 权限时退回与 /media、/mnt、/run/media
    # 前缀匹配，够覆盖绝大多数 U 盘挂载方式。
    best = "/"
    for base in ("/media", "/mnt", "/run/media", "/Volumes"):
        if p == base or p.startswith(base + "/"):
            rest = p[len(base):].lstrip("/")
            seg = rest.split("/")[0] if rest else ""
            cand = f"{base}/{seg}" if seg else base
            if len(cand) > len(best):
                best = cand
    return best


def _drive_type(mount: str) -> int:
    """Windows 上问系统这是什么盘；其它平台返回 UNKNOWN。"""
    if os.name != "nt" or not mount:
        return DRIVE_UNKNOWN
    try:
        import ctypes
        root = mount.rstrip("\\/") + "\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except Exception:
        return DRIVE_UNKNOWN


def _fs_name(mount: str) -> str:
    if os.name != "nt" or not mount:
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(mount), None, 0, None, None, None, buf, 261)
        return buf.value or ""
    except Exception:
        return ""


# ------------------------------------------------------------------ 读速

def measure_read_mbps(path: str, sample_bytes: int = 4 << 20) -> Optional[float]:
    """测顺序读速（MB/s）。**只取一次冷读，且样本要够大**。

    这个函数很容易写错，所以把踩过的坑写在这儿：

    1. **不能读小文件**。读 1 MB 测到的是"寻道 + 缓存未命中"，
       内置 SSD 实测只有 7.7 MB/s —— 于是内置盘被误判成慢盘。
    2. **不能取多次里的最好值**。热缓存下能到 4000+ MB/s，
       那测的是内存不是磁盘。
    3. **只测一次，从冷状态**。第一次读落在磁盘上，这才是真实顺序读速。
       所以样本文件要求 ≥ 8 MB，并且一次读够 4 MB。

    找不到足够大的文件就返回 `None`（**不猜**）—— 上层会退回到
    "按盘类型判断"，也就是只看可移除/网络，不碰 slow 标记。
    """
    target = ""
    size = 0
    try:
        if os.path.isfile(path):
            target = path
            size = os.path.getsize(target)
        else:
            # 挑**最大**的那个文件：小文件测不出顺序读速
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            sz = e.stat().st_size
                            if sz > size:
                                size, target = sz, e.path
                    except OSError:
                        continue
        if not target or size < MIN_SAMPLE_BYTES:
            return None
        n = int(min(sample_bytes, size))
        t0 = time.perf_counter()
        read = 0
        with open(target, "rb", buffering=0) as f:
            while read < n:
                chunk = f.read(min(1 << 20, n - read))
                if not chunk:
                    break
                read += len(chunk)
        dt = time.perf_counter() - t0
        if dt <= 0 or read < MIN_SAMPLE_BYTES // 2:
            return None
        return (read / (1024 * 1024)) / dt
    except OSError:
        return None


# ------------------------------------------------------------------ 主入口

def detect(path: str, *, measure: bool = True) -> DriveInfo:
    """判断 `path` 落在什么盘上，并给出针对性结论。

    `measure=False` 时跳过读速测试（用于列表里批量探测，
    避免每次都去读盘）。

    ★ **slow 的判定只看盘类型，不靠读速测量。**

    踩过的坑：一开始想"实测读速 < 阈值就标慢盘"，但实测证明这个测量
    在真实使用里不可靠 —— 同一块内置 SSD，第一次冷读测出 7.7 MB/s
    （像 U 盘），紧接着热读就是 1800~4000 MB/s（像内存）。
    一个**误报**的慢盘标记会静默把并发压到 2、并跳过全量哈希校验，
    比不检测更糟。

    所以策略是：

      · `removable` / `remote` → 直接判 `slow=True`。这两类**本来就慢**
        （U 盘 8~20 MB/s、网络位置延迟高），不需要测。
      · `fixed` → **不自动判慢**。读速照测、照报（`read_mbps` 会带给界面），
        但只作为**参考信息**，由用户自己决定要不要开"慢速盘模式"
        —— 万一它真是一块 USB2 移动硬盘（Windows 报 fixed）。

    这样最坏情况是"没帮用户开慢速模式"（他自己点一下），
    而不是"把快盘当慢盘用"（悄悄变慢且没人知道）。
    """
    info = DriveInfo(path=str(path or ""))
    if not path:
        return info
    mount = mount_of(path)
    info.mount = mount
    dt = _drive_type(mount)
    info.kind = {
        DRIVE_REMOVABLE: "removable", DRIVE_FIXED: "fixed",
        DRIVE_REMOTE: "remote", DRIVE_CDROM: "cdrom",
        DRIVE_RAMDISK: "ramdisk",
    }.get(dt, "unknown")
    info.removable = (info.kind == "removable")
    info.network = (info.kind == "remote")
    if os.name == "nt":
        info.fs = _fs_name(mount)

    # 类型决定 slow —— 见上面 docstring 的说明
    if info.removable:
        info.slow = True
    elif info.network:
        info.slow = True
    elif info.kind == "cdrom":
        info.slow = True

    if measure and os.path.isdir(path):
        info.read_mbps = measure_read_mbps(path)
        if info.read_mbps is not None:
            info.notes.append(f"实测顺序读约 {info.read_mbps:.0f} MB/s")
            # 只有本来就是慢盘类型时，读速才算"佐证"；对内置盘只作参考
            if not info.slow and info.read_mbps < SLOW_MBPS:
                info.notes.append(
                    f"读速低于 {SLOW_MBPS:.0f} MB/s，但它是内置盘类型，"
                    f"未自动按慢盘处理；如果它其实是外接设备（USB2 等），"
                    f"可以在数据源里手动勾选「慢速盘」")
    if info.removable:
        info.notes.append(
            "可移除存储：写入途中被拔掉可能留下半个文件，"
            "默认按只读 + 低并发处理")
    if info.network:
        info.notes.append("网络位置：延迟高，不适合做写入目标")
    return info


def is_available(path: str) -> bool:
    """这个路径现在还够得着吗（用于拔出检测）。

    注意只做 `stat`，不读内容、不建文件 —— 拔出检测会被频繁调用。
    """
    if not path:
        return False
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def suggested_defaults(info: DriveInfo) -> Dict[str, Any]:
    """按盘的性质给出**新建数据源时的默认值**。

    这些只是默认值，用户在界面上都能改。原则是"宁可先保守"：
    可移除设备默认只读，用户要写就自己取消勾选 —— 反过来
    （默认可写、事后发现写坏了）是不可逆的。
    """
    out: Dict[str, Any] = {}
    if info.removable or info.network:
        out["read_only"] = True
    if info.slow or info.removable or info.network:
        out["extras"] = {
            "slow_disk": True,
            "scan_workers": SLOW_WORKERS,
            "hash_workers": SLOW_HASH_WORKERS,
            "skip_full_hash": True,
        }
    return out


def effective_workers(extras: Dict[str, Any] | None, base: int,
                      hash_mode: bool = False) -> int:
    """慢盘上把并发压下来。

    踩过的思路：一开始想"慢盘就少开线程"，但少到 1 反而浪费 ——
    U 盘的瓶颈在随机寻道，2 个并发通常比 1 个快、又明显优于 8 个。

    ★ 还有一个反向的坑：`slow_disk` 只是"建议低并发"，不是"锁死低并发"。
    如果用户在数据源里显式设了 `scan_workers`，那要以**用户的值**为准
    —— 包括设成 0（= 不限制，沿用全局默认）。最初的写法是
    `min(base, want)`，于是 `want=0` 会变成 1，把"不限制"执行成了
    "只准一个线程"，正好和用户的意图相反。
    """
    ex = extras or {}
    if not ex.get("slow_disk"):
        return max(1, int(base))
    want = ex.get("hash_workers" if hash_mode else "scan_workers")
    if want is None:
        want = SLOW_HASH_WORKERS if hash_mode else SLOW_WORKERS
    try:
        want = int(want)
    except (TypeError, ValueError):
        want = SLOW_HASH_WORKERS if hash_mode else SLOW_WORKERS
    if want <= 0:
        # 0 = 用户明确表示"不限制" → 沿用全局默认，不要替他压到 1
        return max(1, int(base))
    return max(1, min(int(base), want))


def list_portable_devices() -> list:
    """列出 Windows 上的**便携设备 / MTP 设备**（手机、相机、播放器）。

    ## 为什么只"列出来"而不支持读写

    MTP 不是可挂载的文件系统。要用它必须走 Windows Portable Devices (WPD)
    COM 接口（或第三方的 libmtp 原生库）—— Python 标准库**没有**这个能力，
    而这个项目刻意只依赖标准库 + 少数几个纯 Python 包（见 pyproject）。
    硬塞一个原生依赖进来，会让"拷贝即用"的部署方式失效，
    对收尾阶段来说是过大的风险。

    所以这里的定位是**如实告知**：

      · 设备插着 → 列出来，带上"暂不支持"的说明，
        用户不会以为是自己操作错了；
      · 想传歌 → 提示两条真正可行的路：
        1. 在手机/相机上把 USB 模式切成「文件传输 / U 盘模式」，
           它就会变成一个盘符，用「本地」源直接加；
        2. 或者用厂商的同步工具先拷到本地目录。

    `supported` 恒为 False —— 界面据此把它显示成灰色 + [实验性] 标签，
    不允许"添加为数据源"，避免用户建出一个永远连不上的源。
    """
    out: list = []
    if os.name != "nt":
        return out
    try:
        import ctypes
        from ctypes import wintypes

        # 便携设备在"我的电脑"里就是 shell 命名空间的一部分，
        # 用 SHGetKnownFolderPath 找"此电脑"再枚举最稳。
        # 为减少依赖，这里退一步：问 WPD 的 PnP 设备是否在线。
        # （只读注册表/设备枚举，不碰文件系统，失败一律吞掉。）
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-PnpDevice -Class WPD -Status OK -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty FriendlyName"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (r.stdout or "").splitlines():
            name = line.strip()
            if not name:
                continue
            out.append({
                "name": name,
                "kind": "mtp",
                "supported": False,
                "reason": "MTP 协议需要 Windows 便携设备(COM)接口或 libmtp，"
                          "本项目只依赖标准库，暂不支持",
                "workaround": "在设备上把 USB 模式切成「文件传输 / U 盘模式」，"
                              "它会变成一个盘符，然后用「本地」数据源添加",
            })
    except Exception:
        pass
    return out

    """列出这台机器上看起来可用的盘（供"添加数据源"时挑选）。

    只返回**能 stat 通**的，避免把空读卡器槽位也列出来。
    """
    out = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                u = shutil.disk_usage(root)
            except OSError:
                continue
            info = detect(root, measure=False)
            out.append({
                "root": root,
                "label": info.label,
                "kind": info.kind,
                "removable": info.removable,
                "slow_guess": info.removable or info.network,
                "total": u.total, "free": u.free,
                "fs": info.fs,
            })
    else:
        for base in ("/media", "/mnt", "/run/media", "/Volumes"):
            if not os.path.isdir(base):
                continue
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for n in names:
                root = os.path.join(base, n)
                if not os.path.isdir(root):
                    continue
                try:
                    u = shutil.disk_usage(root)
                except OSError:
                    continue
                info = detect(root, measure=False)
                out.append({
                    "root": root, "label": info.label, "kind": info.kind,
                    "removable": info.removable,
                    "slow_guess": info.removable or info.network,
                    "total": u.total, "free": u.free, "fs": info.fs,
                })
    return out


def list_drives() -> list:
    """列出这台机器上看起来可用的盘（供"添加数据源"时挑选）。

    只返回**能 stat 通**的，避免把空读卡器槽位也列出来。
    """
    out = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                u = shutil.disk_usage(root)
            except OSError:
                continue
            info = detect(root, measure=False)
            out.append({
                "root": root,
                "label": info.label,
                "kind": info.kind,
                "removable": info.removable,
                "slow_guess": info.removable or info.network,
                "total": u.total, "free": u.free,
                "fs": info.fs,
            })
    else:
        for base in ("/media", "/mnt", "/run/media", "/Volumes"):
            if not os.path.isdir(base):
                continue
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for n in names:
                root = os.path.join(base, n)
                if not os.path.isdir(root):
                    continue
                try:
                    u = shutil.disk_usage(root)
                except OSError:
                    continue
                info = detect(root, measure=False)
                out.append({
                    "root": root, "label": info.label, "kind": info.kind,
                    "removable": info.removable,
                    "slow_guess": info.removable or info.network,
                    "total": u.total, "free": u.free, "fs": info.fs,
                })
    return out