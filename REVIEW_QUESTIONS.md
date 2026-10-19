# 给审查者的具体问题（REVIEW_QUESTIONS · R2 后）

> 生成时间：2026-10-18　对应快照：本审查包
> 前情：`BUG_REPORT.md`（14 项）修了 12 项；`BUG_REPORT_R2`（12 项 + B1/B2）修了 10 项 + B1。
> 全量测试 **887 全过**。下面是我**这轮没敢动 / 动了但心虚**的地方。

---

## A. 上一轮你已裁决、但我这轮仍未落地的（请确认优先级）

### A1. `block_by_duration`（报告 A1：语义(1) + 模糊阶段硬门禁）

- **现状**：仍是"签名里有、函数体不读"。
- **我没做的原因**：它改变**所有跨库配对结果**，必须配回归用例；而且"模糊阶段"的具体位置
  需要你指到行（`pair_sources` 里有两处候选池：`_tokens` 精确配对之后、`score_pair` 之前）。
- **请给**：一句可执行的改法（在哪一行、加什么条件），我照着改并补测试。

### A2. `keep_flat_below`（报告 A2：废弃，映射到 `small_policy`）

- **现状**：`plan_by_album(keep_flat_below=True)` → 传到 `_plan_by_album_impl` → 无人读。
- **我没做的原因**：报告说"若显式传 `keep_flat_below=False` 且未指定 `small_policy`，
  等价于 `small_policy='sink'`"。但当前 **API 根本不接收 `small_policy`**
  （`/api/organize/album-plan` 的入参里没有），所以这个映射没有落点。
- **请判断**：是先给 API 加 `small_policy` 参数（前端也要加控件），还是**直接把
  `keep_flat_below` 从 API/前端删掉**？我更倾向前者（保留能力），但要你确认产品语义。

---

## B. 我这轮改成这样，请复核

### B1. `JobState.lock_token`（BUG-17 修复）

- 改动：`create(exclusive=True)` 时 `st.lock_token = proclock.acquire()`；
  `finish()` 只释放 `st.lock_token` 并清空。
- **自己想到的疑点**：
  1. `proclock.acquire()` 是**可重入**的（同一进程第二次调用直接返回已有 `_token`）。
     如果进程内先后有两个 exclusive 任务（前一个已 finish 并清空 token），
     第二个拿到的 token 与前一个相同 —— 这没问题吧？
  2. `finish()` 里我把 `st.lock_token = ""` 放在 `proclock.release()` **之后**；
     如果 release 抛异常，token 不会被清 → 下次 finish 会再 release 一次。
     要不要改成 `finally` 里清空？
  3. `_release_at_exit()` 用全局 `_token`，和 per-job token 并存 —— 有没有可能
     "job A 的 token 被 atexit 用掉、job B 的锁被误删"？（理论上进程退出时全删也无害）

### B2. 上传入库的 overwrite 备份（BUG-21）

- **位置**：`upload_api.py::_move_local_to_source`，在 `target = dst_abs` 之后：
  ```python
  if policy == "overwrite" and os.path.exists(target):
      try: src.remove(src.rel_path(target), hard=False)
      except Exception: pass
  ```
- **请重点看**：
  1. 这里 `target` 在后续逻辑里**可能被改名**（rename 策略会加 `(2)`），
     但 overwrite 不会改名 —— 我把条件限定在 `policy=="overwrite"`，对吗？
  2. `except Exception: pass` 会不会掩盖"备份失败"？我认为应该**报错中止**
     （宁可不覆盖，也不能无备份覆盖），但那样会改变现有测试预期。请裁决。

### B3. `_sync_index` 改成绝对路径（BUG-16）

- 改法：`src.abs_path()` 归一；`WHERE source_id=? AND (path=? OR path=?)`
  同时匹配"绝对"和"调用方原始值"；写回用绝对路径 + 重算 `rel_path`。
- **请重点看**：`src.abs_path()` 对**已经越界的路径**（`../x`）会返回什么？
  会不会把一条本应匹配不到的步骤"强行归一"到库内、误改别的行？
  （我在 `_do_*` 里已有 `_guard_inside`，但 `_sync_index` 是执行之后跑的，没有守卫）

### B4. `trash_dir` 加锁（BUG-23）

