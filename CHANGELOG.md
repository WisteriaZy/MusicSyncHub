# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]

### 修复：操作记录里"清理不掉的三个任务"（2026-10-18）

**现象**：操作记录里有 3 条 organize 任务（state=error，"已中断（可以尝试接着做完）"），
无论怎么操作都不会消失，重启后还在。

**根因**：崩溃/强杀后 `load_unfinished()` 会把 DB 里 state=running 的记录恢复成
"error + 可接着做完"，但界面上**只有「接着做完」，没有忽略/删除**。DB 行仍是
running/paused，所以每次启动都会被重新读出来。

**修复**：
- `Jobs.dismiss(jid)`：从内存 + DB 删除（运行中的任务拒绝）；
- `Jobs.dismiss_finished()`：一键清所有已结束任务；
- 新接口 `POST /api/jobs/{jid}/dismiss`、`POST /api/jobs/clear-finished`；
- 操作记录页：每行末加「忽略」按钮（已结束任务才显示），顶部加「清理已结束任务」。

**顺带修的真问题**：`proclock` 只按 mtime(6h) 判陈旧锁，**不看持有进程是否还活着** ——
进程被强杀后锁文件还很新，下一个实例（或测试）会被挡 6 小时。已改为按 PID 判活
（Windows 用 `OpenProcess`，POSIX 用 `kill(pid,0)`），主人已死则直接接管。

**已用它清掉那 3 条记录**：清理前 3 条 → `{"removed": 3}` → 清理后 0 条。

### 修复：BUG_REPORT_R3（10 项 + A1/A2 接线，2026-10-18）

| 编号 | 问题 | 修法 |
| --- | --- | --- |
| BUG-25 | indexwatch 与 scan_source 的 Job 所有权冲突 | `scan_source` 记录 `created_job`，外部传入的 job 不越权 `finish`；indexwatch 巡检期间持 exclusive job，`finally` 释放；扫描路由改为自己收尾 job |
| BUG-27 | `folders_api` 删除目录用 `LIKE` 前缀，`_` 通配误删平行目录索引 | 改 `SUBSTR(path,1,?) = ?` |
| BUG-28 | 数据源离线时 `manager.get` 返回 None → `src.stat` 500 | 4 处补 `src is None` → 404 |
| BUG-29 | 回收站 rename 还原把时间戳文件名固化进曲库 | `_free_name` 基准改 `P.basename(target_rel)` |
| BUG-30 | `min(0.62, name_similarity)` 反向截断，严格设置失效 | 显式 `match_threshold` 优先，否则用配置值 |
| BUG-31 | `parse_filename` 用裸 `-`/`_` 切分，`T-Square` 被拆成 T / Square | 只保留带空格的分隔符 |
| BUG-32 | 断点续传只比大小就删源 | `resumed_from > 0` 时**强制全量哈希**，不一致则隔离目标、不删源 |
| BUG-33 | `tags_api._resolve` 拒收相对路径、手写 startswith 判越界 | 改 `src.abs_path` + `P.inside_root` |
| F5/B4 | `trash_dir` 仅进程内锁，跨进程 `shutil.move` 会嵌套 | 改 `os.rename` 冲突重试（原子） |
| F3/B2 | 上传覆盖前备份失败被 `pass` 吞掉 | 改为抛 `IOError` 中止 |
| A1 | `block_by_duration` 零消费 | 关闭时把容差放大到无穷（不分桶、不惩罚时长） |
| A2 | `keep_flat_below` 零消费 | 新增 `_small_policy()`：`small_policy` 优先，`keep_flat_below=False` 映射为 `sink` |

**事故记录（必须记下来）**：本轮给 `trash_dir` 打 `os.rename` 补丁时，正则跨过了
`remove()` 一直匹配到 `trash_dir`，**把中间的 `trash_restore`/`trash_list` 等函数整段删掉了**。
已从 `R2修复后备份-223430` 恢复 `app/sources/local.py` 并重新精确打补丁。
教训：正则补丁必须绑定**函数边界**（`t.index("def x")` 到下一个 `def`），
不能图省事用 `.*?` 跨函数匹配。

### 修复：BUG_REPORT_R2（10/12 项，2026-10-18）

| 编号 | 问题 | 修法 |
| --- | --- | --- |
| BUG-15 | `rename`/`move` 的 overwrite 直接 os.replace，绕过回收站 | 覆盖前 `src.remove(want, hard=False)` 备份 |
| BUG-16 | `_sync_index` 用相对路径做 WHERE 键（rowcount=0）+ 把相对路径写回 track.path | 统一转绝对路径再读写，WHERE 同时匹配绝对/原值 |
| BUG-17 | `cancel` 后旧线程二次 `finish` 误删新任务的 proclock | `JobState.lock_token`，finish 只用**自己的** token 释放并清空（幂等） |
| BUG-18 | `raw:` 兜底桶只按文件名 → 全库 `01.flac` 并成一组 | 加"时长 5 秒桶 + 艺术家键"隔离 |
| BUG-19 | `station_restore` 对不属于任何数据源的 origin 直接建目录搬文件 | 找不到归属源 → 记 failed 并拒绝 |
| BUG-20 | `folder_fits` 单字母目录子串匹配（`o` in `coldplay`） | 子串匹配要求两侧长度 ≥3，单字符只允许全等 |
| BUG-21 | 上传入库 overwrite 直接 os.replace | **仅在 policy==overwrite 且目标存在时** 先 `src.remove(..., hard=False)` |
| BUG-22 | 整理引擎硬编码 `[:400]` 截断 | 删除两处截断 |
| BUG-23 | `trash_dir` 没加 `_trash_lock` | 找空位 + 搬移整段加锁 |
| BUG-24 | `_drop_track` 用 `LIKE base+"/%"`，`_` 是通配符 | 改 `SUBSTR(path,1,?) = ?` |
| B1 | 测试用 3000 字符窗口 | 改用 `fn_body(self.J, "finish")` |

**未完成**：
- **BUG-25**（indexwatch 登记独占 Job）：尝试过 `create("scan", exclusive=True)`，
  但 `scan_source` 内部还会 `create("scan")` → 自己阻塞自己（全量测试 3 项失败）。
  正确做法是把 job 传进 `scan_source`（改签名）或给扫描加"子任务"概念，
  这需要重构任务所有权，本轮先回退到"只查 busy()"。
- **BUG-26 / BUG-12 剩余**：`block_by_duration`、`keep_flat_below` 两个零消费旋钮
  （报告 A1/A2 已给语义建议，但实现需配套回归用例）。
- **B2**：`deps.DB is self.db` 改为回调注入（报告建议），本轮未做。

### 修复：BUG_REPORT 第二轮（BUG-06/10/11/13/14 + BUG-12 部分，2026-10-18）

| 编号 | 问题 | 修法 |
| --- | --- | --- |
| BUG-06 | `plan_relocate` 归拢段把 keep 又搬一遍（keep+move > total），且把收纳区当来源外搬 | ① `accept_move` 真搬时**从 keep 剔除**（守恒）；② 删掉"收纳区里的歌当来源"的分支（收纳区一律不动） |
| BUG-10 | 无损格式标签保存两次 `f.save()` | 删掉 `_apply` 里两处内层 `save()`，统一由 `write_tags` 外层 `if not isinstance(f, ID3)` 收口 |
| BUG-11 | `artist_conflict` 0.55~0.72 盲区放行 | `ARTIST_VETO_MIN` 0.55 → 0.72，与"确认同一人"的阈值对齐 |
| BUG-13 | `_reserve_targets` 白名单漏 `link` + 重复 `_tally` | 补 `link`（它底层按 copy 执行，会写目标）；删死赋值 |
| BUG-14 | `rmtree(ignore_errors)` 静默失败后仍删索引 | 删除后复查目录是否还在；还在就 400 并保留索引 |
| BUG-12（部分） | `name_similarity` 读了不用 | 作为**配对阈值上限**：`min(match_threshold, name_similarity)`；默认 0.86 时行为不变 |

**BUG-06 的一次自我纠正**：第一版把 keep 从 `placed` 里排除，结果"散在多处的同专辑合并"整条
功能被掐死（6 首全被判 keep → 0 合并）。守恒应该靠"搬走时从 keep 剔除"，而不是禁止合并 ——
已回退，`test_gather_merges_scattered_album` 保持通过。

**仍未做（需要设计决策，不是修 bug）**：
- BUG-12 剩下两个旋钮：`block_by_duration`（配对时是否按时长分块）与
  `keep_flat_below`（小分组是否平铺）。两者要真正的语义设计与回归用例，
  目前仍在签名里但无效果 —— 建议要么实现、要么从界面/API 移除。

