# 安全审查与修复记录（2026-10-19）

本文档合并了 2026-10-19 的两轮安全审查（首轮自审 + Agent 深度审查）及其修复记录。
完整变更见 `CHANGELOG.md`，已知未修问题见 `docs/KNOWN_ISSUES.md`。

---

## 一、审查概况

| 项 | 内容 |
| --- | --- |
| 审查范围 | 全项目约 32000 行代码 |
| 测试基线 | 892 项单元测试（8 项正常跳过），0 失败 |
| 静态检查 | `audit_split.py` 0 错误 · `check_frontend.py` 0 错误 |
| 路由完整性 | 94 个端点全部有效 |

---

## 二、已修复问题

### Critical 级（数据安全，3/3 已修复）

**C1 · 跨进程锁释放逻辑错误**
- 位置：`app/core/proclock.py::release()`
- 问题：空字符串 token 能通过 `if tok not in holder`（Python `"" in "any_string"` 为 True），误释放其他进程的锁 → 双写同一批文件。
- 修复：严格非空检查 + `|{tok}|` 边界精确匹配。

**C2 · 封面缓存 cap=0 被解读为无限制**
- 位置：`app/deps.py` 封面缓存段
- 问题：`cap=0` 时跳过淘汰循环，缓存无限增长。
- 修复：`cap<=0` 直接禁用缓存返回，淘汰循环无条件执行。

**C3 · junction 穿越路径检查**
- 位置：`app/deps.py::in_source_rel()`
- 问题：相对路径只查 `has_dotdot()`，未调 `inside_root_real()` 检测 junction → 可读取库外文件。
- 修复：补 `inside_root_real()` 检查，且只对本地源做（网络源的 root 是 URL，不适用）。

### High 级（数据完整性，3/8 已修复）

**H1 · 根目录传输后索引不刷新**
- 位置：`app/deps.py::_reindex_after_transfer()`
- 问题：`dst_files=None` 时 `"" in dirs and dst_files` 为假，根目录被跳过。
- 修复：根目录优先处理，有文件列表就精确刷新，无列表时扫整个根。

**H2 · 校验失败隔离文件时间戳冲突**
- 位置：`app/core/transfer.py::_quarantine()`、`app/core/ops.py::_do_copy()`
- 问题：`.bad-<时间戳>` 同秒内多文件校验失败时名字冲突，后一个覆盖前一个。
- 修复：改用 `uuid.uuid4().hex[:8]`。

**H4 · 艺术家冲突判定硬编码阈值**
- 位置：`app/core/matching.py::artist_conflict()`
- 问题：第 301 行硬编码 `0.72`，与常量 `ARTIST_VETO_MIN`（第 38 行，值 0.72）不一致，改阈值时容易漏改一处。
- 修复：统一引用常量 `ARTIST_VETO_MIN`。

### Medium 级（1/12 已修复）

**M8 · NUL 字节注入缺少审计日志**
- 位置：`app/sources/local.py::_native()`
- 问题：已有 NUL 字节拦截，但无日志记录。
- 修复：补 `log.warning` 审计记录。

---

## 三、经核实为误报的问题

Agent 报告中的以下问题经逐条核实（含代码路径阅读和正则实测），确认代码原本就正确处理，无需修改：

| 编号 | 问题 | 核实结论 |
| --- | --- | --- |
| H5 | `scanner.py` `_owned_here` 闭包失效 | 所有读取处都用 `getattr(job, "_owned_here", True)`，无 stale 捕获 |
| H6 | 跨盘 `.mshpart` 临时名冲突 | `Executor._claim_target` 已原子占位；`write_file` 用 UUID 临时名 |
| H7 | `_browse_cache_clear` 遗漏 | `ops.py:529-533` 在每次 `Executor.run` 后已调用 |
| H8 | 陈旧任务落库失败卡死 | 内存状态先改 `error`，`_busy_locked` 只认 running/paused |
| M2 | `duration_similarity` 除零 | `tol<=0` 强制 2.0，`tail=max(28, 3*tol)` 恒 ≥28 |
| M4 | `_hash_groups` mode 参数未验证 | 调用方只传字面量，未知值安全降级为 full |
| M7 | `_DUP_COUNTER` 误删 `(12 Bars)` | 实测正则不匹配，要求纯数字且锚定在扩展名前 |
| M10 | `artist_sim` None 未处理 | 两处调用都先查 `ar is None`（matching.py:292,344） |
| M12 | `proclock._pid_alive` PID 复用 | 180s mtime 兜底，实际窗口极小 |