- 改动：`with _trash_lock:` 包住"找空位 + shutil.move"。
- **请重点看**：`_trash_lock` 只是**进程内** `threading.Lock`。多进程（两个实例共享数据目录）
  同时删同名目录时，`shutil.move` 仍可能把源塞进已存在的目标目录。
  要不要在这里加 `proclock` 级别的检查，或者改用"先 `os.rename` 到唯一临时名再加锁判存在"？

---

## C. 这轮我**回退**的改动（请裁决怎么正确做）

### C1. BUG-25：indexwatch 登记独占 Job —— 我试了，失败，已回退

- **现象**：在 `tick_once` 里 `get_jobs().create("scan", exclusive=True)` 之后，
  `scan_source` 内部**自己又** `create("scan")` → 直接 `JobBusy`：
  后台线程自己把自己挡住，3 项 indexwatch 测试全红。
- **我现在的状态**：回退到"只查 `get_jobs().busy()`"，CHANGELOG 里写明未做。
- **请裁决**：正确做法是下面哪一种？
  1. 给 `scan_source` 加 `job` 参数，indexwatch 传自己的 job（但 `scan_source`
     结束时会 `finish(job)` 释放锁 —— 多目录扫描时锁会中途被放掉）；
  2. 给 `scan_source` 加 `own_job: bool = True`，indexwatch 传 `False`
     （不创建、不 finish，由调用方管理）；
  3. 引入"子任务"概念（job 可以挂 child），在 `jobs.py` 里做；
  4. 干脆让 indexwatch 只做**只读探测**，把"真正扫描"作为一条待办丢给前台
     （用户下次打开页面时提示"有 N 个目录需要重扫"）。
- 我个人倾向 2（改动最小、语义清楚），但想知道会不会破坏"任务可恢复/可暂停"那套。

### C2. 我改了一个既有测试的**窗口**（后来按 B1 改成 `fn_body`）

- `tests/test_pause_recover.py`：`self.J[i:i+3000]` → `fn_body(self.J, "finish")`。
- 这轮已经是按报告 B1 整改的版本。请确认这就是你要的形态（不再是字符窗口）。

---

## D. 我怀疑但没复现的

### D1. `_sync_index` 之后 `_browse_cache_clear()` 的时机

- 现在是在 `_sync_index` 之后、汇总结果之前清缓存。
- **疑点**：`refresh_tracks()`（copy/transfer 分支调用）内部也会清一次；
  两次清理没问题，但 `refresh_tracks` 是**同步读文件**，在 `_sync_index` 里
  对每个 copy 步骤都跑一次 —— 批量复制 500 首时会多出 500 次 stat+解析。
  要不要改成"收集完再批量刷新"？（性能 vs 复杂度）

### D2. `raw:` 分桶加了时长/作者后，是否还有误并

- 新键：`raw:{stem}|{int(duration//5)}|{artist_key or '-'}`。
- **疑点**：两首**同专辑、同曲目号、时长都取整到同一个 5 秒桶**的无标签歌
  （比如同一张 OST 的 `01.flac` 和 `01 (1).flac` 副本）**应该**合并 —— 新键能合上；
  但如果是"两个不同专辑都叫 `01.flac`、时长恰好都在 300~305 秒、都没有作者标签"
  → 新键会把它们合并，然后建议删除一份。这个残余风险可接受吗？
  还是应该再加"父目录名"进键（但那样同专辑副本就合不上了）？

---

## E. 复现与验证入口

```bash
python -m unittest discover -s tests -t .        # 应 887 全过
python tools/check_frontend.py
python tools/audit_frontend.py
python tools/audit_split.py

# 关键回归
python -m unittest tests.test_indexwatch tests.test_audit_20261018 \
                   tests.test_audit_r9 tests.test_delete_consistency \
                   tests.test_index_sync tests.test_trash_restore \
                   tests.test_pause_recover tests.test_api_contract_round10
```

---

## F. 请优先回答的 5 个问题

1. **C1**：BUG-25 该用哪种做法（子任务 / own_job / 只读探测）？
2. **A2**：`keep_flat_below` 是"加 `small_policy` 参数"还是"直接删掉"？
3. **B2**：上传覆盖前备份失败时，应该**中止**还是继续（当前是 `pass`）？
4. **B3**：`_sync_index` 里 `abs_path()` 对越界路径的行为，是否需要先 `_guard_inside`？
5. **B4**：`trash_dir` 的跨进程竞态要不要处理，怎么处理？
