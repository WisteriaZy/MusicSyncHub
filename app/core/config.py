"""配置加载与保存。

配置以 TOML 形式保存在用户目录（~/.music-sync-hub/config.toml），
Windows 上即 %USERPROFILE%\\.music-sync-hub\\config.toml。

口令不写进配置文件，改为放在同目录的 secrets.toml（权限 600），
配置里只留一个 `secret_ref` 名字。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

try:  # Python >= 3.11
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - 老版本回退
    tomllib = None  # type: ignore

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 路径

def data_dir() -> Path:
    """应用数据目录（配置、索引、备份都在这儿）。

    ★ 有一个**只对测试生效**的兜底，防止测试写到用户真实数据。

    测试之间靠"设 `MSH_DATA_DIR` 到临时目录、teardown 再 pop 掉"隔离。
    缺口在于：**pop 之后到下一个测试设好之前**它是空的 ——
    这期间任何 `save_config()` 都会落到用户真实的那份 config.toml。

    这不是理论风险：修 B8 时加的一个测试就在 finally 里
    `load_config(force=True)`，正好撞上这个空窗期，
    把用户的两个真实数据源换成了临时目录里的假数据源。

    所以：`MSH_DATA_DIR` 有值时照用（正常隔离不受影响）；
    **空串 / 未设**时，如果当前处于测试进程（由 `PYTHONPATH` 里
    能找到 `tests` 包来判断），就落到一个临时目录，**绝不碰真实目录**。
    """
    override = os.environ.get("MSH_DATA_DIR")
    if override:
        p = Path(override).expanduser()
    elif _in_test_run():
        # 测试进程里的空窗期：给一个稳定的临时目录，而不是真实目录
        p = Path(tempfile.gettempdir()) / "msh-tests-fallback"
    else:
        p = Path.home() / ".music-sync-hub"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _in_test_run() -> bool:
    """当前是不是测试进程（跑在 unittest discover 里）。

    判据：`sys.modules` 里有名为 `tests` 的包。正常启动应用时不会有它。
    缓存结果 —— 这个函数会被频繁调用，而模块表在启动后基本不变。
    """
    global _TEST_RUN_CACHE
    if _TEST_RUN_CACHE is None:
        _TEST_RUN_CACHE = any(
            n == "tests" or n.startswith("tests.")
            for n in list(sys.modules)
        )
    return _TEST_RUN_CACHE


_TEST_RUN_CACHE: Optional[bool] = None


def test_mode() -> bool:
    """**测试模式**：给"开箱即用的测试包"用的，正常使用永远是 False。

    开法只有一条：环境变量 `MSH_TEST_MODE=1`（`start-test.bat` 会设）。

    它做什么：
      · 后端在 `/` 返回的 HTML 里**注入一段水印**（右上角常驻角标）；
      · `/api/system` 返回 `test_mode: true`，前端据此再显示一条顶部横幅；
      · **不改变任何业务行为** —— 不做沙箱、不拦操作。它只是"让人一眼看出
        这是测试环境"，避免把演示库当成真实库去操作。

    为什么不顺便做数据隔离：测试包本来就把 `MSH_DATA_DIR` 指到包内目录，
    已经和数据隔离了。这里再加一层"假装隔离"只会让行为更难解释。
    """
    v = os.environ.get("MSH_TEST_MODE", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    # 兼容把整个值写成路径/说明的情况（打包脚本里容易手滑）
    return bool(v) and v not in ("0", "false", "no", "off")


def _toml_load(fh) -> Dict[str, Any]:
    if tomllib is not None:
        return tomllib.load(fh)
    data = fh.read()
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    try:
        import tomli  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "当前 Python 版本过低（缺少 tomllib）。请用 Python 3.11+ 运行，"
            "或执行: uv run --with tomli python -m app.main"
        ) from exc
    return tomli.loads(data)


def config_path() -> Path:
    return data_dir() / "config.toml"


def secrets_path() -> Path:
    return data_dir() / "secrets.toml"


def default_db_path() -> Path:
    return data_dir() / "index.sqlite3"


# ---------------------------------------------------------------- 数据模型

@dataclass
class SourceConfig:
    """一个音乐库位置（本地目录 / SFTP / WebDAV / HTTP 目录 / SMB）。"""

    id: str
    name: str
    type: str = "local"            # local | sftp | webdav | http | smb
    root: str = ""                 # 根路径或 URL，例如 /mnt/music、D:\\Music、https://host/dav/music
    enabled: bool = True
    # 连接参数（按类型取用）
    host: str = ""
    port: int = 0
    username: str = ""
    secret_ref: str = ""           # 指向 secrets.toml 里的键名
    base_url: str = ""             # webdav/http 的基地址（可与 root 合并）
    share: str = ""                # smb 共享名
    domain: str = ""               # smb 域
    # 行为参数
    follow_symlinks: bool = False
    read_only: bool = False        # 勾选后任何写操作都会被拒绝（保险丝）
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SourceConfig":
        known = {f for f in SourceConfig.__dataclass_fields__}
        return SourceConfig(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------- 设置项校验
# 用户可以在界面里直接填数字/路径。没有校验的话，一个天文数字
# （比如 scan_workers = 99999999）会直接开同等数量的线程把机器打爆；
# 路径里混进控制字符或过长字符串也会让后面写文件时莫名其妙失败。
#
# 这里给每个字段定一个"合理区间"，越界就**拒绝并说明**，
# 而不是悄悄写进配置文件（下次启动才炸，更难查）。
_NUM_LIMITS: Dict[str, tuple] = {
    # key: (最小值, 最大值, 是否允许 0)
    "scan_workers": (1, 64, False),
    "hash_workers": (1, 64, False),
    "hash_size_limit_mb": (0, 1024 * 1024, True),      # 0 = 不限制
    "quick_hash_probe_kb": (1, 65536, False),
    "duration_tolerance_sec": (0.0, 600.0, True),
    "name_similarity": (0.0, 1.0, True),
    "cover_cache_size": (0, 200000, True),
    "max_compare_tracks": (10, 2000000, False),
    "station_max_gb": (0.0, 1024 * 1024.0, True),      # 0 = 不限额
    "staging_max_gb": (0.0, 1024 * 1024.0, True),      # 0 = 不限额
    "staging_stale_hours": (0.0, 24 * 3650.0, True),   # 0 = 不提醒
    "auto_index_refresh_seconds": (0, 86400, True),    # 0 = 关闭自动刷新
    "auto_full_rescan_hours": (0.0, 24 * 3650.0, True),  # 0 = 不兜底
}
_PATH_FIELDS = ("trash_dir", "station_dir", "staging_dir")
_STR_MAX = 400


class SettingsError(ValueError):
    """设置值不合法。带上"哪个字段、为什么"，界面才能告诉用户怎么改。"""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}：{reason}")


# 加载 config.toml 时发现的不合法项（键 -> 原因）。
# 手改配置文件是正常用法，所以不合法时**保留默认值并记下来**，
# 由启动流程 / 设置页把问题显示给用户，而不是静默改掉或直接崩。
_settings_errors: List[str] = []


def settings_errors() -> List[str]:
    """上次加载时被拒绝的设置项（空列表 = 全都合法）。"""
    return list(_settings_errors)


def validate_setting(key: str, value: Any) -> Any:
    """校验一个设置项，返回**转换后**的值；不合法就抛 `SettingsError`。"""
    if key not in Settings.__dataclass_fields__:
        raise SettingsError(key, "不是可识别的设置项")
    cur = getattr(Settings(), key)

    # ---- 数值 ----
    if isinstance(cur, bool):
        if not isinstance(value, bool):
            raise SettingsError(key, "需要 true / false")
        return value
    if isinstance(cur, (int, float)) and not isinstance(cur, bool):
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                value = 0
        # ★ bool 在 Python 里是 int 的子类：`float(True) == 1.0`。
        #   不显式挡住的话 `scan_workers = true` 会被当成 1 悄悄吃进去。
        if isinstance(value, bool):
            raise SettingsError(key, "需要填数字（不是 true/false）")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise SettingsError(key, "需要填数字")
        # ★ NaN / inf 会让后面的比较和格式化全部失效，
        #   是"看起来是数字其实搞炸程序"的典型，必须挡住。
        if num != num or num in (float("inf"), float("-inf")):
            raise SettingsError(key, "不是有效的数字")
        lo, hi, allow_zero = _NUM_LIMITS.get(key, (None, None, True))
        if allow_zero and num == 0:
            return 0 if isinstance(cur, int) else 0.0
        if lo is not None and num < lo:
            raise SettingsError(key, f"不能小于 {lo}")
        if hi is not None and num > hi:
            raise SettingsError(key, f"不能大于 {hi:g}")
        if isinstance(cur, int):
            # ★ 整数字段拒绝小数：`scan_workers = 1.9` 以前会被 int() 截成 1，
            #   用户以为设了 1.9。要么明确报错，要么让用户改成整数。
            if num != int(num):
                raise SettingsError(key, "需要是整数")
            return int(num)
        return num

    # ---- 字符串 / 路径 ----
    if isinstance(cur, str):
        # ★ 必须是文本。以前 `str(value)` 什么都收：
        #     trash_dir = true        → "True"
        #     trash_dir = ["D:/x"]    → "['D:/x']"
        #     trash_dir = {"a": 1}    → "{'a': 1}"
        #   这些值会被当成**相对路径**去 join，回收站就跑到莫名其妙的地方。
        if value is not None and not isinstance(value, str):
            raise SettingsError(key, f"需要是文本（收到 {type(value).__name__}）")
        s = "" if value is None else value
        s = s.strip()
        if len(s) > _STR_MAX:
            raise SettingsError(key, f"太长了（最多 {_STR_MAX} 个字符）")
        # 控制字符会让写文件名/写 TOML 出各种怪问题
        bad = [c for c in s if ord(c) < 32 or ord(c) == 127]
        if bad:
            raise SettingsError(key, "包含了不可见字符，请重新填写")
        if key in _PATH_FIELDS:
            # Windows 文件名里不允许的字符（除了盘符冒号）
            illegal = set('<>"|?*') | {chr(c) for c in range(0, 32)}
            head, _, tail = s.partition(":")
            for ch in (tail if head.isalpha() and len(head) == 1 else s):
                if ch in illegal:
                    raise SettingsError(key, f"路径里不能包含 {ch}")
            if s and not (len(s) == 1 and s[0] == ".") and "\x00" in s:
                raise SettingsError(key, "路径不合法")
        if not s and key == "default_keep_policy":
            return cur
        if key == "default_keep_policy":
            allowed = ("best_quality", "largest", "oldest", "newest", "ask")
            if s not in allowed:
                raise SettingsError(key, f"只能是 {'/'.join(allowed)} 之一")
        return s

    if isinstance(cur, list):
        if not isinstance(value, list):
            raise SettingsError(key, "需要是列表")
        if len(value) > 500:
            # 静默截断会让用户以为 501 项都生效了
            raise SettingsError(key, f"最多 500 项（收到 {len(value)} 项）")
        out = []
        for x in value:
            if not isinstance(x, str):
                raise SettingsError(key, f"列表里只能是文本（收到 {type(x).__name__}）")
            if len(x) > 200:
                raise SettingsError(key, "单项太长了（最多 200 个字符）")
            out.append(x)
        return out

    return value


@dataclass
class Settings:
    scan_workers: int = 8                # 扫描并发
    hash_workers: int = 4
    hash_size_limit_mb: int = 512        # 超过则只做快速哈希
    quick_hash_probe_kb: int = 256       # 快速哈希取头尾各多少 KB
    duration_tolerance_sec: float = 2.0  # 判为同一首歌的时长容差
    name_similarity: float = 0.86        # 模糊匹配阈值
    default_keep_policy: str = "best_quality"   # best_quality | oldest | newest | ask
    trash_dir: str = ""                  # 空则用源根目录下 .music-sync-trash
    cover_cache_size: int = 800          # 内存封面缓存条数
    max_compare_tracks: int = 8000       # 单次对比/查重每侧最多取多少条（超出会截断并提示）
    # ---------------------------------------------------------------- 中转站
    # 上传后暂时没想好放哪的文件先放这里；库里的文件也可以临时挪进来。
    # 空 = 默认放在程序目录下的 `station/`。
    station_dir: str = ""
    # 中转站容量上限（GB）。0 = 不限制。
    # 存在的意义是**防止上传没注意直接把盘写满** —— 满了会拒绝继续上传，
    # 而不是等到磁盘写不进才报错（那时可能已经影响系统了）。
    station_max_gb: float = 20.0
    # ---------------------------------------------------------------- 上传暂存区
    # 上传的**中间态**（还没走完入库向导的文件）放这里。
    # 空 = 默认放在应用数据目录下的 `uploads/`。
    #
    # 和中转站的区别：暂存区属于"这次上传流程"，是一次性的，
    # 归位后就该消失；中转站属于"我还没决定放哪"的长期存放。
    staging_dir: str = ""
    # 暂存区容量上限（GB）。0 = 不限制。
    # 单独设一个（而不是复用中转站的）是因为两者用途不同：
    # 暂存区只是过路，堆太多说明有批次忘了处理，默认给小一点。
    staging_max_gb: float = 5.0
    # 批次滞留多久算"该处理了"（小时）。超过会在界面上标出来。
    # 0 = 不提醒。
    staging_stale_hours: float = 48.0
    # ---------------------------------------------------------------- 索引自动刷新
    # 没有它，索引只在你手动点扫描时更新：外部程序改了库，界面会一直说谎
    # （删了还看得见、移走了播放失败）。0 = 关闭自动刷新。
    auto_index_refresh_seconds: int = 300
    # 兜底整源扫描的间隔（小时）。目录级探测看不见"新目录里的新文件"，
    # 由它来兜。0 = 不做整源兜底。
    auto_full_rescan_hours: float = 24.0
    ignored_names: List[str] = field(
        default_factory=lambda: [".DS_Store", "Thumbs.db", "desktop.ini", "@eaDir"]
    )


@dataclass
class AppConfig:
    sources: List[SourceConfig] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    # ---------------------------------------------------------- 序列化
    def to_dict(self) -> Dict[str, Any]:
        return {
            "sources": [s.to_dict() for s in self.sources],
            "settings": asdict(self.settings),
        }


_lock = threading.RLock()
_cache: Optional[AppConfig] = None
_secrets_cache: Optional[Dict[str, str]] = None


def _dump_toml(cfg: AppConfig) -> str:
    """手写 TOML 输出，避免引入额外依赖。"""
    out: List[str] = []
    out.append("# Music Sync Hub 配置文件")
    out.append("# 修改后可在界面点“重新加载配置”，或重启程序。")
    out.append("")
    for s in cfg.sources:
        out.append("[[sources]]")
        for k, v in s.to_dict().items():
            if k == "extras":
                if not v:
                    continue
                out.append("")
                out.append(f"[sources.extras]")
                for ek, ev in v.items():
                    out.append(f"{ek} = {_toml_val(ev)}")
                continue
            out.append(f"{k} = {_toml_val(v)}")
        out.append("")
    out.append("[settings]")
    for k, v in asdict(cfg.settings).items():
        out.append(f"{k} = {_toml_val(v)}")
    out.append("")
    return "\n".join(out)


_TEMPLATE_HEADER = r"""# Music Sync Hub 配置文件
# ---------------------------------------------------------------
# 也可以在界面「数据源」页面里添加/修改，改完点右上角「重载配置」。
#
# 数据源的例子（去掉行首的 # 即可使用）。
# 路径写法：Windows 用 D:\Music，Linux/macOS 用 /mnt/music。
#
#   [[sources]]
#   id = "win"
#   name = "Windows 音乐库"
#   type = "local"
#   root = "D:\\Music"
#   read_only = true         # 建议把作为"源"的那一侧设为 true
#
#   [[sources]]
#   id = "linux"
#   name = "本地音乐库"
#   type = "local"
#   root = "/mnt/music"
#   read_only = false
# ---------------------------------------------------------------
"""


def _toml_val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    # ★ 控制字符必须转义。
    #   TOML 的基本字符串**不允许**出现裸的换行/回车/制表等控制字符 ——
    #   写进去就是一个语法坏掉的 config.toml，下次启动直接读不出来。
    #   经 `put_settings` 进来是安全的（那里在校验阶段就挡了非法字符），
    #   但用户手改配置时完全可能粘进一个带换行的路径，
    #   保存一次就把整个配置文件写坏 —— 那是"改一处、整个程序起不来"。
    #   这里统一转成 TOML 的 `\uXXXX` / `\n` 转义写法。
    out = []
    for ch in s:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def load_config(force: bool = False) -> AppConfig:
    """读取配置（带缓存）。文件不存在时写入一份带示例的默认配置。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache

        path = config_path()
        cfg: AppConfig
        if not path.exists():
            cfg = AppConfig(sources=[], settings=Settings())
            _write_text(path, _TEMPLATE_HEADER + _dump_toml(cfg))
        else:
            # ★ 解析失败不能让整个应用起不来。
            #   实测：`[settings` 少个括号、重复 `[settings]`、非 UTF-8、
            #   顶层垃圾、`sources = "abc"`、缺 `name` 的 `[[sources]]` ——
            #   这 6 种都会让启动直接崩掉（rc=1），用户只是手滑改错一个字
            #   就打不开程序，而且没有任何界面能告诉他哪里错了。
            #
            #   做法：备份坏文件 → 用默认配置照常启动 → 把原因记下来，
            #   由 `/api/system` 暴露给前端提示。
            try:
                with path.open("rb") as fh:
                    raw = _toml_load(fh)
                if not isinstance(raw, dict):
                    raise ValueError("顶层必须是一个 TOML 表")
                _src_raw = raw.get("sources")
                if _src_raw is None:
                    _src_raw = []
                if not isinstance(_src_raw, list):
                    raise ValueError("sources 必须是数组（[[sources]]）")
                sources = []
                for i, d in enumerate(_src_raw, 1):
                    if not isinstance(d, dict):
                        raise ValueError(f"sources 第 {i} 项必须是表")
                    try:
                        sources.append(SourceConfig.from_dict(d))
                    except Exception as exc:                   # noqa: BLE001
                        raise ValueError(
                            f"sources 第 {i} 项不完整（{type(exc).__name__}: {exc}）")
                _set_raw = raw.get("settings")
                if _set_raw is not None and not isinstance(_set_raw, dict):
                    raise ValueError("settings 必须是一个 TOML 表")
            except Exception as exc:                           # noqa: BLE001
                import datetime as _dt
                stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                broken = path.with_name(f"config.toml.broken-{stamp}")
                try:
                    path.replace(broken)
                except Exception:                              # noqa: BLE001
                    broken = path
                msg = f"{type(exc).__name__}: {exc}"
                _settings_errors[:] = [f"配置文件无法读取（{msg}），已改用默认配置"]
                print(f"[config] ★ 配置文件损坏，已备份到 {broken}，本次使用默认配置")
                print(f"[config]   原因：{msg}")
                cfg = AppConfig(sources=[], settings=Settings())
                _write_text(path, _TEMPLATE_HEADER + _dump_toml(cfg))
                _cache = cfg
                return cfg
            settings = Settings()
            # ★ 手改 config.toml 也要走**同一套校验**，不能直接 setattr。
            #
            #   文件头部的注释明确鼓励手改（"改完在界面点重新加载配置"），
            #   所以这是正常用法而不是攻击。但直接 setattr 会把这些原样吃进去：
            #     scan_workers = 99999999   → 真的开同等线程数
            #     station_max_gb = nan      → `bytes > cap` 恒为 False，容量上限静默失效
            #     duration_tolerance_sec = -5.0 / name_similarity = 7.5 / "很多"
            #   结果要么是资源被打爆，要么是保护悄悄失灵，要么在后面某处抛类型错。
            #
            #   这里逐个键校验：不合法的**保留默认值**并记下来，
            #   由启动流程/接口把这些问题报给用户（不静默吞掉）。
            bad: List[str] = []
            for k, v in (raw.get("settings") or {}).items():
                if not hasattr(settings, k):
                    continue
                try:
                    setattr(settings, k, validate_setting(k, v))
                except SettingsError as exc:
                    bad.append(f"{k}: {exc}")
                except Exception as exc:                       # noqa: BLE001
                    bad.append(f"{k}: 无法使用（{type(exc).__name__}: {exc}）")
            _settings_errors[:] = bad
            if bad:
                print("[config] 以下设置不合法，已改用默认值：")
                for b in bad:
                    print(f"  · {b}")
            cfg = AppConfig(sources=sources, settings=settings)

        _cache = cfg
        return cfg


