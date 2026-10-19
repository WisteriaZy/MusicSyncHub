# Music Sync Hub 技术文档

> 本文档面向**开发者与代码审查者**，描述 `C:\Users\Administrator\Desktop\MusicSyncHub` 这个
> Windows 上运行的 FastAPI + 原生 JS 音乐库管理器的真实实现。
>
> **编写原则**：所有结论都来自实际阅读源码（文件 + 函数级定位），没有读到的内容一律标注
> `未验证`，绝不凭印象补写。文中出现的行号以本次阅读时代码的实际情况为准，代码演进后会漂移，
> 请以函数名为锚点定位。
>
> **代码基线**（行数取自阅读时的文件末尾统计）：
>
> | 文件 | 行数 | 文件 | 行数 |
> |---|---|---|---|
> | `app/main.py` | 2521 | `app/core/matching.py` | 1015 |
> | `app/core/ops.py` | 1025 | `app/core/organize.py` | 1787 |
> | `app/core/tags.py` | 673 | `app/core/jobs.py` | 592 |
> | `app/core/config.py` | 530 | `app/core/paths.py` | 490 |
> | `app/core/device.py` | 490 | `app/core/scanner.py` | 451 |
> | `app/core/db.py` | 366 | `app/core/transfer.py` | 326 |
> | `app/sources/base.py` | 325 | `app/core/plan.py` | 314 |
> | `app/web/static/js/app.js` | 1350 | `app/web/static/js/staging.js` | 666 |
> | `app/web/static/js/ui.js` | 382 | `app/web/static/js/api.js` | 343 |
> | `app/web/static/js/tasksync.js` | 214 | `app/web/index.html` | 188 |
>
> 项目**没有** `SAFETY.md`，也**没有** `AUDIT_LEDGER.md`（根目录只有 `README.md` 与
> `QUICKSTART.md`；已用 glob 在整棵目录树确认）。因此本文的"已知限制"与"开发约定"两章
> 只能从**代码注释里已经记录的教训**提炼——那些注释本身就是一份事实上的台账。

---

## 目录

