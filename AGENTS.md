# AGENTS.md —— 接手这个项目前必读

> 这份文件是写给**接手的 AI 助手**的，也给人看。
> 目标是：你读完这一份，就知道哪些事**绝对不能做**、东西在哪、改完怎么验。
>
> 项目：Music Sync Hub —— Windows 上的多库音乐管理工具（FastAPI + SQLite + 原生 JS，无构建）。
> 规模：应用约 32000 行 / 测试约 11000 行 / 94 个 HTTP 端点 / 723 项单元测试 / 90 项接口冒烟。
> 使用者：单人。所以「够用」优先于「通用」，但**文件安全不许妥协**。

---

## 目录

1. [30 秒上手](#1-30-秒上手)
2. [★ 硬约束（违反会出事）](#2-硬约束违反会出事)
3. [代码地图](#3-代码地图)
4. [一次「移动」的完整数据流](#4-一次移动的完整数据流)
5. [六个必须先懂的概念](#5-六个必须先懂的概念)
6. [改完必须做的检查](#6-改完必须做的检查)
7. [验证工具怎么用](#7-验证工具怎么用)
8. [按症状查踩过的坑](#8-按症状查踩过的坑)
9. [已知问题与待办](#9-已知问题与待办)
10. [明令不要做的事](#10-明令不要做的事)
11. [文档索引](#11-文档索引)

---

## 1. 30 秒上手

```bash
# 依赖
uv sync                                    # 或 pip install -e .

# 跑起来（默认 http://127.0.0.1:8787）
python -m app.main

# 只跑演示数据，不碰真实库
python run-test.py

# 测试（必须全绿）
python -m unittest discover -s tests -t .
python tools/check_frontend.py             # 前端静态检查
python tools/audit_frontend.py             # 深度检查
python tools/audit_split.py                # ★ 拆分专项：模块导入 / 未定义名字 / 路由差分
python tools/audit_split.py --baseline <备份目录>   # 带上基线可查"有没有丢路由"
```

**环境变量**（都在 `app/core/config.py` 和 `app/main.py` 里读）：

| 变量 | 作用 |
| --- | --- |
| `MSH_DATA_DIR` | 数据目录（配置/索引/日志）。不设则用 `%USERPROFILE%\.music-sync-hub` |
| `MSH_DB` | 索引库路径。改了会热切换（见 `db()`） |
| `MSH_HOST` / `MSH_PORT` | 监听地址/端口 |
| `MSH_TEST_MODE=1` | 测试模式：页面顶部横幅 + 右下角 TEST 角标。**只影响显示，不影响业务** |
| `MSH_NODE` | node 可执行文件路径。不设则 5 条 JS 队列测试**干净跳过**（不算失败） |

> ⚠️ 跑测试/冒烟/任何临时脚本前，**确认 `MSH_DATA_DIR` 指向临时目录**。
> 曾经有一次冒烟测试把用户的真实 `config.toml` 覆盖了。详见 `SAFETY.md`。

---

## 2. ★ 硬约束（违反会出事）

这 10 条每一条都对应一次真实事故或一次真实的 bug 排查。**改代码前先对照。**

### 2.1 音频扩展名只有一个来源

```python
# 唯一来源
from app.core.paths import AUDIO_EXTS, is_audio
```

**不要**在别处再写一份 `('.mp3', '.flac', ...)` 清单，也不要在 JS 里写死扩展名。
前端需要时从 `/api/system` 的 `audio_exts` 拿。

- 为什么：以前前后端各有一份清单，加格式时只改了一边，导致"某些格式在文件浏览里能看见、但加入候选区就消失"。
- 守护测试：`tests/test_non_audio.py::AudioExtSingleSourceTest`
- 当前 22 种：`mp3 flac m4a aac alac ogg oga opus wav aiff aif ape wv wma dsf dff mp4 m4b mpc tta mka caf`

### 2.2 路径坐标系统一用「源内相对路径」

`app/sources/base.py` 的 `MusicSource` 有两套坐标：

| 方法 | 返回 | 用途 |
| --- | --- | --- |
| `abs_path(rel)` | 绝对路径 | 真正读写文件时 |
| `rel_path(abs)` | 源内相对路径（`d/x.mp3`） | **做键、做比较、存库** |

**规则：任何跨函数传递的"标识"都用相对路径。** 只有真正打开文件那一刻才转绝对路径。

- 为什么：`_reserve_targets` 用相对路径做键，而 `_claim_target` 曾经收绝对路径 —— 键永远匹配不上，于是"同方案内两条步骤写同一目标"只落一个文件，**却报成'已改名、已处理'**。静默丢文件。
- 守护测试：`tests/test_transfer.py::TargetConflictTest`（类注释里有完整复盘）

### 2.3 新增写操作必须挂独占位

`app/core/jobs.py`：

```python
EXCLUSIVE_KINDS = {"cleanup", "collect", "move", "organize", "scan", "station",
                   "sync", "tags", "transfer", "trash", "upload", "upload-commit"}
```

新增任何会**改文件或改索引**的后台任务，必须：
1. 把 kind 加进 `EXCLUSIVE_KINDS`
2. `get_jobs().create(..., exclusive=True)`
3. HTTP 端点用 `depends(exclusive_guard)` 或显式 `get_jobs().ensure_free()`

- 为什么：漏一个 = 两个流程同时改同一批文件，基于过期状态操作。
- 踩过：`scan` 和 `upload-commit` 曾经漏了。
- `app/core/guard.py` 把 `JobBusy` 转成 **409**，前端要求用户刷新状态后再试。

**只读但需要一致快照的操作**（如方案预演）不占独占位，但要调 `ensure_free()`：
预演时库正在被改，预演结果就是错的。

### 2.4 不要给基础规则加无条件的 `transform` 过渡

```css
/* ✗ 错 —— 会让元素被父级布局带动时也"滑"一段 */
.btn { transition: transform .12s; }
.btn:active { transform: translateY(1px); }

/* ✓ 对 —— 位移照样有过渡，但只在 :active 时 */
.btn { transition: background .12s; }
.btn:active { transition: transform .12s; transform: translateY(1px); }
```

- 为什么：无条件 `transform` 过渡会让**任何** transform 变化都动画化，包括"被父级布局带着走"。表现是按钮里的箭头/图标自己滑过去。这个坑踩过三次（`.btn`、`.tile`、`.stat`）。
- 守护测试：`tests/test_organize_album.py::test_no_unconditional_transform_transition`（扫 `app.css`）
- 另外：展开箭头用**文本字符替换**（`▸`/`▾`），不要用 CSS 旋转 —— 原生 `<details>` 的三角是浏览器自己画的、自带动画，`transition:none` 压不住。

### 2.5 不要写死本机绝对路径

```python
# ✓
ROOT = Path(__file__).resolve().parent.parent

# ✗
ROOT = Path(r"C:\Users\Administrator\Desktop\MusicSyncHub")
```

- 为什么：曾经有 26 处硬编码，测试「在本机全过」但在别人机器上全线失败 —— 测试套件的价值被高估了。
- 已清理干净。新增代码请保持。

### 2.6 改前端必须升 `?v=NN`

`app/web/index.html` 里所有静态资源都带版本号：`/static/js/app.js?v=102`。

改任何 `.js` / `.css` 后**必须**把 `?v=NN` 加 1，否则浏览器用缓存，你会对着旧代码调试很久。

### 2.7 绝不覆盖、绝不真删、搬运先校验

这是项目的三条底线，写在 `app/core/ops.py` 顶部注释里：

1. 目标已存在 → 按 `policy` 处理（`skip` 默认 / `rename` / `overwrite`），**默认不覆盖**
2. 删除 → 移到 `<源根>/.music-sync-trash/YYYYMMDD/`，要永久删除必须显式 `hard=True`
3. 跨库搬运 → **复制 → 校验 → 删源**，校验不过不删源（`app/core/transfer.py::transfer_one`）

改执行器时如果发现自己在写 `os.remove` / `Path.unlink` / `shutil.move` 直接调，**停下来**，先看 `_do_remove` / `_do_move` 怎么做的。

### 2.8 `version_tags` 的字段集必须和 `dup_key` 一致

```python
P.version_tags(name, title, rel_path)      # ✓ 三个都要
P.version_tags(name, title)                # ✗ 会导致同一文件两套结论
```

- 为什么：版本标记可能只在**目录名**里（`专辑 (Remix)/Song.mp3`）。只传 name+title 时配对路径认为"版本一致"、而 `dup_key` 认为"不同版本"，同一文件被分到两组。
- 这个约定写在 `matching.version_mismatch()` 的 docstring 里（含事故复盘）。刚修过 `_same_tag` 和 `group_to_dict` 两处漏传。

### 2.9 测试不许碰真实数据

`tests/__init__.py` 会把 `MSH_DATA_DIR` 指到进程级临时目录，但**只覆盖进程启动时**。
新增测试如果要写配置/建库，**自己再指一个 `tempfile.TemporaryDirectory()`**。

- 为什么：测试之间靠 pop 环境变量隔离，中间有空窗期 —— 这期间任何 `save_config()` 都会落到用户的真实配置。事故已发生一次（用户两个数据源被换成临时假源）。

### 2.10 任何破坏性操作都要过 `_guard_inside`

```python
from app.core.paths import inside_root     # 判定逻辑只在这里
```

删除 / 移入回收站 / 重命名 / 复制 / 跨库传输 / 中转站归位 —— **全部**要先确认
路径确实落在数据源根目录内。`paths.inside_root()` 同时处理 `..` 和 Windows 大小写。

- 为什么：`_inside_root` 原来**只有 `move` 在用**。Windows 上
  `LocalSource.abs_path()` 对"别的盘的绝对路径"是原样返回的，于是构造一个
  `{"op":"delete","src_path":"D:/别处/x.txt"}` 就能**永久删除库外文件并返回 done**。
  实测确认过。
- 另一个坑：`paths.norm()` **不折叠 `..`**，所以 `root + "/../../x"` 能通过
  `startswith(root)` 判断。凡是"拼路径再判前缀"的地方都必须先过 `has_dotdot()`。

### 2.11 "选名字 + 登记"必须是同一个临界区

`Executor._claim_target` —— 同一方案里 ≥3 步写同一目标时，候选名必须**同时**满足
"磁盘上不存在"和"`_planned` 里没人占"，而且这两个判断与登记要在**同一把锁**里。

- 为什么：以前分三次拿锁，40 轮里丢 39 轮文件，三条都报 `done`。
- 同理：回收站同名去重（`local.remove`）也必须**循环找空位 + 加锁搬入**。
  同一秒删三个同名文件时，只试一次时间戳名会让后一个覆盖前一个 —— **那份文件永久消失**。

### 2.12 设置项必须真的接线

加了 `Settings` 字段就要有消费者，否则界面能改、改了没效果、还不报错。

- 已经踩过两次：`cover_cache_size`（硬编码 2000）、`ignored_names`（零消费）。
- 守护测试：`tests/test_settings_wiring.py` —— 每个字段都必须在 `config.py` 之外被引用。

### 2.13 出错的语义要准确

| 情况 | HTTP | 异常 |
| --- | --- | --- |
| 数据源不存在 | 404 | `SourceNotFound` |
| 数据源只读却要写 | 403 | `ReadOnlyError`（端点里先调 `require_writable()`） |
| 已有独占任务在跑 | 409 | `JobBusy` |
| 参数非法（含 NUL 字节路径） | 400 | `SourceError` / `ValueError` |
| 其它未捕获 | 500 | `Exception`（会带堆栈写日志） |

**不要**把可预期的错误变成 500。全局处理器在 `app/main.py` 顶部。

---

## 3. 代码地图

### 3.1 后端核心（真正重要的部分）

| 文件 | 职责 | 改它的风险 |
| --- | --- | --- |
| `app/core/ops.py` | **执行器**。唯一真正动文件的地方 | 🔴 最高。动了必须跑全量测试 |
| `app/core/transfer.py` | 跨库搬运（复制→校验→删源）+ 心跳 | 🔴 高 |
| `app/core/matching.py` | 匹配/查重算法（配对、分组、代表选择） | 🟠 中高。逻辑密，改动要看阈值链 |
| `app/core/plan.py` | 方案生成（三种方向、镜像删除、冲突处理） | 🟠 中高 |
| `app/core/jobs.py` | 任务状态、独占位、心跳回收、崩溃恢复 | 🔴 高。错了会两个流程并行 |
| `app/core/guard.py` | 把 `JobBusy` 转 409 | 🟢 低 |
| `app/core/db.py` | SQLite schema、连接、迁移 | 🟠 中。改 schema 要写迁移 |
| `app/core/scanner.py` | 扫描入库、哈希策略 | 🟠 中 |
| `app/core/paths.py` | 路径工具 + `AUDIO_EXTS`（**唯一来源**） | 🟡 中低 |
| `app/core/config.py` | `Settings`(16 字段) / `SourceConfig`(15 字段) / 密钥 | 🟡 中低。改字段要同步 `_NUM_LIMITS` 与前端 |
| `app/core/applog.py` | 日志落文件 + 环形缓冲 | 🟢 低 |
| `app/core/device.py` | 盘符探测、慢盘测速、便携设备（仅探测） | 🟢 低 |
| `app/core/bgjob.py` | 后台线程跑任务 | 🟢 低 |

### 3.2 数据源适配器

`app/sources/` —— 统一接口在 `base.py`：

```
MusicSource（抽象）
  ├─ list_dir(rel)           列目录
  ├─ stat(rel)               文件信息
  ├─ read_file / write_file  读写
  ├─ open_read(rel)          流式读（试听用）
  ├─ mkdirs / rename / delete
  ├─ exists / can_write / can_delete
  └─ 能力标记：kind / label / experimental / verify_note
```

| 实现 | 类型 | 状态 |
| --- | --- | --- |
| `local.py` | 本地盘 / 映射盘 | ✅ 稳定，实测 |
| `sftp.py` | SFTP | ⚠️ 实验性，**没连过真实服务器** |
| `webdav.py` | WebDAV | ⚠️ 实验性 |
| `httpdir.py` | HTTP 目录 | ⚠️ 实验性 |
| `smb.py` | SMB | ⚠️ 实验性 |
| `ftp.py` | FTP/FTPS | ⚠️ 实验性 |

> 用户的实际用法：网络位置在 Windows 里映射成盘符（如 `Y:\`），走 `local`。
> 原生网络适配器是为了"省掉映射这一步"。**保留它们，别删。**
> 但不要声称"支持" —— 界面上必须继续显示实验性标记。

新增一个数据源类型 = 在 `app/sources/` 加一个类 + `@register`；
`available_types()` 会自动把它带给前端（不要在前端写第二份类型表）。

### 3.3 HTTP 层

**文件布局（2026-10 从单文件巨石拆开的）：**

| 文件 | 端点 | 内容 |
| --- | --- | --- |
| `app/deps.py` | — | **共享运行时状态 + helper**：`db()` / `require_writable()` / 封面缓存 / 目录聚合 / 指纹 / 视图转换 / `refresh_tracks()` |
| `app/main.py` | 1 | 只剩 app 创建、中间件、5 个异常处理器、`lifespan`、router 挂载、静态资源 + 首页、CLI |
| `app/routes/system.py` | 17 | 系统 / 数据源 / 扫描 / 日志 / 盘符 / 设备 / 设置 |
| `app/routes/jobs.py` | 9 | 任务中心 |
| `app/routes/library.py` | 7 | 曲库浏览 / 封面 / 试听（+ `refresh_tracks` helper 已移入 deps） |
| `app/routes/compare.py` | 5 | 对比 / 方案 / 执行 / 查重 |
| `app/routes/files.py` | 10 | 文件浏览 / 跨库传输 |
| `app/search_api.py` 等 | 45 | 更早拆出去的：search / upload / station / tags / mappings / folders |

> **拆除约定**（改代码前必读）：
> 1. 路由模块只依赖 `deps` 和 `core`，**绝不 import `main`**（否则循环导入）。
> 2. `main.py` 在**最底部**才 import 各 router —— 那时 main 的名字已就绪。
> 3. 函数体里的**延迟导入也要写成双点**（`from ..core import X`）。
>    写成 `from .core` 会解析成 `app.routes.core` —— 启动不报错、**调用到才炸**，
>    这是拆分时踩过的坑。
> 4. 外部模块（`tags_api` / `station_api` / `upload_api`）通过 `_deps()` 拿的
>    `db` / `require_writable` / `refresh_tracks` / `manager` / `DB`，
>    `main.py` 已从 deps 转出，别删那几个导入。

#### 原来的三段（保留作对照）

**94 个端点**，分在 7 个文件：

| 文件 | 端点数 | 内容 |
| --- | --- | --- |
| `app/main.py` | 49 | 最大的一坨：系统/数据源/扫描/任务/曲库/对比/方案/执行/查重/文件浏览/设置 |
| `app/search_api.py` | 11 | 搜索、筛选、分面 |
| `app/upload_api.py` | 11 | 上传三步流程 |
| `app/sources/folders_api.py` | 8 | 目录树 |
| `app/station_api.py` | 7 | 中转站 |
| `app/tags_api.py` | 5 | 标签读写/备份 |
| `app/mappings_api.py` | 3 | 路径映射 |

> `main.py` 是 2521 行的巨石，**已知问题**。按功能分组有序，用 `grep "def 函数名"` 定位。
> 拆分风险：`db()` 有 `MSH_DB` 热切换语义，`require_writable()` 和十几个 `_xxx_view` helper 被大量复用 —— 拆路由需要先把这些搬到独立模块，否则循环导入。**目前评估为"收益不抵风险"，暂不拆。**

`main.py` 里仅有的模块级状态（真全局）：

```python
DB: Optional[Database] = None        # 由 db() 管理，带热切换
_cover_cache: Dict[str, Any] = {}    # 封面内存缓存，上限读 Settings.cover_cache_size
_cover_lock = threading.RLock()
_FILE_BROWSE_CACHE: Dict[str, Any] = {}
WEB_DIR = Path(__file__).resolve().parent / "web"
```

### 3.4 前端

**无构建、无模块化、18 个 `<script>` 顺序加载。这是刻意的** —— 维护者不是专业程序员，
有构建链就意味着有一天 `node_modules` 炸了他没法处理。请不要引入打包工具。

| 文件 | 职责 |
| --- | --- |
| `app/web/index.html` | 骨架、SVG 图标 sprite、脚本加载顺序、`?v=NN` |
| `static/js/app.js` | **核心**：`App` 命名空间、`State`、播放器队列、视图路由、boot |
| `static/js/api.js` | 所有 HTTP 调用 |
| `static/js/ui.js` | `UI`（弹窗/toast）、`U`（格式化/转义/图标） |
| `static/js/tasksync.js` | 跨页面任务同步 + 独占位提示 |
| `static/js/staging.js` | 候选区（持久化在 localStorage） |
| `static/js/prefs.js` | 用户偏好（`Prefs.get/set`） |
| `static/js/views.*.js` | 各页面：`views.lib / files / compare / duplicates / station / sources / settings / ops` |
| `static/js/organize.js` | 智能整理向导 |
| `static/js/upload.js` | 上传流程 |
| `static/css/app.css` | 单一 CSS（约 1530 行），Win10 1903 Fluent 风格 |

前端身份：**`source::path`**。分组键前缀 `source_id::`。

图标：`<symbol id="i-NAME">` + `U.icon('NAME', cls)`。
**加图标时检查 id 是否重复** —— 曾经 `i-pause` 定义两次，渲染结果取决于浏览器选哪个。

### 3.5 测试与工具

| 路径 | 作用 |
| --- | --- |
| `tests/` | 33 个文件、723 项。`tests/srchelp.py::fn_body` 能抠出 JS 函数体做断言 |
| `tools/smoke_api.py` | 90 个接口冒烟。**会快照并恢复设置**，跑完校验 config 哈希 |
| `tools/check_frontend.py` | 静态检查：资源引用、括号配平、语法 |
| `tools/audit_frontend.py` | 深度检查：`querySelector('#id')` 拿不到、`API.foo` 不存在、HTML 重复 id、`U.xxx` 缺失 |
| `tools/cdp_eval.js` | 用 CDP 在真浏览器里跑一段 JS |
| `tools/cdp_vp.js` | 多视口截图（宽度/高度列表 + 截图目录） |
| `tools/build_packages.py` | 打 AI 审查包 / 测试包 |
| `run-test.py` | 测试模式启动器（包内演示库 + 包内数据目录） |

---

## 4. 一次「移动」的完整数据流

理解这条链路，就理解了这个项目 80% 的设计。

```
① 前端勾选文件
   views.files.js 收集 source::path → 组装 steps
        ↓ POST /api/files/move
② main.py: files_move()
   ├─ resolve_source() 拿数据源
   ├─ require_writable()          ← 全只读则 403（否则执行器只标 blocked、仍回 200）
   ├─ get_jobs().create("move", exclusive=True)   ← 占独占位，冲突则 409
   └─ bgjob.run_in_background(executor.run, ...)
        ↓
③ ops.py: Executor.run()
   ├─ _reserve_targets(steps)     ← 先把所有目标占位登记（相对路径做键）
   ├─ ThreadPoolExecutor(workers) ← workers 受慢盘探测压制
   ├─ 每个 step: _check() → _apply()
   │    └─ _apply() 按 op 分发：
   │         copy/link → _do_copy()      （_claim_target 处理撞车改名）
   │         transfer  → _do_transfer()  （复制→校验→删源）
   │         move      → _do_move()
   │         trash     → _do_remove(hard=False)   ← 进回收站
   │         delete    → _do_remove(hard=True)    ← 永久删除
   │         rename    → _do_rename()
   │         其它      → skipped（detail 写"执行器不支持"）
   ├─ policy: skip(默认) / rename / overwrite
   ├─ _sync_index()               ← 同步改 SQLite 索引
   └─ _tally()                    ← 汇总 done/skipped/error/blocked
        ↓
④ 前端轮询 /api/jobs/{id} 拿结果；失败的进「操作记录」
```

**关键点**：所有文件操作都在第 ③ 步的一个地方。想保证安全，只需保证 `ops.py` 对。

---

## 5. 六个必须先懂的概念

### 5.1 数据源 = 一个有根的"文件系统"

不是一个目录，而是一个**有根、有坐标系、有能力标记**的抽象。
`root` 是锚点，所有路径都相对于它。`can_write` / `can_delete` 决定能不能动。

### 5.2 两条路径坐标

`abs_path(rel)` ↔ `rel_path(abs)`。**做键用相对，动手用绝对。**（见 2.2）

### 5.3 任务（Job）= 有一次执行的生命周期

`app/core/jobs.py::JobState`：`id / kind / state / total / done / message / exclusive / created_at`。
状态：`running / done / error / cancelled / paused`。
崩溃恢复：`load_unfinished()` 把 `state=running` 的捞回来，界面问"要不要接着做完"。

### 5.4 独占位 = 同一时间只允许一个写流程

`EXCLUSIVE_KINDS` 里的 kind 会占位。占位期间别的写操作得到 **409**。
心跳：`transfer.py::_Heartbeat` 每 20 秒续一次；超过 `STALE_JOB_SECONDS=180` 没心跳会被回收。
**为什么需要心跳**：`shutil.copyfile` 没有回调，6 GB 文件 @ 20 MB/s 要 300 秒 > 180 秒，会被误判为僵尸、释放独占位，此时别的流程就能进来改同一批文件。

### 5.5 方案（Plan）= 一组 Step，先给人看

`Step`：`id / op / kind / src / dst / src_path / dst_path / risk / reason`。
`build_plan()` 产出，前端渲染，用户勾选后 `execute`。
**默认 `execute=false`**，这是刻意的。

### 5.6 三种对比方向

| 方向 | 语义 | 注意 |
| --- | --- | --- |
| `left_to_right` | 左有右无 → 复制到右 | |
| `right_to_left` | 右有左无 → 复制到左 | |
| `complement` | 两边互补，各自补上 | |
| + `mirror` | 多删：右侧多出来的删掉 | ⚠️ **与 `right_to_left` 互斥**（否则"刚复制过去就被镜像删掉"） |

---

## 6. 改完必须做的检查

```bash
cd <项目根>

# ① 全量单元测试（必须 OK；没装 node 时 5 条跳过属正常）
python -m unittest discover -s tests -t .

# ② 前端静态检查（都必须 0 问题）
python tools/check_frontend.py
python tools/audit_frontend.py

# ②b 拆分专项审计（改了 deps / routes / 新增端点后必跑）
#    它能抓到"只在调用时才炸"的问题：单点相对导入（.core → app.routes.core）、
#    模块级常量漏导入、整段重写时弄丢的变量初始化。
python tools/audit_split.py

# ③ 改了前端 → 升版本号
#    app/web/index.html 里 ?v=NN 加 1

# ④ 改了后端/接口 → 起服务跑冒烟
python -m app.main          # 另开一个终端
python tools/smoke_api.py   # 必须 90 个全过；跑完确认 config.toml 哈希没变

# ⑤ 改了界面 → 真浏览器验证（静态检查抓不到视觉问题）
node tools/cdp_vp.js 9224 http://127.0.0.1:8787/ 20000 <探测脚本.js> 1500 <截图目录>
```

**⑤ 别省。** 这次会话里我自己就引入了"测试横幅盖住 8 个导航标签"的 bug，
测试全绿、静态检查 0 问题，只有截图和 `elementFromPoint` 打点能发现。

---

## 7. 验证工具怎么用

### 7.1 冒烟测试为什么带"快照恢复"

`tools/smoke_api.py` 会先用 `GET /api/settings` 拍快照，跑完把设置 PUT 回去并逐项核对。
原因：它其中一条用例为了验证"0 = 不限额"，真的把用户的 `station_max_gb` 从 20 改成了 0 ——
一个号称"只做只读调用"的测试把关用户防写满的保护**悄悄关掉了**。
**新增冒烟用例时不要写"改了就改不回来"的操作。**

### 7.2 CDP 工具的三个坑

1. **`window.resizeTo` 不生效** —— 必须用 `Emulation.setDeviceMetricsOverride`
2. **`setDeviceMetricsOverride` 必须在 `navigate` 之后** —— 顺序错了尺寸不生效
3. **`requestAnimationFrame` 在无头/后台标签会被节流**（可能挂 10 分钟）
   → 轮询用 `setTimeout(16)`，不要用 rAF

### 7.3 测短窗口

`cdp_vp.js` 的高度参数是**列表**，且 PowerShell 传空参数时会错位
（曾经把 `900,600,420` 当成截图目录名，在项目根建了一堆垃圾目录）。
模拟矮窗口更稳的做法：设 `document.getElementById('app').style.height = '420px'`。

### 7.4 动画/过渡类问题

`document.getAnimations()` 和 `transitionrun` 事件在这个环境里**不可靠**。
如果要用，**必须同时做一个对照实验**（故意加一个已知过渡，看探针能不能抓到）。
对照实验也失败时，结论是"探针无效"，不是"对象没问题"。
—— 我因此白走了三轮。

---

## 8. 按症状查踩过的坑

| 症状 | 大概率原因 | 去哪看 |
| --- | --- | --- |
| 点了没反应，控制台只有一行 `[playTrack] 没有曲目对象` | `queue.current` 返回裸曲目而非 `{track,label}` | `app.js::queue.current` |
| 播放列表里出现整个库源，而不是当前这首 | `playTrack` 没管队列 | `app.js::playTrack` |
| 按下一首完全没反应 | 队列里有 `{track:null}` 失效条目 | `queue.prune()` / `queue.step()` |
| 文件"移动成功"但少了一个 | 目标占位坐标系不一致 | `ops.py::_reserve_targets` vs `_claim_target` |
| 任务报成功但文件没动 | 只读检查在端点层缺失，执行器只标 `blocked` 仍回 200 | 端点里加 `require_writable()` |
| 明明插入了 N 首只查到 M 首 | `MSH_DB` 切换后连接没换 | `main.py::db()` |
| 扫描完界面显示 0 首 | `available_types()` 写死只返回 local | `sources/base.py` |
| 上传/扫描跑一半卡住，之后所有操作都 409 | 心跳没续，任务被判定僵尸但没清理 | `jobs.py` 的 `STALE_JOB_SECONDS` |
| 智能整理总数和移动数对不上 | 无元数据曲目被静默丢弃 | `organize.py` 的 `manual` 桶 |
| 3 个库跨库查重出现同一文件在两组 | 配对结果没合并重叠组 | `matching.py::merge_matched_pairs` |
| 展开箭头"自己滑过去" | 无条件 `transform` 过渡 / 原生 `<details>` 三角 | 见 2.4 |
| 改了 CSS/JS 没生效 | 忘记升 `?v=NN` | `index.html` |
| 设置项改了没效果 | 代码里写死了，没读 Settings | 例如 `cover_cache_size` 曾硬编码 2000 |
| 小屏幕设置页底部点不到 | 容器没有 `overflow-y:auto` | `app.css` 的 `.view` |
| 窄屏横向溢出 | `input` 的 UA 内在宽度撑破容器 | `.field input{width:100%;min-width:0}` |

---

## 9. 已知问题与待办

按「值得做 / 不值得做」排。

### 值得做

| 优先级 | 问题 | 位置 | 说明 |
| --- | --- | --- | --- |
| 高 | **网络数据源没有对真实服务器验证过** | `app/sources/{sftp,webdav,httpdir,smb,ftp}.py` | 这是项目最大的未验证面。接一个真实的测一遍就能把它从"理论"变成"可用" |
| 中 | `/api/plan` + `/api/execute` 没有方案指纹 | `main.py` 对比页端点 | 生成方案到执行之间库变了不会被发现 |
| 中 | `main.py` 2521 行 | — | 拆分需要先搬 `db()`/`require_writable()`/`_xxx_view`，收益不抵风险，暂缓 |
| 中 | 前端 18 个阻塞脚本 / ~558 KB / 无 gzip | `index.html` | 首屏偏慢。但引入构建链对本项目是净损失 |
| 低 | `artist_conflict` 在相似度 0.55~0.72 不否决 | `matching.py` | 例：`Windows Only` vs `Linux Only` = 0.636。改阈值动全局防线，谨慎 |
| 低 | `/api/files/list` 每次请求都 `listdir` + 全量 `GROUP BY` | `main.py` | 大目录慢 |
| 低 | 中转站 `restore()`/`moveOut()`、操作记录重试是单击执行 | `views.station.js` | 有确认框，但不是二次确认 |

### 已完成（别再"修"一遍）

- ✅ 跨库 3 库以上查重重叠组 → `merge_matched_pairs`
- ✅ 无元数据曲目被静默丢弃 → `manual` 桶 + 界面警告
- ✅ 播放器队列、失效条目、`prune`
- ✅ 展开箭头动画（含原生 details 三角）
- ✅ 无条件 transform 过渡
- ✅ 矮窗口/窄屏不可达与溢出
- ✅ 26 处硬编码路径
- ✅ `i-pause` 重复 symbol、重复 `select` 规则
- ✅ `cover_cache_size` 不生效
- ✅ `version_tags` 字段集不一致（`_same_tag` / `group_to_dict`）
- ✅ 白名单里的死 `extract` op

---

## 10. 明令不要做的事

1. ❌ **不要引入构建工具**（webpack / vite / ts）。维护者不是专业程序员，构建链坏了没人能修。
2. ❌ **不要为了"优雅"重构 `ops.py`。** 它是唯一动文件的地方，注释里每条都是事故复盘。
3. ❌ **不要删掉网络数据源适配器。** 用户的用法是"省掉映射盘符这一步"。
4. ❌ **不要把 `experimental` 标记去掉。** 没验证过就标实验性，这是诚实。
5. ❌ **不要写"自动清理/自动删除"类功能。** 这个项目的立身之本是"人来决定"。
6. ❌ **不要用 `Set-Content` / `Out-File` / `.Replace()` / `-replace` 改源码文件。**
   PowerShell 会改编码、吃换行、甚至把 `<` 变成 `b`（真的发生过，毁了 106 个 HTML 标签）。
   用 `edit`/`write` 工具，或用 Python 的 `Path.write_text(..., encoding="utf-8")`。
7. ❌ **不要在测试里依赖当前机器环境**（绝对路径、特定盘符、已安装的软件）。
8. ❌ **不要因为一条测试红了就改测试** —— 先看那条测试是不是在守护一个刻意决定
   （例：`test_unknown_op_still_skipped`）。

---

## 11. 文档索引

| 文档 | 读者 | 内容 |
| --- | --- | --- |
| **`AGENTS.md`**（本文） | AI / 开发者 | 硬约束、代码地图、验证方法 |
| `SAFETY.md` | **必读** | 三起真实事故的完整复盘与由此定的规则 |
| **`docs/KNOWN_ISSUES.md`** | **接手前必看** | 未修的缺陷清单，按严重度排，每条都有位置/后果/建议修法 |
| `docs/TESTING.md` | 改代码的人 | 跑了什么、**没跑什么**、新功能怎么测、测试隔离怎么防串库 |
| `docs/TECHNICAL.md` | 开发者 / 审查者 | 2800+ 行：架构、数据模型、算法、执行器、93 个 /api 端点完整 API 表 |
| `docs/USER_GUIDE.md` | 最终用户 | 1150+ 行：每个页面、每个按钮、每个设置项、FAQ、排错 |
| `docs/FEATURE_TREE.md` | 所有人 | **功能关系树**：页面 → 功能 → 接口，一张表看懂"能做什么" |
| `docs/LOGIC_TREE.md` | 改代码的人 | **逻辑关系树**：写操作链路、互斥闸门、状态位置、10 条不变量 |
| `docs/AUDIT_ROUNDS.md` | 维护者 | 多轮地毯式排查记录：每轮角度、证据、修了什么、哪些是假阳性 |
| `docs/GITHUB_SETUP.md` | 维护者 | 推送到 GitHub 的步骤与检查清单 |
| `CONTRIBUTING.md` | 贡献者 | 环境、硬约束摘要、提交规范 |
| `CHANGELOG.md` | 所有人 | 改了什么 |
| `README.md` / `QUICKSTART.md` | 路过的人 | 简介 / 5 分钟上手 |

---

## 最后：这个项目的取舍

它**不追求通用**。使用者是一个人，所以他需要的功能就是全部功能。

它**不追求代码优雅**。2521 行的 `main.py` 不漂亮，但拆它的风险大于收益。

它**只在一件事上不妥协**：**不许弄丢文件**。

如果一次改动让代码更漂亮、但让"会不会丢文件"变得更难判断 ——
**那次改动是错的。**