def save_config(cfg: AppConfig) -> None:
    global _cache
    with _lock:
        _write_text(config_path(), _dump_toml(cfg))
        _cache = cfg


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 口令

def load_secrets(force: bool = False) -> Dict[str, str]:
    global _secrets_cache
    with _lock:
        if _secrets_cache is not None and not force:
            return _secrets_cache
        path = secrets_path()
        if not path.exists():
            _secrets_cache = {}
            return _secrets_cache
        with path.open("rb") as fh:
            raw = _toml_load(fh)
        _secrets_cache = {str(k): str(v) for k, v in raw.items()}
        return _secrets_cache


def save_secret(ref: str, password: str) -> None:
    """写入/更新一个口令项。"""
    global _secrets_cache
    with _lock:
        secrets = dict(load_secrets())
        secrets[ref] = password
        lines = ["# 凭据文件，请勿提交到版本库。", ""]
        for k, v in secrets.items():
            esc = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'"{k}" = "{esc}"')
        _write_text(secrets_path(), "\n".join(lines) + "\n")
        try:
            os.chmod(secrets_path(), 0o600)
        except OSError:
            pass
        _secrets_cache = secrets


def get_secret(ref: str, default: str = "") -> str:
    if not ref:
        return default
    return load_secrets().get(ref, default)


class SourceNotFound(LookupError):
    """数据源不存在。

    单独一个类型，是因为它**不是**编程错误（KeyError 的语义），
    而是"用户给了一个不存在的 id" —— 属于正常的请求错误，
    接口层应当回 404，而不是让它冒成 500。

    踩过：这里原来直接 `raise KeyError(...)`。FastAPI 不认识 KeyError，
    于是请求 `/api/scan/不存在的源` 会 500 内部错误；更糟的是
    `start_scan` 是**先建任务再解析数据源**，异常一抛，
    那个任务的独占位就永远占着 —— 之后所有写操作都提示"已有任务在执行"，
    而任务列表里那条记录还在 running，用户被彻底锁死。
    """


def resolve_source(sid: str, cfg: Optional[AppConfig] = None) -> SourceConfig:
    cfg = cfg or load_config()
    for s in cfg.sources:
        if s.id == sid:
            return s
    raise SourceNotFound(f"未找到数据源: {sid}")
