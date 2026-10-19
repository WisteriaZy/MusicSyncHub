# MusicSyncHub 深度安全审查报告

## 执行摘要

本次审查基于 13 条硬约束和历史事故记录，对核心文件执行器、跨源传输、任务管理、路径验证、并发安全进行了深度检查。**发现 3 个 Critical 级、8 个 High 级、12 个 Medium 级问题。**

---

## Critical 级（必须立即修复）

### C1. 跨进程锁释放逻辑错误 - 可导致双写

**文件**: `app/core/proclock.py:114-119`

**问题**:
```python
# ★ 原始代码
if not tok:
    return
holder = _read_holder(p)
if tok not in holder:
    return
```

**触发条件**: 
1. 进程 A 持有锁并在运行写操作
2. 进程 B 启动，从 DB 恢复"未完成任务"
3. 恢复逻辑调用 `Jobs.finish(st)` → `proclock.release(st.lock_token)`
4. 但 `st.lock_token` 是空字符串（恢复出来的任务没有 token）
5. 原代码检查 `if not tok: return` 会放行
6. 但后续检查 `if tok not in holder` 时，空串 `""` **存在于任何字符串中**（Python `in` 语义）
7. 条件为假，继续执行 `p.unlink()` → 删掉进程 A 的锁

**后果**: 进程 A 的独占位被进程 B 误解除，两个进程同时写同一批文件。

**证据**:
- `jobs.py:360` 恢复任务时 `lock_token` 未落库，恢复出来的 `JobState.lock_token = ""`
- `jobs.py:457` `finish()` 里有 `proclock.release(tok)` 调用
- Python: `"" in "12345|abc|1234567890"` → `True`

**修复**:
```python
def release(token: Optional[str] = None) -> None:
    global _token
    tok = token or _token
    _token = None
    p = lock_path()
    if not p.exists():
        return
    # ★ 必须显式要求非空 token
    if not tok or not tok.strip():
        return
    holder = _read_holder(p)
    # ★ 精确匹配：token 必须完整出现且被 | 包围
    if f"|{tok}|" not in f"|{holder}|":
        return
    try:
        p.unlink()
    except OSError:
        pass
```

---

### C2. 封面缓存上限读取失败后仍继续写入，导致内存泄漏

**文件**: `app/deps.py:185-201`

**问题**:
```python
try:
    cap = int(load_config().settings.cover_cache_size) or 800
except Exception:
    cap = 800  # ✓ 设了默认值
with _cover_lock:
    _cover_cache[ck] = (payload, mime)  # ★ 但还是写进缓存了
    _cover_cache.move_to_end(ck)
    if cap > 0:  # ★ 只有 cap > 0 时才淘汰
        while len(_cover_cache) > cap:
            _cover_cache.popitem(last=False)
```

**后果**: 
- `cap=0` 被解读为"无限制"而非"禁用缓存"
- 配置读取失败时虽然设了 `cap=800`，但前面已经写入缓存，`while` 循环可能只清理一部分
- 长期运行会导致缓存无限增长

**修复**:
```python
try:
    cap = int(load_config().settings.cover_cache_size) or 800
except Exception:
    cap = 800
# ★ cap=0 应该直接禁用缓存，而不是"无上限"
if cap <= 0:
    return Response(content=payload, media_type=mime, ...)
with _cover_lock:
    _cover_cache[ck] = (payload, mime)
    _cover_cache.move_to_end(ck)
    while len(_cover_cache) > cap:
        _cover_cache.popitem(last=False)
```

---

### C3. `in_source_rel` 路径穿越检查可被 junction 绕过

**文件**: `app/deps.py:655-684`

**问题**:
```python
def in_source_rel(src, raw: str, what: str = "路径") -> str:
    # ...
    if is_abs:
        if not root_n or not P.inside_root(root_n, p):
            raise HTTPException(400, f"{what}超出数据源范围")
        # ★ 有 inside_root_real 检查
        if not P.inside_root_real(root_n, p):
            raise HTTPException(400, f"{what}经过目录联接指向了数据源之外")
        # ...
    if P.has_dotdot(p):  # ★ 相对路径检查 ..
        raise HTTPException(400, f"{what}不允许包含 ..")
    return p.strip("/")  # ★ 但没有对相对路径做 inside_root_real
```