### H3 · 扫描任务独占性
Agent 报告担心 `clear_cover_index` / `register_cover_files` 并发调用有风险。
实际：两者只从 `scanner.py` 调用，而 `"scan"` 已在 `EXCLUSIVE_KINDS` 里，并发扫描不会发生。

### 报告中的"新发现" · `files_transfer` 的 dst_dir 路径穿越
Agent 担心 `dst_dir` 未走 `in_source_rel`。
实际：`files.py:147-149` 已有 `has_dotdot` + `inside_root` 双重检查。

---

## 四、低优先级改进（已完成）

**L1 · `mappings_api` 写端点加独占位**
- 给 `PUT /api/mappings` 和 `POST /api/mappings/one` 加了 `Depends(exclusive_slot("mappings"))`。
- `EXCLUSIVE_KINDS` 加入 `"mappings"`。

**L2 · 测试资源泄漏警告**
- `tests/test_player_queue.py` 两处 `setUpClass` 的 bare `open()` 改成 `with` 上下文管理器。

---

## 五、维护建议

新增写端点时：
1. 确认 kind 在 `EXCLUSIVE_KINDS` 里
2. 用 `Depends(exclusive_slot(kind))` 或 `exclusive_guard`
3. 补一条 `test_route_guards_complete` 测试

新增路径操作时：
1. 必须过 `inside_root` / `has_dotdot`
2. 本地源补 `inside_root_real` 挡 junction
3. 统一用相对路径做键

新增删除操作时：
1. 默认 `hard=False`，进回收站
2. 永久删除必须 `confirm=true`
3. 补"只读源拒绝"测试

---

## 六、系统层面：Windows 命名规则

用户报告"能建出名为 `con` 的文件夹"。实测（Win11 + Python 3.13，临时目录）：

| 操作 | 结果 |
| --- | --- |
| `mkdir CON` / `COM1` / `aux.flac` | 成功，得到资源管理器里打不开、删不掉的目录 |
| `mkdir NUL` | 静默"成功"，但目录不存在；之后往里搬文件全部 `WinError 3` |
| 整张专辑目录改名为 `nul` | `shutil.move` 逐个文件报错，半途而废 |
| 写文件 `nul`（无扩展名） | 不报错，数据被设备吞掉（0 字节） |
| `Album.` / `Album ` | 系统静默剥掉尾随点/空格，实际落成 `Album`，可能撞上已有目录 |
| 名字含 `\x01` 等控制字符 | `WinError 123` |

原来只有上传和中转站各自挡了保留名（两份清单），新建目录、目录改名、执行器的 rename/move/copy/transfer、智能整理生成的目录名都没挡。

**修复**：规则收口到 `app/core/paths.py`（唯一来源，和 `AUDIO_EXTS` 同一原则）：

- `name_problem(name)` / `path_problem(rel)`：用户亲手输入的名字 → **拒绝并说明原因**
- `safe_name(name)`：标签值、上传文件名等不可信来源 → **自动修正**（`Con` → `_Con`）

接入点：`folders_api` 新建/改名、执行器 `_resolve_move` / `_do_rename` / `_do_copy` / `_do_transfer` 以及预演 `_dry`、`organize._safe_seg`、上传 `_safe_name` 与入库目录、中转站 `_safe_name` 与归位目录。只检查**写入目标**，不检查源路径（库里已有的怪名字文件要能被移走/改好）。

### 顺带发现：只改大小写的改名会弄丢文件

`Song.mp3` → `song.mp3` 在大小写不敏感的盘上，`exists("song.mp3")` 为真，而"已存在的目标"就是源文件自己：

| 策略 | 修复前 |
| --- | --- |
| skip | 报"目标已存在，跳过" |
| rename | 改成 `song (2).mp3` |
| **overwrite** | **先把"目标"移进回收站 —— 移走的正是源文件；步骤报 error，曲库里文件消失** |

目录 `Album` → `album` 则报"目标目录已存在且非空"。

**修复**：数据源新增 `same_entry(a, b)`（本地源用 `os.path.samefile`），执行器 rename/move 和 `rename_dir`、`folders_api.rename_folder` 在冲突处理前先判断，是同一条目就直接原子改名。

守护测试：`tests/test_audit_fixes.py::WindowsNameRulesTest`、`CaseOnlyRenameTest`。

### 未处理

- **超长路径（>260 字符）**：本机实测 386 字符能建成（系统开了长路径支持）。没开的机器上会报错，但是是明确失败、不丢文件，暂不处理。
