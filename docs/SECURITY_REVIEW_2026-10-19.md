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