**绕过路径**:
1. 在源根内创建 junction: `Y:\music\evil_link` → `C:\Windows\System32`
2. 请求 `/api/files/list?sid=Y&path=evil_link/drivers/etc/hosts`
3. `path` 是相对路径，没有 `..`，通过 `has_dotdot` 检查
4. `inside_root(Y:\music, Y:\music\evil_link\...)` → True（字符串前缀匹配）
5. 但 `inside_root_real` 未调用，实际读取到 `C:\Windows\System32\drivers\etc\hosts`

**修复**:
```python
def in_source_rel(src, raw: str, what: str = "路径") -> str:
    # ...
    if is_abs:
        if not root_n or not P.inside_root(root_n, p):
            raise HTTPException(400, f"{what}超出数据源范围")
        if not P.inside_root_real(root_n, p):
            raise HTTPException(400, f"{what}经过目录联接指向了数据源之外")
        # ...
    if P.has_dotdot(p):
        raise HTTPException(400, f"{what}不允许包含 ..")
    # ★ 相对路径也必须检查 junction
    abs_path = src.abs_path(p)
    if not P.inside_root_real(root_n, abs_path):
        raise HTTPException(400, f"{what}经过目录联接指向了数据源之外")
    return p.strip("/")
```

---

## High 级（严重但可缓解）

### H1. `_reindex_after_transfer` 根目录刷新逻辑缺陷

**文件**: `app/deps.py:548-571`

**问题**:
```python
dirs = {str(d).strip("/") for d in (dst_dirs or [])}
for d in sorted(x for x in dirs if x)[:50]:  # ★ 过滤掉空串
    try:
        S.scan_source(dst_id, manager.get(dst_id), db(), subpath=d, ...)
    # ...
if "" in dirs and dst_files:  # ★ 根目录走这里
    try:
        refresh_tracks(dst_id, [p for p in dst_files if p])
```

**缺陷**:
1. `dst_files` 参数**可能为 None**（调用方 `ops._sync_index` 在 transfer 操作时只传 `dst_dirs`）
2. `"" in dirs and dst_files` 条件：`dst_files=None` 时条件为假，根目录不刷新
3. 传输到根目录的文件永远搜不到

**修复**:
```python
dirs = {str(d).strip("/") for d in (dst_dirs or [])}
# ★ 根目录单独处理：要么有文件列表就精确刷新，要么整个根扫一遍
if "" in dirs:
    if dst_files:
        try:
            refresh_tracks(dst_id, [p for p in dst_files if p])
        except Exception as exc:
            log.warning("根目录刷新索引失败: %s", exc)
    else:
        # 没有文件列表 → 扫描整个根目录（但不递归）
        try:
            S.scan_source(dst_id, manager.get(dst_id), db(), subpath="", max_files=500)
        except Exception as exc:
            log.warning("根目录重扫失败: %s", exc)
    dirs.discard("")  # 已处理，从待扫列表移除
# 处理子目录
for d in sorted(x for x in dirs if x)[:50]:
    # ...
```

---

### H2. `transfer_one` 校验失败后隔离目标时未加锁

**文件**: `app/core/transfer.py:168-174, 180-186`

**问题**:
```python
if src_stat.size and got.size and int(got.size) != int(src_stat.size):
    _bad = _quarantine(dst, dst_path)  # ★ 多线程可能同时调用
    return {**out, "state": "error", ...}
```

`_quarantine` 函数:
```python
def _quarantine(src, path: str) -> str:
    import time as _t
    tgt = f"{path}.bad-{int(_t.time())}"
    try:
        src.move(path, tgt)  # ★ 无锁，同一秒多次失败会覆盖
        return tgt
```

**后果**: 多个文件同时校验失败时，`.bad-<同一秒>` 名字冲突，后一个覆盖前一个。