### 修复：BUG_REPORT 第一轮（P0×2 + P1×4 + P2×2，2026-10-18）

按 `BUG_REPORT.md` 的执行顺序修，**不做未验证的改动**。

| 编号 | 问题 | 修法 |
| --- | --- | --- |
| BUG-02 | `indexwatch` 里 `get_jobs().busy` 漏写括号 → 后台刷新 100% 瘫痪 | 改为 `busy()`；测试的 Mock 也改成方法（原来用字典属性，正好掩盖了这个 bug），并加源码断言 |
| BUG-01 | 终止任务被 resume 复活成幽灵任务，独占写锁永久泄漏 | ① `finish()`：工作量做完（`done>=total`）即使被暂停过也记 `done`；② `resume()`：`finished` 有值（线程已退出）拒绝改回 running；③ `cancel()`：暂停中/线程已退出时直接 `finish()` 收尾 |
| BUG-03 | `_do_transfer` 硬编码 `policy="skip"`/`verify=None`/`hard_delete=False` | 签名接收四个参数，`_apply` 透传；用户选的覆盖/改名/哈希校验/永久删源不再被丢弃 |
| BUG-04 | transfer 成功后只删源索引，目标库"搬丢了" | transfer 分支补目标侧 `refresh_tracks`（注释一直写着"同 copy"，代码漏了） |
| BUG-05 | 对比页严格度阈值与文案反向（选"严格"实际 0.5 最易误配） | 改为 0.55 宽松 / 0.65 标准 / 0.75 严格，默认 0.65 |
| BUG-07 | `find_sinks` 用绝对路径比相对路径键 → 手动指定收纳区全部失效 | 先用 `_rel(root, …)` 转相对路径 |
| BUG-08 | 切源残留旧源 `treePrefix` → 新源被过滤成 0 首 | `selectSource` 清 `State.treePrefix` / `libFilters.prefix` |
| BUG-09 | `listdir` 漏捕 `NotADirectoryError` → 请求文件路径 500 | `except (FileNotFoundError, NotADirectoryError)`，报错文案改成"不存在或不是目录" |

**踩坑记录（两个都是同源）**：
1. BUG-04 的第一版补丁插到了**trash 分支**（两处都有 `_drop_track`），已挪回 transfer 分支；
2. 补目标索引时直接调全局 `deps.refresh_tracks`，在单测（本地 `Database`）里会跨测试干扰 —— 单独跑过、全量挂。改成**只在 `deps.DB is self.db`（生产接线）时刷新**。

**未完成（下一轮）**：BUG-06（`plan_relocate` 守恒 + 收纳区外搬）、
BUG-10（双 `f.save()`）、BUG-11（`artist_conflict` 0.55–0.72 盲区）、
BUG-12（`name_similarity` / `keep_flat_below` / `block_by_duration` 零消费）、
BUG-13（`link` 未进并发白名单 + 重复 `_tally`）、BUG-14（`rmtree(ignore_errors)` 后无条件删索引）。

### 新机制：索引自动刷新（2026-10-18）

以前索引**只在手动点扫描时更新**：外部程序改了库，界面会一直说谎 ——
删了还看得见、移走了播放失败、数字对不上。这是系统性缺陷，不是 bug。

新增 `app/core/indexwatch.py`，三个机制：

1. **目录 mtime 探测**：新增/删除/改名会改父目录 mtime → 只重扫那个子目录
2. **文件指纹探测**：内容被替换（大小/mtime 变）→ 同样重扫所在目录
3. **陈旧兜底**：`last_scan` 超过 24 小时 → 整源扫一次，兜住"新目录里的新文件"

约束：
- **有写任务时让路**（先看 `get_jobs().busy`），不与手动操作抢
- **首次运行只播种**目录 mtime，不在启动时全库重扫
- 单轮最多重扫 8 个目录，剩下的下一轮继续
- 扫描走同一个 `scan_source`，写入/清缓存/清失效行的规则与手动扫描一致

配置（`config.toml`，默认值即下面的数）：
`auto_index_refresh_seconds = 300`（0=关闭）、`auto_full_rescan_hours = 24`（0=不兜底）。

另外：**`copy` 成功后立刻把目标行索引出来**（原来要等下次扫描，
目标库在界面上会"少一首"）。

测试：`tests/test_indexwatch.py`（5 项）—— 内容变更、新增、删除、
忙碌让路、状态上报。

### 修复：删除后幽灵文件 / 误报失败 / 计数不更新（2026-10-18）

**用户反馈**：数据源索引更新不及时；删除后文件浏览里出现幽灵文件（有时看得见
有时看不见）；删除提示失败（尽管已经删除）。

三件事是一条链：

1. **幽灵文件** —— `/api/files/list` 有 20 秒目录聚合缓存，而执行器删完
   没有清缓存。修：`Executor.run` 结束时清 `_browse_cache_clear()`
   （延迟导入避免 ops ←→ deps 循环依赖），`refresh_tracks` 也一并清。
2. **误报失败** —— 列表勾选 + 目录树勾选会让同一路径**重复**进批次；执行器
   是多线程的，两条步骤同时通过存在性检查，后到的那条抛 `FileNotFoundError`
   被记成 error。修：删除改为**幂等**（前置 stat + 捕获
   `FileNotFoundError/NotADirectoryError` → `skipped`），UI 侧按路径去重。
3. **计数不更新** —— 删除后前端只 `load()`，没刷数据源曲目数。
   修：删除成功后 `App.loadSources()` + `renderSidebar()`，toast 区分
   "已移入 / 跳过（已不存在）/ 失败"。

**测试**：新增 `tests/test_delete_consistency.py`（3 项）：
删完立刻查列表不能有幽灵、重复步骤不能算失败、计数必须当场减少。

### 修复：第二轮外部审查 9 条（含 4 条 HIGH）（2026-10-18）

审查包 `135752` 的 9 条全部核实属实，逐条修复：

| # | 严重度 | 问题 | 修法 |
| --- | --- | --- | --- |
| 1 | 高 | `_do_copy` 缺 `policy=rename` 的并发兜底 → 撞名时**静默覆盖** | `_had_old` 分支补 rename → 报错不覆盖 |
| 2 | 高 | `files_transfer` 同名检测区分大小写 → Windows 上 `a.mp3`/`A.mp3` 可绕过 | 按 `lower()` 归一化判重 |
| 3 | 高 | `station_restore` 在**循环内**抛 403 → 前几首已搬走、索引不刷新 | 循环前按归属源预检写权限 |
| 4 | 高 | `group_duplicates` 空 `name_key` 全落进 `"name:"` 桶 → 无关曲目被判重复 | 为空时退回 `raw:<原名>` 分桶 |
| 5 | 中 | `organize_album_apply` 兜底调用没带 `wait=True` → 必然 400 | 补 `"wait": True` |
| 6 | 中 | `empty_remove` 漏 `_guard_write` → 只读源返回 200 假成功 | 入口补守卫（`empty_scan` 仍不拦） |
| 7 | 中 | 播放器 `onended` + shuffle 的 `ended` 监听器双触发 → 一首播完切两首 | 随机切歌并进 `onended`，删掉监听器 |
| 8 | 中 | `upload_dedup` 直接改 `job.state` → 不落库、无完成时间、重启判陈旧 | 改走 `get_jobs().finish()` |
| 9 | 中 | `set_keep` 纯计算却挂 `exclusive_guard` → 查重切策略被 409 | 移除守卫参数 |

**测试**：新增 `tests/test_audit_r9.py`（9 项）。第 1 条用真 Executor 跑
"认领后目标仍存在"的分支验证不覆盖；其余 8 条用源码特征断言锁住
（分支漏写类问题没有纯函数可测，锁特征是更可靠的做法）。

**过程中自己踩的一个坑**：第 6 条的锚点 `src, _abs0, root = _inside(sid, ".")`
在 `empty_scan` 和 `empty_remove` 里各有一处，第一次替换插到了只读的
`empty_scan` 上（会把"查看空目录"也 403 掉）。已挪回 `empty_remove`，
并在测试里加了一条反向断言。

### 修复：目录树勾选与右侧文件勾选不同步（2026-10-18）

**用户反馈**：
1. 取消上一个文件夹的全选后，点另一个文件夹全选，会把上一个也带上
2. 右侧文件的全选 / 部分全选，与左边目录树不同步

**根因有三层，前面两轮只找到第一层**：

1. `treePicked`（勾了哪些目录）和 `checked`（勾了哪些文件）是两套状态 ——
   已在上一轮让"取消全选"同时清 `treePicked`。