1. [概览](#1-概览)
2. [目录结构](#2-目录结构)
3. [依赖与运行环境](#3-依赖与运行环境)
4. [配置系统](#4-配置系统)
5. [数据模型](#5-数据模型)
6. [数据源抽象](#6-数据源抽象)
7. [扫描与索引](#7-扫描与索引)
8. [匹配算法](#8-匹配算法)
9. [计划生成](#9-计划生成)
10. [执行器](#10-执行器)
11. [跨库传输](#11-跨库传输)
12. [任务系统与并发控制](#12-任务系统与并发控制)
13. [HTTP API 参考](#13-http-api-参考)
14. [前端架构](#14-前端架构)
15. [界面契约测试](#15-界面契约测试)
16. [测试体系](#16-测试体系)
17. [日志与可观测性](#17-日志与可观测性)
18. [安全不变量](#18-安全不变量)
19. [已知限制与未完成项](#19-已知限制与未完成项)
20. [开发约定](#20-开发约定)

---

## 1. 概览

### 1.1 一句话定位

把**多个音乐库位置**（本地盘、U 盘、SFTP、WebDAV、SMB、FTP、HTTP 目录）当成同一种东西来
**扫描索引 / 双栏对比 / 查重查漏 / 生成同步计划 / 安全执行**，并且所有写操作都遵守同一套
"先看计划、再确认执行、删除进回收站"的规则。

### 1.2 架构分层

```
┌──────────────────────────────────────────────────────────────────────┐
│ 浏览器（无构建的原生 JS）                                             │
│   index.html ── 18 个 <script> 顺序加载                                │
│   api.js(API) / ui.js(UI) / tasksync.js(TaskSync)                     │
│   app.js(State + App 外壳 + 播放器) + staging.js + views.*.js         │
└───────────────▲──────────────────────────────────────────────────────┘
                │ fetch(JSON) / XHR(上传进度) / <audio> Range 流
┌───────────────┴──────────────────────────────────────────────────────┐
│ FastAPI 应用层                                                        │
│   app/main.py            ── 48 条 /api 路由 + "/" + 静态挂载 + 5 个异常处理器
│   app/search_api.py      ── 搜索 / 集合 / 智能整理            11 条路由 │
│   app/upload_api.py      ── 上传暂存区 + 入库                 11 条路由 │
│   app/station_api.py     ── 中转站                            7 条路由 │
│   app/tags_api.py        ── 标签读写                          5 条路由 │
│   app/mappings_api.py    ── 映射规则                          3 条路由 │
│   app/sources/folders_api.py ── 文件夹级操作                  8 条路由 │
└───────────────▲──────────────────────────────────────────────────────┘
                │ 只依赖"能力接口"，不认协议
┌───────────────┴──────────────────────────────────────────────────────┐
│ core 业务内核（纯 Python，可单测）                                     │
│   paths.py    路径/文本归一化、AUDIO_EXTS（音频清单唯一真相）          │
│   audio.py    mutagen 标签解析、封面缩略图、quality_score              │
│   scanner.py  遍历 + 并发解析 + 增量缓存 + 哈希                        │
│   matching.py 配对打分 / 同库查重 / 组归并                             │
│   plan.py     把匹配结果翻译成 Step（只生成，不动手）                  │
│   ops.py      Executor：计划 → 磁盘（占位、去重、校验、索引同步）      │
│   transfer.py 跨库传输：复制 → 校验 → 删源                            │
│   jobs.py     任务注册表 + 独占位 + 崩溃恢复                           │
│   guard.py    exclusive_guard（HTTP 409）                             │
│   bgjob.py    只读长计算的后台线程                                    │
│   config.py   Settings / SourceConfig / secrets                       │
│   db.py       SQLite（WAL，线程局部连接，atexit 关闭）                 │
│   device.py   U 盘 / 网络盘识别与降并发                                │
│   applog.py   文件日志 + 内存环形缓冲                                  │
└───────────────▲──────────────────────────────────────────────────────┘
                │ MusicSource 接口（stat/listdir/walk/read_range/write_file/...）
┌───────────────┴──────────────────────────────────────────────────────┐
│ 数据源适配器 app/sources/                                             │
│   local.py(kind=local，唯一"非实验性")                                 │
│   sftp.py / webdav.py / httpdir.py / smb.py / ftp.py（全部 experimental）│
└──────────────────────────────────────────────────────────────────────┘
```

反向依赖只有一条：各 `*_api.py` 用 `def _deps(): from . import main` **延迟导入**拿
`manager / db() / Executor / require_writable`，避免与 `main.py` 循环导入
（见 `app/search_api.py`、`app/sources/folders_api.py`、`app/station_api.py` 的 `_deps()`）。

### 1.3 进程模型

* **单进程**：`uvicorn.run(app, ...)`（`main.main()`），没有多 worker、没有消息队列、没有外置服务。
* **后台线程**承担长任务，不占 HTTP 请求：
  * 扫描：`main.start_scan` 里 `threading.Thread(target=run, name=f"scan-{sid}", daemon=True)`；
  * 只读长计算（对比 / 查重 / 整理方案）：`core.bgjob.run_in_background`；
  * 执行计划：`Executor.run` 内部用 `ThreadPoolExecutor(workers=…)`（默认 3）。
* **状态只在内存 + SQLite**：`Jobs` 注册表是进程内单例（`core/jobs.py` 的 `jobs` 全局变量），
  `job` 表只用于**崩溃后恢复**与跨请求查询。
* **心跳 + 独占位**是唯一的互斥手段（第 12 章），没有文件锁、没有数据库分布式锁。

---

## 2. 目录结构

```
MusicSyncHub/
├─ app/
│  ├─ main.py                  FastAPI 主体：lifespan、48 条路由、异常处理器、静态挂载、CLI 入口
│  ├─ search_api.py            搜索 / 集合文件夹 / 智能整理（organize）路由
│  ├─ upload_api.py            上传暂存区（staging）与"入库"（commit）路由
│  ├─ station_api.py           中转站（长期暂存区）路由 + 容量配额
│  ├─ tags_api.py              标签 / 封面读写路由（仅本地源）
│  ├─ mappings_api.py          自定义映射规则的持久化（mappings.json）与路由
│  ├─ core/
│  │  ├─ paths.py              AUDIO_EXTS / is_audio / norm / name_key / version_tags / fuzzy_match
│  │  ├─ audio.py              mutagen 解析（parse_bytes）、封面缩略图、quality_score/label
│  │  ├─ scanner.py            scan_source / ensure_hashes / quick_hash / full_hash
│  │  ├─ matching.py           name_similarity / duration_similarity / pair_sources / group_duplicates
│  │  ├─ plan.py               Step / missing_steps / duplicate_steps / build_plan / compare_sources
│  │  ├─ ops.py                Executor + 错误分类 + 超时包装 + 路径越界校验
│  │  ├─ transfer.py           transfer_one（复制→校验→删源）、_Heartbeat、mode_of
│  │  ├─ jobs.py               JobState / Jobs / 独占位 / 暂停取消 / 崩溃恢复
│  │  ├─ guard.py              exclusive_guard（FastAPI 依赖，409）
│  │  ├─ bgjob.py              run_in_background（exclusive=False）
│  │  ├─ device.py             detect / mount_of / effective_workers / list_drives
│  │  ├─ config.py             data_dir / Settings / SourceConfig / secrets / 校验
│  │  ├─ db.py                 SCHEMA / Database（WAL、线程局部连接、迁移、atexit）
│  │  ├─ applog.py             日志落文件 + 内存环形缓冲
│  │  ├─ tags.py               标签写入（原子写 + JSON 备份 + 还原）
│  │  └─ organize.py           智能整理的规则引擎（收纳区识别、放错判定、plan_by_album）
│  ├─ sources/
│  │  ├─ base.py               Entry / MusicSource 接口 / register / available_types / build_source
│  │  ├─ __init__.py           SourceManager（按 id 复用实例、配置签名变化时重建）
│  │  ├─ local.py              本地盘（原子改名、跨盘退化复制、回收站、dir_stat）
│  │  ├─ sftp.py               paramiko SFTP
│  │  ├─ webdav.py             httpx + PROPFIND/GET/PUT/MKCOL/MOVE/DELETE
│  │  ├─ httpdir.py            只读 HTTP 目录（HTML autoindex / JSON autoindex）
│  │  ├─ smb.py                smbprotocol（未安装时退化成"已挂载盘符"）
│  │  ├─ ftp.py                ftplib（MLSD 列举，PASV）
│  │  └─ folders_api.py        文件夹新建/改名/删除/空目录清理路由
│  └─ web/
│     ├─ index.html            图标 sprite + 布局骨架 + 18 个 script + 末尾 App.boot()
│     └─ static/
│        ├─ css/app.css        全部样式（含 .ico 描边规则）
│        └─ js/                prefs / api / ui / tasksync / app / views.* / staging / upload / tags / organize / folderpicker
├─ tests/                      29 个 test_*.py + __init__.py + srchelp.py + e2e_check.py + make_sample_libs.py
├─ tools/                      check_frontend.py / audit_frontend.py / smoke_api.py / cdp_*.js
├─ scripts/dev_reset.sh        开发联调：杀进程 → 清索引 → 起服务 → 生成样例库
├─ station/                    中转站默认目录（含 .station.json 来源清单）
├─ docs/TECHNICAL.md           本文档
├─ run-test.py                 演示包启动器（MSH_TEST_MODE=1 + 包内 MSH_DATA_DIR）
├─ start.bat / start.sh        正式启动（Windows 用 GBK 编码的 .bat）
├─ "启动 Music Sync Hub.command"  macOS 双击启动脚本（未读，未验证细节）
├─ pyproject.toml / uv.lock    依赖声明与锁定
├─ README.md / QUICKSTART.md   面向使用者的说明
└─ MusicSyncHub-backup-20261015-130822.zip  打包备份（605 KB，非运行所需）
```

> `.venv/` 是已存在的虚拟环境（`start.bat` 优先使用其中的 `python.exe`）。

---

## 3. 依赖与运行环境

### 3.1 Python 版本

`pyproject.toml`：`requires-python = ">=3.12"`；`config.py` 里对 `tomllib` 缺失（<3.11）给了
回退到 `tomli` 的分支，但项目基线仍是 3.12。`[tool.uv] package = false`——本项目作为
**应用**运行，不作为包安装。

### 3.2 直接依赖（`pyproject.toml` 的 `dependencies`，全部 8 个）

| 依赖 | 版本约束 | 在本项目里的用途（实现位置） |
|---|---|---|
| `fastapi` | `>=0.110` | 路由、`Depends`、`StaticFiles`、异常处理器（`main.py` / 各 `*_api.py`） |
| `uvicorn[standard]` | `>=0.27` | ASGI 服务器（`main.main()` 里 `uvicorn.run`）；`[standard]` 拉入 uvloop/httptools/watchfiles/websockets |
| `httpx` | `>=0.27` | WebDAV 客户端（`sources/webdav.py`）、HTTP 目录源（`sources/httpdir.py`） |
| `mutagen` | `>=1.47` | 标签解析与写入（`core/audio.py`、`core/tags.py`，按扩展名挑具体类） |
| `Pillow` | `>=10.0` | 封面缩略图（`audio.make_thumbnail`） |
| `paramiko` | `>=3.4` | SFTP 数据源（`sources/sftp.py`） |
| `smbprotocol` | `>=1.13` | SMB 数据源（`sources/smb.py` 用 `smbclient`） |
| `python-multipart` | `>=0.0.32` | `UploadFile` 表单解析（`upload_api.py` 的 `File(...)`/`Form(...)`） |

`mutagen` 与 `Pillow` 缺失时程序**仍能启动**：`/api/system` 返回
`capabilities: {mutagen: bool, pillow: bool}`（`main.system_info` 读 `A._HAS_MUTAGEN`），
供界面提示能力降级。

### 3.3 传递依赖（`uv.lock` 锁定清单，37 个 name 条目）

`annotated-doc`、`annotated-types`、`anyio`、`bcrypt`、`certifi`、`cffi`、`click`、`cryptography`、
`fastapi`、`h11`、`httpcore`、`httptools`、`httpx`、`idna`、`invoke`、`music-sync-hub`（本项目自身）、
`mutagen`、`opentelemetry-api`、`paramiko`、`pillow`、`pycparser`、`pydantic`、`pydantic-core`、
`pynacl`、`pyspnego`、`python-dotenv`、`python-multipart`、`pyyaml`、`smbprotocol`、`sspilib`、
`starlette`、`typing-extensions`、`typing-inspection`、`uvicorn`、`uvloop`、`watchfiles`、`websockets`。

值得注意的**刻意不引入**：

* FTP 用标准库 `ftplib`（`sources/ftp.py` 的 import 只有 `ftplib`），不加第三方 FTP 库；
* WebDAV 用 `httpx` 手写 PROPFIND，不加 webdav 客户端库（`sources/webdav.py` 文件头说明）；
* MTP（手机/相机）**故意不支持**，因为需要 Windows Portable Devices 的 COM 接口或 libmtp，
  与"只依赖标准库 + 少数纯 Python 包"的部署目标冲突（`core/device.py::list_portable_devices` 的 docstring）。

### 3.4 启动方式

| 方式 | 命令 | 说明 |
|---|---|---|
| 正式（Windows） | 双击 `start.bat` | 默认 `127.0.0.1:8787`；`start.bat 0.0.0.0 9000` 暴露到局域网 |
| 直接跑模块 | `python -m app.main --host … --port … [--reload]` | `main.main()` 用 argparse；默认值取 `MSH_HOST`/`MSH_PORT` |
| uv + uvicorn | `uv run uvicorn app.main:app --host 0.0.0.0 --port 8787` | `main.py` 模块 docstring 里给出的写法 |
| 演示/测试包 | `python run-test.py [--port 8899] [--no-browser] [--rescan]` | 见 3.6 |
| 开发联调 | `bash scripts/dev_reset.sh 8791` | 杀旧进程 → 清索引 → 起服务 → 生成样例库并校验 10/7 首 |

### 3.5 `start.bat` 要点（已按 GBK 解码阅读）

1. 文件头明确写着：**本文件必须保存为 ANSI/GBK 编码**。`cmd.exe` 按系统代码页读取批处理，
   存成 UTF-8（无 BOM）会让中文注释变乱码并导致语法错误、直接启动失败。
2. `setlocal` + `cd /d "%~dp0"`：切到脚本所在目录，双击运行时工作目录才对。
3. 参数：`%~1` = HOST（默认 `127.0.0.1`），`%~2` = PORT（默认 `8787`）。
4. 解释器：优先 `%CD%\.venv\Scripts\python.exe`；不存在则要求 `where uv` 成功并执行
   `uv sync`，失败就打印中文错误并 `pause; exit /b 1`。
5. 设 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`（避免 Windows 控制台中文乱码）。
6. 打印访问地址 / 数据目录 `%USERPROFILE%\.music-sync-hub` / "按 Ctrl+C 停止"。
7. 以 `"%PY%" -m app.main --host %HOST% --port %PORT%` 启动；非 0 退出码时提示并 `pause`，
   保证用户能看到报错（否则窗口一闪而过）。

### 3.6 `run-test.py`（演示包启动器，注意它**不是**测试运行器）

`run-test.py` 的 docstring 直接写明它的目的：把**演示环境**一条命令跑起来，并保证
**绝不碰** `%USERPROFILE%\.music-sync-hub`。流程：

1. `prepare_data_dir()`：建 `<包>/userdata/`，必要时写一份**只含一个演示源**（`id="demo"`，
   `root=<包>/demo`）的 `config.toml`；`--rescan` 时强制重写。
2. 环境变量：`MSH_TEST_MODE=1`（水印开关）、`MSH_DATA_DIR=<包>/userdata`（数据隔离）、
   `PYTHONUTF8=1`、`PYTHONPATH` 追加包根。
3. `subprocess.Popen([sys.executable, "-u", "-m", "app.main", …])`。
4. `wait_ready()` 轮询 `/api/system`；就绪后 `ensure_scanned()` 打 `/api/scan/demo` 并轮询
   `/api/busy` 直到不忙；最后打开浏览器。
5. `finally` 里 `terminate()`（超时再 `kill()`）。

其中"测试模式"的语义在 `config.test_mode()` 的 docstring 里被明确限定：**不改变任何业务行为**，
只做两件事——服务端在 `/` 注入水印 DOM、`/api/system` 返回 `test_mode: true`。

---

## 4. 配置系统

### 4.1 文件与目录

| 文件 | 路径（默认） | 内容 | 写入方式 |
|---|---|---|---|
| `config.toml` | `<data_dir>/config.toml` | `[[sources]]` 数组 + `[settings]` 表 | `_write_text()`：`.tmp` → `os.replace` |
| `secrets.toml` | `<data_dir>/secrets.toml` | `"ref" = "口令"` | 同上 + `os.chmod(0o600)` |
| `index.sqlite3` | `<data_dir>/index.sqlite3`（可被 `MSH_DB` 覆盖） | SQLite 索引 | `db.py` |
| `mappings.json` | `<data_dir>/mappings.json` | 按数据源分组的映射规则 | `mappings_api._save_all()`：`.part` → `os.replace` |
| `logs/msh.log` | `<data_dir>/logs/msh.log` | 运行日志（4 MB × 3 轮转） | `applog.setup()` |

### 4.2 数据目录解析顺序（`config.data_dir()`）

```
1) 环境变量 MSH_DATA_DIR 非空        → 用它（expanduser）
2) MSH_DATA_DIR 为空/未设 且 处于测试进程 → <系统临时目录>/msh-tests-fallback
3) 其它（正常使用）                  → ~/.music-sync-hub
最后一律 p.mkdir(parents=True, exist_ok=True)
```

"处于测试进程"的判据是 `_in_test_run()`：`sys.modules` 里存在名为 `tests` 或 `tests.xxx` 的
模块（结果缓存在 `_TEST_RUN_CACHE`，因为该函数被频繁调用）。

**为什么必须有第 2 条兜底（这不是理论风险，是真实事故）**：测试之间靠"设 `MSH_DATA_DIR`
到临时目录、teardown 再 pop 掉"隔离，缺口在 **pop 之后到下一个测试设好之前**——这期间
`MSH_DATA_DIR` 是空的。修 B8（`_inside` 支持相对路径）时新加的一个测试在 `finally` 里调用
`load_config(force=True)`，正好撞上空窗期，**把用户真实 `config.toml` 里的两个数据源换成了
临时目录里的假数据源**。这段事故记录在 `config.data_dir()` 与 `tests/__init__.py` 的 docstring 里。

`tests/__init__.py` 是第一道防线：导入时就 `mkdtemp(prefix="msh-tests-")`，并把
`MSH_DATA_DIR` 指向该临时目录下的哨兵路径 `.msh-tests-sentinel`、`MSH_DB` 指向
`<tmp>/index.sqlite3`，同时 `atexit` 删目录。`config.data_dir()` 是第二道防线。
`mappings_api._data_dir()` 的注释特意强调：**必须**走 `config.data_dir()`，不能自己再写一遍
`MSH_DATA_DIR or ~/.music-sync-hub`（否则 mappings.json 会重演同一个坑）。

### 4.3 `config.toml` 结构

生成逻辑在 `config._dump_toml()`（手写 TOML，不引入序列化依赖）；首次运行还会在文件头插入
`_TEMPLATE_HEADER`（含两个示例数据源，注释掉的）。形状：

```toml
# Music Sync Hub 配置文件
# 修改后可在界面点"重新加载配置"，或重启程序。

[[sources]]
id = "win"
name = "Windows 音乐库"
type = "local"
root = "D:\\Music"
enabled = true
host = ""
port = 0
username = ""
secret_ref = ""
base_url = ""
share = ""
domain = ""
follow_symlinks = false
read_only = false

[sources.extras]
slow_disk = true
scan_workers = 2
transfer_mode = "fast"

[settings]
scan_workers = 8
duration_tolerance_sec = 2.0
# …（16 项，见 4.4）
```

`extras` 是自由字典，会被写成 `[sources.extras]` 子表；空的 `extras` 直接省略。

### 4.4 `Settings` 全字段（16 项，`config.Settings`）

| 字段 | 类型 | 默认 | 校验（`_NUM_LIMITS` / `validate_setting`） | 用途 |
|---|---|---|---|---|
| `scan_workers` | int | 8 | 1–64，不允许 0 | 扫描时解析元数据的并发线程数 |
| `hash_workers` | int | 4 | 1–64，不允许 0 | 补算哈希的并发数 |
| `hash_size_limit_mb` | int | 512 | 0–1048576，0 合法 | 超过此体积只做"快速哈希"（0 = 不限制） |
| `quick_hash_probe_kb` | int | 256 | 1–65536，不允许 0 | 快速哈希头尾各取多少 KB（实际下限 16 KB） |
| `duration_tolerance_sec` | float | 2.0 | 0.0–600.0，0 合法 | "同一首歌"的时长容差；**门禁 = 2×该值**（见第 8 章） |
| `name_similarity` | float | 0.86 | 0.0–1.0，0 合法 | 模糊匹配阈值（对比计划里作为 `threshold` 的参考量） |
| `default_keep_policy` | str | `"best_quality"` | 只能是 `best_quality`/`largest`/`oldest`/`newest`/`ask` | 查重"保留谁"的默认策略 |
| `trash_dir` | str | `""` | 路径字段（禁 `<>"\|?*` 与控制字符，≤400 字） | 回收站根；空则用 `<源根>/.music-sync-trash` |
| `cover_cache_size` | int | 800 | 0–200000，0 合法 | 内存封面缓存条数（**注意**：`main._cover_cache` 实际用的是"超过 2000 条就整体清空"的硬编码策略，并不读这个字段） |
| `max_compare_tracks` | int | 8000 | 10–2000000，不允许 0 | 单次对比/查重每侧最多取多少条，超出会截断并告警 |
| `auto_index_refresh_seconds` | int | 300 | 0–86400，0=关闭 | 后台索引自动刷新间隔（indexwatch） |
| `auto_full_rescan_hours` | float | 24.0 | 0–87600，0=不兜底 | 超过多久没扫就整源扫一次 |
| `station_dir` | str | `""` | 路径字段 | 中转站目录；空 = `<程序目录>/station` |
| `station_max_gb` | float | 20.0 | 0.0–1048576.0，0 合法 | 中转站配额（GB），0 = 不限额 |
| `staging_dir` | str | `""` | 路径字段 | 上传暂存区；空 = `<data_dir>/uploads` |
| `staging_max_gb` | float | 5.0 | 0.0–1048576.0，0 合法 | 暂存区配额（GB），0 = 不限额 |
| `staging_stale_hours` | float | 48.0 | 0.0–31536000.0（24×3650），0 合法 | 批次滞留多久算"该处理了"；0 = 不提醒 |
| `ignored_names` | list[str] | `[".DS_Store","Thumbs.db","desktop.ini","@eaDir"]` | 列表项截断 200 字、最多 500 项 | 扫描时要忽略的文件名 |

校验实现要点（`config.validate_setting`，同一套逻辑同时服务"PUT /api/settings"和"手改
config.toml 后加载"）：

* 未知键 → `SettingsError("不是可识别的设置项")`；
* 数值：字符串会先 `strip()`，空串当 0；**显式拒绝 NaN / inf**（否则 `bytes > cap` 恒为 False，
  配额保护会静默失效）；
* 字符串：≤ `_STR_MAX = 400` 字符；含 `ord(c) < 32` 或 `0x7F` 的不可见字符一律拒绝；
  路径字段额外挡 Windows 非法字符 `<>"\|?*`（盘符冒号被放过：`head` 是单字母时从 `tail` 开始查）；
* 布尔：只接受真正的 `bool`；
* 列表：必须 list，元素 `str(x)[:200]`，最多 500 项。

`load_config()` 的容错策略：**保留默认值并把不合法项记进 `_settings_errors`**
（`settings_errors()` 可读），同时 `print` 出来。理由（注释原话）：
"手改配置文件是正常用法，所以不合法时保留默认值并记下来，由启动/设置页把问题显示给用户，
而不是静默改掉或直接崩"。

### 4.5 `SourceConfig` 全字段（15 项）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | str | 必填 | 数据源唯一 id；网页新增时缺省生成 `src<epoch秒>` |
| `name` | str | 必填 | 显示名；空则回退成 `id`（`main._validate_source`） |
| `type` | str | `"local"` | 必须在 `available_types()` 的 kind 集合里（`local/sftp/webdav/http/smb/ftp`） |
| `root` | str | `""` | 根路径或 URL；本地 `D:\Music`、Linux `/mnt/music` |
| `enabled` | bool | `True` | 停用后 `/api/sources/status` 直接返回"已停用" |
| `host` | str | `""` | sftp/smb/ftp 主机 |
| `port` | int | `0` | 0 表示用各协议默认端口 |
| `username` | str | `""` | 登录名 |
| `secret_ref` | str | `""` | **指向 `secrets.toml` 的键名**，配置文件里不存口令 |
| `base_url` | str | `""` | webdav/http 的基地址（可与 `root` 合并） |
| `share` | str | `""` | SMB 共享名 |
| `domain` | str | `""` | SMB 域 |
| `follow_symlinks` | bool | `False` | 扫描是否跟随符号链接（默认不跟随） |
| `read_only` | bool | `False` | **保险丝**：勾选后任何写操作都拒绝（`can_write`/`can_delete` 全为 False） |
| `extras` | dict | `{}` | 行为参数：`slow_disk` / `scan_workers` / `hash_workers` / `skip_full_hash` / `transfer_mode` / `tls` / `key_passphrase_ref` 等 |

`SourceConfig.from_dict()` 只接受 dataclass 已知字段（`known = SourceConfig.__dataclass_fields__`），
所以前端 POST 上来的多余键会被忽略，不会炸。

### 4.6 密钥引用机制（`secret_ref`）

```
config.toml:   secret_ref = "win_password"        # 只有名字
secrets.toml:  "win_password" = "hunter2"          # 真正的口令（权限 600）
运行时:        password = get_secret(sc.secret_ref) # 传给 build_source(cfg, password)
```

* `sources/__init__.py::SourceManager.get()` 每次建实例都走
  `get_secret(sc.secret_ref, "") if sc.secret_ref else ""`；
* 新增/修改数据源时如果 payload 里带 `password`，会写 `secret_ref = secret_ref or f"{id}_password"`
  并 `save_secret()`（`main.create_source` / `update_source`）；
* `save_secret()` 会把整个字典重写一遍（先转义 `\` 和 `"`），写成功后再 `chmod 0o600`
  （失败忽略——Windows 上 chmod 语义有限）；
* `main._source_view()` 用 `has_secret` 字段告诉界面"这个源有没有存口令"，**不回传口令本身**；
* SFTP 的私钥口令走另一个引用：`extras.key_passphrase_ref`（`sources/sftp.py` 文件头）。

### 4.7 配置写回的两处细节

1. **原子写**：`_write_text()` 先写 `<name>.toml.tmp` 再 `os.replace()`，避免写到一半断电
   留下坏配置。
2. **控制字符转义**：`_toml_val()` 把 `\n`/`\r`/`\t`/其它控制字符转成 `\n`/`\r`/`\t`/`\uXXXX`。
   注释解释了原因：TOML 基本字符串不允许裸控制字符，用户手改时粘进一个带换行的路径，
   保存一次就能把整个配置文件写成语法错误——"改一处、整个程序起不来"。

---

## 5. 数据模型

### 5.1 `SCHEMA`（`core/db.py`，逐字抄录）

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS source (
  id          TEXT PRIMARY KEY,
  name        TEXT,
  type        TEXT,
  root        TEXT,
  last_scan   REAL,
  track_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS track (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     TEXT NOT NULL,
  path          TEXT NOT NULL,
  rel_path      TEXT NOT NULL DEFAULT '',
  name          TEXT NOT NULL,
  parent        TEXT NOT NULL DEFAULT '',
  ext           TEXT NOT NULL DEFAULT '',
  size          INTEGER NOT NULL DEFAULT 0,
  mtime         REAL NOT NULL DEFAULT 0,
  -- 元数据
  title         TEXT, artist TEXT, album TEXT, album_artist TEXT,
  track_no      TEXT, year TEXT, genre TEXT,
  duration      REAL, bitrate INTEGER, sample_rate INTEGER, channels INTEGER,
  bits_per_sample INTEGER, codec TEXT, is_lossless INTEGER DEFAULT 0,
  has_cover     INTEGER DEFAULT 0,
  cover_mime    TEXT, cover_hash TEXT,
  meta_status   TEXT DEFAULT 'pending',   -- ok | partial | error | skipped
  meta_error    TEXT,
  -- 计算字段
  name_key      TEXT DEFAULT '',
  dup_key       TEXT DEFAULT '',
  quality       REAL DEFAULT 0,
  -- 哈希
  hash_quick    TEXT, hash_full TEXT, hash_state TEXT DEFAULT 'none',
  scanned_at    REAL DEFAULT 0,
  UNIQUE(source_id, path)
);
CREATE INDEX IF NOT EXISTS idx_track_source ON track(source_id);
CREATE INDEX IF NOT EXISTS idx_track_namekey ON track(source_id, name_key);
CREATE INDEX IF NOT EXISTS idx_track_dupkey ON track(dup_key);
CREATE INDEX IF NOT EXISTS idx_track_hash ON track(hash_quick);

CREATE TABLE IF NOT EXISTS cover (
  hash    TEXT PRIMARY KEY,
  mime    TEXT,
  width   INTEGER,
  height  INTEGER,
  data    BLOB,
  bytes   INTEGER,
  created REAL
);

CREATE TABLE IF NOT EXISTS job (
  id        TEXT PRIMARY KEY,
  kind      TEXT,
  state     TEXT,
  total     INTEGER DEFAULT 0,
  done      INTEGER DEFAULT 0,
  message   TEXT,
  started   REAL,
  finished  REAL,
  detail    TEXT
);
```

### 5.2 四条索引的用途

| 索引 | 列 | 服务的查询 |
|---|---|---|
| `idx_track_source` | `(source_id)` | 按源取全部曲目（`db.all_tracks`、`counts_by_source`） |
| `idx_track_namekey` | `(source_id, name_key)` | 同库按归一化文件名分组/查重 |
| `idx_track_dupkey` | `(dup_key)` | 跨源按"标题+艺术家+时长桶+版本"分桶 |
| `idx_track_hash` | `(hash_quick)` | 内容指纹比对（快速查重） |

> 注意 `track` 表的唯一约束是 `UNIQUE(source_id, path)`，**不是** `path`——两个数据源可以有
> 完全相同的绝对路径（例如两个库都在 `D:\Music`），这是跨库查重必须带 `source_id` 的根源
> （见 `main._duplicates_core` 里 `d["key"] = f"{sid_of}::{d['key']}"` 与 `item["did"]`）。

### 5.3 `track` 关键字段的用途

| 字段 | 用途 | 写入点 |
|---|---|---|
| `path` | 源内**绝对**内部路径（正斜杠归一化），所有磁盘操作的落点 | `scanner.scan_source`（`P.norm(e.path)`） |
| `rel_path` | **相对数据源根**的路径。计划/执行都用它当目标路径（由目标源的 `root` 决定实际落点），天然保持两侧目录结构一致 | `sources.abs_path/rel_path`、`scanner` |
| `parent` | 直接父目录（绝对），目录树聚合与 `tracks(prefix=…)` 靠它 | `P.parent(path)` |
| `size` | 字节数。既是"是否同一份文件"的廉价判据（`size_similarity`、`identical`），也是体积统计与"目标装不装得下"的依据 | 扫描 `Entry.size` |
| `mtime` | 修改时间，只用于增量缓存失效判定（`_stale`）与"保留最新/最早"策略 | 扫描 `Entry.mtime` |
| `duration` | 秒。**匹配的第二强证据**：同一录音时长几乎一致，试听/剪辑版会被判为不同 | `audio.parse_bytes` |
| `name_key` | `P.name_key(name)` 的结果：去掉音轨号、码率、`(1)` 之类噪声后的归一化文件名。配对阶段 1 的键 | `_row_from_meta` |
| `dup_key` | `title\|artist\|时长桶\|版本标记`，精确字符串键。同库查重的主键、跨库配对阶段 2 的键 | `_row_from_meta` → `matching.dup_key` |
| `quality` | `audio.quality_score()` 的综合分，查重"保留最好那份"的第一排序键 | `_row_from_meta` |
| `hash_quick` | 抽样哈希（头尾各 probe）+ 总长度。跨库/同库"字节级"证据 | `scanner.quick_hash*`、`ensure_hashes` |
| `hash_full` | 全文件 SHA-1。设了它 `hash_state="full"`，`_stable_hash` 优先取它 | `ensure_hashes(mode="full")` |
| `hash_state` | `none` / `quick` / `full`。**决定"这条记录的哈希算不算数"**——`_stable_hash` 要求 `hash_state in ("quick","full")` | 哈希相关全部路径 |
| `meta_status` / `meta_error` | `ok/partial/error/skipped` + 错误摘要（截断 500 字）。解析失败的文件仍然入索引（`_error_row`），只是没有时长/码率 | `_row_from_meta` / `_error_row` |
| `cover_hash` | 指向 `cover` 表的缩略图指纹（`sha1(缩略图)`），列表页靠它出图 | `scan_source` |

### 5.4 WAL、连接管理与 `atexit`

* `SCHEMA` 开头两条 PRAGMA：`journal_mode=WAL`（并发读写）+ `synchronous=NORMAL`（性能折中）。
  WAL 的副作用是数据先进 `-wal`，所以 `main._db_size()` 会把 `""`/`-wal`/`-shm` 三个文件
  的大小加起来算"索引真实占用"。
* **连接按线程局部保存**：`Database._conn()` 用 `threading.local()`，`sqlite3.connect(...,
  timeout=30.0, check_same_thread=False)`，`row_factory=Row`，并在每个连接上再设
  `PRAGMA foreign_keys=ON`、`PRAGMA busy_timeout=30000`。
  docstring 的理由：所有连接跑在各自线程里（FastAPI 线程池 + 扫描线程池），必须支持并发读写。
* **写串行化**：`Database.write()` 是上下文管理器，进入时取 `self._write_lock`（`RLock`），
  正常退出 `commit()`、异常 `rollback()` 后重抛。
  `Executor._sync_index` 的注释专门强调：**必须走 `db.write()`**，裸 `execute()` 不会提交，
  界面（别的连接）读不到更新。
* **连接登记表**：`_all_conns: Dict[int, conn]` 记下每个线程的连接（线程局部变量别的线程看不见），
  `close_all()` 一次性关干净。
* **`atexit` 关闭**：`Database.__init__` 里用 `weakref.ref(self)` 注册 `_close_at_exit`。
  注释记录的原因很具体：不注册的话 sqlite 文件句柄会挂到解释器退出，Windows 上的表现是
  **临时目录删不掉**（`WinError 32`），测试里到处显式 `db.close_all()` 就是被这个逼出来的；
  换数据源（rebind）之后旧 `Database` 的连接也会一直不释放。用弱引用是为了不拦着对象被 GC。

### 5.5 迁移机制

`Database._migrate()`：读 `PRAGMA table_info(track)` 得到现有列，然后对 7 个"后来才加的列"
逐个 `ALTER TABLE track ADD COLUMN`：

```python
wanted = {
    "hash_quick": "TEXT", "hash_full": "TEXT", "hash_state": "TEXT DEFAULT 'none'",
    "rel_path": "TEXT NOT NULL DEFAULT ''", "name_key": "TEXT DEFAULT ''",
    "dup_key": "TEXT DEFAULT ''", "quality": "REAL DEFAULT 0",
}
```

即：**只做加法**（没有版本号表、没有 downgrade），`CREATE TABLE IF NOT EXISTS` 保证新库直接
建全量结构，老库靠补列追平。索引同理（`CREATE INDEX IF NOT EXISTS`）。

### 5.6 业务方法一览（`Database`）

| 方法 | 作用 |
|---|---|
| `upsert_source` / `delete_source` | 写 `source` 表；删除时连带删该源所有 `track` |
| `upsert_tracks(rows)` | 按 `(source_id, path)` 批量 `ON CONFLICT DO UPDATE`（33 个字段） |
| `update_computed` / `update_computed_many` | 只更新"计算字段"（哈希、quality 等） |
| `tracks(sid, prefix, limit, offset)` | 按源 + 目录子树取曲目，`ORDER BY path COLLATE NOCASE` |
| `all_tracks` / `track` / `track_by_id` | 全量 / 按 (源,路径) / 按自增 id |
| `remove_track` / `remove_tracks` / `remove_missing` | 单条 / 批量 / **增量扫描后清理已消失项** |
| `delete_tracks_by_paths` | 跨库传输后清源侧记录（注释：不清则"文件已搬走、搜索还能搜到，点进去播放失败"） |
| `counts_by_source` | `{source_id: n}`，几乎每个接口都用来做"整库口径" |
| `set_scan_meta` | 更新 `source.last_scan` 与 `track_count` |
| `put_cover` / `get_cover` | 封面缩略图 BLOB（`ON CONFLICT DO NOTHING`，同图只存一份） |
| `create_job` / `update_job` / `get_job` / `list_jobs` | `job` 表，崩溃恢复用 |
| `vacuum` | 整理数据库 |

---

## 6. 数据源抽象

### 6.1 `Entry`（`sources/base.py`）

```python
@dataclass
class Entry:
    path: str        # 源内部绝对路径（正斜杠）
    name: str        # 文件名
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    etag: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
```

设计要点（base.py 文件头）：`listdir` **只做一次目录枚举**，返回轻量 `Entry`，元数据解析交给
扫描器——避免网络源的 N+1 请求。

### 6.2 `MusicSource` 接口逐方法

| 方法/属性 | 语义 | 谁实现/覆盖 |
|---|---|---|
| `open()` / `close()` / `__enter__` / `__exit__` | 生命周期；`with src:` 可用 | 各协议各自管理连接池（按线程 id 缓存） |
| `can_write` | `not cfg.read_only` | 基类属性 |
| `can_delete` | 默认等于 `can_write` | 基类属性 |
| `supports_range_read` | 默认 True（能否只读文件一段） | 基类 |
| `_guard_write()` | 不可写就抛 `ReadOnlyError` | 所有写方法开头调用 |
| `device_info(measure=False)` | 返回 `{kind, removable, network, slow, read_mbps, label, notes}`。基类返回"未知"，**只有本地源真的探测** | `local.py` |
| `is_slow` / `is_removable` | 便捷属性；`is_slow` 先用 `cfg.extras["slow_disk"]`（用户手动标记），再看 `device_info()` | 基类 |
| `abs_path(path)` | 把相对/绝对路径**收敛到 root 内**并返回源内绝对路径 | 基类 |
| `rel_path(path)` | 剥掉 root 前缀，得到源内相对路径 | 基类 |
| `stat(path)` | 返回 `Entry` 或 `None` | 必须实现 |
| `listdir(path)` | 返回该目录的 `Entry` 列表（**一次请求**） | 必须实现 |
| `walk(path, on_dir, should_descend)` | 深度优先遍历，`on_dir(dir, entries)` 回调、`should_descend(path)` 剪枝；**起点目录的 `SourceError` 会抛出，子目录的错误被吞掉继续** | 基类（所有源共用） |
| `open_read(path, offset, length)` | 返回可读二进制流 | 必须实现 |
| `read_range(path, offset, length)` | 便捷读（内部 `open_read` + `read` + `close`） | 基类 + `local.py` 覆盖 |
| `read_head(path, nbytes)` | `read_range(path,0,n)` | 基类 |
| `mkdirs` / `write_file` / `replace` / `move` / `remove` / `remove_dir` | 写操作原语 | 必须实现（`move` 基类退化成 `replace`） |
| `copy_from(src, src_path, dst_path, progress, verify_hash)` | 跨源复制：**先写 `<dst>.mshpart` → `replace()` 原子改名**，异常时删临时文件再重抛 | 基类；`local.py`/`smb.py`/`sftp.py`/`ftp.py` 有加速实现 |
| `exists(path)` | `stat() is not None`（异常当 False） | 基类 |
| `display(path)` | 按平台风格显示路径（Windows 本地源用 `\`） | 基类 |

### 6.3 注册表与 `available_types()`

```python
_REGISTRY: Dict[str, type] = {}

def register(cls):            # 装饰器：@register
    _REGISTRY[cls.kind] = cls
    return cls

def build_source(cfg, password=""):   # 未知 type → SourceError
    ...
```

`sources/__init__.py` 顶部"导入即注册"六个实现，`SourceManager` 按 id 复用实例，
并用**配置签名**判断是否需要重建：

```python
def _sig(sc):  # type|root|host|port|username|secret_ref|base_url|share
```

签名变了就 `close()` 旧的再 `build_source()`（改端口/改路径后不会继续用旧连接）。

`available_types()` 的行为（注释记录了一次真实事故）：

1. **必须从注册表推导**，不能手写。曾经写死只有 `local`，而四个网络源"早就实现并注册好了"，
   导致后端能力**在界面上根本选不到**；
2. 给 `experimental=True` 的类自动加"（实验性）"后缀——**在这里统一加**，不写进各源的 `label`，
   否则改标记时容易只改一处、界面和代码说法不一致；
3. 排序键 `(kind != "local", kind)`：`local` 永远第一；
4. 返回字段：`kind` / `label` / `hint` / `experimental` / `verify_note`。

### 6.4 六个实现的差异

| kind | 类/文件 | label | `experimental` | 协议要点（源码可证） |
|---|---|---|---|---|
| `local` | `LocalSource` / `local.py` | 本地 / 已挂载盘符 | **False** | 唯一"非实验性"。`move` 优先 `os.replace`，遇 `EXDEV`（跨盘）才退化成"copyfile → 比对 size → replace → 删源"；`_BoundedReader` 支持范围读；有 `dir_stat`/`trash_dir`/`remove_empty_dir`/`rename_dir`/`copy_from` |
| `sftp` | `SftpSource` / `sftp.py` | SFTP（SSH 文件传输） | True | paramiko；连接按线程 id 缓存并检查 `transport.is_active()`；`_prefetch_max = 64 MB`；密码或私钥（`extras.key_passphrase_ref`） |
| `webdav` | `WebDavSource` / `webdav.py` | WebDAV | True | 只用 `PROPFIND/GET/PUT/MKCOL/MOVE/DELETE`，`httpx` 手写；`_PROPFIND_BODY` 请求 5 个属性（displayname/getcontentlength/getlastmodified/getetag/resourcetype） |
| `http` | `HttpDirSource` / `httpdir.py` | HTTP 目录（只读） | True | 解析 `<a href>` 与 `<pre>` 两种 autoindex 形态，支持 nginx `autoindex_format json`；**只读**，写方法直接 `ReadOnlyError` |
| `smb` | `SmbSource` / `smb.py` | SMB / Windows 共享 | True | 优先 `smbclient`；**未安装则自动回退**成"把 root 当本地已挂载路径"（内部包一个 `LocalSource`），`using_fallback` 属性可查 |
| `ftp` | `FtpSource` / `ftp.py` | FTP / FTPS | True | 标准库 `ftplib`；被动模式；**一律用 MLSD 列目录**，服务器不支持就明确报错（不猜 `LIST` 文本格式）；`extras.tls = true` 走 FTPS |

各源 `verify_note` 原文（这是项目对"验证到什么程度"的诚实声明）：

* sftp：只做过代码实现，**没有**连过任何真实 SSH 服务器。上传、断点读、删除进回收站（改名）这几条路径都未验证。
* webdav：只做过代码实现，**没有**连过任何真实 WebDAV 服务端。PUT / MKCOL / PROPFIND / MOVE 的组合行为未验证。
* http：只做过代码实现，**没有**连过任何真实 HTTP 目录服务。各服务端 autoindex 的 HTML 格式差异较大，解析可能失效。
* smb：只做过代码实现，**没有**连过任何真实 Windows 共享。依赖 pysmb，认证、域、共享内路径语义都未验证。
* ftp：只做过代码实现，**没有**连过任何真实 FTP 服务器。MLSD 列目录、REST 断点读、回收站改名都未验证。

`available_types()` 的注释给出了"实验性"的判定标准：**没有对着真实服务器完整验证过**；
按这个标准"除了本地源，所有网络协议都算"，并且"标成稳定而实际没测过，等于把风险转嫁给用户"。

### 6.5 路径坐标系约定（为什么统一用源内相对路径）

代码里同时存在两套写法，边界非常清楚：

| 坐标系 | 形态 | 出现位置 | 用途 |
|---|---|---|---|
| **绝对内部路径** | `D:/Music/专辑/a.flac`、`/mnt/music/…`，永远正斜杠（`P.norm`） | 索引 `track.path`、`Entry.path`、`stat/read_range/write_file` 的参数 | 真正落到磁盘的定位 |
| **源内相对路径** | `专辑/a.flac` | `track.rel_path`、`Step.dst_path`（计划阶段）、`/api/files/transfer` 的 `dst_dir` | 跨源搬运时的"结构对齐" |

统一用相对路径的理由（`plan.missing_steps` 注释）：**`dst_path` 用相对路径，由目标源的 root
决定实际落点，天然保持目录结构一致**。如果计划里写死绝对路径，把 `D:\Music\A` 的目录结构
搬到 `/mnt/music` 就会变成"在 /mnt/music 下再造一个 D:/Music/A"。

`MusicSource.abs_path()` 的收敛语义要注意：它把"已经是绝对路径但不在 root 内"的输入
**强制拼接回 root**（`P.join(root, path.lstrip("/"))`）。这是安全设计（避免越权），但也意味着
"收敛结果看不出原本是否越界"，所以破坏性操作（移动）额外走 `ops._inside_root()` 做严格校验（见 10.6）。

---

## 7. 扫描与索引

### 7.1 `scan_source` 签名

```python
def scan_source(sid: str, source: MusicSource, db, job: Optional[JobState] = None,
                subpath: str = "", workers: int = 8,
                compute_hash: bool = False, hash_limit_mb: int = 512,
                quick_probe_kb: int = 256, progress: Optional[Callable[[str], None]] = None,
                max_files: int = 0, skip_full_hash: bool = False) -> ScanResult
```

参数语义：

* `subpath`：只扫某个子目录（局部扫描；清理失效记录时也只清该子树）；
* `workers`：解析并发；**调用方负责按盘的性质压过**（`core.device.effective_workers`），
  扫描器只保证"不再往上抬"（`max(1, workers)`）；
* `compute_hash`：是否顺手算快速哈希；`skip_full_hash` 为真时**一律不算**；
* `hash_limit_mb`：超过这个体积的文件不参与"整读"分支（`head_only`）；
* `max_files`：硬上限，触顶后 `res.truncated = True` 并停止收集；
* `progress`：额外的进度回调（`say()` 里除了写 `job.message` 还会 `job.touch()`）。

### 7.2 四个阶段

```
1. 遍历（单线程）
   source.walk(subpath, on_dir=…, should_descend=…)
     · on_dir 每 25 个目录报一次进度（"已发现 N 个音频文件"）
     · should_descend 剪掉 .music-sync-trash / .msh / $recycle.bin / system volume information
     · 命中 P.is_audio → entries；命中 P.is_cover → cover_files（顺带登记外挂封面）
     · job.cancel 或 max_files 触顶 → 停

2. 元数据解析（ThreadPoolExecutor(workers)）
   每个 Entry：
     · db.track(sid, path) 命中且 !_stale → 直接复用旧行（保留已算好的 hash_*），res.cached += 1
     · 否则 _fetch_bytes()：
         size <= FULL_READ_LIMIT(12 MB) → 整读（head_only=False）
         否则只读头部：无损格式 3 MB / 其它 2 MB（head_only=True）
       A.parse_bytes() → AudioMeta → _row_from_meta()（补 name_key/dup_key/quality/cover_*）
     · 封面：内嵌封面 → make_thumbnail + sha1 → 进 covers_to_store；
             无内嵌则 A.find_cover_file() 记下 _cover_path，第 3 阶段再读
     · 快速哈希条件：compute_hash and not skip_full_hash
                      and size <= hash_limit_mb*1MB and not head_only
   单个文件异常 → res.errors += 1，仍写入一条 _error_row（title 用文件名、codec 用扩展名大写）

3. 封面落库
   处理 row["_cover_path"]（外挂 cover.jpg 等）→ 缩略图 → db.put_cover()
   统一裁剪成 _TRACK_FIELDS（33 个）→ db.upsert_tracks(clean)

4. 清理失效记录
   if not truncated and not subpath:  db.remove_missing(sid, alive)
   elif subpath:                      只删该目录下已消失的记录
   然后 set_scan_meta() / upsert_source() / jobs.finish(job, result=res.to_dict())
```

`ScanResult` 字段：`source_id, seen, parsed, cached, errors, removed, covers, truncated,
duration, samples`（`samples` 只带前 20 条，用于界面快速自检）。

### 7.3 增量与"什么时候算过期"（`_stale`）

```python
CACHE_ETAG_TOLERANCE = 1.0   # 秒
```

判定顺序：

1. `cached["size"]` 与 `Entry.size` **都存在且不等** → 过期；
2. `cached["mtime"]` 与 `Entry.mtime` **都存在且差 > 1.0 秒** → 过期；
3. 两侧都没有 size/mtime，而 `meta_status in (None, "pending")` → 过期。

也就是说：**size+mtime 都没变就复用旧行**，连标签都不重读——这是大库增量扫描能快的前提；
网络源如果只有 etag 没有 mtime，会退化成"每次都解析"（`etag` 字段在 `Entry` 里有，但
`_stale` 目前只用 size/mtime，`未使用 etag`）。

### 7.4 慢速盘压制（`skip_full_hash`）

`skip_full_hash` 的存在理由写在 `scan_source` 的 docstring 里：**U 盘上按内容做全量哈希会让
扫描从几分钟变成几十分钟，而且中途一拔盘就白干**。此时只保留"大小 + 时长 + 标签"这套轻量判据，
查重仍然可用（`size_similarity` / `duration_similarity`），只是少了字节级铁证。

调用链（`main.start_scan`）：

```python
extras = dict(sc.extras or {})
slow_disk = bool(extras.get("slow_disk"))
if slow_disk:
    workers = DEV.effective_workers(extras, workers)     # 压并发
skip_full_hash = bool(extras.get("skip_full_hash")) or slow_disk
```

`device.effective_workers` 的关键细节（注释记录过一次反向坑）：
`slow_disk` 只是"建议低并发"，用户显式写的 `scan_workers` 优先，**包括写成 0**——
0 表示"不限制"，必须沿用全局默认 `base`，不能 `min(base, 0) → 1` 把"不限制"执行成"只准一个线程"。
默认值：`SLOW_WORKERS = 2`、`SLOW_HASH_WORKERS = 1`。

### 7.5 哈希的三种口径（务必区分）

| 函数 | 输入 | 算法 | 用途 |
|---|---|---|---|
| `quick_hash_bytes(data, probe_kb=256)` | **整份文件内容** | 若 `len(data) <= 2*probe`：`sha1("F"+data)`；否则 `sha1("Q" + 头probe + 尾probe + str(len))` | 扫描阶段（本地源）把整份内容算出的值写入 `hash_quick` |
| `quick_hash(source, path, size, probe_kb)` | 数据源 + 路径 | `size <= QUICK_FULL_THRESHOLD`(6 MB) → 整读后 `sha1("F"+data)`；否则 Range 读头尾 → `sha1("Q"+head+tail+size)` | 远端源的 `ensure_hashes`（避免整文件下载） |
| `full_hash(source, path)` | 数据源 + 路径 | 分块（1 MB）读全文件 `sha1` | `ensure_hashes(mode="full")`，同时把 `hash_quick` 也设成同一个值 |
| `quick_hash_local(path)` | 本地路径 | `quick_hash_bytes(整份内容)` | 入库查重时**必须与索引里的值逐位一致**，所以复刻扫描器的实际做法（整份读、整份哈希），而不是抽样版 |

`QUICK_FULL_THRESHOLD = 6 MB` 的注释解释了为什么小文件要整份哈希：**抽样在"头尾相同、中间不同"
的文件上会误判**（同一首歌的高/低采样率版本，头尾标签区一致），多读几 MB 换准确性很值。

`ensure_hashes(...)` 的语义：

* 只处理 `hash_state` 不满足要求的记录（`quick` 模式跳过 `quick`/`full`，`full` 模式只跳过 `full`）；
* 两侧都受 `limit_mb` 约束（超限直接跳过，计入 `skipped`）；
* 并发 `workers`（默认 4），每 5 条刷新 `job.current`，每条 `job.touch()`；
* **复用外层任务时不会 `finish`**：`if job.kind == "hash": get_jobs().finish(...)`。
  注释：外层（比如查重）还没结束，提前标完成会让界面显示"已完成"但结果还没出来。

---

## 8. 匹配算法

这是整个项目最核心、注释也最密的模块（`core/matching.py`，1015 行）。它要回答两个不同的问题：

* **跨库配对**（`pair_sources`）：左库的这一首，在右库里是哪一首？（一对一）
* **同库查重**（`group_duplicates`）：这个库里有哪几组"同一首歌的多份副本"？（一对多，并查集）

### 8.1 打分模型

```python
W_NAME     = 0.38   # 文件名相似度
W_DURATION = 0.34   # 时长相似度
W_META     = 0.16   # 标签（album 0.5 / artist 0.3 / title 0.2）
W_SIZE     = 0.12   # 体积相似度
score = W_NAME*ns + W_DURATION*ds + W_META*ms + W_SIZE*ss   # 权重和 = 1.00
```

`Match` 数据类字段：`left, right, score, name_sim, duration_diff, artist_sim, reasons,
identical`；`to_dict()` 另外算出 `confidence`（`identical ≥0.94 / high ≥0.86 / medium ≥0.72 /
low` 其余）、`same_size`、`size_delta`、`quality_delta`。

`score_pair` 的四个加分/保底规则：

1. `artist_sim >= 0.9 and name_sim >= 0.8` → `score *= 1.05`（上限 1.0），理由串"艺术家一致"；
2. `hash_quick` 相同且 `hash_state not in (None,"none")` → `score = max(score, 0.995)`，
   理由"抽样内容哈希一致"；
3. `identical = (hash_full 相同) or (size 相同 and name_sim>=0.985 and duration_diff<=0.5)`；
4. `reasons` 会写成人话（"文件名归一化后完全一致" / "时长一致（差 0.3s）" / "有一侧缺少时长信息"…），
   界面直接展示，让用户知道"凭什么说它俩像"。

### 8.2 `name_key` / `name_similarity`

`paths.name_key(name)` 的处理流水线（顺序即代码顺序）：

```
stem → NFKC → 剥"重复下载计数"尾巴 (Song (1) / Song（2） / Song [3])
     → 删 feat./ft./with 括号段（_BRACKET_KEEP）
     → 剥音轨号前缀（_TRACKNO：`01 -` / `cd1-01 ` / `1. `）
     → 删紧贴数字的音质标记（`320k`/`96khz`/`24bit`）
     → 删 _QUALITY 词表（hq/sq/hi-res/无损/试音/母带/[FLAC]/【…】/结尾年份…）
     → 删空括号 → normalize_text（NFKC+casefold+去标点+压缩空白）
```

其中"剥重复下载计数"这条的注释很重要：浏览器/下载工具遇到同名文件会自动加 `(1)`，于是同一首歌
在库里出现两份（`Song.mp3` 与 `Song (1).mp3`），归一化后是 `song` 与 `song 1`——键不同、
名称相似度只有 0.80，**跨键合并门限 0.62 也救不回来**。而且"只在 `name_key` 这一处归一，
`dup_key` / `song_key` / 对比配对 / 跨键合并就全都一致了"（分散到各处打补丁会变成两套规则打架）。
规则只剥"纯计数括号尾巴"且要求前面还有内容，所以真实曲名 `(1)`、`Song (Live)`、`Song (Remix)` 不受影响。

`name_similarity(a, b)` 取三条路的最大值：

```
base = P.similarity(name_key(a.name), name_key(b.name))          # 文件名为主
if 任一侧有 title: base = max(base, P.similarity(title_a, title_b))
if title_a and title_b:                                          # ★ 必须两侧都有标题
    sa = name_key(artist_a + " " + title_a)
    sb = name_key(artist_b + " " + title_b)
    base = max(base, P.similarity(sa, sb))
```

第三路"艺术家+标题"必须要求**标题真的存在**，注释记录了原因：原来只判 `if sa.strip() or sb.strip()`，
而 `sa` 是"艺术家 + 标题"，标题为空时 `sa` 退化成"只有艺术家"，两个同艺术家的无标题文件
（`Bohemian Rhapsody.flac` ↔ `Smells Like Teen Spirit.flac`）会得到 `name_sim = 1.0`，
`max()` 把文件名算出的分数盖掉，后续所有"名字像不像"的门禁全部失效。

`paths.similarity(a, b)` 是混合相似度：`difflib.SequenceMatcher.ratio()` 与
`0.45*char + 0.4*jaccard(tokens) + 0.15`、以及"包含且短串≥4 字时 0.93"取最大。

### 8.3 `duration_similarity` 与 `duration_tolerance_sec` 的契约

```python
def duration_similarity(a, b, tolerance=2.0) -> (score, diff):
    da, db = a["duration"], b["duration"]
    if not da or not db:  return 0.55, None      # 未知：中间分，不奖不罚
    diff = abs(da - db)
    gate = 2.0 * tol                             # ★ 门禁 = 2×容差
    if diff <= gate:      return 1.0, diff       # 门禁内满分
    tail = max(28.0, 4.0 * tol)                  # 尾长跟着容差走
    score = max(0.0, 1.0 - (diff - gate) / tail)
```

这个契约有三个必须一起看的地方：

1. **门禁 = `2 × tolerance`**，且 `group_duplicates` 的时长门禁与这里**必须是同一个数**；
2. 门禁内给**满分**——否则"用户把容差调大"只是让边界变宽，分数还是被扣，等于没用；
3. 衰减尾巴 `tail = max(28, 4×tolerance)`。踩过的坑：原来写死 28 秒，于是 `tolerance=10`
   时门禁允许 30 秒，但 28.5 秒的一对只有 0.34 分，总分过不了阈值——表现是"调容差也没用，
   边界莫名其妙卡在 28 秒附近"。

### 8.4 `dup_key` 与 `bucket_step`

```python
def dup_key(r, bucket_step=2.0):
    title  = normalize_text(r["title"]) or name_key(r["name"])
    artist = normalize_text(primary_artist(r["artist"]))    # 空则取 name_key 的第一个词
    bucket = str(int(round(dur / step)) * int(step)) if dur else ""
    ver    = P.version_tags(r["name"], r["title"], r["rel_path"])
    return f"{title}|{artist}|{bucket}|{ver}"
```

要点：

* 这是**精确字符串键**，桶号不同就永远进不了同一个候选池；
* `bucket_step` 是**给调用方用的旋钮**。默认 2 秒对同库查重没问题（那边有并查集兜底），
  但跨库配对的阶段 2 完全靠它，于是会出现"文件名相同（阶段 1 有 tolerance）→ 差 3 秒能配；
  文件名不同（阶段 2 只有精确键）→ 差 3 秒配不上，而且调大容差也没用"。实测案例：
  `Get Lucky (Live Version).flac` ↔ `daft punk - get lucky.flac` 的键分别是
  `get lucky|daft punk|200|live` 与 `get lucky|daft punk|204|`；
* **版本标记必须进键**：归一化会故意把"伴奏/重制版"当噪声删掉（跨库同名匹配需要），
  不在这里补回来，`晴天.wav`、`晴天 (伴奏).wav`、`晴天 (Remastered).wav` 会拿到同一个键；
* 默认值保持 2.0，所以"索引里存的键"和"其它调用点"的行为不变。

`song_key(r)` = `title|artist`（不含时长），供"歌键"比较用。

### 8.5 `version_tags`（版本标记）——查重的第四根支柱

`paths.version_tags(*texts)` 用 14 组正则（`_VER_PATTERNS`）抽取"这是不同录音"的标记：
`instrumental`（伴奏/off vocal/karaoke/纯音乐）、`remaster`、`remix`、`live`、`acoustic`、
`cover`、`demo`、`remake`、`language`（国/粤/日/英/韩语）、`edit`、**`speed`**（sped up/slowed/
nightcore/加速版）、**`clip`**（TV size/短版/试听）、`version`（通用"版"）。
另有 `_VER_ZHTW`（简/繁）记作 `script`，以及 `_VER_PREFIX` 兜底 `([\u4e00-\u9fa5]{2,4})版` 记作 `v:xx`。

输出是 **排序后用 `|` 拼接**的稳定字符串，可直接参与分组键比较。
`speed` / `clip` 两组的注释说明了为什么必须补上：`Song.mp3` 与 `Song (Sped Up).mp3`
原来版本标记都是空串，被当成同一首并进同一组，用户点"清理重复"就会丢掉一份真正不同的版本
——"宁可分开让用户自己判断"。

### 8.6 `version_mismatch` 与 `artist_conflict`（两道否决）

```python
def version_mismatch(a, b) -> bool:
    va = version_tags(a.name, a.title, a.rel_path)     # ★ 三个字段，必须与 dup_key 一致
    vb = version_tags(b.name, b.title, b.rel_path)
    return va != vb
```

"标记必须一致"而不是"都不为空才比"：`晴天.flac`（空）与 `晴天（伴奏）.flac`（instrumental）
也要判为不一致——归一化会把"伴奏"删掉，两者文件名相似度 1.0、时长一致、分数顶满，
不在这里拦住就会被自动配对/合并。
字段必须和 `dup_key` 完全一致的原因也踩过：原来这里只传 `name`+`title`，而 `dup_key` 还传了
`rel_path`，于是"版本标记只在目录名里"的歌出现自相矛盾——`dup_key` 认为不同版本（分两组），
`version_mismatch` 看不到目录名、以为一致（又并回来）。典型场景：`专辑 (Remix)/Song.mp3` 对 `专辑/Song.mp3`。

```python
ARTIST_VETO_MIN   = 0.55   # 艺术家相似度低于此值：一票否决
def artist_conflict(m) -> bool:
    if m.artist_sim is None or m.artist_sim >= ARTIST_VETO_MIN: return False
    na, nb = normalize_text(left.name), normalize_text(right.name)
    if not _distinguishing_tokens(na, nb) and P.similarity(na, nb) < 0.95:
        return False        # 名字压根不像 → 不归本条管
    if P.similarity(primary_artist(a), primary_artist(b)) >= 0.72:
        return False        # 只是拼写/缩写/后缀差异
    return True
```

这是"最后一票否决"，专门拦这种陷阱：文件名高度相似（甚至归一化后一模一样）、标题相同、
时长接近，但一个是 Windows 独占曲、一个是 Linux 独占曲，只有艺术家能揭穿。
`_distinguishing_tokens` 找的是"只出现在一侧、且在另一侧找不到相似对应（`similarity<0.6`）"的词，
`windows` / `linux` 就是真差异词。

守卫条件的坑也记在注释里：原来要求"存在互不相似的差异词"否则直接 `return False`，
而**两个文件名一模一样时差异词必然是空集**，于是唯一能揭穿它们的信息被挡掉，
`Song.mp3`（作者 A）与 `Song.mp3`（作者 B）分数 0.949、被判重复。现在差异词为空时要分两种情况：
名字几乎一样（≥0.95）继续走否决；名字压根不像则放行。

`artist_similarity` 取"序列相似度"与"词级 Jaccard"里**更乐观**的一个：
`Pink Floyd` vs `Pink Floyd (Remaster)` 会被多余词拖低序列分，而 `Windows Only` vs `Linux Only`
的 Jaccard 只有 0.33，两条证据互补。

### 8.7 `fuzzy_admissible`（配对放行门禁）

```python
def fuzzy_admissible(m) -> bool:
    if version_mismatch(m.left, m.right):  return False
    if artist_conflict(m):                 return False
    if m.name_sim >= FUZZY_NAME_MIN(0.90): return True
    artist_ok      = m.artist_sim is not None and m.artist_sim >= 0.9
    near_same_dur  = m.duration_diff is not None and m.duration_diff <= 0.7
    return bool(artist_ok and near_same_dur)
```

即：**名字像到 90% 直接放行；否则必须"艺术家一致 + 时长差 ≤0.7 秒"**。
`ARTIST_VETO_FLOOR = 0.25` 是"否决后的折扣系数"，但**声明后没有被任何代码引用**（见 19 章）。

### 8.8 `pair_sources` 的四段（阶段 0 + 三阶段）

```python
def pair_sources(left, right, tolerance=2.0, threshold=0.6,
                 block_by_duration=True) -> {"matches","only_left","only_right","stats"}
```

`used_r` 是"右库已被占用的 id 集合"，每段都只能从**未占用**的右库记录里挑，保证一对一。

**阶段 0（代码注释称为"准备"）——词序颠倒的一对一精确配对**

```python
_tokens(x)   = " ".join(sorted(name_key 的词集合))，词数 < 2 则返回 ""
_same_tag(x) = version_tags(name, title)
按 (tokens, 版本标记) 分桶 → 对每个左记录只在同桶未占用的右记录里挑 score 最高的一个
再经 version_mismatch 复核
```

理由：`Daft Punk - Get Lucky` 与 `Get Lucky - Daft Punk` 归一化字符串不同（相似度只有 0.76），
文件名键阶段配不上、模糊兜底又够不到门槛，于是同一首歌被当成两首。只认"词元集合完全一致
且 ≥2 个词"，所以不会把 `Get Lucky` 和 `Get Lucky (Remix)` 配到一起。

**阶段 1 —— 归一化文件名精确配对**（最强证据，优先级最高）

```python
_pair_by_key(left, right, used_r,
             key_fn = lambda x: x["name_key"] or name_key(x["name"]),
             tolerance, threshold,
             require_duration_close=True,      # 时长差 > 2×tolerance 直接排除
             apply_fuzzy_gates=True)           # 走 fuzzy_admissible
```

注释解释了为什么要排在"标题+艺术家"之前：Windows 有 `Pop/` 与 `Backup/` 两份同名 Get Lucky 时，
若先跑歌键阶段，Linux 那一份会被 Pop 版抢走、Backup 版无人可配。

**阶段 2 —— 歌键（`dup_key`）精确配对**，只处理还没配上的左记录：

```python
_tol2  = float(tolerance or 2.0)   # <=0 → 2.0
_gate2 = 2.0 * _tol2               # 与 duration_similarity 同一个数
_step2 = max(2.0, _gate2)          # 桶距 = 门禁窗口 → 覆盖完整
_pair_by_key(..., key_fn=lambda x: dup_key(x, bucket_step=_step2),
             filter_left=rest_l, apply_fuzzy_gates=True, duration_gate=_gate2)
```

为什么桶要放宽到 `2×tolerance`：阶段 2 是精确字符串键，桶号不同就永远进不了同一个候选池，
而阶段 1 是有 tolerance 判断的，于是出现"文件名相同能配、文件名不同配不上且调容差没用"的不一致。
**放宽桶只是扩大候选池**，能不能配仍由 `score_pair` 的时长门禁和 `fuzzy_admissible` 把关。
`duration_gate` 参数的存在理由写在 `_pair_by_key` 的 docstring 里：桶一宽，键本身就失去了
"时长筛选"作用，必须用显式门禁补回来，否则会放进时长差很大的候选（实测过：同标题不同艺术家、
时长差 20 秒也能配上）。

**阶段 3 —— 模糊兜底**（只对"仍有右库剩余 且 仍左库未配"时执行）

```
_win3 = 2.0 * tolerance ; _K3 = 3
右库按 int(duration/_win3) 分桶，每行同时进 b0, b0-1, b0-2 三个桶（多偏移分桶）
对每个未配上的左记录：
  · 无时长 → 只允许"文件名 ≥0.95 且 size 完全相同"的窄路
  · 有时长 → 取 bucket 的 -1/0/+1 三个桶做候选池
  过滤：version_mismatch → 跳过；score < max(threshold+0.06, 0.7) → 跳过；!fuzzy_admissible → 跳过
取每行最佳候选 → 全局按分数降序 → 逐个占位（taken/used_r）
再做两道**分数下限**：
  · 总分 _sc >= 0.86
  · 名称相似度 _m_best.name_sim >= 0.85
```

这两道下限各有一段事故记录：

* 只看"进了 cands 就配"时，实测出现过
  `Bohemian Rhapsody.flac ↔ Smells Like Teen Spirit.flac`（同艺术家、时长相同、体积相同 → 0.7336）
  **配上了**；更糟的是这两个文件本来各自都不在对方库里，被强行配成一对后对比页把它们从"独有"
  里拿掉，用户反而看不到"两边各缺一份"。原则是"**宁可不配**（显示成独有，用户一眼能看出），
  也不要瞎配（悄悄没了，用户发现不了）"。0.86 也是 `stats.suspect` 的分界。
* 只看总分也不够：`size_similarity` 同体积给 1.0、时长短差给 1.0，两项占 0.46 权重，
  "同艺术家 + 时长几乎相同 + 体积相同"就能把总分顶到 0.86，哪怕名字不相关——实测
  `Alpha Song.flac ↔ Beta Song.flac`（名字相似度 70%）配上了。所以名称相似度单独设 0.85 下限。

**输出**：

```python
matches.sort(key=lambda m: (-m.score, m.left["path"]))
{"matches", "only_left", "only_right",
 "stats": {left_total, right_total, matched, only_left, only_right, identical, suspect}}
```

`identical` = `Match.identical` 为真的条数；`suspect` = `score < 0.86` 的条数。

### 8.9 `group_duplicates`（同库查重 + 并查集）

签名：`group_duplicates(rows, tolerance=2.0, threshold=0.9) -> List[List[row]]`

```
1) 分组：对每条 row **就地重算** dup_key(r)（不信任索引里缓存的那一列）
   · 算法会演进（例如新增版本标记），而增量扫描会复用旧行的缓存值；
     直接读库里的旧键，新规则要等整库重扫才生效
   · 回写 r["dup_key"] = k —— group_to_dict() 用 row["dup_key"] 当组键，
     不回写会取到库里的旧值/空值，不同组可能撞成同一个 key
   · 键为空或以 "|" 开头 → 退化成 "name:" + name_key

2) 每个键取一个"代表项"：`max(组内成员, key=len(name))`
   同时算出 sigs[k] = {name_key, tokens(frozenset), version}

3) 时长多偏移分桶（只和邻近桶比）：
   _window = 2×tolerance ; _K = 3 ; bucket_w = _window
   每行进 b0, b0-1, b0-2

4) 对每个键 k，取 bucket 的 -1/0/+1 邻居 → 候选 k2，先做**廉价合并门禁**：
     name_equal   = 两边 name_key 非空且相等
     same_key     = 两边 dup_key 相等
     tokens_equal = 两边词元集合相等且 ≥2 个词
     三者皆假 → 跳过
     两边 version 不同 → 跳过
     两边都有 duration 且 |差| > _window → 跳过
   全部通过才 score_pair()，再判 score >= threshold、artist_conflict() → union(k, k2)

5) merged[find(k)].extend(items) → 只保留 len>1 的组
   组排序：(-len, 组内首个 name 的 lower)
```

**为什么必须有多偏移分桶**（注释列了两次失败，值得原样记住）：

1. 原来 `int(d // 3)` + 邻居 ±1 —— 等效 ±6 秒硬上限，而且**完全不理会 tolerance**，
   容量从 2 调到 10 毫无变化；
2. 改成"桶宽 = tolerance、邻居 ±1"仍会漏：整数除法 + 落在桶边界上，`int(a/w)` 与 `int(b/w)`
   的差可能比预期大一，于是 3.6 秒的一对在容差 2.0 下时收时不收（真实库里没并、合成数据里并了
   ——"边界靠运气"）。

现在的做法：每行按 K 个等距偏移各进一个桶，查询时取主桶邻居；只要 `K × bucket_w ≥ 门禁窗口`，
任意落在门禁内的一对**必然**共享至少一个桶——"覆盖是构造出来的，不靠运气"。

**廉价门禁为什么必要**：没有它，`size_similarity` 同体积给 1.0、标签都缺时 `meta_similarity`
给中性 0.5，"体积相同 + 时长接近"就能把分数顶到阈值，整张专辑被并成一个巨型组
（看起来像"查重只有几组"）。也不能用普通相似度阈值：`difflib` 对 `song 01`/`song 02`
这种只差一两个字符的不同歌也给 0.857，照样误并。

**性能注解**：`score_pair` 里的 `difflib` 是全层最贵的一步，2000 首的库全量两两打分实测 179 秒，
所以门槛判断全部前置成字符串相等比较。

`_version_of(k, r)` 的写法也很关键：

```python
parts = str(r.get("dup_key") or k).split("|", 3)   # 从左切 3 段，剩下全是版本
return parts[3] if len(parts) == 4 else ""
```

用 `split(..., 3)` 而不是 `rsplit`：版本段自己可能含 `|`（`instrumental|remix`），
`rsplit` 会把它切成 `remix`，于是"原曲 vs 伴奏"又被判成同版本（实测就是这么漏掉一组的）。
而且这个函数**必须与 `dup_key` 同源**：原来组内合并门禁自己重算
`version_tags(name, title)`（少了 `rel_path`），于是"版本标记只在目录名里"的歌会出现
`dup_key` 分两组、合并门禁又并回来的矛盾。线上实测受害者：
`野火 Wildfire (伴奏) - HOYO-MiX.flac` 与 `野火 Wildfire - HOYO-MiX, ...flac` 被并成一组，
界面提示"清理其中一份"，而那是两个不同的录音。

### 8.10 `merge_matched_pairs`（多库重叠组必须合并）

这是 `main._duplicates_core` 在 `within == "cross"` 且源数 > 1 时用的：

```python
for i, a in enumerate(ids):
    for b in ids[i+1:]:
        lp = M.pair_sources(pools[a], pools[b], tolerance, threshold)
        pairs += [(m.left, m.right) for m in lp["matches"] if m.score >= threshold]
groups.extend(M.merge_matched_pairs(pairs))
```

**为什么必须存在**（docstring 原文提炼）：跨库查重是拿**每一对库**分别比一次的。3 个库里各有
一份同一首歌时会产生 3 个配对 `A+B、A+C、B+C`。如果每对各自成组，**同一份文件会同时属于 2 个组**，
前端"每组保留一份"的规则立刻自相矛盾——实测会生成把同一个文件删两次的计划，而且"保留哪一份"
可能在组之间打架（组 1 里选保留 A，组 2 却把 A 删了），跑起来第二条报
`FileNotFoundError: 系统找不到指定的文件`（幻影错误），用户以为出事了。合并之后 3 份变成
**一组 3 条成员**，和单库查重的组形状完全一致，`clusters` / `suggested_keep` / 前端保留策略
全部直接复用。

**做法**：并查集，但**键是 `(source_id, path)` 而不是 `dup_key`**——后者不含 `source_id`，
两个库里同一首歌的 key 完全相同，会把不同源的文件混成一个键。
细节：

* `union` 时**字典序小的当根**（`if rb < ra: ra, rb = rb, ra`），保证结果与输入顺序无关、可复现；
* `ka == kb` 的自我配对直接跳过，避免造出 1 条成员的组；
* 输出按根键排序：`[buckets[k] for k in sorted(buckets)]`，否则同一份数据两次跑出来的组序不同。

### 8.11 `pick_representative` 与 `group_to_dict`

```python
def pick_representative(group) -> dict:
    return max(group, key=lambda r: (
        r["quality"] or 0,
        r["is_lossless"] and 1 or 0,
        r["bitrate"] or 0,
        r["size"] or 0,
        r["duration"] or 0,
        str(r["path"] or ""),        # ★ 稳定兜底键
    ))
```

末尾带 `path` 的理由：前几项全并列时（同一张专辑里两份**完全一样**的音轨，质量/体积/时长都相同），
`max` 取的是"参数顺序里的第一个"，而 `group_duplicates` 传进来的顺序会随排序、扫描顺序变化
——于是同一组在不同时候算出来的"建议保留"可能是**不同的那一个**，用户按建议删就会删掉不同的文件。

`group_to_dict(group, keep_path=None)`：

* **空组安全返回**（`max of empty sequence` 会 ValueError，当前调用点都有长度保护，但复用就会炸）；
* `items` 按 `quality` 降序；`total_bytes` 求和；`wasted_bytes` = 除 `keep` 外的体积和；
* `key` = 代表项的 `dup_key`（没有就用 `name_key`）；
* `base_key` = `dk.rsplit("|", 1)[0]`（**去掉版本段**），供界面"不区分版本"视图把
  原曲/伴奏/重制版重新并成一个总体视图（注意这里用 `rsplit` 是**故意的**：只切最后一段）；
* `version_tags` 来自代表项的 `name`+`title`（注意与 `dup_key` 的字段集**不完全一致**，见 19 章）。

### 8.12 `naming_conflicts`

```python
def naming_conflicts(left, right):
    lmap = {name_key(r.name): r for r in left}
    for r in right:
        o = lmap.get(name_key(r.name))
        if o and o["name"] != r["name"]:      # 归一化相同、原文件名不同
            out.append({"left": o, "right": r, "score": 1.0})
```

用途：给出"同一首歌两边都有、但文件名写法不同"的清单，交给 `plan.conflict_steps` 生成
`rename` 步骤（可选功能）。

---

## 9. 计划生成

### 9.1 `Step` 结构（`core/plan.py`）

```python
@dataclass
class Step:
    op: str                  # copy | trash | rename | link | mkdir（docstring 里列出的取值）
    src: str = ""            # 源数据源 id
    src_path: str = ""       # 源内绝对路径
    dst: str = ""            # 目标数据源 id
    dst_path: str = ""       # **目标源内相对路径**（计划阶段）
    size: int = 0
    reason: str = ""         # 给人看的一句话
    risk: str = "low"        # high | medium | low | none
    kind: str = ""           # missing | duplicate | name_conflict | manual
    group_key: str = ""
    meta: dict = {}          # track 摘要 / keep / overwrite / mirror / trash_mode …
    id: str = uuid4().hex[:10]
```

`to_dict()` 额外算 `human_size`。`RISK_INFO = {"high":3,"medium":2,"low":1,"none":0}` 用于汇总
`risk_max`。

`_t(r)` 是"曲目摘要"：只取 path/name/size/title/artist/album/duration/bitrate/codec/
is_lossless/quality/sample_rate/bits_per_sample/hash_quick/hash_state 十五个字段，
塞进 `meta["track"]` 供界面显示。

### 9.2 `build_plan` 全流程

```python
def build_plan(left, right, cfg=None, options=None) -> dict
# left/right = {"id","label","tracks":[...]}
```

```
0) 读参数
   tol  = cfg.settings.duration_tolerance_sec（默认 2.0）
   thr  = cfg.settings.name_similarity（默认 0.86）—— 读入但**未在后续使用**
   match_threshold = options.match_threshold or 0.62
   lp   = M.pair_sources(left.tracks, right.tracks, tolerance=tol, threshold=match_threshold)

1) 查漏 → copy（options.copy_missing，默认 True）
   direction 白名单：left_to_right | right_to_left | both
   非法值 → 追加 warning 并**按 left_to_right 处理**（绝不静默反转）
   both → 先左→右、再右→左，各生成一批
   missing_steps(...)：
     · 取对应侧的 only_left / only_right
     · subdir 过滤（相对于 **rel 路径**的父目录）
     · dst_path = P.norm(rel_path 或 basename)  ← 相对路径
     · risk **固定 "medium"**，不随 overwrite 变（见下）
     · meta = {track, overwrite, from_side, to_side}

2) 镜像删除（options.mirror_delete）
   _mirror_conflict = mirror and copy_missing and direction in ("right_to_left","both")
   若冲突：先摘掉刚生成的"右 ➜ 左"copy 步骤，warning 说明摘掉了几条
   然后对每个 only_right 生成 trash（risk="high"，reason="镜像模式：右库存在但左库没有"）
   + warning "已启用镜像删除…"

3) 重复（options.include_duplicates）
   跨库 identical 配对 → 只**生成 warning 条数与摘要**，不产生 Step
   （`cross` 列表里 suggested_keep 取 quality 较大的一侧；`mirror_delete` 时完全跳过）

4) 命名冲突（options.generate_conflicts）
   naming_conflicts → conflict_steps(apply_to=conflict_side，默认 "right")

5) 汇总 _summarize(steps) + 输出
```

输出字段：`generated_at, left{id,label,total}, right{…}, match_threshold, stats,
matches（前 match_preview=500 条）, only_left（前 preview 条）, only_right, conflicts,
steps, summary, warnings`。

### 9.3 三种方向的语义

| `direction` | 生成什么 | 说明 |
|---|---|---|
| `left_to_right`（默认） | 左有右无 → `copy 左→右` | "补齐右库" |
| `right_to_left` | 右有左无 → `copy 右→左` | "补齐左库" |
| `both` | 两批都生成 | 双向补齐 |

方向值必须走**白名单校验**。注释记录的事故：原来 `missing_steps` 是二选一分支
（`if direction == "left_to_right" … else …`），任何拼错的值（`left-right`、`BOTH`、`""`）
都会**静默反转方向**——用户以为在"左→右补齐"，实际把右库的东西往左库搬。现在不认识的值
一律按 `left_to_right` 处理并给出 warning。

### 9.4 镜像删除与方向的互斥规则（为什么）

`build_plan` 里那条 `_mirror_conflict` 判断是整份计划逻辑里最值得看的一处：

```
镜像删除（右有左无 → 从右 trash） 只对"右 ➜ 左不补"的方向成立
```

原因是并发执行：

* 冲突组合会同时给出两条关于**同一个右库文件**的步骤：`copy R→L`（第 1 步生成）
  与 `trash R`（镜像生成）；
* `/api/execute` 用 `ThreadPoolExecutor(workers=3)` **并发**跑步骤，步骤之间没有顺序依赖
  （`_reserve_targets` 只占 copy/move/rename/transfer 的目标，**trash 不在其中**）；
* trash 先跑完的话，文件进了回收站、**却从来没有被复制到左边**——界面上两条都报成功，
  用户以为"补齐好了"。

语义上也自相矛盾：一边要把右库独有的文件搬到左边，一边又要把它从右边删掉。所以现在
**源头把组合变成"镜像优先、跳过右➜左补齐"**，并明确告诉用户发生了什么（warning 里还给出
"想保留那些文件就关掉镜像模式"的建议）。

### 9.5 `transfer` 选项如何把 `copy` 改写成 `transfer`

**注意：这一步不在 `plan.py` 里，而在 `main.make_plan`（`/api/plan`）里**：

```python
options = {…, "transfer": bool(payload.get("transfer", False)), …}
result = PLAN.build_plan(left, right, cfg=cfg, options=options)
if options.get("transfer"):
    for st in result["steps"]:
        if st["op"] == "copy":
            st["op"] = "transfer"
            st["reason"] += "（安全搬运：校验通过后删源）"
            n_xfer += 1
    result["warnings"] += ["已把 N 个「复制补齐」步骤改成「安全搬运」…"]
```

语义差别：纯 `copy` 是"补齐"——缺的补过去、源那份留着（两边各一份）。用户很多时候的真实
意图是**搬**（尤其往 U 盘/网络位置搬，纯复制会悄悄把两边都占满，还得多做一步手工删源）。
**只在计划阶段换 op**，执行仍走同一个 `Executor`，所以独占、进度、暂停/取消、步骤重放全部照旧。

`main.make_plan` 里还有两处必须知道的细节：

1. `options["transfer"]` **必须放进 options 字典**。注释自嘲："只在下面 `if options.get('transfer')`
   里读它、却不把它放进来，那个分支永远不会执行（我第一次就是这么写的，实测计划里还是 copy）"。
2. 截断告警与只读告警都会并进 `warnings`；`result["truncated"]` / `result["totals"]` 也会回填。

### 9.6 另外三个步骤生成器

* `duplicate_steps(groups, keep_map, within, trash_mode)`：每组除 `keep` 外全部生成
  `trash`（或 `delete`）。`keep` 优先 `keep_map[group_key]`，其次 `suggested_keep`。
  `risk="high"`，`reason` 里带"质量 A < 保留 B"的对比。
* `conflict_steps(conflicts, left_id, right_id, apply_to)`：把一侧改成另一侧的文件名，
  `op="rename"`，`dst_path = P.join(parent(cur), want)`，`risk="medium"`。
* `compare_sources(left, right, cfg, options)`：双栏视图用的**只读**版本，只返回
  `stats / matches / only_left / only_right`，不生成 Step。

---

## 10. 执行器

`core/ops.py::Executor` 是唯一"把计划落到磁盘"的地方（除了上传/标签/中转站那几个专用写路径）。
文件头列了 8 条安全设计，逐条都能在代码里找到对应实现。

### 10.1 构造与 policy

```python
Executor(sources: Dict[str, MusicSource], db=None, default_policy="skip")
```

`policy` 取值（HTTP 层都做过白名单校验）：`skip`（默认，目标存在就跳过）/ `rename`（自动
`a (2).mp3`）/ `overwrite`（直接覆盖，靠"写临时文件 + 改名"保证不会写坏）。

`run()` 签名：

```python
run(steps, execute=False, policy=None, verify=True, verify_hash=False,
    hard_delete=False, workers=3, job=None) -> dict
```

### 10.2 `run()` 的完整流程

```
1) policy = policy or self.policy
2) 慢速盘降并发：
   _slow = self.slow_sources(steps)      # 遍历 src/dst，问 device_info()["slow"]
   if _slow: workers = DEV.effective_workers({"slow_disk": True}, workers)
             （device 导入失败则退化成 max(1, min(workers, 2))）
3) **预演不占独占位**：
   if not execute: get_jobs().ensure_free()   # 只"问一次"，忙就抛 JobBusy（→409）
   job = job or get_jobs().create("sync", total=len(steps), exclusive=bool(execute))
4) self._job = job ; job.total = len(steps)
5) n_conf = self._reserve_targets(steps)     # 方案内目标占位
6) if execute: get_jobs().record_steps(job, steps)   # 落库，供崩溃恢复
7) if not execute: 逐条 _dry(st) → finish(job) → 返回 {dry_run: True, …}
8) 并发执行：
   work(st)：
     · job.cancel → 记 cancelled 返回
     · get_jobs().wait_if_paused(job)        # 暂停时在这里等
     · r = self._apply_guarded(st, job, **opts)   # 带超时的单步
     · 异常 → state=error + classify_error()
     · 每条都 job.done += 1 / job.touch() / job.current = 路径
     · 失败/跳过/冲突/阻断的条目进 job.skipped（供"哪些要重试"）
     · job.message = "执行中 d/total｜成功 n｜失败 m"
   ThreadPoolExecutor(max_workers=max(1, workers)).map(work, steps)
9) _tally(results) → _sync_index(results, by_id)
10) 补齐"没做的步骤"：不是 done 的填成 cancelled / pending（否则界面数字对不上）
11) summary 合并 _extra_summary（dst_conflicts）与 index_errors 计数
12) get_jobs().finish(job, result=summary)
13) 返回 {dry_run, policy, results, summary, device_notes, workers[, index_errors, warnings]}
    + job.pause → {paused: True, resumable: n} / job.cancel → {cancelled: True}
```

预演那一段的注释很长，值得单独摘出来（它解释了三个真实 bug）：

* 原来无论预演还是真跑都 `create("sync")`，而 `sync` 在 `EXCLUSIVE_KINDS` 里，于是**只读的预演**
  会真的占住独占位（别的页面 `/api/busy` 变忙、写操作被 409）；还会 `record_steps` 落库，
  预演途中被杀后重启，`/api/unfinished` 把它当"可恢复任务"，用户点"接着做完"就把**只预演过的
  方案真的执行了**；另外 `/api/execute` 当时没有 guard 也没包 try，`create` 抛 `JobBusy`
  会冒到顶层 → HTTP 500（`JobBusy` 那时没有全局处理器）。
* 预演虽然不占独占位，但**也不能在别人写文件的时候跑**：它读的是"当前磁盘状态 + 索引"，
  若此刻有写流程在移动/删除，算出来的方案就是基于正在变动的状态，用户拿着它点执行等于照过期快照动手。
  所以 `ensure_free()` 要"只问一次且不要 try/except 包住"——`JobBusy` 必须原样上抛给全局处理器转 409。

### 10.3 `_reserve_targets`：方案内目标占位

```python
for st in steps:
    if st["op"] not in ("move", "copy", "rename", "extract", "transfer"): continue
    dst_path = st["dst_path"]
    sid_key  = st["dst"] or st["src"]
    _src = self.sources.get(sid_key)
    dst_path = _src.rel_path(dst_path)          # ★ 统一到"目标源内相对路径"
    key = (sid_key, P.norm(dst_path).lower())   # ★ lower()：Windows 大小写不敏感
    if key in self._planned:
        st["_dst_conflict"] = self._planned[key]["id"]; conflicts += 1
    else:
        self._planned[key] = st
```

**为什么必须"先占"**（docstring）：`skip` 的判断是"执行到这一步时目标在不在"。同一方案里前一步
刚把文件放到那里，第二步看到"已存在"就直接跳过或（overwrite 时）覆盖——两个都报 `done`，
实际只剩一个文件。多线程下更糟：两步的检查可能都发生在对方写入之前。用户看不到任何警告，
这是**静默丢文件**。

`transfer` 必须在内：它同样往 `dst_path` 写文件，漏掉它的后果和当初漏掉 `copy` 一样。
`extract` 在名单里但执行器没有对应分支（见 19 章）。

### 10.4 `_claim_target`：坐标系必须与占位一致

```python
def _claim_target(self, st, want) -> str:
    if not st.get("_dst_conflict"): return want
    sid = st["dst"] or st["src"]
    key_rel = src.rel_path(want)                       # 绝对 → 相对
    key = (sid, P.norm(key_rel).lower())
    owner = self._planned.get(key)
    if owner is None or owner["id"] == st["id"]:
        self._planned[key] = st; return want
    new = _dedupe_name(src, want)                      # a.mp3 → a (2).mp3
    self._planned[(sid, P.norm(src.rel_path(new)).lower())] = st
    return new
```

**坐标系**：`_reserve_targets` 记的是**目标源内相对路径**（`d/x.mp3`），而调用方传进来的是
**绝对路径**（`C:/…/B/d/x.mp3`）——两边对不上就永远查不中，改名逻辑形同虚设。这个不一致
造成的后果很隐蔽（也真实存在过）：

```
同一方案里两条 copy 指向同一目标
→ dst_conflicts: 1 报出来了（看起来"已处理"）
→ 但实际只落了一个文件，另一条是 conflict
→ 用户看到"有 1 个目标撞车、已自动改名"，而文件少了一个
```

历史上 `_do_copy` 一直带着这个 bug，`_do_transfer` 是新加的、同样中招；现在统一键空间，
两个调用点一起受益。

### 10.5 单步语义 `_apply` / `_do_copy` / `_do_move` / `_do_remove` / `_do_rename` / `_do_transfer`

```python
_apply(st, policy, verify, verify_hash, hard_delete):
    op in ("copy","link")   → _do_copy
    op == "transfer"        → _do_transfer(verify_hash=verify_hash)
    op in ("trash","delete")→ _do_remove(hard = hard_delete or op=="delete")
    op == "rename"          → _do_rename
    op == "move"            → _do_move
    其它                     → state=skipped, detail="未知操作 {op}"
    统一补 res["seconds"]
```

| 操作 | 关键行为 | 失败/边界处理 |
|---|---|---|
| **copy** | 目标只读 → `blocked`；先 `_claim_target` 认领目标名；`exists` 时按 policy（skip→`conflict`，rename→`_dedupe_name`，overwrite→直接覆盖）；源文件不存在/是目录 → `error`；复制用 `dst.copy_from(src, src_path, dst_path)`，并包在 `transfer._Heartbeat(self._job)` 里 | `verify=True` 时比 size；`verify_hash=True` 时比 `S.quick_hash` 两侧；不一致只报 error（**不删目标**，因为 copy 不删源） |
| **move** | 只认同源：`_resolve_move` 校验 `src == dst`、`can_write`、越界（`_inside_root`）；`cur == want` → `skipped`（"源与目标相同"）；目标是目录 → `error`（"需要给出完整的目标文件名"）；先 `_claim_target` 再判 policy；`mkdirs(parent)` 放在 skip/rename 判断**之后**（免得"其实什么都不做"也留下空目录）；`src.move()` 后重新 `stat` 校验目标存在且 size 一致 | 校验不过如实报错，**绝不假装成功** |
| **trash/delete** | `can_delete` 为假 → `blocked`；`src.remove(path, hard=…)` 返回回收站落点写进 `trash` 字段 | `reason` 里区分"已永久删除 / 已移入回收站" |
| **rename** | 目标已存在：skip → `skipped`，其它 → `_dedupe_name`；**借道临时名**：`cur → cur+".mshren" → want`（兼容没有 rename 原语的源）；异常时尝试把 `.mshren` 还原回 `cur` | 同源内改名，不跨源 |
| **transfer** | 见第 11 章；`policy="skip"` 写死（由 `transfer_one` 自己处理冲突） | `verify_hash` **会**透传（原来写死 False，等于用户勾了没勾） |

`_apply_guarded`：给每一步再套一层 `STEP_TIMEOUT_SEC`（默认 120 秒，可用
`MSH_STEP_TIMEOUT_SEC` 覆盖；`<=0` 表示不限制）。超时后**放弃等待**并把该步标成
`error_kind="timeout"` 继续处理其它文件。

注释特别说明：Python 没法真正杀掉卡在系统调用里的线程，所以是"工作线程池层面放弃等待"；
已写一半的临时文件由 `.mshpart` + 原子改名保证不会被当成成品（最坏是留一个 `.mshpart` 残留）。

### 10.6 越界校验（`_inside_root` / `_resolve_move`）

```python
def _inside_root(src, path) -> bool:
    p = P.norm(path)
    if not p or not (p.startswith("/") or P.is_windows_abs(p)): return False
    if ".." in p.split("/"): return False
    root = P.norm(src.root).rstrip("/")
    if not root: return False
    if os.name == "nt": p, root = p.casefold(), root.casefold()   # Windows 不区分大小写
    return p == root or p.startswith(root + "/")
```

为什么"只看收敛结果"不够（docstring）：`MusicSource.abs_path` 会把相对路径、越界路径一律
**收敛**回 root，所以只看收敛结果分不清"本来就在 root 内"和"越界后被悄悄改写成了 root 下的怪路径"。
移动是破坏性操作，这里要求**绝对内部路径**并按原样对照 root 校验：越界宁可报错。

`_resolve_move` 的两段逻辑：

1. **先展开、再校验**：相对路径（`out/x.mp3`）按"数据源根下的相对路径"解释，再走同一套越界校验。
   注释记录了原来的错误：`_inside_root` 只认绝对路径（有意的安全设计），于是完全合理的相对路径
   被报成"路径超出数据源根目录"，"报错内容和真实原因完全不是一回事"。
   **注意顺序**：先展开再校验，所以 `../别处/x` 展开后仍落在 root 外，照样被挡住——
   "放开相对路径不等于放开越界检查"。
2. `dry_run` 与真正执行**共用同一段判断**（`_dry` 里也调 `_resolve_move`），
   注释："两边判断不一致才是最容易丢文件的地方"。

### 10.7 `_sync_index`：让界面立刻正确

| 成功的 op | 索引动作 |
|---|---|
| `trash` / `delete` | `db.remove_track(src, src_path)` |
| `transfer` | 同样 `remove_track(src, src_path)`（源那份已进回收站）；目标那份**留到下次扫描**补元数据 |
| `rename` | `UPDATE track SET path=?, parent=?, name=? WHERE source_id=? AND path=?`（走 `db.write()`） |
| `move` | 额外重算 `rel_path`（`src.rel_path(new_path)`，拿不到源实例时退化成整条路径），**必须走 `db.write()`** |
| `copy` | 不动索引（目标文件留到下次扫描补全元数据，避免执行期再做一次远端解析） |

失败**不能静默吞掉**：`_index_errors` 会收集 `{step, op, src_path, error}`，`run()` 把它汇总成
`summary["index_errors"] = n`、`out["index_errors"]`（前 20 条）和一条 warning
"有 N 个文件已经移动成功，但索引没更新过来——建议重新扫描"。注释原话：
"原来这里是 `except: pass`，用户只会看到'移动成功'，然后过一阵发现库里对不上，而且完全不知道原因"。

### 10.8 `_tally` 与错误分类

`_tally(results)` 产出：

```python
{"total": n, "done": …, "skipped": …, "error": …, "conflict": …, "blocked": …, "cancelled": …,
 "bytes_moved":  Σ bytes  (state=done 且 op in copy/move/transfer)
 "bytes_trashed":Σ bytes  (state=done 且 op in trash/delete)
 "error_kinds": {kind: count}, "error_labels": {kind: 中文}, "retryable_steps": [step id…]}
```

* `bytes_moved` 必须包含 `move` 与 `transfer`：只统计 copy 的话"移动完永远显示 0 B"、
  "跨库搬运完显示搬运了 0 B"（两处注释都写了）。
* 错误分类（`classify_error`）的意义是**告诉用户该怎么办**："文件被占用"值得过一会儿重试；
  "路径不存在"重试也没用，得先扫描；"空间不足"要先清理。一锅端地报"失败"帮不上忙。

```python
ERR_MISSING / ERR_LOCKED / ERR_NOSPACE / ERR_CONFLICT / ERR_READONLY / ERR_TIMEOUT / ERR_OTHER
ERR_LABEL  = {"missing":"文件不存在", "locked":"文件被占用或权限不足", "nospace":"磁盘空间不足",
              "conflict":"目标已存在", "readonly":"目标只读", "timeout":"操作超时（已跳过）",
              "other":"其他错误"}
ERR_RETRYABLE = {locked, nospace, timeout, other}      # 只有这些"重试"有意义
```

分类顺序：先异常类型（`StepTimeout` → timeout；`FileNotFoundError/NotADirectoryError` → missing；
`IsADirectoryError` → conflict；`PermissionError` → locked（Windows 上最常见的是"文件正被别的程序打开"）；
`OSError` 按 errno：2/3→missing、13/32/33→locked、28→nospace、60/110/116→timeout），
再用文本关键字兜底（底层可能把 Errno 包成别的异常），最后按类名兜底。

### 10.9 并发安全小结

| 共享状态 | 保护方式 |
|---|---|
| `self._planned`（目标占位） | `self._planned_lock = RLock` |
| `results` / `done` | `run()` 内的 `lock = RLock`（`work()` 里只有 append 与计数） |
| `job.done/current/message` | 在 `lock` 内写；`job.touch()` 独立 |
| `db` 写 | `Database._write_lock`（`db.write()` 内串行） |
| 慢盘并发 | `run()` 入口把 `workers` 压到 `effective_workers` 结果 |
| 单步卡死 | `_apply_guarded` 的 `STEP_TIMEOUT_SEC` + 心跳线程 |

`device_notes(steps)` 会把"设备层面"的注意事项随结果带回界面：可移除设备"执行过程中**不要拔盘**"、
网络位置"读写延迟高"、慢盘"已自动降低并发"。注释记录了教训：识别出 U 盘/网络位置之后**扫描**会用上
（压并发、跳过全量哈希），但**对比/同步/整理**这些走 Executor 的路径完全不知道，于是往 U 盘上同步
照样开 3 个并发写，"比单线程还慢"。

---

## 11. 跨库传输

`core/transfer.py`。为什么单独做一层而不让调用方自己拼 `copy + delete`（文件头给了两条理由）：

1. **顺序错了会丢文件**。先删后拷、或者拷完不校验就删，一旦中途失败就是"源没了、目标也没有"。
   `ops.py` 的注释里也写着"跨源搬运请用 copy（复制成功后再单独删源）"——也就是**靠人去记得分两步做**。
   这层把"复制 → 校验 → 再删源"固化成一个不可分割的操作。
2. **回滚要能说清**。万一目标写了一半，必须明确告诉用户"源还在，目标那份不完整，要不要清掉"，
   而不是留一个含糊的失败。

### 11.1 `transfer_one` 的四步与失败路径

```python
transfer_one(src, dst, src_path, dst_path, *, policy="skip", verify=None,
             verify_hash=False, hard_delete=False, job=None, mode="") -> dict
# state: done | skipped | conflict | blocked | error
```

| 步骤 | 代码 | 失败路径（都在源码里） |
|---|---|---|
| **前置** | 源/目标为 None → `error("数据源不存在")`；`src is dst` 或 `src.cfg.id == dst.cfg.id` → `error("源和目标在同一个数据源里，请用「移动到…」…")`；`!dst.can_write` → `blocked/readonly`；`!src.can_delete` → `blocked/readonly`（"无法完成「复制后删源」的传输（可以只做复制）"）；`src.stat` 为空 → `error("源文件不存在（可能已经被移动或删除）")`；源是目录 → `error("跨库传输目前只支持单个文件")` | 目标已存在时：`policy="skip"` → `state="conflict"`，detail 明写"源**没有**被动过"；`rename` → `_dedupe_name()`；`overwrite` → 直接覆盖 |
| **① 复制** | `with _Heartbeat(job): info = dst.copy_from(src, src_path, dst_path)`——先写 `<dst>.mshpart` 再原子改名（由各源的 `write_file`/`copy_from` 保证） | 任何异常 → `error(error_kind="copy")`，**此时源尚未被动过** |
| **② 校验** | `verify is None` → 跟着 mode（standard 校验、fast 跳过）；`verify=True/False` 显式传则**盖过 mode** | 目标不见了 → `error("复制后目标文件不见了——源**未**删除，请重试")`；size 不一致 → **先 `dst.remove(dst_path, hard=True)` 清掉不完整的那份**，再 `error("大小不一致…源**未**删除")`；`verify_hash` 时内容哈希不一致 → 同样删目标、报错；哈希过程异常 → 报错（源未删） |
| **③ 删源** | `trashed = src.remove(src_path, hard=hard_delete)`（默认进回收站） | 删源失败**不回滚目标**（"目标那份是好的，回滚反而会把它删掉"），detail 明写"文件已复制到目标，但删除源失败…现在**两边都有**这个文件，请手动删掉源那份"，`error_kind="delete-src"` |
| **返回** | `{state:"done", detail, target, bytes, overwrote, steps_done:["copy","verify","delete-src"], mode, verified, trashed}` | `steps_done` 能精确告诉用户"卡在哪一步之前" |

`detail` 文案很讲究：没校验时会在结尾如实追加"｜快速模式：未校验大小"或"｜未校验大小"，
"别让用户以为验过了"；`hard_delete` 为真时写"（源已永久删除）"，否则"（源已移入回收站）"。

### 11.2 standard / fast 两种模式

```python
VALID_MODES = ("standard", "fast")

def mode_of(src, explicit="") -> str:
    # 优先级：请求里显式指定 > 数据源 extras["transfer_mode"] > "standard"
```

两种模式的差别**只有一个**：复制完要不要先量一下大小再删源。

| 模式 | 行为 | 代价 |
|---|---|---|
| `standard`（默认） | 复制 → **校验大小一致** → 才删源 | 慢一点（多一次 stat 往返），但"目标写歪了"能被发现并中止 |
| `fast` | 复制 → 校验（默认关）→ 删源 | 网络盘/U 盘上批量搬运能省下可观的往返开销 |

**★ 两种模式下源都进回收站**（不是永久删除），所以 fast 最坏也只是"目标那份可能不完整，
但源还在回收站里"，随时能拿回来。注释明确写着："这一点是'快速模式可以存在'的前提——
如果它会真删源，就不该提供。"

`verify` 参数的默认 `None` = 跟着 mode 走；显式 `True/False` 是更强的意图，会盖过 mode：
"'快速模式'只是默认省一步，不该让调用方**没法**要求校验。"

### 11.3 `_Heartbeat`：为什么必须存在

```python
class _Heartbeat:
    def __init__(self, job, interval=20.0):   # interval 下限 5 秒
    def __enter__(self):  起一个 daemon 线程，每 interval 秒 job.touch() 一次
    def __exit__(self):   set 停止事件
```

docstring 原文逻辑：`copy_from` 有可能是 `shutil.copyfile`（本地→本地），它**中途不给任何回调**
——一个 6 GB 的文件拷到 U 盘要 ~300 秒，这段时间里 `job.heartbeat` 一次都不动。
`STALE_JOB_SECONDS` 默认 180 秒，于是任务会被 `_reap_stale_locked` 判成"心跳中断"：
**标成 error 并释放独占位，而复制其实还在跑**——之后任何写操作都会被放进来，两边同时改文件。

所以起一个小线程每隔几秒 `touch()` 一次，搬运结束就停；**心跳失败绝不能影响搬运本身**
（`except: return`）。

使用点有三处：`transfer_one` 的复制段、`ops._do_copy`（`from .transfer import _Heartbeat as _HB`）、
`ops._do_transfer`（把 `self._job` 传给 `transfer_one`）。`_do_transfer` 的注释补充：
"不传的话，一个 40 GB 的文件在网络盘上搬 20 分钟，期间没有任何心跳…那之后任何写操作都会被放进来"。

### 11.4 `plan_transfer` 与 HTTP 层的编排

`plan_transfer(src_id, dst_id, items, dst_dir="")` 把"要传哪些文件 + 落到哪"翻译成执行器认识的
`transfer` 步骤（`id=f"xfer-{i}"`，文件名**保持不变**——"跨库搬运不该顺手改名"）。

`main.files_transfer`（`POST /api/files/transfer`）是另一条独立路径，直接循环调 `transfer_one`
而不是走 `Executor`：

```
校验：from/to 必填且不同、items 非空、len(items) <= 2000、policy 白名单、
      execute=true 必须 confirm=true、两个源都能 resolve、
      目标 can_write、源 can_delete、**只接受音频**（P.is_audio）
预检（execute=false）：逐条返回 {src_path, name, dst_rel, dst_path, exists, size, found}
执行：get_jobs().create("transfer", total=len(items), owner=payload.client)（忙 → 409）
     逐条 transfer_one，每条 r["src_path"]/r["dst"] 回填，累计 summary
     完成后 _reindex_after_transfer()：源侧 delete_tracks_by_paths + 目标侧按目录局部重扫（最多 50 个目录）
```

"**只接音频**"这条规则写在 `files_transfer` 的注释里：库里的非音乐文件可以做基础文件操作
（重命名/移动/删除/查看），但**不能跨库传输**——跨库传输带校验和"复制后删源"，是给曲目用的；
把 `.nfo/.jpg/.cue` 捎带过去，到了对面就成了孤儿文件，而且源那份已经进回收站，
用户很难联想到是这次操作干的。

---

## 12. 任务系统与并发控制

### 12.1 `JobState` 字段（`core/jobs.py`）

| 字段 | 类型/默认 | 含义 |
|---|---|---|
| `id` | `uuid4().hex[:12]` | 任务 id |
| `kind` | str | 任务类型（决定是否独占、界面显示名） |
| `state` | `"running"` | `running / paused / done / error / cancelled` |
| `total` / `done` | int | 进度；`to_dict()` 算 `percent` |
| `message` / `current` | str | 进度文案 / 当前处理的文件 |
| `started` / `finished` | float / None | `to_dict()` 算 `elapsed`、`rate`（`elapsed>0.3` 才算）、`eta` |
| `result` | dict | 任务结果（`/api/jobs/{jid}` 里返回） |
| `error` | str | 出错原因 |
| `cancel` | bool | 取消请求（worker 主动检查） |
| **`exclusive`** | bool | **这个任务实际占不占独占位**（建任务时定，之后不再变） |
| `pause` | bool | 暂停请求（与 cancel 分开：取消是不做了、暂停是先停一下） |
| `owner` | str | 发起者页面 id（`TaskSync.ME`），用于"隔壁在忙"判断 |
| `heartbeat` | float | 最后一次心跳（只有 `touch()` 会写它） |
| `skipped` | list | 被跳过的条目（上限 500 条，`Jobs.skip` 裁剪） |
| `acked` | bool | 结果是否已被前端读取（"收尾完成"） |
| `steps` | list | 崩溃恢复用的原始步骤 |

`to_dict()` 里几个由状态推导出来的字段：`stale`（`is_stale()`）、`idle`（距心跳秒数）、
`exclusive`（**来自 `self.exclusive`**）、`can_pause` / `can_cancel`（只有 running 才行）、
`can_resume`（只有 paused）、`recoverable`（有 steps 且 state in error/paused/cancelled）、`step_count`。

### 12.2 `EXCLUSIVE_KINDS`（12 个）

```python
EXCLUSIVE_KINDS = {"sync", "move", "organize", "collect", "upload", "trash",
                   "tags", "station", "cleanup",
                   "transfer",          # 同时改两个源（目标写入 + 源删除）
                   "scan",              # ★ 会写索引
                   "upload-commit"}     # ★ 会把暂存区文件搬进库并改索引
```

注释解释了 `transfer` 为什么在：跨库传输会同时改两个数据源，必须独占，和别的写流程并行会
基于过期的文件状态操作。

`scan` 与 `upload-commit` 是"实打实的漏网"（注释原话）：

* `scan` 会**写索引**，`main.start_scan` 的注释里早就写着"扫描会写索引，和文件移动冲突 → 也要独占"，
  但那时 `create("scan")` 走默认推导而名字不在集合里 → `exclusive=False` → 扫描期间可以开始
  移动/整理，索引里会留下指向**已经移走文件**的记录（表现：搜索得到、点进去播放失败）。
* `upload-commit` 会把暂存区文件搬进库里并改索引，同样不能在别的写流程中途插进来。

### 12.3 独占位语义

```python
def create(kind, total=0, owner="", exclusive=None):
    want_excl = (kind in EXCLUSIVE_KINDS) if exclusive is None else bool(exclusive)
    with self._lock:
        self._reap_stale_locked()          # 先回收卡死的任务
        if want_excl:
            busy = self._busy_locked()
            if busy is not None: raise JobBusy(busy)     # ★ 不排队
    …建 JobState、落库 db.create_job()

def _busy_locked():
    for jid in reversed(self._order):                  # 从最新往回找
        st = self._jobs.get(jid)
        if st and st.state in ("running", "paused") and st.exclusive: return st
    return None
```

三条不可动摇的规则：

1. **不排队**。docstring 原话："排队在文件操作里是危险的：用户以为提交了就完事，实际上操作的
   是很久以后的磁盘状态。"
2. **暂停中的任务也算占用**——它随时可能继续，让别的流程插进来两边会操作同一批文件。
3. **判据是 `st.exclusive` 而不是"kind 在不在 `EXCLUSIVE_KINDS` 里"**。

第 3 条是实打实的 bug（注释写了）：`run_in_background` 一律用 `exclusive=False` 建任务
（只读计算，本不该挡写操作），但 `_busy_locked` 原来按 kind 判断 —— 于是
`run_in_background("organize", …)` 一边"没占独占位"、一边**真的把写操作全挡住了**，
整整 6 秒（实测）里 `/api/tags/write` 之类的接口全返回 409。建任务时那次检查反而是对的
（用的是 `want_excl`），两处判据不一致才会出现"看起来没占、实际占着"的怪状态。

### 12.4 `exclusive` 字段为什么必要

* `Jobs.create()` 需要它来记"这次到底占没占"（`exclusive=False` 可以建出 kind 属于
  `EXCLUSIVE_KINDS` 的任务，比如只读的方案计算）；
* `to_dict()` 需要它来**如实上报**：原来写 `self.kind in EXCLUSIVE_KINDS`，于是
  `run_in_background("organize", …, exclusive=False)` 在界面和日志里被报成独占，
  "看起来像'生成方案把写操作全挡住了'，排查时完全被带偏"；
* `_busy_locked()` 需要它作为唯一判据（见上）。

唯一允许"按 kind 猜"的地方是 `Jobs.load_unfinished()`（崩溃恢复）：`exclusive` 没落库，
只能按 kind 推。注释说明这是可接受的——恢复出来的任务是 `error` 状态、本来就不占位，
偏保守没有实际代价。

### 12.5 `guard.exclusive_guard`

```python
def exclusive_guard(x_msh_client: str = Header(default="", alias="X-MSH-Client"),
                    x_msh_job: str = Header(default="", alias="X-MSH-Job")) -> None:
    jb = current_busy()           # get_jobs().busy()
    if jb is None: return
    if x_msh_job and str(jb["id"]) == x_msh_job: return   # 正在执行的任务自己回来
    raise HTTPException(409, "已有任务在执行：{kind}（done/total）…")
```

为什么放在独立模块而不是 `main.py`（docstring）：避免循环导入——各 router 都要用它，
而 `main.py` 又要 import 这些 router；`guard.py` 只依赖 `core.jobs`，没有环。

为什么需要服务端这一层：**前端的 `runExclusive` 是体验，这里是约束**。任何客户端——另一个
标签页、脚本、curl、重放请求——都能绕过前端。两个写流程同时跑会基于过期的文件状态操作。

挂载方式：路由函数参数 `exclusive: None = Depends(exclusive_guard)`（FastAPI 依赖注入）。
带这个依赖的端点见 13 章每个表里的"守卫"列。**唯一例外**是任务自己带
`X-MSH-Job: <自己的 id>` 回来做后续调用，否则任务跑到一半会被自己挡住。

### 12.6 `bgjob.run_in_background`

```python
def run_in_background(kind, total, work, on_done=None, owner="") -> JobState:
    job = get_jobs().create(kind, total=total, owner=owner, exclusive=False)
    # daemon 线程：out = work(job) → finish → job.result = out
    # 异常：log.error(带 traceback) + job.result={"traceback": 前4000字} + finish(error=…)
```

分工（docstring）：`Executor` 是"会改文件"的执行器（要独占、要记步骤、要崩溃恢复）；
这里是**只读计算**，不占独占位、不写任何文件，所以可以放心并行。
`work` 的契约：定期写 `job.done/message/current`、定期检查 `job.cancel`、定期 `job.touch()`。

后台任务失败**必须入日志**（注释）：以前只塞进 `job.error`——用户看到界面"任务失败"，
但"为什么失败"（堆栈）只在那一刻的内存里，关掉页面就再也找不回来了。

### 12.7 暂停 / 取消 / 恢复

```python
cancel(jid):  state in (running,paused) → cancel=True, pause=False（取消优先）, touch()
pause(jid):   state == running → pause=True, message="已暂停（可以继续）", touch()
resume(jid):  pause → pause=False, state=running, touch()
wait_if_paused(st, timeout=3600):   while st.pause and not st.cancel: st.state="paused"; touch(); sleep(0.2)
```

`Executor.work()` 在**每一步开始前**调 `wait_if_paused`：已经做完的不回退，恢复后从下一个
未处理的步骤继续。`finish()` 的状态映射（容易搞错的一处）：

```python
if error:        state = error
elif st.pause:   state = paused   # ★ 不能标 done，否则"继续"就找不到它，界面也会显示"已完成"
elif st.cancel:  state = cancelled
else:            state = done
```

`is_stale()` 也特意排除非 running 状态：**暂停中的任务不算卡死**——它是用户主动停的，
心跳本来就该停，否则会被"卡死回收"误判成中断。

### 12.8 崩溃恢复

两条互不替代的路径：

1. **进程内恢复**（`unfinished()` + `mark_interrupted()`）：状态是 running/paused 且
   `now - heartbeat > STALE_JOB_SECONDS` → 认为进程没了。`mark_interrupted()` 把这类任务
   标成 error，理由："不这么做的话，重启后界面上会出现一个永远不动、也无法清理的'运行中'任务，
   还会一直占着独占位。"
2. **跨进程恢复**（`load_unfinished()`，`lifespan` 启动时调用）：
   `db.list_jobs(limit=self.keep)` 读回 `state in (running, paused)` 的记录，标成
   `state="error"`、`message="已中断（可以尝试接着做完）"`，并从 `job.detail` 里解析步骤 JSON：

```python
raw = r["detail"] or ""
if raw.startswith("[") or raw.startswith("{"):
    obj = json.loads(raw)
    st.steps = obj["steps"] if isinstance(obj, dict) else obj
```

   步骤落库由 `record_steps()` → `persist_steps()` 完成：`update_job(detail=json.dumps(
   {"steps": steps, "recoverable": True}, ensure_ascii=False)[:200000])`。

   为什么必须做（docstring）：任务状态原来只活在内存里，程序一挂（崩溃/被任务管理器结束/断电）
   就什么都没了——用户重启后既看不到"上次没做完"，也没法接着做完，只能自己回忆改了哪些文件。

**重放安全的前提**（`/api/unfinished/recover` 与 `record_steps` 的 docstring）：重放用的是
`skip` 策略——已经搬完的文件会因为源不存在而被跳过，目标已存在的也会被跳过，所以重放不会覆盖、
不会重复，最多是"白跑一趟"。`record_steps` 只在 `execute=True` 时调用（预演不记，
否则会变成"幽灵可恢复任务"）。

### 12.9 `STALE_JOB_SECONDS` 与心跳回收

```python
STALE_JOB_SECONDS = float(os.environ.get("MSH_STALE_JOB_SEC") or 180)
```

`_reap_stale_locked()`：对每个 `is_stale()` 的任务 → `state="error"`、
`error="任务心跳中断，已判定为中断（超过 N 秒没有进展）"`、`finished=now`、`message="已中断"`，
**并且必须落库**：

```python
self.db.update_job(st.id, state=st.state, done=st.done, total=st.total,
                   message=st.error, finished=st.finished)
```

注释记录了两个坑：

* 原来只改内存里的 JobState → 进程重启后从 DB 恢复时这条任务仍是 `running`，界面显示"还在跑"，
  而它永远不会再动了；
* `job` 表**没有 `error` 列**（只有 detail/done/finished/id/kind/message/started/state/total），
  传 `error=` 会 `OperationalError`，而 `except` 一吞就变成"看着落库了、其实没有"——
  "这正是这一条待办原本要修的东西，差点用同样的方式再犯一次"，所以中断原因写进 `message`。
  落库失败也只 `log.warning` 不静默（重启后这条任务会显示成"还在跑"）。

`Jobs` 的 `keep` 从 40 提到 200，理由：崩溃恢复要靠**上一次运行**留下的记录，留得太少的话，
用户跑了一堆任务再崩溃，那条关键记录可能已经被挤掉了。

### 12.10 `_adopt` / `rebind`：注册表与数据库的对齐

`get_jobs()` 不带参数时会惰性建出 `db=None` 的注册表（`guard.py`、`scanner.py` 兜底、
任何只读路径都会这么调）。`_adopt(db)` 的三条规则都是踩出来的：

| 情形 | 行为 | 原因 |
|---|---|---|
| 还没有注册表 | 建一个 | — |
| 有注册表但 `db is None` | **只把 db 装上，不重建** | 否则 `main.db()` 第一次建库时"正在跑的任务就凭空消失了"——列表空了、`/api/busy` 说没人忙，可任务还在跑（实测：扫描任务建好 0.3 秒后从列表里消失） |
| `db` 指向另一个库 | 重建（新库不该背旧库的任务） | — |
| 换库时 `jobs.busy()` 非空 | **保持旧库**，不重建 | 安全网："绝不为了换库而丢掉正在跑的任务"——最多是任务记录写不进新库，也不能让"正在移动文件的任务"从界面上消失 |

`rebind(db)` 存在的理由：调用方（尤其测试）换库的方式是改 `MSH_DB` 再让 `main.DB = None`，
但**没人会记得同时重置 `core.jobs` 的单例**，结果后面的任务都进了第一个测试那个库的注册表，
而那个库的连接早关了——"表现是任务建好就从列表里消失，报错点离真正原因非常远"。
`main.db()` 每次解析完都调 `rebind_jobs(DB)`，就没有"记得重置"这个要求了。

---

## 13. HTTP API 参考

**统计口径**：`/api/*` 共 **93** 条，加首页 `GET /` 共 **94** 个端点
（`main.py` 48 / `search_api.py` 11 / `upload_api.py` 11 / `sources/folders_api.py` 8 /
`station_api.py` 7 / `tags_api.py` 5 / `mappings_api.py` 3）。

**命令约定**（贯穿本章，也是前端的约定）：

* 所有写操作都接受 `"execute": false`（预演）与 `"execute": true + "confirm": true"`；
  `confirm` 缺失 → **400**（不是 200 + 部分失败）。
* `policy` 只能是 `skip` / `rename` / `overwrite`，其它值 → 400。
* 守卫列写"**是**"表示参数里有 `exclusive: None = Depends(exclusive_guard)`：
  有独占任务在跑时返回 **409**（任务自己带 `X-MSH-Job` 回调用除外）。
* 错误码语义：`SourceNotFound`→404、`ReadOnlyError`→403、`SourceError`→400、
  `JobBusy`→409、未处理异常→500（详见 17.2）。

### 13.1 系统与基础（`main.py`）

| 方法 | 路径 | 作用 | 关键参数 |
|---|---|---|---|
| GET | `/api/system` | 版本、`test_mode`、数据目录/索引路径与大小、各源曲目数、运行中任务、`capabilities{mutagen,pillow}`、`audio_exts`（**后端提供给前端的音频清单**） | — |
| GET | `/api/source-types` | 可选数据源类型（`available_types()`：kind/label/hint/experimental/verify_note） | — |
| GET | `/api/logs` | 后台日志：默认读内存环形缓冲 | `limit`(默认200)、`level`(ERROR/WARNING/…)、`full=false\|true`、`lines`(400) |
| POST | `/api/logs/clear` | 清空**内存**日志（不动文件） | — |
| GET | `/api/drives` | 本机盘列表 + 便携设备（MTP，`supported=false`） | — |
| GET | `/api/device/probe` | 探测某目录落在什么盘上（**会测读速**）并给默认策略 | `path`(必填；缺→400，不存在→404) |

### 13.2 数据源（`main.py`）

| 方法 | 路径 | 作用 | 关键参数 | 守卫 |
|---|---|---|---|---|
| GET | `/api/sources` | 数据源列表（配置 + `has_secret`/`last_scan`/`indexed`/`connected=null`） | — | — |
| GET | `/api/sources/status` | 轻量探活：只 `stat` 根目录，返回 `ok/error/latency/checked_at` | — | — |
| POST | `/api/sources` | 新建数据源（缺 id 自动生成 `src<epoch>`；id 重复→409；带 `password` 则写 secrets） | body = `SourceConfig` 字段 + 可选 `password` | — |
| PUT | `/api/sources/{sid}` | 更新数据源（与旧值 merge 后校验；可改 `password`） | body 同上；不存在→404 | **是** |
| DELETE | `/api/sources/{sid}` | 删除数据源 | `purge`(bool，同时清索引) | **是** |
| POST | `/api/sources/{sid}/test` | 连接测试：`listdir` 后统计 entries/dirs/audio，附 `device` 与 `tip` | — | — |
| POST | `/api/sources/{sid}/browse` | 浏览目录树（只列子目录名 + 前 500 个音频） | body `{path}` | — |

### 13.3 扫描与任务（`main.py`）

| 方法 | 路径 | 作用 | 关键参数 |
|---|---|---|---|
| POST | `/api/scan/{sid}` | 起一次扫描（后台线程）。**先校验数据源再占独占位** | `subpath`、`workers`、`compute_hash`、`hash_limit_mb`、`quick_probe_kb`、`max_files`、`client` |
| GET | `/api/jobs` | 任务列表（最新在前，最多 `keep=200` 条） | — |
| GET | `/api/busy` | `{busy, job}`：当前独占任务快照（多页面轮询用） | — |
| GET | `/api/jobs/{jid}` | 单个任务详情（`to_dict()` 全字段） | 不存在→404 |
| POST | `/api/jobs/{jid}/ack` | 标记结果已读（"一次只允许一个流程"的收尾点） | — |
| POST | `/api/jobs/{jid}/cancel` | 请求取消（`cancel=True`，并清 `pause`） | — |
| POST | `/api/jobs/{jid}/pause` | 请求暂停（worker 在下一个检查点停） | — |
| POST | `/api/jobs/{jid}/resume` | 继续 | — |
| POST | `/api/jobs/{jid}/dismiss` | 忽略：从记录里删掉（运行中→拒绝） | `{"ok": bool}` |
| POST | `/api/jobs/clear-finished` | 一键清理所有已结束任务 | `{"removed": n}` |

**回收站**（`app/routes/trash.py`，只支持本地/映射盘源）：

| 方法 | 路径 | 说明 | 失败/边界 |
| --- | --- | --- | --- |
| GET | `/api/trash` | 列出回收站条目（含 `origin` 原位置、大小、删除时间） | `400` 源非本地 |
| POST | `/api/trash/restore` | 还原（默认回原目录；同名 `skip/rename/overwrite`） | 越界 `rel` → 该条 `error` |
| POST | `/api/trash/purge` | 永久删除（必须 `confirm=true`） | 越界 `rel` → 该条 `error` |
| GET | `/api/unfinished` | 上次运行没做完的任务 `{count, recoverable, jobs}` | — |
| POST | `/api/unfinished/recover` | 用 `policy="skip"` 重放该任务的步骤 | `job`(必填；不存在→404，无步骤→400，仍在跑→409)、`client`、`workers` |

### 13.4 曲库浏览 / 试听 / 封面（`main.py`）

| 方法 | 路径 | 作用 | 关键参数 |
|---|---|---|---|
| GET | `/api/library/{sid}` | 曲库分页 + 筛选 + 排序 + 模糊搜索；同时返回**同一套筛选条件**下的统计与整库统计 | `q`、`prefix`、`sort`(path/name/title/artist/album/size/duration/bitrate/quality/mtime)、`order`、`limit`、`offset`、`lossless`、`min_bitrate`、`max_bitrate`、`min_duration`、`max_duration`、`has_cover`、`codec`、`fuzzy`、`min_score`(默认 0.45) |
| GET | `/api/folders/{sid}` | 目录树 + 每个目录的曲目数/字节数/深度 | — |
| GET | `/api/track/{tid}` | 单曲详情（`_track_view` + `display_path`） | 不存在→404 |
| POST | `/api/track/{tid}/refresh` | 只重扫一个文件（改完标签立即生效）；文件已消失则删索引并返回 `removed=true` | 解析失败→400 |
| GET | `/api/cover/{tid}` | 封面缩略图；无 `cover_hash` 时回退到同目录 cover 文件 | `size`(默认 160，夹在 48–1024) |
| GET | `/api/cover-raw` | 按路径直接取封面（浏览未索引目录用） | `sid`、`path`、`size` |
| GET | `/api/preview/{tid}` | 音频流，**支持 Range**（206 / 416），浏览器可直接拖进度条 | `Range` 头 |

### 13.5 对比 / 计划 / 执行（`main.py`）

| 方法 | 路径 | 作用 | 关键参数 |
|---|---|---|---|
| POST | `/api/compare` | 双栏对比（**默认后台任务**，`wait=true` 走同步分支） | `left`、`right`、`left_prefix`、`right_prefix`、`match_threshold`、`wait` |
| POST | `/api/plan` | 生成同步计划（只算不动） | `left`、`right`、`left_prefix`、`right_prefix`、`copy_missing`、`mirror_delete`、`direction`、`subdir`、`overwrite`、`include_duplicates`、`generate_conflicts`、`conflict_side`、`match_threshold`、**`transfer`** |
| POST | `/api/execute` | 执行计划（`execute=false` 为预演，**不占独占位**） | `steps`(必填，≤`max_steps`=20000)、`execute`、`confirm`、`policy`、`verify`、`verify_hash`、`hard_delete`、`workers`、`client` |
| POST | `/api/files/transfer` | **跨库传输**（语义是"移动"：成功后源那份进回收站）。`execute=false` 只做预检；执行时直接循环 `transfer_one`（不经 Executor），完成后对齐两侧索引 | `from`、`to`、`items[]`(源内绝对路径，≤2000)、`dst_dir`(目标内目录，空=根)、`policy`、`verify`、`verify_hash`、`hard_delete`、`transfer_mode`、`execute`、`confirm`、`client`；**守卫 是** |
| POST | `/api/files/move` | 同源内移动到另一个目录（翻译成 `move` 步骤走 Executor） | `steps[{id?,src,src_path,dst?,dst_path,size?}]`、`execute`、`confirm`、`policy`、`workers`、`max_steps`；**守卫 是** |

### 13.6 查重（`main.py`）

| 方法 | 路径 | 作用 | 关键参数 | 守卫 |
|---|---|---|---|---|
| POST | `/api/duplicates` | 查重（**默认后台任务**）。`within=same` 走 `group_duplicates`；`within=cross` 走两两 `pair_sources` + `merge_matched_pairs` | `sources`(列表；空=全部)、`within`、`hash_mode`(none/quick/full)、`threshold`(0.9)、`min_size`、`wait` | — |
| POST | `/api/duplicates/keep` | 按策略给出每组"保留哪份"的建议（`largest`/`oldest`/`newest`/其它=quality 优先） | `groups`、`policy`(默认取 `settings.default_keep_policy`) | **是** |

`/api/duplicates` 的返回里，每组额外带：`key`（前缀 `source_id::`）、`base_key`、
`source_id`、`size_equal`、`identical_paths`、`clusters`、`exact` / `hash_verified` /
`verified_items`、`safe_wasted_bytes`、`wasted_bytes`，以及每条 item 的
`version_tags` 与 `did`（`source_id::path`，前端用它当唯一键）。

### 13.7 文件浏览（资源管理器式，`main.py`）

| 方法 | 路径 | 作用 | 关键参数 |
|---|---|---|---|
| GET | `/api/files/folders` | 列某目录下的文件夹（带曲目数，含子目录汇总） | `sid`、`path` |
| GET | `/api/files/list` | 列真实目录内容：子文件夹（带计数）+ 音频文件（索引里有元数据的带上元数据）+ **磁盘余量** `disk` | `sid`、`path`、`sort`(name/count/mtime/size/duration/quality)、`order`、`limit` |
| GET | `/api/state` | 很轻的"数据变了没有"信号（`tracks`/`mtime`(WAL 文件时间)/`sources`/`jobs_done`/`busy`），前端据此自动刷新 | `sid`(可选) |
| POST | `/api/files/under` | 列出若干目录（含子目录）下**索引里**的全部音频（目录树多选联动） | `source`、`paths[]`（相对或绝对；越界丢弃） |
| POST | `/api/files/select` | 按条件快速集合曲目（同作者/同专辑/同格式/同 codec） | `source`、`prefix`、`by`(artist/album/ext/codec)、`value` |
| GET | `/api/files/search` | 按名字搜**文件夹**（只搜索引里出现过的目录） | `sid`、`q`、`limit`(≤300) |
| GET | `/api/files/facets` | 按维度统计分布（"按作者快速集合"用） | `sid`、`path`、`field`(artist/album/codec/ext)、`limit`(≤500) |
| POST | `/api/files/exists` | 批量问"这些文件还在不在"（候选区标过期项；只 `stat`） | `source`、`paths[]`(≤5000) |

### 13.8 设置（`main.py`）

| 方法 | 路径 | 作用 | 关键参数 |
|---|---|---|---|
| GET | `/api/settings` | 读设置 + `config_path` | — |
| PUT | `/api/settings` | 保存设置，**逐项校验**；全部不合法→400，部分不合法→200 + `errors/warning` | body = 任意 `Settings` 字段 |
| POST | `/api/reload` | 强制重读 `config.toml` 并 `manager.close_all()` | — |

### 13.9 首页（`main.py`，非 `/api`）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 返回 `web/index.html`；`MSH_TEST_MODE=1` 时**服务端注入**测试模式水印 DOM（不只靠前端 JS，避免脚本报错时看不到"这是演示环境"） |

### 13.10 文件夹级操作（`sources/folders_api.py`）

| 方法 | 路径 | 作用 | 关键参数 | 守卫 |
|---|---|---|---|---|
| POST | `/api/folders/create` | 新建子目录（名字禁 `\ / : * ? " < > \|`；同名→409） | `source`、`parent`、`name` | **是** |
| POST | `/api/folders/rename` | 目录改名；给 `new_parent` 时等价"移动到另一个父目录"；不允许移进自己的子孙 | `source`、`path`、`name`、`new_parent` | **是** |
| POST | `/api/folders/delete` | 删除目录：默认**移入回收站**；`hard=true` 必须再给 `confirm_hard=true`；删除后清索引 | `source`、`path`、`hard`、`confirm_hard` | **是** |
| GET | `/api/folders/info` | 删除/移动前的信息（files/dirs/bytes/audio + `read_only`） | `sid`、`path` | — |
| GET | `/api/folders/files` | 列出目录下**所有层级**的音频文件（含 `did`/`rel`） | `sid`、`path` | — |
| POST | `/api/folders/dissolve` | **已移除**：恒定返回 410，提示改用 `empty-scan`/`empty-remove` | — | 是 |
| GET | `/api/folders/empty-scan` | 递归找出所有"空文件夹"（只有空子目录也算空；防御上限 20000 个目录） | `sid`、`start`(空=根) | — |
| POST | `/api/folders/empty-remove` | 删除选中的**空**目录（先深后浅；非空一律跳过并报告） | `source`、`paths[]`、`confirm=true` | 是 |

### 13.11 搜索 / 集合 / 智能整理（`search_api.py`）

| 方法 | 路径 | 作用 | 关键参数 | 守卫 |
|---|---|---|---|---|
| GET | `/api/search` | 模糊搜索曲目/文件夹；路径命中打 0.8 折（避免"整目录刷屏"） | `sid`、`q`、`scope`(all/here)、`path`、`kind`(track/folder/both)、`limit`(≤500)、`min_score` | — |
| POST | `/api/collect/preview` | 集合前的检查：同名冲突、缺失、总体积 | `source`、`parent`、`name`、`paths[]` | — |
| POST | `/api/collect/apply` | 把选中曲目**移动**到一个（可新建的）目录 | 同上 + `execute`、`confirm`、`policy` | **是** |
| GET | `/api/organize/preview` | 现状体检：收纳区（sinks）、目录画像、疑似放错位置、合并建议 | `sid`、`limit` | — |
| GET | `/api/organize/analyze` | "这首歌应该放哪？"：按证据强度给候选目录（4=目录名是专辑 / 3=目录内有同专辑 / 2=目录名是作者 / 1=目录内有同作者） | `sid`、`path` **或** `q`、`limit` | — |
| POST | `/api/organize/plan-paths` | **只为指定文件**推算去向（候选区/入库用），规则同智能整理 | `source`、`paths[]`、`group_by`、`structure`、`min_group` | — |
| POST | `/api/organize/album-plan` | 按专辑/作者分组生成整理方案（**默认后台**，`wait=true` 同步） | `source`、`group_by`、`structure`、`scopes[]`、`sinks[]`、`min_group`、`small_policy`、`include_artist_fallback`、`custom{}`、`wait` | — |
| POST | `/api/organize/album-apply` | 执行"按专辑整理"（可只执行 `only` 里的专辑键） | `source`、`plan`(可省，省则重算)、`only[]`、`plan_hash`、`execute`、`confirm`、`policy`、`workers` | **是** |
| POST | `/api/organize/plan` | 生成"只修放错位置单曲"的方案（只算不动） | `source`、`sinks[]`、`scopes[]`/`path`、`min_group`、`singles_dir`、`fallback_sink`、`include_gather`、`orphan_policy` | — |
| POST | `/api/organize/apply` | 执行整理方案（可直接传界面那份 `plan`） | `source`、`plan`、`plan_hash`、`only[]`、`execute`、`confirm`、`policy`、`workers` | **是** |
| POST | `/api/organize/folder` | 对**单个目录**就地整理（按作者/专辑分到子目录） | `source`、`path`、`mode`(artist_album/artist/album)、`min_group`、`template`、`sinks`、`singles_dir`、`fallback_sink`、`include_gather`、`orphan_policy`、`execute`、`confirm`、`only[]`、`policy`、`workers` | **是** |

`plan_hash` 的语义：`_plan_hash(plan)` 用 `sha1(path + ">" + dst_dir + "@" + size)` 取前 16 位。
**必须包含 size** 的原因写在注释里：原来只拼 `path` 和 `dst_dir`，于是"同一路径的文件被替换了
（重新下载/换质量/损坏修复）"检测不到，用户点执行时搬走的是他确认时**没看过**的那个文件。
不用 `mtime` 是因为"重新扫描/同步工具碰一下就会变，会误报"。

### 13.12 上传与暂存区（`upload_api.py`）

| 方法 | 路径 | 作用 | 关键参数 | 守卫 |
|---|---|---|---|---|
| POST | `/api/upload` | 收文件进暂存区 `<staging>/<batch>/`；只接音频；单文件上限 **1.5 GB**；同名自动改名，绝不覆盖 | multipart `files[]`、`source`、`subdir` | — |
| GET | `/api/upload/batches` | 批次列表（**以磁盘为准**，manifest 丢了也会列出来；含 `stale`/`orphan`/`ignored` 非音频） | — | — |
| DELETE | `/api/upload/batch` | 丢掉一个批次（只在暂存区删） | `batch`(query) | **是** |
| POST | `/api/upload/batch-to-station` | 把批次转进中转站（长期存放） | `batches[]` | **是** |
| POST | `/api/upload/cleanup` | 清理暂存区：`stale`/`empty`/`all`（`all` 需 `confirm`） | `mode`、`confirm`、`keep[]` | **是** |
| POST | `/api/upload/dedup` | 启动入库查重任务（只提示、不拦截） | `batch`、`source` | — |
| GET | `/api/upload/dedup` | 查查重任务进度/结果 | `job` | — |
| GET | `/api/upload/dups` | 取某批次上次查重结果（存在 manifest 里） | `batch` | — |
| GET | `/api/upload/inspect` | 分析批次：读标签 + 与库内目录比对 + 给建议去向 | `batch`、`source`、`min_group`、`custom`(JSON 字符串) | — |
| GET | `/api/upload/cover` | 暂存区某文件的封面缩略图（`.<stem>.cover` 旁文件） | `batch`、`rel`（含 `..` → 400） | — |
| POST | `/api/upload/commit` | 把文件按确认后的去向搬进库（走 Executor） | `batch`、`source`、`moves[{rel,dst}]`、`confirm`、`policy`、`scan` | **是** |

### 13.13 中转站（`station_api.py`）

| 方法 | 路径 | 作用 | 关键参数 | 守卫 |
|---|---|---|---|---|
| GET | `/api/station` | 概况（物理占用/配额/剩余/磁盘余量）+ **音频**清单 + `ignored`（非音频杂物） | — | — |
| POST | `/api/station/move-in` | 把库里的文件**移入**中转站（记录来源，供还原） | `source`、`paths[]` | **是** |
| POST | `/api/station/move-out` | 把中转站文件归入某目录（**最后一道闸：非音频绝不归库**） | `source`、`files[]`、`dest` | **是** |
| POST | `/api/station/remove` | 删除中转站文件（**移入回收站**：优先数据源的 `.music-sync-trash`，否则 `<station>/_trash/<日期>`） | `source`、`files[]`、`confirm=true` | **是** |
| POST | `/api/station/restore` | 按 `.station.json` 记录把文件还原到原位置 | `files[]` | **是** |
| POST | `/api/station/suggest` | 为选中文件算**建议去向**（纯计算，复用 `organize.plan_by_album`） | `source`、`files[]` | — |
| GET | `/api/station/check` | 容量预检（判定与 `check_capacity` **必须一致**：物理盘是硬约束、配额是软约束） | `add_mb` | — |

### 13.14 标签 / 封面编辑（`tags_api.py`）

| 方法 | 路径 | 作用 | 关键参数 | 守卫 |
|---|---|---|---|---|
| GET | `/api/tags/read` | 读一首歌的标签 + 封面 data URL + 缩略图 | `sid`、`path`（越界→400；只支持 `type=local`） | — |
| POST | `/api/tags/preview` | 改之前先看：哪些文件、哪些字段、从什么变成什么（≤500 个文件） | `source`、`paths[]`、`changes{}` | — |
| POST | `/api/tags/write` | 写标签（`fields` 限定可改字段；`cover_base64` 上限 12 MB；自动备份 + 可选刷新索引） | `source`、`paths[]`、`changes{}`、`fields`、`confirm=true`、`cover_base64`、`remove_cover`、`delete_empty`、`refresh_index` | **是** |
| GET | `/api/tags/backups` | 标签备份列表 | `limit` | — |
| POST | `/api/tags/restore` | 从备份还原标签 | `name`、`confirm=true`、`source` | **是** |

### 13.15 映射规则（`mappings_api.py`）

| 方法 | 路径 | 作用 | 关键参数 |
|---|---|---|---|
| GET | `/api/mappings` | 读映射规则：给 `sid` 返回该源的规则，否则返回全部源 | `sid` |
| PUT | `/api/mappings` | 整体替换某源的映射（空对象=删掉该源条目） | `source`、`rules{}` |
| POST | `/api/mappings/one` | 加/改/删一条规则（`dst` 为空 = 删除该条） | `source`、`key`、`dst` |

映射存在 `<data_dir>/mappings.json`，形状 `{"<sid>": {"<专辑或作者>": "<目录>", "__sink__": "0单曲", "_updated": <ts>}}`。
选独立文件而不是塞进 `config.toml` 的理由（模块 docstring）：它是纯运行时数据、量可能不小、
读写频繁，"不该让主配置反复重写"。写入同样走 `.part` → `os.replace`。

---

## 14. 前端架构

### 14.1 无构建

* 没有打包器、没有 `node_modules`、没有 TS 编译：`app/web/index.html` 直接用
  `<script src="/static/js/xxx.js?v=101">` 顺序加载 18 个文件，最后一行 `<script>App.boot();</script>` 启动。
* 每个 JS 文件都是 IIFE 风格的全局单例（`const API = (() => {…})()`），靠**加载顺序**解决依赖：
  `prefs → api → ui → tasksync → app → views.* → folderpicker → staging → upload → tags → organize → views.files → …`。
* CSS 单文件 `static/css/app.css`（1385 行），图标用内联 SVG sprite（见 14.8）。
* 静态资源带 `?v=101` 缓存戳（`tools/check_frontend.py` 正是按 `\?v=\d+` 这个模式扫描引用的）。

### 14.2 全局 `State`（`app.js` 开头）

```javascript
const State = {
  view: 'sources',        // 当前视图名
  sources: [],            // 数据源列表
  system: null,           // /api/system 的结果
  activeSource: null,     // 侧栏选中的数据源 id
  tracks: [],             // 当前曲库视图的曲目
  libFilters: {},         // 曲库过滤条件
  treePrefix: '',         // 侧栏目录树当前选中目录（曲库/文件视图共用）
  playlist: [],           // 当前表格的 track id 顺序
  curTrackId: null,       // 正在播放的 track id
  cmp: null,              // 最近一次对比结果
  plan: null,             // 最近一次同步计划
  dup: null,              // 最近一次查重结果
  opLog: [],              // 操作记录
  job: null,
};
```

运行期还会动态挂上：`State.srcStatus`（探活结果 map）、`State.srcStatusAt`、
`State.exclusiveBusy`、`State.bootErrors`。

### 14.3 `App` 外壳（`app.js` 的返回对象）

| 成员 | 作用 |
|---|---|
| `registerView(name, v)` / `viewApi(name)` | 视图注册与跨视图取方法（`views[name]` 映射） |
| `setView(name)` | 切视图：更新 `.tab.active`/`.view.hidden`、**懒挂载**（`el.dataset.mounted`）、调 `mount()`+`render()`，并对非 `files` 视图额外调一次 `load()`；同时同步侧栏折叠兜底导航、按视图显隐侧栏目录树 |
| `loadSources()` / `selectSource()` / `sourceById()` | 数据源加载与选中（选中项记进 `Prefs`） |
| `refreshSourceStatus()` / `statusOf()` / `dotHtml()` | 探活并画在线小圆点（带请求序号 `_statusSeq` 丢弃过期响应） |
| `renderSidebar()` / `renderTree()` / `treeSelect()` / `syncTree()` | 侧栏与目录树 |
| `runExclusive(kind, fn)` | **写操作统一入口**：先 `TaskSync.begin` → `paintBusyUi` → `fn()` → `finally` 里 **只调一次** `TaskSync.finish` |
| `paintBusyUi()` | 忙时禁用 `[data-exclusive]` 按钮、显示 `#busy-bar`（区分"隔壁独占任务"与"只读长任务"两种文案） |
| `logOp(plan, result, steps)` | 追加操作记录，并挂上"只重试可重试步骤"的 `retry()`（契约：**只生成步骤、不执行**） |
| `showJobs()` / `refreshJobs()` / `checkUnfinished()` | 任务中心、轮询（1.5 s）、崩溃恢复提示 |
| `playTrack` / `playList` / `queue` / `stepTrack` / `paintNowPlaying` | 播放器（见 14.7） |
| `startAutoRefresh(ms=4000)` / `autoRefreshTick()` / `currentSig()` | 基于 `/api/state` 的自动刷新 |
| `boot()` | 逐段初始化（每段独立 `try`，失败记进 `State.bootErrors` 而不是静默中断） |

`boot()` 的关键顺序（`app.js::boot`）：

```
step('播放器', mountPlayer)
step('导航窗格', mountNavToggle)
step('恢复后台任务', mountBackgroundTaskSync)   // TaskSync.onChange/startPolling(3000)/resume()
setTimeout(checkUnfinished, 1500)              // 崩溃恢复询问
绑定 .tab onclick / #btn-add-source / #btn-reload-cfg
await loadSources(); await loadSystem(); setView(Prefs 记住的 lastView 或 'sources')
setTimeout(报告 bootErrors, 800)
setInterval(refreshJobs, 1500) / setInterval(loadSystem, 20000) / setInterval(refreshSourceStatus, 60000)
window 'focus' 时若距上次探活 >30s 再探一次
startAutoRefresh(4000)
```

注释解释了为什么每段独立 `try`："以前是串成一条直线，中间任何一步抛错，后面的初始化就
**全部悄悄跳过**——表现是'按钮点了没反应'，而且不报错，极难查"。

### 14.4 `UI` 与 `U` 工具层

* `ui.js` → `const UI = (() => {…})()`：`toast(msg, type, ms)`（应用名/标题/正文三段式，
  悬停暂停自动消失）、`modal({title,size,body,footer})`、`confirm()`、进度条、
  `contextMenu(x, y, items)` 等。**`toast` 依赖 `#toast-root` 带 `class="toast-wrap"`**
  （index.html 里那段注释记录过事故：只有 id 没 class 时 toast 是 `position:static`，
  落在页脚之后、首屏之外，所有操作反馈用户都看不见）。
* `U`（定义在 `api.js` 尾部，`audit_frontend.py` 里按 `U` 检查其导出）：转义 `esc`、
  图标 `icon(name, cls)`、`parent`、`qualityLabel`、`humanSize` 等纯函数。
* `Prefs`（`prefs.js`）：键值登记的持久化层。已知登记键（由测试断言可证）：
  `volume`、`navHidden`、`libCols`、`activeSource`、`lastView`、`libFilters`、`filesSort`、
  `dupSepVer`；并有 `_backfill` 把历史裸键（`localStorage.setItem('msh.volume', …)`）迁移进来。
  测试 `PrefsConsistencyTest` 的规则：**登记了什么，设置页就显示什么**——不能登记永远不写的键
  （`filesView`/`stationCols`/`dupFilter` 等已被删除）。

### 14.5 视图注册机制

每个 `views.*.js` 末尾一行注册：

```javascript
App.registerView('sources',  { mount, render, openAdd, refreshStatus });
App.registerView('library',  { mount, render, reload, pickFolder, setScope });
App.registerView('files',    { … });        // 方法最多，见 views.files.js:2554
App.registerView('station',  { mount, render, load });
App.registerView('compare',  { mount, render, run, planDialog });
App.registerView('duplicates', { mount, render, run });
App.registerView('ops',      { mount, render });
App.registerView('settings', { mount, render });
```

契约：`mount(el)` 只在首次进入时调用（`setView` 用 `el.dataset.mounted` 判断）；
`render(el)` 每次进入都调；可选的 `load()` 在 `render` 之后被调（`files` 视图除外）。
跨视图跳转通过 `App.viewApi('files').xxx()` 这类方式，而不是直接访问别人的内部函数。

### 14.6 `TaskSync`：跨页面防冲突 + 刷新接续

`tasksync.js` 解决两件事（文件头说明）：

1. **多开页面**：后端独占锁保证"一次只有一个流程在跑"，但前端得**知道**隔壁在忙——
   否则按钮还是亮的、点了才被 409 顶回来。用 `BroadcastChannel('msh-task')` 广播，
   不支持就退回 `localStorage` 的 `storage` 事件；另外每 3 秒轮询 `/api/busy` 兜底。
2. **刷新页面不中断任务**：任务跑在**后端线程**里，刷新不会杀掉它，但前端要能**重新接上**。
   实现：`localStorage['msh.task.active.id']` 存 job id，`resume()` 启动时先问 `/api/busy`
   （以它为准，localStorage 可能过期），再退回按 id 调 `/api/jobs/{jid}`。

对外 API：`ME`（每页面实例 id）、`begin(kind)`、`attach(job)`、`update(job)`、`finish(job)`、
`resume()`、`onChange(fn)`、`startPolling(ms)`、`kindName(k)`、`snapshot()`、`busy`、
`backgroundJob`。

`snapshot()` 返回 `{me, busy, job, mine, others}`——`others` 才是"要禁用本页写操作"的依据
（"自己这边忙不算"）。`finish()` 里会 `API.jobAck(jid)`：docstring 明确这是"一次只允许一个流程"
的收尾点——"不堆积任务，因为堆积的任务会基于**已经变化**的文件状态执行"。

`startPolling` 还额外做了一件容易被忽略的事：`/api/busy` 对**非独占**长任务（`compare`/
`duplicates`/`hash`）恒为 false，于是对比跑 40 秒时顶栏一片安静、用户以为卡死又点一次。
所以它单独走 `/api/jobs` 找出这类只读长任务，只用于**显示**（`backgroundJob`），
**不参与 `begin()` 的占用判断**——"只读任务本来就不该挡写操作"。

前端的最后一道防线是 `app.js` 里挂在 document **捕获阶段**的点击拦截：

```javascript
document.addEventListener('click', ev => {
  const t = ev.target.closest('[data-exclusive]');
  if (!t) return;
  if (!TaskSync.snapshot().others) return;
  ev.preventDefault(); ev.stopPropagation(); …
  UI.toast('另一个页面正在执行…请等它结束后再操作');
}, true);
```

注释解释了为什么光靠 `paintBusyUi()` 不够：它只在"进入忙状态那一刻"禁用**当时存在**的按钮，
而弹窗里的按钮是之后才插进 DOM 的、文件视图的候选条每次选中变化都会重建（按钮全是新的）。
"挂在捕获阶段：不管按钮什么时候出现、由谁创建，只要标了 `data-exclusive`，忙的时候就吃掉这次点击"。

### 14.7 播放器：`playTrack` / `playList` / `queue` / `stepTrack`

* `playTrack(t, {label, _fromQueue})`：
  1. `queue.prune()` 剪掉失效条目（老 bug：播完后 `next()` 一路跳过不存在的曲目，看起来像"播放器没反应"）；
  2. **保证"正在播的这首"一定在队列里**，规则三档（可预期、不毁用户手排队列）：
     已在队列 → 只移动 `queue.index`；不在且队列非空 → 队列换成**只这一首**；
     队列为空 → 补成只这一首；
  3. `audio.src = t.stream_url || '/api/preview/' + t.id`（流地址来自 `_track_view`），
     `play()` 失败 `UI.toast('播放失败: …')`；
  4. 更新 `#np-title`/`#np-sub`/`#np-cover`/`#stream-badge`，再 `paintNowPlaying()`。

  `_fromQueue` 是内部参数：`playList`/上一首下一首/队列弹窗点选都已经安排好队列，
  不要再让这里重排。

* `playList(tracks, startId, label)`：`queue.set()` → `queue.current` → `playTrack(..., _fromQueue:true)`。
  找不到曲目时**必须明确报出来**（`console.warn` 全量上下文 + `UI.toast('这一组里没有可播放的曲目')`）：
  "之前这里静默 return，表现就是'专辑点了播放但什么都没发生'，极难查"。
* `stepTrack(dir, {quiet})`：队列为空时**把当前列表装进队列**再走（原来直接 `playTrack(t)`，
  于是每按一次"下一首"都重新退回列表算一遍，队列永远是空的）；到头时 `quiet` 场景不打扰用户。
* 队列对象（`app.js` 内 `queue`）：`items[{track,label}]`、`index`、`set/step/prune/current`。

### 14.8 图标 sprite 机制

`index.html` 顶部一个 `<svg style="display:none" aria-hidden="true">`，里面是若干
`<symbol id="i-xxx" viewBox="0 0 24 24">`。用法：

```html
<svg class="ico"><use href="#i-play"></use></svg>        <!-- 细线描边 -->
<svg class="ico fill"><use href="#i-prev"></use></svg>   <!-- 实心 -->
```

* 描边宽度/圆头圆角规则**统一写在 `app.css` 的 `.ico` 上**，而不是每个 symbol 里；
* 风格对标 Windows 10 1903 的 Segoe MDL2 Assets（1.5px 描边）；
* **可核对的小瑕疵**：`id="i-pause"` 在这个 sprite 里被定义了两次（细线版与实心版）。
  按浏览器 `getElementById` 取首个匹配的规则，后一个定义不会生效——
  即"暂停"按钮的实心样式可能没生效（**未在浏览器实测**，仅按 DOM 行为推断）。

### 14.9 候选区 `Staging`（跨目录挑歌）

`staging.js`（666 行）解决的问题：想在多个文件夹里各挑几首歌一起处理时，以前每换一个目录
选择就被清空。

* **持久化**：`Prefs.set('stagingItems', slim)`，`save()` 用 400 ms 防抖合并多次变更；
  `restore()` 重建时按 `did` 去重并按存下来的顺序重建颜色槽（保证刷新前后同一去向颜色一致）。
  只存必要字段（`cover_url` 能按 id 重建，不占地方）。**写失败静默降级成不持久化**，不影响使用。
  docstring 的定位很清楚："只放'待处理清单'这种界面状态，不放任何业务真相"。
* **颜色分组**：7 种高对比色 + 1 个"待定"警示色（`#ff6b6b`，不占颜色槽位、不参与并发）。
  去向相同的文件自动同色；`slots` 是"先到先得、删掉之后别人不重排"（否则颜色会乱跳）。
  颜色只有 7 种但**分组数量不设上限**——超了循环复用颜色（早期版本"最多 7 个去向、多了拦住不让操作"
  被判定为"把颜色的限制强加给了功能"）。
* **过期项检查**（`checkStale()`）：按数据源分组、每 4000 条一批调 `/api/files/exists`
  （后端上限 5000，不分批会整批 400，导致所有条目被标成"无法确认"，看着像数据源挂了）。
  只**提示**、不自动移除；数据源离线时把条目标成 `unknown` 而不是 `stale`——
  "那会让用户以为文件没了，标成'未知'更诚实"。
* **音频判定**：`isAudioName()` 从 `State.system.audio_exts` 取清单（后端 `/api/system` 提供），
  拿不到清单时**放行**（"宁可漏挡也不能把用户的音频误挡在候选区外面"）。
* **智能分配**：`smartAssign(dids)` 调 `/api/organize/plan-paths`（只为选中的文件算去向，
  但目录画像读全量）。

### 14.10 请求层 `API`（`api.js`）

* `req(path, {method, body, raw})`：`fetch` + JSON 编解码；非 2xx 时抛出
  `Error(detail || message || statusText)`，并把 `status` 与 `payload` 挂在错误对象上
  （视图据此区分 409/400）。
* `uploadReqProgress()`：用 **XMLHttpRequest** 而不是 fetch，因为 fetch 没有上传进度事件。
  注释："用户一次拖几十个文件进来，界面要是一点反应都没有，就会以为卡死然后强杀进程——
  那才是最糟的结果（还会留下半截的暂存批次）"。
* **统一注入 `client`**：`execute: (b) => req('/api/execute', {body: {...b, client: TaskSync.ME}})`
  —— 后端拿它当任务 `owner`，跨页面靠 owner 判断"这个任务是不是我发起的"。注释记录了事故：
  "之前每个调用点都要自己记得传——结果全都没传，owner 为空串，于是'别人在跑'恒为真、
  本页按钮被自己置灰"。所以放在这一层就不可能漏（`views.ops.js` 的重试与恢复另外显式传）。
* 方法表覆盖本文 13 章的全部只读/写端点（`system`/`sources`/`library`/`files*`/`folders*`/
  `station*`/`upload*`/`tags*`/`organize*`/`duplicates*`/`jobs*`/`settings`/`logs`/`state`）。

---

## 15. 界面契约测试

前端没有测试框架（无 node 工程），所以"界面契约"靠两个 Python 静态检查脚本 + 两个 CDP 脚本。

### 15.1 `tools/check_frontend.py`（84 行，"能加载吗"）

按可靠性从低到高做三件事：

1. **`index.html` 引用的静态资源是否都存在**：用正则 `/static/(?:js|css)/([\w.]+)\?v=\d+`
   抓引用，逐个 `exists()`。少一个文件就是白屏。
2. **括号配平粗筛**：统计每个 JS 的 `()`/`{}`/`[]` 差值。`{}`/`[]` 不配平一定是结构问题（记失败）；
   单独的 `()` 不配平只标"疑似"（会被中文文案里的半角括号干扰）。
3. **`node --check` 真语法检查**：先找 DSH 自带的 node（`…\DeepSeek Harness\resources` 下
   `rglob("node.exe")`），找不到再 `shutil.which("node")`。注释解释了为什么必须用它：
   "曾经有一次模板字符串提前闭合，括号数照样配平，但整个模块加载失败（`Organizer is undefined`），
   靠肉眼根本发现不了"。
4. 附带检查 `app.css` 的 `{}` 配平。
5. 退出码：有问题 `sys.exit(1)`。

### 15.2 `tools/audit_frontend.py`（197 行，"引用对得上吗"）

抓的是"静默坏掉"的七类问题（界面上点了没反应，控制台却不一定报错）：

| # | 检查 | 判定方式 |
|---|---|---|
| 1 | 同一函数内重复 `id` | 按 `\n  (async )?function name` 切块，块内相同 id 且**标签写法完全一样**才算抄漏（互斥分支里同 id 是正常的）；`DYNAMIC_IDS` 白名单排除 |
| 2 | `API.xxx(...)` 是否都有定义 | 从 `api.js` 抽 `^    name: (` / `^    name: function`，与所有 `API.xxx(` 做差集 |
| 3 | `App.xxx` 是否在导出里 | 从 `app.js` 的 `return {…}`/`window.App = {…}` 抽名字；有 `if (App.x)`/`App.x &&`/`App.x ?` 守卫的算安全 |
| 4 | `Staging` / `UI` / `U` 的导出 | `_brace_block()` 手写配对（不能用 `\{([^}]*)\}`——导出对象里有 getter `get items() {…}`，非贪婪匹配会在 getter 的 `}` 处提前截断） |
| 5 | `querySelector('#id')` / `getElementById('id')` 的目标是否存在 | 把 JS 模板、`index.html` 里出现过的 id 收集成全集做差集 |
| 6 | 勾选框写死 `checked` | 在 `paintTreeFiles`/`paintList` 里找 `data-check="…" checked>`（取消勾选后重绘又被勾上） |
| 7 | 半选 `indeterminate` 是否都设了 | `views.files.js` ≥2 处、`staging.js` ≥1 处 |

注意 `audit_frontend.py` 里 `ROOT = Path(r"C:\Users\Administrator\Desktop\MusicSyncHub")`
是**硬编码绝对路径**，换机器/换目录必须改，否则脚本直接读不到文件（见 19 章）。

### 15.3 CDP 工具：为什么需要

`window.resizeTo` 对普通标签页无效，要真的改视口必须走 CDP 的
`Emulation.setDeviceMetricsOverride`。而布局问题（尤其是"窗口又矮又宽"或小屏幕）
只有**真的把视口改掉**才测得出来——"在 1600×900 的窗口里量 `innerWidth` 永远得到 1600，
看起来'没问题'"（`cdp_vp.js` 文件头）。

| 脚本 | 作用 | 用法 |
|---|---|---|
| `tools/cdp_eval.js`（90 行） | 在**真实页面**上执行一段 JS 并把结果带回来（含 `Runtime.exceptionThrown` 与 console 的 error/warning） | `node cdp_eval.js <port> <url> <waitMs> <js文件>` |
| `tools/cdp_vp.js`（109 行） | 在真实页面上按**多个视口尺寸**跑探针并可选截图 | `node cdp_vp.js <port> <url> <waitMs> <js文件> <宽,宽> [截图目录] [高,高]` |
| `cdp_check.js` / `cdp_live.js` / `cdp_probe.js` / `cdp_break.js` | 同族的其它探针脚本（**未逐个阅读**，用途未验证） | — |

`cdp_eval.js` 比"自建 HTML 探针"可靠的地方（文件头原话）："测的就是用户真正打开的那个页面，
不会因为'探针页少了某个元素/脚本'而得出假结论"。

### 15.4 `Emulation.setDeviceMetricsOverride` 的坑（`cdp_vp.js` 注释原文提炼）

顺序**必须**是：**先导航 → 再设尺寸 → 最后跑探针**。两个坑叠在一起，最容易得出"没问题"的错误结论：

1. **只设尺寸不导航**：页面仍按旧视口布局，`innerHeight` 不变（实测设了 `height=420` 结果还是 900）；
2. **导航会清掉尺寸覆盖**：所以"设完再导航"也白设。

实现细节：每轮循环都 `Page.navigate` → `sleep(WAIT_MS)` → `setDeviceMetricsOverride` →
`sleep(1500)` 让浏览器按新尺寸重排一次 → `Runtime.evaluate(awaitPromise, returnByValue)` →
可选 `Page.captureScreenshot`；全部跑完 `Emulation.clearDeviceMetricsOverride`。
宽高列表**按位置配对**，只给一个高度就全部用它。

> 顺带一个可核对的不一致：`cdp_eval.js` 是在 `Page.navigate` **之前**设 1600×950 覆盖的，
> 按上面这条经验它可能并不生效（**未实测**）。

---

## 16. 测试体系

### 16.1 测试文件清单（29 个 `test_*.py`）

| 文件 | 覆盖范围（依据各自模块 docstring） |
|---|---|
| `test_core.py` | 核心算法：路径归一化、`name_key`、相似度、配对、查重分组、计划生成、执行器 copy/trash/只读门禁、`quick_hash` 区分度、扫描入库 |
| `test_audit_fixes.py` | 一轮全面审计发现缺陷的回归：guard 独立模块、目标占位、专辑模式父艺术家、备份命名、设置校验、`_safe_name`、超时、心跳、前端 toast/队列/`Prefs` 契约 |
| `test_dedup_rules.py` | 查重规则的**对抗性**回归：版本变体不合并、时长门禁、多偏移分桶、`dup_key` 的 `bucket_step`、计划指纹 |
| `test_duplicates.py` | 查重分组：不允许把不同的歌并成一组（版本标记、艺术家冲突、编号专辑、词序差异） |
| `test_duplicates_api.py` | 查重接口响应契约：组身份字段一致性、items 上的 `version_tags` |
| `test_device_usb.py` | 外置存储识别与优化：`detect`/`suggested_defaults`/`effective_workers`/`device_notes`/只读默认 |
| `test_files_api.py` | 文件浏览接口：中间目录计数、深层累加、facets、select、move 的 confirm 与 dry-run |
| `test_files_tree_ui.py` | 目录树多选与布局的**前端源码契约**（真 checkbox、高度/宽度约束、不自己滚动） |
| `test_folder_ops.py` | 文件夹新建/改名/移动/删除、越界拒绝、空目录清理、`dissolve` 已移除、映射持久化 |
| `test_job_lifecycle.py` | 任务体系不变量：init 幂等、终态、独占位释放、暂停仍占位、只读任务不占位、`exclusive` 字段 |
| `test_library_aggregate.py` | 曲库统计口径：整库/前缀/筛选/分页参数不串到聚合 |
| `test_library_fuzzy.py` | 曲库模糊搜索：标题/作者/错别字/缩写/不误命中散字母/与其它筛选共存/分页一致 |
| `test_library_paging.py` | 分页无重复无缺口、升降序、模糊+分页、越界 offset、首页元数据完整 |
| `test_mappings_tags_api.py` | 映射规则读写往返/重载存活/无残留临时文件；标签预览不写、写入有备份、还原、WAV/FLAC 不被写坏 |
| `test_move.py` | 执行器 `move` 步骤 + `/api/files/move` 接口 |
| `test_non_audio.py` | 非音乐文件处理（含"禁止在 Python 侧再抄一份音频清单"这条） |
| `test_organize_album.py` | `plan_by_album` 的各种分组/结构/收纳区/自定义映射组合 |
| `test_organize_analyze.py` | `/api/organize/analyze`：搜索 + 结合现有文件夹给候选与证据 |
| `test_organize_pertrack.py` | "智能整理"组内单曲勾选（`only`）的行为 |
| `test_pause_recover.py` | 暂停/取消、崩溃恢复、自动刷新、配额语义、后台任务、设置校验、超时 |
| `test_platform_features.py` | 多页面/任务互斥/后台任务/偏好记忆/播放器联动/磁盘余量 的契约 |
| `test_player_queue.py` | 播放器队列的**形状契约**（`playTrack` 不改队列、`stepTrack` 装队列等） |
| `test_resilience.py` | 执行容错：错误分类、可重试清单、批量全选 |
| `test_search_organize.py` | 模糊搜索与智能整理 |
| `test_staging_areas.py` | 上传暂存区 / 中转站两个区域的边界与可管理性（非音频隔离、配额、清理） |
| `test_tags.py` | 标签读写 |
| `test_tag_safety.py` | 标签写入的安全性与格式覆盖（原子写、备份、不损坏文件） |
| `test_transfer.py` | 跨库传输（`transfer_one` 的各失败路径、standard/fast、心跳） |
| `test_upload_dup.py` | 入库查重 |

非测试文件：

* `tests/__init__.py`（53 行）——**数据目录隔离的安全网**，见 4.2；
* `tests/srchelp.py`（69 行）——两个断言辅助（见 16.3）；
* `tests/e2e_check.py`（318 行）——**端到端联调脚本**（直接打真实 HTTP 接口），
  不是 unittest 用例；`README.md` 声称"71 项检查"（未逐条核对）；
* `tests/make_sample_libs.py`（93 行）——生成"结果唯一"的样例库（真实 WAV，可被 mutagen 解析）。
  `scripts/dev_reset.sh` 会校验生成结果为 `win=10 / linux=7` 首。

### 16.2 总数与运行方式

* 用 `grep -c "def test_"` 统计：**723 个 `def test_*`**（含 `unittest.TestCase` 方法与模块级测试函数）。
* 框架：标准库 `unittest`（`unittest.TestCase` + `assertEqual/assertIn`），HTTP 层用
  `fastapi.testclient.TestClient`（`test_*_api.py` 系列），没有 pytest 依赖。
* 运行命令（`README.md` 第十节原文）：

```bash
uv run python -m unittest discover -s tests -t . -v
```

  `-t .` 把顶层目录设为仓库根，这样测试内 `import app.…` 才成立；
  `config._in_test_run()` 的判断依据正是"`sys.modules` 里有 `tests` 包"。

* 端到端：

```bash
bash scripts/dev_reset.sh 8791
uv run python tests/e2e_check.py --base http://127.0.0.1:8791
```

* 静态自检（改前端后必跑）：`python tools/check_frontend.py`、`python tools/audit_frontend.py`。
* 接口冒烟（**对着真实运行的服务**）：`python tools/smoke_api.py`。

### 16.3 `tests/srchelp.py`：`fn_body` 为什么存在

项目里有大量"断言源码内容"的测试（前端契约、声明式检查）。早期写法是"从函数开头切 N 个字符"，
例如 `src[i:i + 2600]`——**天生脆弱**：函数里加几行，被断言的字符串就滑出窗口，测试莫名其妙地红。
注释记录的具体案例：给 `upload_batches` 加了"非音频单独列出"的逻辑之后，
`test_lists_stray_files_in_root` 就失败了，而代码完全正确。

`fn_body(src, name)` 改为**按缩进取完整函数体**，与行数无关。支持三种写法：

```
def name(...)            # Python
function name(...)       # 老式 JS
const name = (...) => {  # 本项目大量使用的箭头函数
```

结束判定：下一个"同级或更浅缩进"的 `def`/`class`/`@`/`function`/`const NAME =` 即为结束；
`len(cur) == len(indent)` 时还要排除收尾括号与续行（`st.startswith((")", "]", "}", ".", "+", "?"))`）。
另有 `around(src, needle, before=300, after=600)`：取 `needle` 前后一段，用于"附近有某内容"的断言。

### 16.4 `tools/smoke_api.py` 的做法与"设置快照恢复"的意义

* **90 条用例**（`CASES` 列表），每条是 `(路径, 方法, 体, 期望状态码集合, 说明)`；
  只做**只读或幂等**调用，写操作一律故意带错参数，确认校验生效而**不真改数据**。
* 覆盖范围能从用例名看出来：系统/数据源/曲库分页与模糊/文件列表（含 `../../etc` 越界）/目录树/
  空目录清理/中转站全部动作的"空参 400"/映射读写/搜索/查重（同步与后台两种）/对比非法参数/
  任务列表与不存在任务/执行器空步骤与未确认/计划/上传批次/整理全套（含 `wait=true` 与后台两种）/
  盘列表与探测/试听与封面（不存在的曲目）/`files/exists` 与 `files/transfer` 的守卫/
  设置校验（越界、NaN、非法字符、0=不限额）/标签读写/映射单条。
* **"设置快照恢复"的意义**（这是整个脚本最重要的一段注释）：脚本跑的是**真实服务 + 真实配置**，
  所以任何成功的写调用都必须还原。踩过的事故：其中一条 `PUT /api/settings {'station_max_gb': 0}`
  用来验证"0 = 不限额"是合法的——它确实返回 200，于是**真的把用户 config.toml 里的中转站容量上限
  改成了 0（不限额）**，用户的 20 GB 保护就这么被一个"只读冒烟测试"悄悄关掉了，而且没有任何提示。
  现在脚本开头先 `GET /api/settings` 拍快照（打印"已记录设置快照，共 N 项"），跑完再 `PUT` 回去，
  并**逐项比对**：不一致就 `bad.append(('设置未能恢复', …))` 并以失败退出。
  拿不到快照时会打印"★ 拿不到设置快照——本次若有用例改设置将无法恢复"。

---

## 17. 日志与可观测性

### 17.1 `core/applog.py`

| 项 | 值/行为 |
|---|---|
| 文件 | `<data_dir>/logs/msh.log`，`RotatingFileHandler(maxBytes=4 MB, backupCount=3, encoding="utf-8")` |
| 内存缓冲 | `deque(maxlen=MEMORY_LINES=500)`，由 `_RingHandler` 追加 `{t, level, name, msg[, exc]}` |
| 格式 | `%(asctime)s %(levelname)-7s [%(name)s] %(message)s`，`datefmt="%Y-%m-%d %H:%M:%S"` |
| 幂等 | `setup()` 可重复调用：先清掉自己上次装的 handler（标记 `_msh = True`） |
| 范围 | 只作用于 `logging.getLogger("msh")` 这棵树，并显式把 `msh.jobs/msh.ops/msh.transfer/msh.scanner` 的 level 设好、`propagate=True`；**不动 uvicorn/httpx**（太吵） |
| 失败策略 | 所有 handler 都吞掉自身错误——"日志本身出问题（目录不可写、磁盘满）绝不能影响主流程" |
| API | `setup(data_dir, level) -> path\|None`、`log_path()`、`recent(limit, level)`、`read_file(lines, level)`、`clear()` |

两个**设计上的克制**（文件头）：① **不动请求处理路径**：只注册 handler、不加中间件——
"每个请求都写一条日志的话，日志会被刷屏，真正有用的那几行反而被埋掉"，
所以只记"错误 + 我们自己主动记的关键事件"；② **不抛异常**。

`read_file()` 用 `deque(fh, maxlen=n)` 只保留文件尾部 n 行（`lines` 夹在 1–5000），
`level` 过滤靠 `" LEVEL "` / `" LEVEL   "` 两种宽度匹配（对齐 `%(levelname)-7s`）。

### 17.2 HTTP 侧的全局异常处理器（`main.py`，注册顺序有意义）

| 处理器 | 状态码 | 为什么 |
|---|---|---|
| `SourceNotFound` | **404** | `resolve_source` 原来抛裸 `KeyError`，FastAPI 不认识 → 任何带错 id 的请求都变 500；更实际的问题是很多端点是**先占独占位再解析数据源**，异常一冒独占位就泄漏了 |
| `ReadOnlyError` | **403** | 必须在 `SourceError` **之前**注册，否则被父类的 400 抢走——"只读是权限不够，不是参数不对"，分开报用户才知道该去取消只读勾选 |
| `SourceError` | **400** | 这一类都是"你给的路径/参数不对"或"源现在连不上"（路径带空字符、目录不可达…） |
| `JobBusy` | **409** | 以前没有任何处理器，谁没包 try 谁就冒成 500；实测踩到的是 `/api/execute` 的**预演**路径 |
| `Exception` | **500** | 带完整堆栈写进 `logs/msh.log`（`logging.getLogger("msh").error("未处理异常 %s %s → …\n%s")`），但**回给用户的消息不暴露堆栈**（可能有路径等隐私），只告诉他去哪看日志 |

### 17.3 谁会往日志里写

| 位置 | 内容 |
|---|---|
| `main.lifespan` | 启动信息（含日志文件路径）、"发现 N 个上次没做完的任务"、恢复失败告警 |
| `main._unhandled` | 未处理异常（method + path + 类型 + 消息 + traceback） |
| `bgjob.run_in_background` | 后台任务失败的**完整堆栈**，并把前 4000 字放进 `job.result["traceback"]` |
| `jobs._reap_stale_locked` | 陈旧任务落库失败（`log.warning`，含任务 id 与异常类型） |
| `main.clear_logs` | "内存日志缓冲已清空" |

`bgjob` 的注释解释了为什么必须入日志："以前只塞进 `job.error`——用户看到的是界面上'任务失败'，
但'为什么失败'（堆栈）只在那一刻的内存里，关掉页面就再也找不回来了。"

### 17.4 可观测性接口

* `GET /api/logs`（内存 500 条 / 或文件尾部）、`POST /api/logs/clear`；
* `GET /api/jobs`、`GET /api/jobs/{jid}`、`GET /api/busy`、`GET /api/unfinished`
  ——任务级进度（`percent`/`elapsed`/`rate`/`eta`/`idle`/`stale`/`skipped_count`）；
* `GET /api/state`——"数据变了没有"的廉价信号，前端每 4 秒比一次；
* `GET /api/sources/status`——每个源的可达性与延迟；
* `GET /api/system`——`db_size`（含 WAL/Shm）、各源曲目数、运行中任务、能力位。

---

## 18. 安全不变量

下表每一条都是**代码强制**的，不是约定俗成。列"实现位置"的地方可以直接对照审。

| # | 不变量 | 实现位置 | 备注 |
|---|---|---|---|
| 1 | **删除默认进回收站** | `sources/local.py::remove`（`hard=False` → `_trash_dir/YYYYMMDD/`，重名加 epoch 前缀）、`webdav/smb/sftp/ftp.remove` 同约定、`local.trash_dir()` 移整个目录（名字带时间戳 + `(2)` 去重） | 回收站路径 `<root>/.music-sync-trash/<YYYYMMDD>/`；可用 `settings.trash_dir` 覆盖（`local._trash_dir` 会再拼上 `cfg.id`） |
| 2 | **永久删除必须显式要求** | 执行器：`hard_delete`/`op=="delete"`；目录删除：`hard=true` **且** `confirm_hard=true`；`transfer_one`: `hard_delete` | `_do_remove(hard = hard_delete or op == "delete")` |
| 3 | **写入先落临时文件再改名（原子写）** | `base.copy_from`（`<dst>.mshpart` → `replace`）、`local.copy_from`、`ftp.write_file`（`ap + ".mshpart"`）、`local.move` 的跨盘分支（`dst + ".mshpart"`）；因此**所有源**的跨源复制都继承同一套原子语义 | 中断只会留下 `.mshpart`，不会被当成成品 |
| 4 | **绝不覆盖** | 默认 `policy="skip"`；`_dedupe_name()`（`a (2).mp3`）；`station._safe_name()` / `upload._safe_name()`（含 Windows 保留设备名 `CON/PRN/AUX/NUL/COM1-9/LPT1-9` 防护）；`tags` 备份不覆盖同名备份；`local.rename_dir` 目标是"空目录或不存在"才允许 | 设备名的注释："写到这些名字上数据会被设备吞掉（得到 0 字节文件），而 `os.replace` 到它们会**永久挂起**" |
| 5 | **搬运必须校验，没验过不许删源** | `transfer_one`（copy → 比对 size → 可选 hash → 才 `src.remove`）；size 不一致时**删掉目标那份**并保留源 | `verify=None` 跟 mode；显式 `True/False` 盖过 mode |
| 6 | **移动后校验** | `ops._do_move`：`src.move()` 后重新 `stat`，不存在或 size 不一致 → `state="error"`（"绝不假装成功"）；`local.move` 跨盘时"copyfile → 比 size → replace → 删源" | — |
| 7 | **删除源失败不回滚目标** | `transfer_one` 第③步：只报"两边都有，请手动删源那份" | 回滚会把好的目标删掉 |
| 8 | **越界拒绝（路径必须落在数据源根内）** | `ops._inside_root` + `_resolve_move`（`..` 段、非绝对路径、Windows `casefold`）；`folders_api._inside`/`_resolve`（拒 `..`、拒绝对越界、拒根目录本身作为被操作对象）；`search_api._abs_in_root`（**先查 `..` 再拼接**）；`tags_api._resolve`；`station_api._inside_station`（用 `os.path.commonpath` 而不是 `startswith`） | `_inside_station` 的注释：`startswith` 会被同名目录骗过（`station-evil` 也以 `station` 开头） |
| 9 | **只读门禁三重** | ① `MusicSource.can_write/can_delete` + `_guard_write()`（抛 `ReadOnlyError`）；② `main.require_writable(*sids)`：**全部**相关源都只读时直接 403（否则会返回"看着成功"的 200 而 `results[].state` 全是 `blocked`）；③ `ReadOnlyError` → 403 全局处理器 | 只在"全部都不可写"时拒绝；一写一读仍交给执行器逐步骤处理（信息更全） |
| 10 | **目标占位（同一目标只能有一个写入者）** | `Executor._reserve_targets` + `_claim_target`（键 = `(目标源 id, rel_path.lower())`） | 覆盖 op：`move/copy/rename/extract/transfer` |
| 11 | **写流程严格串行（不排队）** | `EXCLUSIVE_KINDS`（12 个）+ `Jobs.create` 抛 `JobBusy` + `guard.exclusive_guard`（409）+ `main` 的 `JobBusy` 全局处理器（409） | 暂停中的任务仍占位 |
| 12 | **执行写操作必须二次确认** | `execute=true` 且无 `confirm` → 400：`/api/execute`、`/api/files/transfer`、`/api/files/move`、`/api/collect/apply`、`/api/organize/apply`、`/api/organize/album-apply`、`/api/organize/folder`、`/api/upload/commit`、`/api/upload/cleanup(mode=all)`、`/api/tags/write`、`/api/tags/restore`、`/api/station/remove`、`/api/folders/delete(hard)`、`/api/folders/empty-remove` | — |
| 13 | **默认预演** | `Executor.run(execute=False)` 是默认；`/api/plan`、`/api/compare`、`/api/organize/plan*`、`/api/collect/preview` 只算不动 | 预演不占独占位、也不写"可恢复步骤" |
| 14 | **改标签前先备份 + 原子写 + 回读校验** | `core/tags.py`：`_save_backup()` 存 JSON、写临时文件→重新解析确认→`os.replace`；`/api/tags/restore` 可一键还原 | 注释："标签写错不像文件删了能进回收站，没有备份就只能靠记忆手工敲回来" |
| 15 | **单步卡死不能拖死整批** | `ops.call_with_timeout` + `_apply_guarded`（`STEP_TIMEOUT_SEC` 默认 120 s，`MSH_STEP_TIMEOUT_SEC` 可覆盖）；`folders_api._guarded`、`tags._guarded`、`station._moved/_copied`、`upload` 的删除都套了超时 | 超时只放弃等待，把该步标成 `timeout` 并继续 |
| 16 | **长复制必须打心跳** | `transfer._Heartbeat`（20 s 间隔，下限 5 s）；`ops._do_copy` / `_do_transfer` / `transfer_one` 都包住 | 否则超过 `STALE_JOB_SECONDS` 会被判中断并释放独占位，而复制还在跑 |
| 17 | **跨库传输只接音频** | `main.files_transfer`：`_non_audio = [x for x in items if not P.is_audio(...)]` → 400 并列出前 5 个 | — |
| 18 | **归位只接音频** | `station_api.station_move_out`：`if not P.is_audio(srcp): failed.append(…已跳过)` | 注释："列表那边已经过滤，但客户端可以直接构造 files 绕过去" |
| 19 | **计划指纹防"执行了没看过的方案"** | `search_api._plan_hash`（含 `size`）+ apply 时比对 `plan_hash`，不一致 → **409**"方案已变化，请重新生成后再执行" | — |
| 20 | **索引动过但没同步必须报告** | `Executor._index_errors` → `summary["index_errors"]` + `out["index_errors"]` + warning | 不允许 `except: pass` |
| 21 | **配置写回不产生坏 TOML** | `config._toml_val` 转义控制字符；`_write_text` 走 `.tmp` + `os.replace` | — |
| 22 | **设置值必须过校验** | `config.validate_setting`（范围/NaN/长度/控制字符/路径非法字符/枚举）；`PUT /api/settings` 部分失败会明确报 `errors` | "宁可现在拒绝" |
| 23 | **测试绝不碰真实数据目录** | `tests/__init__.py`（mkdtemp + 哨兵路径 + atexit 清理）+ `config.data_dir()` 的测试兜底 + `mappings_api._data_dir()`/`tags._backup_dir()`/`upload_api._uploads_root()` 统一走 `data_dir()` | 见 4.2 的事故记录 |
| 24 | **跨库传输不能同源** | `transfer_one` 与 `/api/files/transfer` 都判 `src == dst` → 400/error；`_resolve_move` 反向要求 `src == dst` | 同源移动走 `_do_move`（原子改名，更快更安全） |
| 25 | **慢盘/可移除设备降低并发** | `device.effective_workers` + `Executor.run` 入口；`start_scan` 的 `slow_disk` 分支 | `slow_disk` 只是"建议"，用户显式值优先 |

---

## 19. 已知限制与未完成项

**只列能从代码或文档直接确证的事实。**

### 19.1 功能边界

1. **所有网络数据源都未在真实服务端验证过**（sftp/webdav/http/smb/ftp 全部 `experimental=True`，
   各自的 `verify_note` 明确写了"没有连过任何真实…服务器"）。界面会给它们加"（实验性）"后缀。
2. **不支持 MTP / 便携设备（手机、相机）**：`device.list_portable_devices()` 只把设备**列出来**
   并给出 `supported=False` 与替代方案；理由是 MTP 需要 Windows Portable Devices 的 COM 接口或
   libmtp，与本项目"只依赖标准库 + 少数纯 Python 包"的部署目标冲突。
3. **只有本地源能改标签**：`tags_api._resolve()` 对非 `local` 源直接 400
   （"远端源需要先下载整个文件再传回，风险太大，没有实现"）。
4. **跨库传输只支持单个文件**：`transfer_one` 遇到目录直接报"源路径是目录；跨库传输目前只支持单个文件"。
5. **跨库传输只接音频**（见 18 章 #17）；非音频只能在同一库内用「移动到…」整理。
6. **对比/查重有硬上限**：每侧最多 `settings.max_compare_tracks`（默认 8000，最小 100）条，
   超出会 `truncated` 并在 `warnings` 里提示"建议先在侧栏选目录再对比"。
7. **一次传输 ≤ 2000 个文件、一次 exists 查询 ≤ 5000、一次标签写 ≤ 500 个文件、
   一次入库 ≤ 5000 条、步骤 ≤ 20000 条**（各端点自己的上限）。
8. **`/api/folders/dissolve` 已被移除**（恒定 410），"解散文件夹"被"清理空文件夹"取代。

### 19.2 执行器里未实现/半实现的 op

| 现象 | 依据 |
|---|---|
| `link` 实际执行的是**复制**（不建硬链接/符号链接） | `_apply`: `if op in ("copy","link"): _do_copy(...)` |
| `mkdir` **没有分支**，会得到 `state="skipped", detail="未知操作 mkdir"` | `Step.op` 的注释列了 `link`/`mkdir`，但 `_apply` 没有 `mkdir` 分支 |
| `extract` 只出现在目标占位名单里，没有执行分支 | `_reserve_targets` 含 `"extract"`，`_apply` 无对应分支 |

### 19.3 声明但未生效的配置/常量

| 项 | 事实 |
|---|---|
| `Settings.cover_cache_size` | `main._cover_cache` 用的是硬编码"超过 2000 条整体 `clear()`"，**不读这个设置** |
| `matching.NAME_TRUST_LEN = 14`、`FUZZY_NAME_SOFT = 0.72`、`ARTIST_VETO_FLOOR = 0.25`、`DUP_MERGE_NAME_MIN = 0.62` | 全项目 `grep` 只有定义处，**没有任何引用**（死常量） |
| `plan.build_plan` 里的 `thr = settings.name_similarity` | 读完赋给 `thr` 后**未再使用**；实际阈值来自 `options.match_threshold`（默认 0.62）。所以界面上改"名称相似度"对计划生成**没有影响** |
| `Entry.etag` | `scanner._stale()` 只用 `size`/`mtime`，**没有用 etag** |
| `plan._root_placeholder()` | 空壳函数，`return ""`，无调用点 |
| `JobState.error` 与 `job` 表 | `job` 表**没有 `error` 列**，所以中断原因只能写进 `message`（见 12.9） |

### 19.4 一致性 / 死代码 / 小瑕疵

1. **`group_to_dict` 的 `version_tags` 与 `dup_key` 字段集不一致**：
   `dup_key` 用 `name + title + rel_path`，而 `group_to_dict` 末尾算 `version_tags` 时只传了
   `name + title`（`matching.group_to_dict` 最后一行）。当版本标记**只在目录名**里时，
   返回值里的 `version_tags` 可能与分组键表达的意思不同（`_version_of` 那条路径已经修过同类问题，
   但这一处显示字段仍是旧写法）。
2. **`device.list_portable_devices()` 之后有一段不可达代码**：该函数在第
   `return out`（`if os.name != "nt"` 分支之后）结束，其后紧跟着一份旧版 `list_drives()` 的函数体
   副本（`for letter in string.ascii_uppercase: …`），永远执行不到；真正的 `list_drives()` 在其后
   重新定义了一次。
3. **`index.html` 里 `id="i-pause"` 定义了两次**（细线版 + 实心版），按 DOM 规则后一个多半不生效
   （未在浏览器实测）。
4. **`tools/audit_frontend.py` 硬编码了绝对路径** `C:\Users\Administrator\Desktop\MusicSyncHub`，
   换目录/换机器必须改；`check_frontend.py` 则偏好 DSH 自带的 `node.exe`，找不到才用 PATH 里的。
5. **`main.py` 中的 `/api/state` 依赖 WAL 文件的 mtime** 作为"索引刚被写过"的信号；
   `journal_mode` 若被改成非 WAL，这个信号会退化（`mtime` 恒为 0）。
6. **没有 `SAFETY.md`，也没有 `AUDIT_LEDGER.md`**（已 glob 全树确认）。因此项目里的"历史教训"
   只存在于**代码注释**中——本文第 20 章就是从这些注释提炼的。
7. **任务书里提到的"不要给基础规则加 transform 过渡"这一条，在现有代码/注释里没有找到对应记载**，
   无法确证，故不作为开发约定列出（标注：未验证）。

### 19.5 本文档未覆盖的部分

* `core/organize.py`（1787 行）的规则引擎只做了概述（收纳区识别、放错判定、`plan_by_album`
  的入参契约），没有逐函数展开；
* `core/audio.py` 的格式细节（各容器标签位置、封面安放规则）只引用了结论；
* `tools/cdp_check.js`、`cdp_live.js`、`cdp_probe.js`、`cdp_break.js` 未阅读；
* `start.sh` 与 `启动 Music Sync Hub.command` 未阅读；
* `test_*.py` 只按模块 docstring 概述覆盖范围，没有逐用例核对。

---

## 20. 开发约定

以下每一条都对应上文某处代码注释里的**真实事故**。改代码前建议先扫一眼。

### 20.1 唯一真相（不要抄第二份）

* **音频扩展名只有 `core/paths.py` 的 `AUDIO_EXTS`（22 个）与 `is_audio()` 一处**。
  历史：`upload_api.py` 抄了一份 16 个的（少 `.caf/.m4b/.oga/.mpc/.tta/.wv`），
  于是"手动拷进库能被扫到，走上传反而传不上去"；`station_api.py` 抄的那份少了 10 个，
  那些格式在中转站里被当成"非音频"。两处都已删掉本地清单并改注释说明。
  `tests/test_non_audio.py` 有一条测试专门禁止在 Python 侧再抄一份。
* 前端要判断"是不是音频"时，从 `/api/system` 的 `audio_exts` 取（`staging.isAudioName` 就是这么做的），
  不要自己写清单；拿不到清单时**放行**（宁可漏挡）。
* `data_dir()` 只有 `config.data_dir()` 一处。`mappings_api`、`tags._backup_dir()`、
  `upload_api._uploads_root()` 都统一走它；自己拼 `MSH_DATA_DIR or ~/.music-sync-hub`
  会重演"测试写坏真实配置"的坑。
* `version_tags` 的字段集：**调用点必须与 `dup_key` 一致**（`name + title + rel_path`）。
  已有两处因此出现过自相矛盾（`version_mismatch` 与 `group_duplicates._version_of`），
  教训是"同一份语义不要在两处各算一遍，能复用就复用"。

### 20.2 坐标系

* 索引与磁盘操作用**绝对内部路径**（`P.norm` 后的正斜杠形式）；计划/跨源落点用**源内相对路径**。
* 新增任何"写文件"的 op，必须同时：
  1. 把 op 加进 `_reserve_targets` 的名单；
  2. 确保执行端调用 `_claim_target()` 认领最终目标名；
  3. 键空间用 `(目标源 id, P.norm(rel_path).lower())`。
  漏第 1 步 = 同方案内两条步骤互相覆盖且都报成功（静默丢文件）；漏第 2 步 = 报出了
  `dst_conflicts` 但文件还是少一个；键空间不一致 = 改名逻辑形同虚设。
* 相对路径的展开顺序永远是**先展开、再校验**（`_resolve_move` 的 `_expand`）：
  `../x` 展开后仍要落在 root 外被拒——"放开相对路径不等于放开越界检查"。

### 20.3 独占位与心跳

* 任何**会改文件或改索引**的新流程，必须：① kind 进 `EXCLUSIVE_KINDS`，或用
  `create(..., exclusive=True)`；② 长循环里定期 `job.touch()`（只改 `done` 不算"活着"）；
  ③ 单次大 IO 用 `transfer._Heartbeat` 包住。
* 判"有没有人占位"只用 `st.exclusive`，**不要按 kind 重新猜**（`_busy_locked`/`to_dict` 都被这个坑过）。
* 只读长计算用 `bgjob.run_in_background`（`exclusive=False`），**不要**为了让顶栏有提示就把它
  改成独占——那会平白挡住写操作（`TaskSync.backgroundJob` 就是为此加的显示通道）。
* 预演（`execute=False`）**不占独占位、也不 `record_steps`**，但要 `ensure_free()`
  防止照着变动中的状态算方案。

### 20.4 门禁数字要同源

"2 × `duration_tolerance_sec`"这个门禁出现在三处，**必须是同一个数**：

1. `matching.duration_similarity` 的 `gate`；
2. `pair_sources` 阶段 2 的 `_gate2`（以及与之相等的桶距 `_step2`）；
3. `group_duplicates` 的 `_window`（以及 `bucket_w`）。

改动其中一处必须同步另外两处，否则会出现"调容差没用""边界卡在 28 秒""时收时不收"这类怪现象
（第 8 章列了三次真实翻车）。同理：分桶若要覆盖门禁窗口，必须满足 `K × bucket_w ≥ 窗口`
——用多偏移分桶，不要用"整数除法 + 邻居 ±1"去赌边界。

### 20.5 数据库

* **写库一律走 `db.write()`**（它才 `commit()`）。裸 `db.execute()` 只在本连接可见，
  别的连接（也就是界面后续请求）读不到，进程退出还会丢。
* 索引里的路径都是 `P.norm` 形式，查询前也要归一化，否则 `D:\Music\x` 与 `D:/Music/x` 会被当两条记录。
* 新增列必须同时改 `SCHEMA`（新库）与 `_migrate` 的 `wanted`（老库），两处缺一不可。
* 排序要**稳定**：分页查询必须追加 `id` 作为最终决胜键（`/api/library` 的注释：同键行顺序不保证，
  "加载更多"会看到重复或缺失）。

### 20.6 错误处理

* 文件系统异常要经 `classify_error()` 分类，再进 `_tally` 的 `error_kinds`/`retryable_steps`；
  "一锅端地报失败"帮不上忙。
* **不要 `except: pass`**。`_sync_index` 原来就是 `except: pass`，结果是"移动成功但库里对不上，
  而且完全不知道原因"。现在失败的索引同步会进 `summary`/`warnings`。
* 用户可见的错误必须是**能纠正**的错误：`SourceNotFound`→404、`ReadOnlyError`→403、
  `SourceError`→400、`JobBusy`→409；不要让它冒成 500。
* 只读的拒绝要在**接口层**就拒绝（`require_writable`），不要只靠执行器逐步骤标 `blocked`
  ——那会返回一个"看着成功"的 200。

### 20.7 前端

* 新的写操作按钮标 `data-exclusive`（捕获阶段的点击拦截靠它）；调用统一走
  `App.runExclusive(kind, fn)`，并且**只调一次** `TaskSync.finish`（调两次会发两次 ack + 两次跨页广播）。
* 需要 `client`（owner）的请求，优先在 `api.js` 统一注入，**不要靠每个调用点自己记得传**。
* `#toast-root` 必须保留 `class="toast-wrap"`（丢了就 `position:static`，所有反馈跑到首屏之外）。
* 视图通过 `App.registerView(name, {mount, render[, load]})` 注册；跨视图调用走 `App.viewApi(name)`；
  `mount` 只在首次进入时调用（懒挂载），不要假设每次 `setView` 都会 `mount`。
* 改完前端**必须**跑 `python tools/check_frontend.py` 与 `python tools/audit_frontend.py`
  （前者查"能不能加载"，后者查"引用对得上吗"）；`index.html` 里的 `?v=101` 版本戳记得同步。

### 20.8 测试

* 断言源码内容用 `tests/srchelp.fn_body()`（按缩进取完整函数体），**不要**再写"从函数开头切 N 个字符"。
* 测试换数据目录只设 `MSH_DATA_DIR`/`MSH_DB`，**绝不要**自己拼真实路径；
  `tests/__init__.py` 的兜底要保住。
* 换库的测试类要在 `setUp/tearDown` 里考虑 `jobs.reset_for_tests()`（正常使用不需要；见 `jobs._adopt` 的说明）。
* 跑真实服务做检查（如 `tools/smoke_api.py`）时，**任何成功的写调用都必须还原**——
  设置类调用先拍快照、跑完比对恢复。

---

## 附录 A：常用环境变量

| 变量 | 作用 | 读取位置 |
|---|---|---|
| `MSH_DATA_DIR` | 应用数据目录（配置/索引/日志/暂存区） | `config.data_dir()` |
| `MSH_DB` | 覆盖索引库路径 | `main._wanted_db_path()` |
| `MSH_TEST_MODE` | 测试模式（水印 + `/api/system.test_mode`，不影响业务） | `config.test_mode()` |
| `MSH_STALE_JOB_SEC` | 心跳超时（默认 180 s） | `jobs.STALE_JOB_SECONDS` |
| `MSH_STEP_TIMEOUT_SEC` | 单步超时（默认 120 s，`<=0` 关闭） | `ops.STEP_TIMEOUT_SEC` |
| `MSH_HOST` / `MSH_PORT` | CLI 默认值 | `main.main()` 的 argparse |

## 附录 B：关键常量速查

| 常量 | 值 | 位置 |
|---|---|---|
| `AUDIO_EXTS` | 22 个扩展名 | `core/paths.py` |
| `LOSSLESS_EXTS` | 10 个（`.flac/.alac/.ape/.wv/.aiff/.aif/.wav/.dsf/.dff/.tta`） | `core/paths.py` |
| `FULL_READ_LIMIT` / `HEAD_BYTES_DEFAULT` / `HEAD_BYTES_LOSSLESS` | 12 MB / 2 MB / 3 MB | `core/audio.py` |
| `QUICK_FULL_THRESHOLD` | 6 MB | `core/scanner.py` |
| `CACHE_ETAG_TOLERANCE` | 1.0 s | `core/scanner.py` |
| 打分权重 | 0.38 / 0.34 / 0.16 / 0.12 | `core/matching.py` |
| `FUZZY_NAME_MIN` / `ARTIST_VETO_MIN` | 0.90 / 0.55 | `core/matching.py` |
| 兜底配对下限 | 总分 0.86、名称 0.85 | `core/matching.py::pair_sources` |
| `STALE_JOB_SECONDS` | 180 s | `core/jobs.py` |
| `STEP_TIMEOUT_SEC` | 120 s | `core/ops.py` |
| 日志环形缓冲 / 轮转 | 500 条 / 4 MB × 3 | `core/applog.py` |
| `MEMORY_LINES` | 500 | `core/applog.py` |
| `SLOW_MBPS` / `SLOW_WORKERS` / `SLOW_HASH_WORKERS` | 15.0 / 2 / 1 | `core/device.py` |
| 覆盖率阈值（模糊搜索）`FUZZY_MIN_SCORE` | 0.45 | `main.py` |
| 单文件上传上限 `MAX_FILE_BYTES` | 1.5 GB | `upload_api.py` |
| 封面内存缓存上限 | 2000 条（硬编码） | `main._serve_cover_bytes` |