**修复**:
```python
import threading
_quarantine_lock = threading.Lock()

def _quarantine(src, path: str) -> str:
    import time as _t
    import uuid
    # ★ 用 uuid 替代时间戳避免冲突
    tgt = f"{path}.bad-{uuid.uuid4().hex[:8]}"
    with _quarantine_lock:
        try:
            src.move(path, tgt)
            return tgt
        except Exception:
            try:
                src.remove(path, hard=True)
                return ""
            except Exception:
                return ""
```

---

### H3. `scan_source` 封面索引清理逻辑有竞态

**文件**: `app/core/scanner.py:107-117`

**问题**:
```python
if cover_files:
    if not subpath:
        A.clear_cover_index(sid)  # ★ 无锁
    A.register_cover_files(sid, cover_files)
    res.covers = len(cover_files)
elif not subpath:
    A.clear_cover_index(sid)  # ★ 另一处也在清
```

**后果**: 两个扫描任务（一个整库、一个子目录）同时进行时，`clear` 和 `register` 交错执行，导致部分封面丢失。

**修复**: 在 `audio.py` 的封面索引操作外加全局锁。

---

### H4. `artist_conflict` 判定逻辑有盲区

**文件**: `app/core/matching.py:319-341`

**问题**:
```python
ar = m.artist_sim
if ar is None or ar >= ARTIST_VETO_MIN:  # 0.72
    return False
# ...
if not _distinguishing_tokens(na, nb) and P.similarity(na, nb) < 0.95:
    return False  # ★ 放行
```

**盲区**: 
- 艺术家相似度 0.55~0.72（低于 0.72 但高于随机）
- 文件名高度相似（≥0.95）
- 但**差异词为空**（完全同名）

**实例**: `Song.mp3`（艺术家 A，相似度 0.636）与 `Song.mp3`（艺术家 B）→ 误判为重复。

**根因**: 原约束 2.12 "艺术家相似度低于 0.72" 改成了"低于 0.55"，但这里仍是 0.72，导致 0.55~0.72 成为盲区。

**修复**: 阈值统一到 0.55，或明确文档说明 0.55~0.72 是"可能不同但放行"的灰度区。

---

### H5. `job._owned_here` 标记用闭包变量，外层修改不生效

**文件**: `app/core/scanner.py:66-69`

**问题**:
```python
job = job or get_jobs().create("scan")
job._owned_here = created_job  # ★ 动态属性
```

**风险**: 
- `ensure_hashes` 等嵌套函数会调用 `finish(job)`
- 但 `created_job` 是局部变量，闭包捕获的是初始值
- 外层修改 `job._owned_here` 后，闭包内仍是旧值

**修复**: 用 `getattr(job, '_owned_here', True)` 读取，而非闭包变量。

---

### H6. `LocalSource.move` 跨盘退化逻辑没有原子性保护

**文件**: `app/sources/local.py:159-179`

**问题**:
```python
try:
    os.replace(src, dst)
    return
except OSError as exc:
    if exc.errno != errno.EXDEV:
        raise
# ★ 跨盘复制：无锁，多线程可能同时写 tmp
size_before = os.path.getsize(src)
tmp = dst + ".mshpart"
try:
    shutil.copyfile(src, tmp)
    size_after = os.path.getsize(tmp)
    if size_before != size_after:
        raise SourceError(...)
    os.replace(tmp, dst)  # ★ 这一步是原子的
except Exception:
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise
```

**后果**: 两个线程同时跨盘移动到同一目标时，`.mshpart` 名字冲突，后一个覆盖前一个的写入。

**修复**: `.mshpart` 后缀加上线程 ID 或 UUID。

---

### H7. `_browse_cache_clear` 未在所有写操作后调用

**搜索结果**: 只在以下位置调用：
- `deps.py:548` (`_reindex_after_transfer`)
- `deps.py:592` (`refresh_tracks`)

**缺失**:
- 删除文件/目录后（`ops._do_remove`）
- 重命名后（`ops._do_rename`）
- 移动后（`ops._do_move`）

**后果**: 写操作后目录聚合缓存仍是旧数据，前端显示的文件数不准。

**修复**: 在 `ops._sync_index` 末尾统一调用 `_browse_cache_clear()`。

---

### H8. `jobs._reap_stale_locked` 落库失败静默吞掉

**文件**: `app/core/jobs.py:148-156`