2. **真正的主因是执行顺序**：`syncTreeToFiles` 里先
   `refreshTreeChecks()`、后 `paintTreeFiles()`，而目录索引
   `dirTotal`/`dirOn` 是在后者里才重建的。于是刷新时读的是**上一轮的旧索引**：
   刚勾的目录显示"未选"，刚取消的目录还显示"全选"。
3. 两套算法：`refreshTreeChecks` 读索引、`paintTree` 读 `treePicked`，
   任何一次整树重绘都会把状态倒回去。

**修法**：
- 顺序改为 先 `paintTreeFiles()`（重建索引）→ 再 `refreshTreeChecks()`；
- 两者统一调用 `treeCover()`：树视图下由新增的 `coverFromFiles()`
  按文件实际勾选算（全选/半选/未选），平时按目录勾选算；
- 树视图里"没有任何文件"一律算未选，**不再回退 `treePicked`**；
- 显式勾某个目录时清掉它范围内的 `treeExcluded`（手动取消记录），
  这样"取消过几首 → 再勾这个目录"能恢复全选。

**验证**（真实库、浏览器逐场景）：

```
勾 A                → 274 行  A=全选  B=未选
取消全选            →   0 行  A=未选
勾 B                →   8 行  A=未选  B=全选     ← 不再连带 A
B 里取消 5 个       → 3 已勾  B=半选
再勾 A              → 282 行 277 已勾  A=全选  B=半选（保留手动取消）
取消 A              →   8 行 3 已勾  A=未选  B=半选
全部取消            →   0 行  A=未选  B=未选  ← 回到当前目录
```

**测试**：新增 `TreeSelectionSyncTest`（5 项）覆盖刷新顺序、统一判据、
树视图按文件算、勾目录清排除、取消全选清目录 —— 这块之前**零自动化测试**，
也是它能反复出问题的原因。

### 修复：外部审查 3 条 CRITICAL + 3 条 HIGH（2026-10-18）

外部审查（`AUDIT_REPORT_20261018.md`）在 `20261018` 审查包上发现了 3 条
CRITICAL。**全部属实**，且 CRITICAL 都是我自己上一轮引入的。

**NEW-01（CRITICAL）断点续传元数据写晚了**
- 原实现：`.mshpart.json` 在**整份复制成功之后**才写。
- 真实中断（拔盘/掉网/杀进程）发生在途中 → 执行流走不到那行 →
  盘上只有半截 `.mshpart`、没有 json → `_resume_offset` 读不到 → `wb` 清空重来。
- **结论：续传在现实中从未生效**，之前的测试是靠人工预置 meta 才通过的。
- 修法：meta 在**开始写数据之前**落盘；并新增 `src_mtime` 校验
  （源在中断期间被改过、大小又相同 → 拒绝续传，避免拼出损坏音频）。

**NEW-02（CRITICAL）回收站路径穿越**
- `trash_purge(rel)` / `trash_restore(rel)` 直接 `P.join(trash_root, rel)`，
  没校验 `..` → `{"rel": "../../Music"}` 可 `rmtree` 掉库外任意目录。
- 修法：新增 `_trash_abs()`，先拒绝含 `..` 的路径，再用 `inside_root` 兜底。

**NEW-03（CRITICAL）并发改名占位脱节**
- `_do_move` / `_do_copy` 在 `_claim_target` 之后又调**锁外**的 `_dedupe_name`；
  `_do_rename` 则完全没有认领。并发时两步算出同一个候选名，后写覆盖先写。
- 修法：候选名计算与登记**全部收敛进 `_claim_target` 的锁内**；
  三个 `_do_*` 不再直接调 `_dedupe_name`；`rename` 也接入认领。
  并发撞名时**报错而不是覆盖**。

**NEW-04（HIGH）传到目标根目录后不回填索引**
- `deps.py` 的 `if str(d).strip("/")` 把空串（根目录）当假值滤掉 →
  搬到根目录的文件永远进不了索引。
- 修法：根目录改用 `refresh_tracks` 按文件刷新（不整库重扫）。

**NEW-05（HIGH）中转站归位/还原不刷新索引**
- 文件已回到库目录，曲库却查不到。
- 修法：归位后 `refresh_tracks(sid, paths)`；还原按"路径属于哪个源"分组刷新，
  并补上 `require_writable`（只读源不许往里还原）。

**NEW-06（HIGH）换源后查重结果残留**
- `State.dup` 从不清理 → 切到 B 库打开查重页看到 A 库的分组，
  点"清理"会对 A 库文件动手。
- 修法：`selectSource` 里清空 `State.dup`。

**测试**：新增 `tests/test_audit_20261018.py`（9 项）。
其中续传测试改为**真的在复制途中抛异常**（而非人工预置 meta）——
这正是上一轮测试没能发现 NEW-01 的原因。

**仍未处理**（已知，优先级低，记录于此）：
- `files_transfer` 同名检测区分大小写（Windows 上 `a.mp3`/`A.mp3` 可绕过）
- `compare.py::set_keep` 是纯计算却挂了 `exclusive_guard`（查重切策略会 409）
- `search_api` 的 `collect_apply` / `organize_*_apply` 仍是 `exclusive_guard`（非 slot）
- `folders_api::empty_remove` 缺入口 `_guard_write`（只读源上假成功）
- `group_duplicates` 的 `name:` 兜底桶（旧 A1）
- 播放器 `onended` 与 shuffle 双推进（旧 K15）

### 改进：本层没有文件时给出"歌在子目录里"的提示（2026-10-18）

**用户反馈**："没有全选文件夹的时候显示文件夹内没有文件，全选文件夹后
文件就冒出来了。"

排查后的结论：**不是数据错，是没有任何提示**。

- 根目录（或任何父目录）的「本层文件」本来就是 0 首，
  接口返回 `file_count=0`、`dir_audio_total=2301`；
- 界面只显示 101 个目录行，文件区空着，用户不知道歌在下一层；
- 只有碰巧发现"勾选左边目录"才会看到 2301 首。

现在列表顶部会出现一条提示：

> 这个文件夹本层没有音频文件 —— 它下面有 101 个子目录、共 **2301** 首
> 　[包含子目录查看]

点按钮 = 勾上当前目录（级联子目录）并切到"选中结果"视图，等于一键"全选"。
本层有文件但子目录更多时，提示会变成"本层 N 首 · 子目录里还有 M 首"。

另外：目录树的空结果状态也加了一键出口「取消勾选，看当前目录」。

**顺手修的测试脆弱点**：`test_resilience` 里两处用"固定字符窗口"
（2600 / 3000 字）截取函数体，函数里加一行就会把断言挤出窗口而误报。
已改为按函数体匹配。

### 修复：删除后索引不同步（"移到回收站却删不掉"）+ 多选逻辑统一（2026-10-18）

**用户反馈**：文件浏览里多选文件/多选文件夹操作逻辑混乱；回收站不能用
（移动到回收站却删不了文件）。三个根因叠加：

1. **前端构造的步骤没有 `id`**。`Executor._sync_index` 是按
   `results[].step` 回查原始步骤的 —— 没有 id 就 `continue`，整段跳过。
   于是磁盘上的文件进了回收站，**索引里还留着**：曲库照样显示、数字不变。
2. 即便有 id，`remove_track(sid, src_path)` 也是按**原样**匹配，
   而索引里存的是绝对路径；传相对路径时 DELETE 命中 0 行。
3. 目录删除走的是另一个接口（按前缀清索引），所以"删单个文件"这条
   路径一直没人发现。

**修法**：
- `Executor.run` 给每条步骤补默认 `id`（前端/脚本/旧客户端都不会再踩）；
- 新增 `_drop_track()`：依次尝试绝对 / 源内相对 / 原样路径，都没命中
  再按目录前缀兜底。
- **顺带修好了 rename / move / transfer 的索引同步** —— 它们以前同样
  因为"没有 id"被跳过，只是症状隐蔽（移动后搜索还能搜到旧路径）。

**多选逻辑统一**：
- 取消勾选的文件不再在"再勾一个文件夹"时被重新勾上
  （新增 `treeExcluded`，记住用户的手动取消）；
- 树结果与普通列表的选中条**按钮集对齐**：两边都有
  「整个文件夹 / 集合成文件夹… / 移动到… / 移入回收站 / 跨库传输…」；
- 去掉重复 id：工具条的「回收站」改名 `#fb-recycle`（原来和选中条的
  「移入回收站」撞了三个同名 id）。

**测试**：新增 `tests/test_index_sync.py`（5 项：无 id 步骤、相对路径、
绝对路径、目录删除、跨库传输）；把三态勾选测试从"固定字符窗口"
改为按函数体匹配（加按钮不再误报）。

### 审查包说明重写 + 统计修正（2026-10-18）

`tools/build_packages.py` 里的 `PROJECT_INDEX.md` / `REVIEW_NOTES.md` 模板
**已严重过期**，会误导外部审查者：

- 还写着"`main.py` 约 2500 行" → 实际已拆成 `deps.py` + `routes/*`（约 400 行）
- 还写着"没有 GZip / 没有方案指纹 / 封面缓存不生效" → 全都修了
- 还把它们列在"已知问题（不用再报）"里 → 审查者会以为在审旧版本

已整体重写为当前状态：审查顺序按新文件布局重排、已知问题只列**仍开着的**、
明确区分"设计选择"与"缺陷"、补上回收站与断点续传的审查要点。

**统计口径修正**（之前是错的）：

| 项 | 之前 | 现在 | 原因 |
| --- | --- | --- | --- |
| API 路由 | 1 | **92** | 只数了 `main.py` 的 `@app.`，路由已拆到 `routes/*`，改为跨模块数唯一路径 |
| 冒烟用例 | 67 | **90** | 正则漏了 f-string 形式的用例，改用 `ast` 精确数 `CASES` 元素 |

### 新功能：跨库传输支持断点续传（2026-10-18）

以前传一个几十 GB 的文件，中途断了（网络抖动 / 拔线 / 休眠 / 关程序）
**必须从头再来** —— `.mshpart` 只保证"不污染库"，不保证省时间。

现在：复制中断后，下次对**同一个源文件**再传会从已完成的字节处继续。
关键安全设计：

- 临时文件旁边写一份 `<目标>.mshpart.json`：`{src_id, src_path, src_size}`；
- **三者全等**才续传（否则从头写）—— 防止把"另一个源文件的半截"接上来，
  那种损坏最坏：大小可能正好对得上，听着却是噪音；
- 续传完成后再比一次总大小，不一致就丢弃临时文件；
- 失败时**保留**临时文件（下次能续），超过 24 小时的由启动清理删掉
  （`.mshpart` 和 `.mshpart.json` 一起清）；
- 结果里带 `resumed_from`，界面显示"断点续传：从 N 字节处继续"。

覆盖范围：本地/映射盘之间（`local.copy_from`）、以及**目标为本地**的跨协议传输
（`base.copy_from` + `append_file`）。目标不是本地、或适配器不支持随机读时，
自动退回整份复制（行为与以前一致）。

### 新功能：回收站可以还原了（2026-10-18）