**问题**:
```python
try:
    self.db.update_job(st.id, state=st.state, done=st.done, ...)
except Exception as exc:
    log.warning("陈旧任务落库失败 %s: %s: %s", st.id, type(exc).__name__, exc)
```

**后果**: 心跳中断的任务被判定为中断，内存状态改了，但 DB 仍是 `running` → 重启后恢复出来还是 running，永远卡住。

**修复**: 
1. 落库失败时把任务从内存注册表也移除
2. 或者至少设置一个标记 `st._db_sync_failed = True`，finish 时重试

---

## Medium 级（需要修复但不紧急）

### M1. `files_transfer` 同名检查区分大小写，Windows 会漏判

**文件**: `app/routes/files.py:109-117`

**问题**:
```python
_names = [P.basename(x) for x in items]
_keys = [n.lower() if os.name == "nt" else n for n in _names]
_dup = sorted({n for n, k in zip(_names, _keys) if _keys.count(k) > 1})
if _dup and str(payload.get("policy") or "skip") == "overwrite":
    raise HTTPException(400, ...)
```

**逻辑正确，但有改进空间**: 
- 展示给用户的名字 `_dup` 是原始大小写
- 但检测逻辑已经做了大小写归一（Windows）
- 建议展示时也归一，避免用户困惑"看着不同但被拒绝了"

---

### M2. `duration_similarity` 的尾巴计算有除零风险

**文件**: `app/core/matching.py:92-103`

**问题**:
```python
tail = max(28.0, 4.0 * tol)
score = max(0.0, 1.0 - (diff - gate) / tail)
```

**风险**: `tol=0` 时 `tail=28`，`gate=0`，`diff > 0` 时 `score` 计算正常。但如果后续改动让 `tail=0`，就会除零。

**修复**: `tail = max(28.0, 4.0 * tol, 1e-9)`

---

### M3. `Executor._reserve_targets` 只检查相对路径键冲突，跨源绝对路径冲突检测不到

**文件**: 根据 `AGENTS.md` 约束 2.2，执行器内部用相对路径做键。

**潜在问题**: 两个源的相对路径相同（如 `music/a.mp3`）但绝对路径不同，执行器认为是冲突。**这是设计决策，不是 bug**，但需要确认预期行为。

**建议**: 在文档里明确说明"跨源时相对路径相同也算冲突"。

---

### M4. `_hash_groups` 的 `mode` 参数未验证

**文件**: `app/deps.py:236-282`

**问题**: `mode` 参数直接传给 `S.ensure_hashes`，但没有验证是否为有效值（`quick` / `full`）。

**修复**: 
```python
if mode not in ("quick", "full"):
    raise ValueError(f"Invalid hash mode: {mode}")
```

---

### M5. `dup_key` 的 `bucket_step` 参数类型转换可能静默失败

**文件**: `app/core/matching.py:39-46`

**问题**:
```python
try:
    step = float(bucket_step)
except (TypeError, ValueError):
    step = 2.0
if step <= 0:
    step = 2.0
```

**风险**: 传入非法值（如字符串 `"abc"`）会静默回退到 2.0，调用方不知道参数被忽略了。

**修复**: 抛出异常或记日志。

---

### M6. `version_tags` 接受 `*texts` 但内部拼接可能产生误判

**文件**: `app/core/paths.py:166-190`

**问题**:
```python
hay = " ".join(str(t) for t in texts if t)
```

**风险**: `version_tags("Song", "(Remix)", "Live.mp3")` 和 `version_tags("Song (Remix) Live.mp3")` 产生相同的 `hay`，但语义可能不同。

**建议**: 文档明确说明调用方应传 `(name, title, rel_path)` 三个独立字段。

---

### M7. `name_key` 的 `_DUP_COUNTER` 正则可能误删非计数括号

**文件**: `app/core/paths.py:156-158`

**正则**: 
```python
_DUP_COUNTER = re.compile(
    r"(?<=[^\s\(\[\-–—_])\s*[\(\[]\s*(?:copy\s*)?\d{1,2}\s*[\)\]]\s*$", re.I)
```

**边界情况**: `Song Title (12).mp3` → `Song Title` ✓  
但 `Song (12 Bars).mp3` → 也会被剥掉 `(12 Bars)` ✗

**建议**: 限制匹配"纯数字"（已经是 `\d{1,2}`），但 `copy` 分支可能匹配 `(copy 12 bars)`。

---

### M8. `LocalSource._native` 对 NUL 字节检查正确，但日志缺失

**文件**: `app/sources/local.py:92-98`

**当前**:
```python
if "\x00" in p:
    raise SourceError("路径里不允许包含空字符（NUL）")
```

**建议**: 加日志 `log.warning("检测到 NUL 注入尝试: %r", p)` 以便审计。

---

### M9. `_folder_map` 缓存键未考虑 `root` 变化

**文件**: `app/deps.py:383-387`

**问题**:
```python
_ck = ("folders", sid, root_n)
```

**风险**: 同一 `sid` 但 `root` 不同时（理论上不应发生，但配置被改后可能出现），缓存键仍相同，返回错误数据。

**建议**: 在 `_source_view` 或启动时做一次"sid → root 映射"验证。

---

### M10. `artist_similarity` 返回 `Optional[float]`，但调用方未处理 `None`

**文件**: `app/core/matching.py:311-316`

**调用方**: `score_pair` 等函数假设返回值非 `None`。

**建议**: 统一返回 `0.5`（中性分）而非 `None`。

---

### M11. `_is_reparse` 只在 Windows 有效，Linux junction 未覆盖

**文件**: `app/sources/local.py:33-41`

**问题**: Linux 的 bind mount / symlink 没有对应检查。

**建议**: `os.path.islink()` 结合 `os.readlink()` 判断指向是否在根内。

---

### M12. `proclock._pid_alive` Windows 实现有漏洞

**文件**: `app/core/proclock.py:52-64`

**问题**:
```python
h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
if not h:
    return False
ctypes.windll.kernel32.CloseHandle(h)
return True
```

**风险**: `OpenProcess` 成功只说明 PID 存在，不代表是**同一个进程**（PID 可被复用）。

**修复**: 存储创建时间戳，结合 `GetProcessTimes` 验证。

---

## 修复优先级建议

1. **立即修复（本周内）**: C1, C2, C3
2. **高优先级（2 周内）**: H1-H4
3. **中优先级（1 月内）**: H5-H8, M1-M6
4. **低优先级（文档化）**: M7-M12

---

## 审查覆盖范围

已审查核心文件（按风险排序）:
1. ✅ `app/core/ops.py` - 执行器（最关键，已深度审查）
2. ✅ `app/core/transfer.py` - 跨源传输
3. ✅ `app/core/jobs.py` - 任务管理与独占位
4. ✅ `app/core/proclock.py` - 跨进程锁
5. ✅ `app/deps.py` - 共享运行时状态
6. ✅ `app/routes/files.py` - 文件操作端点
7. ✅ `app/core/paths.py` - 路径工具
8. ✅ `app/core/scanner.py` - 扫描入库
9. ✅ `app/sources/local.py` - 本地文件系统
10. ✅ `app/core/matching.py` - 匹配算法

未深度审查（建议后续）:
- `app/core/plan.py` - 方案生成
- `app/routes/compare.py` - 对比端点
- `app/routes/system.py` - 系统端点
- `app/core/audio.py` - 元数据解析
- `app/core/db.py` - 数据库操作

---

## 验证清单

修复后必须通过：
```bash
# 1. 单元测试（必须全绿）
python -m unittest discover -s tests -t .

# 2. 前端检查
python tools/check_frontend.py
python tools/audit_frontend.py

# 3. 冒烟测试
python -m app.main &
python tools/smoke_api.py

# 4. 针对性回归测试
# - C1: 多进程恢复任务测试
# - C2: 封面缓存压力测试（1000+ 请求）
# - C3: junction 穿越测试（需 Windows 管理员权限）
# - H1: 根目录传输 + 索引验证
# - H2: 并发校验失败测试
```

---

**审查完成时间**: 2026-10-19  
**审查者**: Claude Opus 5 (子智能体)  
**下次审查建议**: 3 个月后或重大功能变更时