**这是安全闭环上最后一个缺口。** 界面和文档一直写着"删除 = 移入回收站，可恢复"，
但应用里没有任何还原入口 —— 用户得自己打开资源管理器翻 `.music-sync-trash\日期\`，
还得记得文件原来在哪个目录。

- 删除时把**原相对路径**记进回收站目录里的 `_origin.json`
  （跟着回收站走：索引库重建、回收站整体搬走，都还能还原）。
- 文件浏览工具条新增 **「回收站」**：按时间列出，显示原位置/大小/删除时间，
  勾选后 **还原选中** 或 **彻底删除…**。
- 还原默认回**原目录**；同名默认**跳过**，可选"自动改名"，**绝不覆盖**。
- 老回收站没有 `_origin.json` 也能列出并还原（落回库根目录）。
- 彻底删除需要 `confirm=true`（界面还要输入"永久删除"），且走独占位。
- 新接口：`GET /api/trash`、`POST /api/trash/restore`、`POST /api/trash/purge`
  （`app/routes/trash.py`，只支持本地/映射盘源）。

### 启动脚本健壮性（2026-10-18）

- **双击 `start.bat` 时如果服务已在运行，以前会因端口被占用直接闪退**
  （窗口一闪就没，表现就是"打不开"）。现在先探测一次：
  已在运行 → 打印地址并自动打开浏览器；端口被他人占用 → 换端口提示；
  依赖缺失 → `uv sync` 提示；启动失败时窗口会**停住**并显示原因，不再一闪而过。
- 修掉脚本里混用的换行符（26 行裸 LF → 全部 CRLF）—— cmd.exe 会把 LF-only
  行当成命令执行，导致整段脚本报"不是内部或外部命令"。

### 交互修复（2026-10-18）

#### 播放：点一首歌不该把前面的全加进列表

- 曲库行首 ▶ / 双击：**只播这一首**（队列 = 1 首）。以前把"当前列表"整批入队，
  于是播放列表里出现这首歌**之前**的全部曲目。
- 文件浏览 / 曲库 行尾新增 **＋ 加入播放列表**：追加到队列末尾，不打断当前播放。
- 曲库工具栏新增 **「全部入队」**：保留"整列表入队"这条能力，但改成显式操作。
- 专辑视图的「播放」仍是整张专辑入队（语义本来就是"整张"）。

#### 候选区智能分配：改成「选项 → 预览 → 采用」

- 以前 `group_by='artist_album'`、`min_group=5` 写死，点一下就直接把去向全填了 ——
  用户既不知道用了什么规则，也没机会改。
- 现在先弹选项：**分组依据**（作者+专辑 / 仅作者 / 仅专辑）、
  **目录结构**（分层 / 单层）、**最少同组**（1–100）；
  点「预览去向」列出"目录 → 文件数"，确认后才写入。
- 采用只写"去向"（分好颜色），**不移动任何文件**；最后仍由「执行移动」动手。

### 文档（2026-10-18）

- 新增 [`docs/FEATURE_TREE.md`](docs/FEATURE_TREE.md)（功能关系树：页面→功能→接口）
  与 [`docs/LOGIC_TREE.md`](docs/LOGIC_TREE.md)（逻辑关系树：写操作链路、状态位置、不变量）。
- `README.md` 文档索引补上上述两篇 + `AUDIT_ROUNDS.md` + `GITHUB_SETUP.md`。
- `docs/USER_GUIDE.md`：更新播放语义（播放 vs 加入播放列表）与候选区智能分配流程。
- `docs/GITHUB_SETUP.md`：发布前检查清单加入 `tools/audit_split.py`。
- `.github/workflows/tests.yml`：去掉重复的 `check_frontend`，补上 `audit_split`。

### 测试（2026-10-18）

- `tests/test_drive_root_paths.py` 标记为 **Windows 专属**（盘符根只在 Windows 有意义），
  避免 CI 的 ubuntu job 误报。

### 收尾诊断（2026-10-17）—— 修掉 4 条实测确认的数据丢失路径

这一轮做了一次全面诊断（不只查语法，重点查逻辑缺陷），三路并行深审
文件操作/并发、匹配算法、接口与前端。下面按"会不会丢文件"排序。

#### CRITICAL（会静默丢文件）

- **同一方案 ≥3 步写同一目标 → 丢文件且全报成功**
  `Executor._claim_target` 的"选名字"只问磁盘、不问本方案的目标占位，
  而且"选名 + 登记"分三次拿锁。实测 **40 轮丢 39 轮**。
  改成：候选名必须同时满足"磁盘上没有"和"`_planned` 里没人占"，整段在**同一把锁**内。
  复现脚本连跑 20 轮，每轮都落 3 个文件（`x.mp3` / `x (2).mp3` / `x (3).mp3`）。

- **越界检查只保护了 `move` → 库外文件可被永久删除**
  `_inside_root` 原来只有 `move` 在用。Windows 上 `LocalSource.abs_path()`
  对"别的盘的绝对路径"原样返回，于是 `{"op":"delete","src_path":"D:/别处/x"}` 会
  **永久删除库外文件并返回 done**。现在 `delete` / `trash` / `rename` / `copy` /
  跨库传输 / 中转站归位全部过 `paths.inside_root()`。四种越界构造实测全被拒。

- **跨库传输可以把库内文件搬到库外任意目录再删源**
  `files_transfer` 只对 `dst_dir` 做 `norm()`、不拒 `..`，`items` 原样当源路径用。
  接口层与 `transfer_one` 双端都补了根内校验。

- **回收站同名去重只试一次 → 同秒删同名文件会覆盖**
  同一秒删三个 `01 Track.mp3`，第 2 个内容被第 3 个覆盖，**那份永久消失**，
  三次都返回成功。改成循环找空位 + `_trash_lock` 保护"找空位 → 搬入"。

#### HIGH

- **`Jobs.create()` 分两次拿锁** → 两个并发写任务能同时拿到独占位。改为单临界区。
- **`wait_if_paused` 默认 3600 秒超时** → 暂停超过一小时 worker 会**自己接着干**，
  界面仍显示"已暂停"。改成默认不超时（只有取消能打破）。
- **`set_keep` 返回 `path`、接口返回 `did`** → 前端严格比较永远不等，
  用户选的"保留最早那份"被**静默丢弃**，回落到按音质挑，可能删掉用户想留的。
  统一成 `did`，并整段重写该函数。
- **`wasted_bytes` 拿 `path` 和 `did` 比** → 每项都不相等 → 浪费体积变成整组体积，
  连保留那份也算成浪费。改按 `did` 比较。
- **`version_tags` 第三处漏 `rel_path`**（`_duplicates_core` 里的 `_ver_tags`）。
  至此全项目所有调用点都与 `dup_key` 字段集一致，并有测试守着。

#### MEDIUM

- **`ignored_names` 设置项零消费**：界面能填、存进 config.toml、后端从不读。
  现在接到扫描器：目录名命中就整棵子树跳过，文件也过滤；只在整库扫描时清封面索引。
- **封面索引不自失效**：`clear_cover_index()` 写好了却从没被调用，封面文件删掉后
  索引仍指向它。整库扫描时现在会先清。
- **`cover_cache_size` 设置项不生效**：`main._serve_cover_bytes` 硬编码 2000（默认值是 800）。
- **版本类别清单前后端各一份**：后端 14 项、前端 12 项，漏了 `clip` 和 `speed`，
  这两类在查重页显示成英文。改成 `/api/system.version_kinds` 单一来源。
- **`create_source` 没有独占守卫**（而 `update_source` 有）→ 补上。
- **`put_settings` 允许在任务运行时改 `trash_dir`/`station_dir`/`staging_dir`**
  → 同一个任务前后几步会落到不同目录。现在这三个字段在有任务跑时返回 409，
  其它设置照常可改（不会把整个设置页锁死）。
- **测试模式横幅关掉后自己回来**：`loadSystem()` 每 20 秒又把 class 加回去，
  横幅没了但顶部永远留 30px 空白带。改成先查"用户是否关过"再加 class。
- **`toast` 用 `innerHTML`** 渲染服务端 `detail`，而 detail 内嵌用户可控的文件名
  → DOM XSS。全项目 204 处 toast 调用**零处**传 HTML，所以默认转义是安全的。
- **`tags.js` 引用 `App.views`**（不存在，只有 `viewApi`）→ 改完标签后文件视图从不刷新。

#### 清理

- 删掉 `app/core` 里 6 个零引用函数（`dirname_parts` / `version_label` /
  `common_prefix_dirs` / `_root_placeholder` / `_apply_hook` + 其依赖）
  → **`app/core` 死函数 0 个**
- `ops.py` 白名单里去掉没人生成的 `extract`
- `tools/audit_frontend.py` 支持剥注释，消除"注释里提到 API 名就误报"的假阳性
- `tools/audit_frontend.py` 的 `ROOT` 改成 `__file__` 推导

- **配对阶段的一对一与确定性**（算法审计实测确认）：
  · `pair_sources` 阶段 1 复用阶段 0 已配上的左记录 → 同一左文件进两个 Match
    （实测 `left_total=1` 却 `matched=2`；跨库查重会把它 union 成"一组 3 条含同库两文件"）
  · 阶段 0 无任何门禁且按输入顺序贪心 → 255 秒差的文件也配上（score 0.558），
    左右顺序反过来结果会变。现在补了时长门禁并排序遍历，结果与输入顺序无关
  · `paths.similarity("","")` 返回 1.0 → 空 `name_key` 被当成"完全一致"，
    `320k.flac` 与 `无损.flac` 判成 `name_sim=1.0 / identical=True`。改成 0.0
- **`exclusive_slot` 三条测试**：写端点真的建任务 / 不留 running 僵尸 / 只读端点不被拦
- **新测试 `test_pairing_contract.py`(6 项)**：锁住一对一、确定性、空键不算一致，
  并**显式锁住两条刻意决定**（"同名是铁证，差 8 秒仍要配" / "词序颠倒必须能配"）——
  这两条我一度改过头，被既有测试挡回来了

#### 第三方审计报告（AUDIT_REPORT.md）逐条处理

| 编号 | 问题 | 状态 |
| --- | --- | --- |
| BUG-01 | `_claim_target` 不查 `_planned` → 同目标 3 步互相覆盖 | **已修**（本轮更早的 F1） |
| BUG-02 | 跨库传输压平目标名 → `overwrite` 时同名文件互相覆盖、双源都已进回收站 | **已修**：同一批里出现同名文件且 policy=overwrite → **400 拒绝**并说明原因；rename/skip 不受影响（实测 rename 得到 `t.mp3` + `t (2).mp3`） |
| BUG-03 | `exclusive_guard` 只检查不占位 → station/tags/folders 写期间不持有独占锁 | **已修**：`guard.py` 新增 `exclusive_slot(kind)` —— 用 FastAPI 的 `yield` 依赖，**请求进来就建独占任务、响应结束自动 `finish()`**，异常时记成 error 不留僵尸。11 个端点各改一行（不用重排函数体）：station×4 / tags×2 / folders×5。测试同时升级：从"签名里有 `exclusive_guard`"改成"必须是**真占位**的 `exclusive_slot`"，并新增"写端点确实建了任务"+"请求结束不留 running 任务"+"只读端点不被拦"三条 |
| BUG-04 | `_reindex_after_transfer` 拿**源侧绝对路径**当 `subpath` 传给目标源 → 目标库永远搜不到新文件 | **已修**：改为记录每个文件的实际目标相对目录（`dst.rel_path(target)`）再重扫；失败不再静默 |
| BUG-05 | WebDAV `stat()` 恒返回 `None`（`Depth:0` 时唯一的自身条目被当"自身"跳过） | **已修**：`_parse_multistatus(include_self=...)`，`stat` 传 `True` |
| BUG-06 | `artist_conflict` 在相似度 0.55~0.72 不否决，`>=0.72` 是死代码 | **未修**（记入 KNOWN_ISSUES，改阈值会动全局防线，需实测数据支撑） |
| BUG-07 | `is_sink_name` 裸 `endswith("other")` → `Brother`/`Mother`/`Another` 被当收纳区 | **已修**：英文关键词要求词边界（实测三者均不再误判，`其他`/`单曲`/`精选集` 仍正确命中） |
| BUG-08 | `tags_write` 静默截断到前 500 首 | **已修**：超过 500 → 400 并说明需分批；预览接口同样处理 |
| BUG-09 | `_inside_root` 在 root=`/` 时全部判越界 | **已修**：`paths.inside_root` 特判 `r == "/"`（实测 `/` 下路径通过、`/mnt/m` 仍正确隔离） |
| BUG-10 | FTP `open_read` 全量下载到内存 | **未修**（实验性数据源，记入 KNOWN_ISSUES） |
| BUG-11 | `base.abs_path` 跨盘符拼出 `C:/Music/D:/Other/x` → WinError 123 | **已修**：跨盘符原样返回，交给 `inside_root` 明确拒绝（实测返回 `D:/Other/x.mp3`，不再拼坏） |
| BUG-12 | WAV/AIFF/APEv2 两次 `f.save()` | **未修**（Low，无谓 IO） |

#### 性能

- **启用 GZip**：`GZipMiddleware(minimum_size=1000)`。前端 18 个脚本约 558 KB 裸传，
  压缩后大幅缩小（实测 `app.js` 响应 21 KB，`Content-Encoding: gzip`）。

#### 审计报告第四节（核心优化机会清单）逐条落地

| 报告条目 | 状态 | 做法与验证 |
| --- | --- | --- |
| 4.1(1) 启用 GZip | **已做** | `GZipMiddleware(minimum_size=1000)`；实测 `app.js` 响应 `Content-Encoding: gzip`、21 KB |
| 4.1(2) 激活 `_FILE_BROWSE_CACHE` | **已做** | 它原来**声明了却从没被读过**（`_FILE_BROWSE_TTL` 零引用）。现在 `_folder_map` 走 TTL 20 秒缓存；**返回副本**（调用方会就地改 `rel`/`depth`，不拷贝会把缓存改脏）；写路径（搬运回填、扫描完成）主动清。实测 `/api/files/folders` **155 ms → 25 ms** |
| 4.1(3) 根目录列表查询 | **已做** | 原来根目录 `SELECT * FROM track WHERE source_id=?` 把整库拉回 Python 再逐行 `P.parent()` 过滤。`track` 表本来有 `parent` 列 → 改等值查询。**实测新旧口径结果一致**（2301 首下根目录直属 0 首，两者都 0） |
| 4.1(4) 封面缓存改 LRU + 读配置 | **已做** | 原来超上限 `_cover_cache.clear()` —— 一满全清，下一屏所有封面同时回源（缓存击穿）。改成 `OrderedDict` 真 LRU：命中 `move_to_end`、超限 `popitem(last=False)`；上限读 `Settings.cover_cache_size` |
| 4.2(1) 拆解 `main.py` | **未做**（见下文） | 需要独立一轮 + 可回滚点 |
| 4.2(2) 补全 `include_duplicates` | **已做** | 原来 `cross` 算完只加一条 warning 就丢了，用户勾选后方案里一条步骤都没有。现在真的生成 `trash` 步骤（进回收站，可恢复），返回 `duplicate_groups`。**顺带修掉一个会删错文件的 bug**：`duplicate_steps` 里 `keep` 可能是 dict、`item["path"]` 是字符串，比较永远不等 → 连"保留项"也会被生成删除步骤 |
| 4.2(3) 引入方案指纹 | **已做** | `/api/plan` 返回 `plan_hash` + **`step_hashes`（逐步骤）**；`/api/execute` 校验。按单步而不是整份哈希，是因为对比页允许只勾选一部分执行 —— 只发整份哈希的话取消勾选必然对不上，只能放弃校验。前端把选中步骤的指纹带回来。**实测：篡改 `dst_path` → 409** |
| 4.3(1) 中转站还原 / 操作记录重试加二次确认 | **已做** | 两处都接 `UI.confirmHard`（列出将受影响的文件，要求输入确认短语）。这两个都是"点一下就直接再执行一遍文件操作"的按钮 |

#### 第六轮：补掉全部已知遗留项（junction / 跨进程 / 临时文件 / 孤儿 / revision）

| 问题 | 修法 | 验证 |
| --- | --- | --- |
| **junction（目录联接）绕过根校验** | `_is_reparse()` 跳过 reparse point（junction 不是 symlink）+ `paths.inside_root_real()` 用 realpath 二次判定；`_guard_inside` / `in_source_rel` 都接上 | 真造 junction：`inside_root` 认为是根内、`inside_root_real` 识破；`listdir` 不再列出 |
| **跨进程无互斥** | 新增 `core/proclock.py`（`O_CREAT\|O_EXCL` 锁文件 + 过期接管 + 只删自己的）；接进 `Jobs.create/finish` | 扫描时持锁、结束自动释放；第二次写操作 200；锁文件不残留 |
| **`.mshpart` 无人清理** | `ops.cleanup_stale_parts()` 只删 >24h 的；`lifespan` 起后台线程跑 | 过期删、新写保留、正常文件不动 |
| **删数据源留孤儿索引** | 删源时**总是**清 track/source 行（文件从不删） | 删源后该源 track=0 |
| **`/api/state` 检不出元数据更新** | `state_meta` 表 + `write()` 每次提交 +1，`/api/state` 返回 `rev` | 12 次改标题 → rev +12（原来漏 11 次） |

**第 7–9 轮复查**（同一轮地毯排查）：
- 算法层：`name_key` / `dup_key` / `version_tags` / `similarity` 在刁钻文件名（全角括号、前后空格、
  `Song (1)`、`[FLAC]`、无扩展名、`A_B-C`）上行为正确；同名不同格式仍能配对；现场版不会被当同一首。
- 资源层：200 次混合请求线程数不变（1→1）、重复扫描 3 次索引不重复、120 次写设置 DB 连接数不变（40→40）、
  遗留 running 任务不阻塞新任务、取消后可立刻再起。
- 前端：200 处 `innerHTML` 插值中**未转义用户数据 0 处**；`esc()` 对 7 种 XSS 载荷全部正确转义。

#### 地毯式排查 · 第三路（并发 / 配置 / 索引库）

**新增 `tests/test_config_db_hardening.py`（10 项）**。

##### CRITICAL：旧版索引库缺列 → 应用**启动失败**

`SCHEMA` 同时含建表和建索引，而索引建在 `name_key` / `dup_key` / `hash_quick` 上。
老库没这些列时，`CREATE INDEX` 在 `_migrate` 补列**之前**执行：

    sqlite3.OperationalError: no such column: name_key   → 启动 rc=1

从旧版升级的用户会**直接打不开**（实测三种缺列全中）。

修法：`_init_schema()` —— **建表 → 补列 → 建索引**，并加锁重试
（多线程首次初始化同一个库时 `executescript` 会撞 `database is locked`）。
**复验：7 种缺列的旧库全部能打开且数据保留。**

##### CRITICAL：config.toml 改坏 → 应用**启动失败**

手改配置是文档鼓励的用法，但 9 种手滑会让启动直接崩：
`[settings` 少括号 / 重复 `[settings]` / 非 UTF-8 / 顶层垃圾 /
`sources = "abc"` / `[[sources]]` 少 `name` … 而且**没有任何界面**能告诉用户原因。

修法：解析失败 → 备份成 `config.toml.broken-<时间戳>` → 用默认配置照常启动
→ 原因写进 `settings_errors`。**复验：9 种坏配置全部正常启动。**

##### 设置校验的三处松口

| 输入 | 以前 | 现在 |
| --- | --- | --- |
| `scan_workers = true` | `float(True)=1.0` → 存成 1 | 400 |
| `scan_workers = 1.9` | `int(1.9)=1` 静默截断 | 400「需要是整数」 |
| `trash_dir = {"a": 1}` | 存成 `"{'a': 1}"`，当相对路径 join | 400 |
| `max_workers = 7`（不存在） | **静默 200** | 报进 `errors` |

##### 半截状态

- **config.toml 只读**：内存配置已改、磁盘没改 → GET 显示新值，重启又变回。
  现在写盘失败会 `load_config(force=True)` 回滚并回 500。
- **建数据源时 DB 被锁**：config 里有了、索引表里没有 → 界面有源但 0 首歌，
  再建还 409「id 已存在」。现在会回滚配置并回 503。

#### 新增守护测试（+13 项，基线 778）
#### 地毯式排查（三路并行：路径安全 / 故障注入 / 契约模糊）

**新增 `tests/test_security_boundaries.py`（13 项）** 锁住下面每一条。

##### CRITICAL：读/列越界（可读库外任意文件）

`src.abs_path()` 对"跨盘绝对路径"是**原样返回**的（早先为修 `C:/Music/D:/Other`
拼接而加）。于是任何接受用户路径的端点都能被带出根外：

| 请求 | 实际（修复前） |
| --- | --- |
| `GET /api/cover-raw?sid=s1&path=C:/…/config.toml` | **200**，把库外文件当封面吐回来 |
| `GET /api/cover-raw?sid=s1&path=../outside/config.toml` | **200**，同上 |
| `GET /api/cover-raw?path=<目录/空>` | **500**（PermissionError） |
| `GET /api/files/list?sid=s1&path=<库外绝对目录>` | **200**，列出库外文件 |

修法：新增 `deps.in_source_rel()` 作为**信任边界**（绝对路径必须在根内、
相对路径不许含 `..`），`cover_raw` / `files_list` / `files_folders` /
`files_facets` 入口统一过它；目录/空路径改成 400/404 而不是 500。

##### CRITICAL：入库写出数据源根外

`/api/upload/commit` 的 `dst` 只做 `startswith(root)` 检查 —— 而
`root/../outside` **确实以 root 开头**，文件系统却会解析 `..` 逃出去。
另外 `rel` 只在 `/` 边界上查 `..`，`..\outside` 用反斜杠绕过。

修法：`dst` 拒绝 `..` 与盘符前缀；`rel` 先把反斜杠归一成 `/` 再查；
`moves` 走新的 `as_dicts()`（字符串/数字/元素非对象 → 400）。

##### CRITICAL：只读源被中转站端点绕过

`station_api.py` 里 **0 处** `require_writable`。实测在只读源上：
`move-in` 把文件移出库、`remove` 在只读源根内建 `.music-sync-trash` 并写文件、
`move-out` 新建目录。`tags/restore` 更是直接 `make_backup=False` 覆写。

修法：三个 station 写端点 + `tags/restore`（新增 `tags.backup_target()`
先查目标路径**再**判断所属源是否只读）全部补 `require_writable`。

##### CRITICAL：覆盖 + 校验失败 = 原件和副本都没了

- `transfer_one(policy=overwrite)`：先覆盖原件，校验失败再 `remove(hard=True)`
  → **原件和坏副本一起消失**，路径变空。
- `_do_copy(verify=True)`：校验失败把半截文件**留在正式名下**，
  下次扫描会把它当成一首正常的歌入库。

修法：覆盖前先把原件**移进回收站**（可恢复）；校验失败把坏副本改名
`.bad-<ts>` 保留（不删、不占正式名）。`overwrote` 标志改用"覆盖前是否存在"。

##### HIGH：未校验就永久删源

`transfer_mode=fast` + `hard_delete=true`：跳过校验却**永久**删源。
短写时源没了、目标也是坏的。现在这条组合直接拒绝：
"永久删除必须先校验"。

##### MEDIUM：类型混淆 → 500（17 → 0）

所有写端点都是 `payload: Dict[str, Any]`，**FastAPI 不做类型校验**。
一轮模糊测试打出 **17 种 500**：`items=数字`、`paths=数字`、
`name=数字`、`steps=[1,2]`、`groups=数字`、`moves="x"`、`keep=数字`、
`batch=数字`……全局兜底把 TypeError/AttributeError 变成"服务端出错"。

修法：新增 `deps.as_list()` / `as_text()` / `as_dicts()` 三个 helper
（缺省值 + 类型检查 + 清晰 400），替换 11 处列表、4 处字符串、3 处对象数组。
**复测：60268 个用例，5xx 归零**。

#### 新增守护测试（+5 项，基线 760）
#### 盘符根数据源：索引路径畸形（多轮排查第 1 轮抓到）

用户把网络位置映射成盘符 `Y:\` 当数据源（`Linux-music`）。实测发现索引里
**2301 条路径全是 `Y:节奏医生/…`** —— 盘符后少一个斜杠。

**根因**：`Y:` 在 Windows 上是「**盘符相对路径**」（相对 Y: 盘自己的当前目录），
**不是**根目录。`app/sources/local.py::_native` 把根转成 native 形式时得到 `Y:`，
于是 `os.path.join("Y:", "x.mp3")` 给出 `Y:x.mp3`。而 `rel_path()` /
`inside_root()` / 目录树相对路径全都按 `Y:/…` 比较。

**后果（都是实测）**：

| 症状 | 说明 |
| --- | --- |
| `rel_path` 带着盘符 | `Y:节奏医生/…` 而不是 `节奏医生/…` → 与另一个库的键格式不一致 |
| `inside_root` 永远 `False` | 一旦该源可写，**所有写操作都会被判"越界"拒绝** |
| **扫描漏文件** | 老索引 2301 条，磁盘实际 **2308** 条，少了 7 个 |
| 界面看着正常 | 因为 Windows 恰好把裸盘符解析成根目录 —— 纯靠运气 |

**修法**：`_native` 里裸盘符补上分隔符，让盘符根变成真正的根。重扫后
`Linux-music` 索引 **2308 条 = 磁盘真值**，`rel_path` 干净，越界 0。
`win-music`（`D:/Music` 这种子目录根）不受影响。

**回归测试**：`tests/test_drive_root_paths.py`（5 项，纯字符串断言不碰磁盘）。

#### 多轮功能排查（6 轮，不同角度）

| 轮次 | 角度 | 结果 |
| --- | --- | --- |
| 1 | 索引 vs 磁盘真值 | **抓到盘符根 bug**（见上）；`win-music` 2301/2301 完全一致 |
| 2 | 守恒律 / 不变量 | 全过：分页合计==total、`matched+only_*==total`、配对一对一、方案 id 唯一、查重组不重叠、整理 `keep+move+manual==total` |
| 3 | 端到端流程 | 全过：建源→扫描→查重→方案→执行→汇总 |
| 4 | 并发与幂等 | 全过：并发扫描一个 200 一个 **409**；重复执行同一步骤 → `conflict`；重复 PUT 设置结果一致 |
| 5 | 错误路径与副作用 | 全过：只读源 403、越界 400、未知源 404、NUL 路径 400；**回收站无垃圾、无 `.mshpart` 残留** |
| 6 | 前端真实交互 | 见下方 |

#### 新增守护测试（+5 项，基线 760）
#### 拆分后专项复核（抓到 2 个真 bug）

`tools/audit_split.py`（新增常驻工具，四类检查）+ `tests/test_no_500.py`（2 项）。

**抓到并修掉的 2 个 bug**（都是"启动不报错、测试也没覆盖"的类型）：

| bug | 后果 | 怎么抓到的 |
| --- | --- | --- |
| `set_keep()` 里 `out = []` 丢了 | `POST /api/duplicates/keep` **必然 500**（即使空输入，`return` 里也引用它） | `audit_split` 的 symtable 检查。当时 **758 项单测 + 90 个冒烟全绿**，因为这个端点一处都没被覆盖 |
| `create_source()` 空 body → TypeError | `POST /api/sources` 回 500「服务端出错」，用户不知道是自己少填了字段（应为 400） | `test_no_500` 把每个端点用空参数打一遍 |

**新增 `tools/audit_split.py`**（常驻，不是一次性脚本）：

1. 逐模块导入（循环导入 / 顶层 import 失败）
2. **未定义名字**（`symtable`）—— 抓"只在调用路径才炸"的 NameError
3. 路由集合差分（`--baseline` 指向拆解前备份，必须"不丢"）
4. 前后端路径一致性（`api.js` 的路径必须真实存在）

**本轮结果**：38 个模块导入 0 失败 · 未定义名字 **0** 处 ·
路由基线 **88 → 88**（无丢失无新增）· 前后端 **0** 问题。

**新增 `tests/test_no_500.py`(2 项)**：
- 每个 `/api` 端点用**空参数**打一遍，**500 就是 bug**（400/404/409/422 都是正常拒绝）
- `/api/duplicates/keep` 专项：空输入 200 + `oldest` 策略按 mtime 正确挑保留项

**顺带盘点**：88 条 `/api` 路由里有 **11 条从没被任何测试/冒烟调用过**。
逐条打了一遍，确认全部正常（无 500）；其中 `/api/duplicates/keep` 就是坏的那条。

#### 新增守护测试（+8 项，基线 758）

`tests/test_optimizations.py` —— 按**行为**测而不是按源码测，因为这几处都属于"看起来实现了、实际可能没接线"：

- `include_duplicates` 必须有步骤、必须是 `trash`、**保留项不能出现在步骤里**
- 指纹：篡改步骤 → 409；指纹一致 → 200；单步/整份哈希函数自洽
- 目录聚合缓存**返回副本**（就地改不污染缓存）
- 根目录 SQL 口径与旧的全表过滤**结果一致**

#### 架构：拆解 `app/main.py`（审计报告 4.2(1)）

`app/main.py` **2850 行 → 425 行**，按功能拆成 `deps` + 5 个路由模块：

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `app/deps.py` | 570 | 运行时状态 + 23 个跨端点 helper |
| `app/routes/system.py` | 444 | 系统 / 数据源 / 扫描 / 日志 / 盘符 / 设置（17 端点） |
| `app/routes/jobs.py` | 183 | 任务中心（9） |
| `app/routes/library.py` | 407 | 曲库 / 封面 / 试听（7） |
| `app/routes/compare.py` | 564 | 对比 / 方案 / 执行 / 查重（5） |
| `app/routes/files.py` | 689 | 文件浏览 / 跨库传输（10） |
| `app/main.py` | 425 | app 创建 / 中间件 / 异常处理器 / lifespan / 挂载 / 首页 / CLI |

**做法**：用 `ast` 取**精确行范围**把函数源码原样切出来，只把 `@app.` 换成 `@router.`
—— 函数体一个字都没改。路由总数 **94 条不变**（93 个 `/api` + 首页），
`/api/sources/status` 仍排在 `/api/sources/{sid}` 之前（有测试守）。

**拆分中踩到、并写进 `AGENTS.md` 的四个坑**：

1. 路由模块里的**延迟导入**（`from .core import X`）在 `app/routes/` 下会解析成
   `app.routes.core` —— **启动不报错，调用到才 500**。必须写双点。
   （实测：`/api/duplicates`、`PUT /api/settings` 直接 500）
2. `system_info` 用了 `app.version` → 会形成 routes→main→routes 循环，
   改成 `deps.APP_VERSION`。
3. `FUZZY_MIN_SCORE` 等模块级常量属于 deps，要显式导入 ——
   否则 `/api/library` 一调用就 NameError。
4. `refresh_tracks` 被 `tags_api` 通过 `_deps()` 调用 → 必须放在 deps，
   不能留在 `routes/library.py`。

**测试同步升级**：把"某段代码存在于 `main.py`"这类源码级断言改成"存在于任一后端模块"
（新增 `tests/srchelp.py::app_src()`）。否则每拆一次文件就要改一批测试 ——
那样迟早有人图省事直接改测试，契约就名存实亡。
`tests/test_docstring_index.py` 也改成跨模块校验导航索引。

#### 新增守护测试（+27 项，基线 750）

| 测试 | 守什么 |
| --- | --- |
| `test_settings_wiring.py` | 每个 `Settings` 字段必须真被消费；`ignored_names` 端到端生效 |
| `test_endpoint_guards.py` | 写接口必须挂独占守卫（走真 HTTP）；路径类设置在有任务时 409 |
| `test_non_audio.py::VersionLabelSingleSourceTest` | 版本类别只有 `paths.VERSION_KINDS` 一份 |
| `test_docstring_index.py` | `main.py` 导航索引不许腐烂 + 静态路由必须排在带参路由之前 |

#### 新增文档

- **`docs/KNOWN_ISSUES.md`** —— 本轮**没修**的 27 条，按严重度排，每条带位置/后果/建议修法
- **`docs/TESTING.md`** —— 测试体系：覆盖什么、**没覆盖什么**、新功能怎么测、隔离怎么防串库
- **`AGENTS.md`** 新增 3 条硬约束（破坏性操作必须过 `_guard_inside`、选名登记同临界区、设置项必须接线）


### 新增

- **`AGENTS.md`**（538 行）：接手开发必读。10 条硬约束（每条对应一次真实事故）、
  代码地图、一次「移动」的完整数据流、改完必须做的检查、按症状查踩过的坑、
  明令不要做的事
- **`app/main.py` 顶部导航索引**：49 个路由按功能分组列出 + 模块级状态说明。
  配套测试 `tests/test_docstring_index.py` 守住索引不腐烂（缺路由/多路由/幽灵函数名都会红），
  并额外检查"静态路径必须排在带参路由之前"（FastAPI 按注册顺序匹配）
- **`docs/GITHUB_SETUP.md`**：推送步骤 + 提交前必查清单
- **测试包水印**：`MSH_TEST_MODE=1` → 顶部横幅 + 右下角 TEST 角标 + `/api/system.test_mode`
- **`run-test.py`**：测试包启动器（包内演示库、包内数据目录、自动扫描、开浏览器）
- **`tools/build_packages.py`**：一键打 AI 审查包 / 测试包

### 修复

- **`version_tags` 字段集不一致（两处）**：`matching._same_tag` 和 `group_to_dict`
  只传了 name+title，漏了 `rel_path`，违反代码里自己写下的约定
  （约定在 `version_mismatch` 的 docstring 里，还附了事故复盘）。
  后果："版本标记只在目录名里"的歌（`专辑 (Remix)/Song.mp3`）会被配对路径当成
  与原版同版本，而 `dup_key` 认为不同版本 —— 同一个文件两套结论
- **`cover_cache_size` 设置项完全没生效**：`main._serve_cover_bytes` 硬编码 2000，
  而默认值是 800。用户改了界面上的值不会有任何效果、也不报错
- **`ops.py` 白名单里的死 `extract`**：在目标占位白名单里，但全项目没有任何地方
  生成它、执行器也没有对应分支。多一个白名单项的实际害处是"占位生效但执行时静默跳过"
- **测试/工具里 26 处硬编码本机路径**：全部改为 `__file__` 推导；
  node 位置改为 `MSH_NODE` / PATH / 常见安装位置（没有 node 时 5 条 JS 测试干净跳过）
- **`i-pause` 图标重复定义**、**重复的 `select` 规则**、**4 个无人引用的死常量**
- **`tools/audit_frontend.py` 硬编码 `ROOT`**

### 新增

- **测试模式**：`MSH_TEST_MODE=1` 时首页顶部出现常驻横幅、右下角出现 TEST 角标，
  `/api/system` 返回 `test_mode: true`。用于"开箱即用测试包"，
  **不改变任何业务行为**
- **测试包启动器** `run-test.py`：自动生成只含演示库的配置、指到包内数据目录、
  首次启动自动扫描演示音频、打开浏览器。一条命令就能试
- **FTP / FTPS 数据源**（实验性，标 `experimental = True`）
- **数据源类型下拉不再写死**：从注册表推导，SFTP / WebDAV / HTTP 目录 / SMB
  终于能在界面上选到（之前只有 `local` 一项）
- **网络数据源的连接参数表单**：主机 / 端口 / 用户名 / 密码 / 共享名 / 域 / 基地址
- **MTP 便携设备探测**：只列出并说明为什么暂不支持（需要 WPD COM 或 libmtp
  原生库，标准库没有这个能力）
- **每个数据源可选传输模式**：标准（复制→校验大小→删源）/ 快速（跳过校验）
- **后台日志**：落文件（4 MB 轮转）+ 500 条内存环形缓冲 + `/api/logs` 接口 +
  设置页查看器；未处理异常带堆栈入库
- **`/api/files/exists`**：一次批量查最多 5000 个路径是否存在
- **文档**：`docs/USER_GUIDE.md`（用户手册）、`docs/TECHNICAL.md`（技术文档）、
  `SAFETY.md`（安全规则与事故记录）、`CONTRIBUTING.md`

### 修复

- **播放器队列（用户报）**：`playTrack` 只换歌、不动队列 ——
  在曲库播过一次后再去文件浏览点单曲，播放列表里还是整个库源，
  而正在播的那首根本不在队列里。现在保证"正在播的一定在队列里"：
  已在队列里就只移动位置，不在就换成这一首
- **`queue.step()` 会返回失效条目**：队列里混进 `{track: null}` 时
  `playTrack(null)` 静默 return，表现是"按下一首完全没反应"。改为跳过坏条目
- **新增 `queue.prune()`**：剪掉失效条目；当前这首被删时落到它之后第一个有效条目
- **多库跨库查重会产生重叠组**：3 个库以上时同一份文件同时属于多个组，
  前端"每组保留一份"自相矛盾，会生成把同一文件删两次的计划并报幻影错误。
  新增 `matching.merge_matched_pairs()` 合并成互不重叠的组
- **无元数据曲目被静默丢弃**：`_plan_by_album_impl` 里 `if not key: continue`
  让这些曲目既不被移动、也不进任何结果列表，`total` 对不上。
  现在单列为 `manual`，界面明确显示"不参与智能整理"及处理办法
- **展开栏箭头的"滑动"动画**：不再用 CSS 旋转，改成纯文本箭头字符替换；
  藏掉原生 `<details>` 自带的三角（浏览器绘制、自带动画，CSS 压不住）
- **基础规则里无条件的 `transform` 过渡**（`.btn` / `.tile` / `.stat`）：
  会让"被父级布局带着走"也变成动画。只保留 `:hover` / `:active` 需要的部分
- **矮窗口下设置页等内容永远看不到**：`.view` 没有 `overflow`，
  内容超出被 `.main{overflow:hidden}` 裁掉且没有滚动条
- **小屏幕横向溢出**：`input` 的 UA 内在宽度把卡片撑破；
  文件浏览的目录树写死 `290px` 让文件列表只剩 34px
- **`i-pause` 图标重复定义**：43 个 symbol 里有两个 `id="i-pause"`，
  渲染结果取决于浏览器选哪个
- **重复的 `select` 规则**：两条都在定义箭头，后一条整体覆盖前一条
- **跨库传输不检查文件类型**：现在显式拒绝非音频文件并说清该怎么处理
- **`/api/transfer/_transfer_one` 丢弃 `verify_hash`**：用户勾"按内容校验"等于没勾
- **`overwrite` 只是装饰**：只影响风险徽标，执行时没人读。风险等级不再跟随它，
  并把对比页的"覆盖"勾选框和"目标同名时"下拉联动成同一个开关
- **场景化错误分类**：NUL 字节路径 / 只读数据源 / 坏数据源 分别返回 400 / 403 / 404，
  不再一律 500
- **`smoke_api.py` 会改真实设置**：现在快照 + 恢复 + 校验

### 重构

- `available_types()` 从注册表推导，新增数据源类型只需实现类 + `@register`
- 前端类型显示名从 `/api/system` 拿，不再在前端写第二份映射
- **测试路径可移植**：去掉全部本机绝对路径（15 + 6 处），改用 `__file__` 推导；
  node 位置改为 `MSH_NODE` 环境变量 / PATH / 常见安装位置

### 已知限制

见 `docs/USER_GUIDE.md` 第 20 章 与 `docs/TECHNICAL.md` 第 19 章。
要点：仅 Windows 实测；网络协议全部实验性；`.wma`/`.mka`/`.caf` 不支持标签编辑；
MTP 只探测不读写；`/api/plan` + `/api/execute`（对比页）没有方案指纹。
