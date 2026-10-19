# 已知问题清单

> 这是收尾诊断（2026-10-17）的产出。**已修的**在 `CHANGELOG.md`，
> **没修的**在这里，按严重度排，每条都写明位置、后果、以及"什么情况下会咬人"。
>
> 未修的**不代表不重要**，只是收敛在"本轮改动风险可控"的范围内停手。
> 接手时建议按本文顺序推进。

---

> **状态更新（2026-10-19）**：本文件是历史累积清单，其中一部分条目在后续轮次已经修掉了 —— 判断某条是否还成立，请以 `CHANGELOG.md` 对应轮次为准。
> 已确认修复的包括：**K4**（transfer 策略透传）、**K5**（校验失败不再删原件）、**K30**（双 `f.save()`）、**K31**（根目录查询）、**A1**（`name:` 兜底桶）、**A12**（`name_similarity`/`keep_flat_below`/`block_by_duration` 已接线）、**BUG-25**（indexwatch 独占 Job）、以及回收站越界、续传元数据时序、锁 PID 判活等。
> 2026-10-19 安全审查另修复了 **C1**（跨进程锁误释放）、**C2**（封面缓存无限增长）、**C3**（junction 穿越）、**H1**（根目录传输后索引不刷新）、**H2**（校验失败隔离文件名冲突）、**H4**（艺术家阈值硬编码）；详见 `docs/SECURITY_REVIEW_2026-10-19.md`。
> 其余条目（K1/K2/K3/K6–K17、A2–A14 等）**仍未处理**，请按本文顺序推进。

## 一、CRITICAL / HIGH（会造成数据损失或错误结果）

### K1. 单步超时 120 秒 与 心跳 180 秒 打架 → 孤儿线程仍在搬运

| 项 | 内容 |
| --- | --- |
| 位置 | `app/core/ops.py` 的 `_apply_guarded`（单步超时） + `app/core/jobs.py` 的 `STALE_JOB_SECONDS` |
| 现象 | 单个大文件搬运超过 120 秒 → 报「超时（已跳过）」并列为**可重试**；几秒后那个孤儿线程**照样把复制做完并删掉源**。界面上是"失败可重试"，磁盘上是"已经搬完了"。 |
| 后果 | 用户按提示重试 → 目标已存在 → 冲突；或者比这更糟：索引没同步，库里指向旧路径。 |
| 为什么会这样 | 超时只让**调用方**放弃等待，没有真正中止工作线程；`ThreadPoolExecutor` 里的任务无法从外部杀掉。 |
| 建议修法 | ① 让超时与 `STALE_JOB_SECONDS` 的关系明确（超时必须远大于心跳周期且能区分"慢"和"死"）；② 或给每个搬运步骤一个"我已放弃"标志，worker 在检查点看到就不再继续、也不删源；③ 最稳的是把"删源"拆成单独一步，只在复制+校验确认成功后由主流程发起。 |
| 触发条件 | 大文件（数 GB）+ 慢盘（U 盘 / 网络盘）。日常小文件不会遇到。 |

### K2. 恢复链三处断点 → 崩溃/暂停后"接着做完"不可用

| 位置 | 问题 |
| --- | --- |
| `app/core/jobs.py::load_unfinished` + `/api/unfinished` | `load_unfinished` 把中断任务写成 `state=error`，而 `/api/unfinished` 只挑 `running`/`paused` → **启动提示永远不出现**。`mark_interrupted` 是死代码。 |
| `app/core/jobs.py::finish` | 用 `str(result)` 覆写 `detail`，把里面存的 `steps` 冲掉 → 可恢复信息丢失。 |
| `app/core/jobs.py::persist_steps` | JSON 用 `[:200000]` 直接截断 → 约 500 步以上变成**非法 JSON**，解析异常被 `except: pass` 吞掉。 |
| 后果 | 崩溃后「操作记录」页的"接着做完"按钮要么不出现、要么出现但没有步骤可续。 |
| 建议修法 | 统一 `state` 取值；`finish` 不要覆盖 `detail`；截断前先判断 JSON 完整性（超限就不存 steps，并明确标记"步骤过多，无法续做"）。 |

### K3. 只读保护缺口（接口回 200「成功」，实际一步没做）

| 位置 | 缺口 |
| --- | --- |
| `app/station_api.py` | 4 个写端点（`move-in` / `move-out` / `remove` / `restore`）**没有** `require_writable` / `_guard_write`；落盘用原生 `shutil.move`，绕过适配器的 `_guard_write` |
| `app/sources/folders_api.py::empty_remove` | 没有 `_guard_write`，且把 `ReadOnlyError` 当"跳过"吞掉 → 返回 `{"ok": true}` |
| `app/tags_api.py::tags_restore` | 不解析源、没有 `require_writable`，`tags.write_tags` 直接 `os.replace` |

| 后果 | 只读数据源上做这些操作，接口返回成功，文件没动。用户会以为已经生效。 |
| --- | --- |
| 建议修法 | 照 `files_move` / `tags_write` 的写法补 `require_writable(...)`；所有落盘走适配器方法，不要直接 `shutil.move` / `os.replace`。 |

### K4. `_do_transfer` 把用户的策略选择丢掉了

| 项 | 内容 |
| --- | --- |
| 位置 | `app/core/ops.py::_do_transfer` |
| 现象 | 写死 `policy="skip"` / `verify=None` / `hard_delete=False`。对比页点「安全搬运」时，用户选的"同名时改名/覆盖"和"按内容校验"**全部失效**。 |
| 后果 | 用户选"覆盖"，实际走了"跳过"；用户选"按内容校验"，实际只比了大小。 |
| 建议修法 | 把 `policy` / `verify` / `hard_delete` 从 `_apply` 一路传进来，和 `_do_copy` 一致。 |

### K5. 校验失败时可能删掉用户原有文件

| 项 | 内容 |
| --- | --- |
| 位置 | `app/core/transfer.py`（校验失败分支用 `hard=True` 删目标） |
| 现象 | `policy=overwrite` 时目标已被覆盖成新内容；若随后校验失败，删的是**用户原来那份**（且不进回收站）。 |
| 建议修法 | overwrite 前先把目标备份到回收站（而不是直接覆盖）；或校验失败时把目标移入回收站而不是硬删。 |

### K6. `_sync_index` 不检查 rowcount

| 项 | 内容 |
| --- | --- |
| 位置 | `app/core/ops.py::_sync_index` |
| 现象 | UPDATE 影响 0 行也算成功 → 索引里留着旧路径，界面显示"已移动"但库里对不上。 |
| 建议修法 | 检查 `cursor.rowcount`，为 0 时记一条 warning（不必失败，因为索引可能本来就没这条）。 |

---

## 二、MEDIUM（行为不符合预期，但不直接丢文件）

### K7. `exclusive_guard` 只检查、不占位

`app/core/guard.py` —— 它是**依赖注入时的快照检查**，通过之后并不持有独占位。
`tags_write`（可 500 首）、`folders_*`、`station_*` 这些端点只挂 guard、不 `create()` 任务，
所以真正写盘期间 `busy()` 仍是 `None`，扫描等流程可以合法插进来。

**建议**：这些端点内部统一 `get_jobs().create(kind, exclusive=True)`，和 `Executor.run` 一致。

### K8. `set_keep` 是纯计算，却挂了 `Depends(exclusive_guard)`

`app/main.py::set_keep` —— 方向挂反了：有任务在跑时，一个**只读**的"算一下保留谁"请求会 409。
应该只对真正的写操作挂守卫。

### K9. `make_plan` / `_compare_core` 不 `ensure_free()`

`app/main.py` —— 方案预演要一致快照（`ops.py` 里明确要求）。现在预演时库正在被改，
得到的方案可能是错的。

### K10. `local.listdir` 漏一种异常 → 500

`app/sources/local.py::listdir` 只捕 `FileNotFoundError` / `PermissionError`。
`?path=` 指向一个**文件**时 `os.scandir` 抛 `NotADirectoryError` → `/api/files/list`、
`/api/sources/{sid}/browse` 返回 500（应该是 400）。

### K11. `upload_dedup` 直接改 `job.state`，不走 `finish()`

`app/upload_api.py` —— 数据库里永久留下 `running`，重启后被当成"上次没做完"，
而 `recover` 对它只会返回 400。另外取消状态会被 `work()` 无条件覆盖成 `done`。

### K12. 智能整理的兜底分支必然失败

`app/search_api.py` 的 `album-apply` 兜底调用的是**路由壳** `organize_album_plan`
（默认后台执行、返回 `{"job":…,"async":true}`），于是 `plan.get("moves")` 为空 → 必然 400，
还额外留一个白跑的后台任务。

### K13. `files_move` 同步执行且不返回 job id

`app/main.py::files_move` —— 前端只能显示静态的"正在移动…"，没有进度、不能取消。
对照：文件传输走 `runExclusive` 有完整进度。**建议**改成后台任务 + job id。

### K14. 换数据源时旧状态没清

| 位置 | 残留 |
| --- | --- |
| `app/web/static/js/app.js::selectSource` | 不清 `State.treePrefix` / `libFilters.prefix`（`views.library.js` 会消费）→ 换源后新库被旧库的绝对目录过滤成 0 首 |
| 同上 | `State.tracks` 从不按源清空 |
| `app/web/static/js/views.duplicates.js::mount` | `if (State.dup) paintAll()` —— 直接把**上一个库**的查重结果画出来，而且那个结果是可以执行清理的 |

**这类"换源不清状态"是前端最危险的一类 bug**：界面显示的是上一个库的数据，但操作会作用到当前库。

### K15. 播放器：`onended` 与 shuffle 双推进

`app/web/static/js/app.js` —— 顺序推进的 `a.onended` 和随机播放的 `ended` 监听器同时生效，
每次播完先播下一首、再被随机那首顶掉（双请求 + 跳歌）。

### K16. 参数没有上限

`max_steps` 由**客户端自己给**（`app/main.py` 的 `/api/execute`、`files_move`）、
`/api/library` 的 `limit` 无上限、`files/under` / `station/move-out` / `empty-remove` 的
`paths` / `names` 无上限。

### K17. CORS 全开 + 无鉴权

`app/main.py` 的 `allow_origins=["*"]`。本机工具通常这样也行，但配合"接口能接受客户端自带
steps 路径"这类能力，任何本机网页都能驱动它改文件。

**建议**：绑定 `127.0.0.1` 时把 CORS 收紧到同源；如果以后要跑在局域网，必须加鉴权。

---

## 三、LOW（体验/整洁问题）

| 编号 | 位置 | 问题 |
| --- | --- | --- |
| K18 | `app/core/ops.py::_dry` | 没有 `transfer` 分支：预演时不检查只读/可删 |
| K19 | `app/core/ops.py::_reserve_targets` | 白名单里没有 `link`，而 `_apply` 把 `link` 当 `copy` 执行 |
| K20 | `app/core/ops.py::_do_rename` | 借道 `.mshren` 两步；崩溃会留孤儿文件，且 `.mshpart` / `.mshren` 从不清理 |
| K21 | `app/sources/base.py::copy_from` | 写失败时 `stream` 没 close（Windows 句柄泄漏） |
| K22 | `app/sources/folders_api.py::delete_folder` | `rmtree(ignore_errors=True)` 之后无条件删索引并回 ok |
| K23 | `app/core/ops.py::run` | 有一处死赋值 |
| K24 | `app/main.py::_FILE_BROWSE_CACHE` | 定义了 `_FILE_BROWSE_TTL = 20.0` 但**没有消费**（文档里承诺的 20 秒缓存不存在） |
| K25 | `app/web/static/js/app.js::queue.step(-1)` | 在第一首时返回当前项 → "上一首"变成重播，和注释里"不循环"的说法矛盾 |
| K26 | `app/web/static/js/views.ops.js` | `title` 属性里内嵌双引号会破坏 HTML |
| K27 | `app/web/static/js/views.station.js` | `moveOut` / `restore` / 自动归位不走 `runExclusive`，且 `catch` 吞掉原因 |

---

## 四、本轮**没有**再验证的边界

这几件事不是"已知 bug"，而是"没有证据"，不要当成能用：

| 项 | 状态 |
| --- | --- |
| SFTP / WebDAV / HTTP / SMB / FTP 连真实服务器 | 从未验证 |
| macOS / Linux 实机 | 从未运行 |
| 超过约 3000 首的库 | 性能未测 |
| 断电 / 拔盘 / 磁盘满 等真实故障注入 | 未测 |
| 浏览器兼容性 | 只在 Edge / Chromium 内核验证 |

---

---

## 六、算法层（匹配 / 查重 / 整理）—— 第三路审计的未修项

审计方法：通读 `matching.py` / `organize.py` / `plan.py` + 14 组实测。
**本轮已修的**（进 `CHANGELOG.md`）：阶段 1 复用已配左记录、阶段 0 无门禁 +
顺序依赖、空 `name_key` 当"完全一致"。

### A1（HIGH）`group_duplicates` 的 `"name:"` 兜底桶把无关文件并成一组

`matching.py` —— `dup_key` 在 title 空且 `name_key` 也空时返回 `""`，
兜底键退化成常量 `"name:"`，于是**所有**这类文件进同一组。这一层的组员
**不经过任何分数/门禁**（门禁只用于"合并不同键的组"），组直接被输出成"重复组"
还带 `suggested_keep` / `wasted_bytes`。

实测：100 秒/1KB 的 `伴奏.flac` 与 500 秒/99999B 的 `试音.flac` 被判成一组，
并建议保留体积更大的那份。`k.startswith("|")` 那一半条件永远不成立（死分支）。

**建议**：`dup_key` 为空时用逐行唯一键（`__nokey__{source_id}::{path}`）或不参与查重，
像 `organize` 的 `manual` 桶那样单独回报"无法判定"。

### A2（HIGH）`plan_relocate` 归拢段破坏守恒，并把收纳区的歌搬走

`organize.py` 的 section 4 —— `placed = {m["path"] for m in moves}` 只排除
"已经判要移动"的行，**已判 `keep` 的行照样能被再判成 move**；而且这一段刻意把
`sink_set` 里的歌当来源，与模块 docstring 第 24 行、界面文案
"收纳区里的歌一律不动"**直接矛盾**。

实测 5 首的库报表变成 `keep=5 / move=3`（合计 8 > 5），界面把马上要被搬走的
3 首显示成"原地不动"。

**建议**：把"每行的最终裁决"收敛成一个 `decision: path -> "keep"|"move"|"manual"`
字典（重复写就 assert），归拢段先读再覆盖。想保留"从收纳区捞回专辑曲目"的行为，
就必须同步改 docstring 和界面文案 —— 现在的状态是**承诺与实现相反**。

### A3（MEDIUM）查重"代表项"取最长文件名、同长看行序

`matching.py` —— `reps = {k: max(v, key=lambda x: len(name))}`，同长度时取**第一个**。
而合并门禁比的是代表项的 `name_key`，于是代表项一变、能不能合并就变。
实测 200 条随机数据（48 组）打乱行序后 **5 组发生变化**。

**建议**：`key=lambda x: (len(name), name_key, path)` —— 加确定性兜底键。

### A4（MEDIUM）`merge_matched_pairs` 的并查集闭包把"互相否决"的两个文件并进一组

A~B、B~C 都通过 `fuzzy_admissible`，但 A~C 会被 `artist_conflict` 否决 ——
并查集不管传递性，三份并成一组，界面提示"保留一份、清理其它"。

实测：`Pink Floyd` / `Pink Floyd Live Band` / `Live Band`，首尾两两 `admissible=False`，
仍然输出一组 3 条。

**建议**：成组之后再做一遍"组内两两 `fuzzy_admissible`"的复核，不通过就拆组。

### A5（MEDIUM）`plan_by_album` 不把 API 传来的**绝对** `sinks` 转成相对

`organize.py` —— `find_sinks` 只认"在 `folders` 里的键"（相对路径），而 API 传的是
绝对路径（`search_api._abs_in_root`）。于是**用户勾的收纳区一个都没生效**。
对照：`plan_relocate` 有 `_as_rel` 转换 —— 两个规划器行为不一致。

实测 `summary.sinks = []`，同一份数据 `plan_relocate` 得到 `['MyStash']`。

**建议**：`find_sinks(..., [_as_rel(s) for s in (sinks or [])])`，并让两个规划器
共用同一段"入参归一化"。

### A6（MEDIUM）`custom` 映射是绝对路径 → 生成 `src == dst` 的空搬步

`organize.py` —— `_folder` 是相对路径、`dest`（来自 custom）是绝对路径，
"已在目标目录"判定恒为空，已经在目标目录里的文件也会被生成一条 move 步。
`ops._do_move` 有 `cur == want → skipped` 兜底，**不会丢文件**，但方案里多出无意义步骤、
"已在目标 0 首"的数字是错的。

**建议**：custom 的 value 统一过 `_as_rel`。

### A7（MEDIUM）`base_key` 用 `rsplit("|", 1)` 切版本段，多标记版本会切错

`matching.py::group_to_dict` —— `晴天|周杰伦|196|instrumental|remix` 得到
`base_key = 晴天|周杰伦|196|instrumental`，而原曲是 `晴天|周杰伦|196`。
前端"不区分版本"视图按 `base_key` 分组，于是"伴奏+Remix"那份不会和原曲出现在同一个
总体视图里。**同一文件两套结论**，且与同文件 `_version_of` 的注释矛盾（那里明确要求
从**左边** split）。

**建议**：`"|".join(dk.split("|", 3)[:3])`，或让 `dup_key` 返回结构化对象。

### A8（MEDIUM）`dup_key` 用 `|` 分隔，但 `normalize_text` 不清 `|`

标题里的全角 `｜` 经 NFKC 变成 `|` 后留在 title 里，`_version_of` 会把桶号当成版本段。
实测两套结论再次分裂。**建议**：分隔符换成 `normalize_text` 会清掉的字符
（如 `\x1f`，`organize._group_key` 已在用），或 sanitize title。

### A9（MEDIUM）`folder_fits` 的 `c in k` 让单字符目录名命中任意标签

`organize.py` —— 候选 `c` 没有最小长度，实测 `folder_fits("M", {album:"Some Album"}) = True`、
`"1"` 命中 `"A1 Project"`、`"a"` 命中 `"Adele"`。于是 `1/` `2/` `A/` `#/` 这类**真实音乐库里
非常常见**的目录里的歌一律被判"位置正确"，永不整理（漏整理，不丢文件）。

**建议**：`if len(c) >= 2 and (...)`，最好整体换成 `_is_same_identity`。

### A10（MEDIUM）目标目录选择用 `next()` 遍历 → 依赖行顺序

`organize.py` —— 两个目录都能匹配同一作者时，正序落 `Zed A`、反序落 `Zed B`。
同一库两次规划可能给出不同落点。**建议**：候选先按 `(-count, len(folder), folder)` 排序再取。

### A11（MEDIUM）性能：`O(曲目 × 目录)`，以及无时长分支的 `O(n×m)`

实测：

```
plan_relocate   500 首 8.99s → 1000 首 35.3s → 2000 首 134.4s
plan_by_album   500 首 8.76s → 1000 首 34.8s → 2000 首 138.8s
固定 2000 首：目录 10 → 4.7s，600 → 132.1s（随目录数线性、乘曲目数）
pair_sources 两侧无时长：50×50 0.29s → 200×200 4.63s
```

`/api/organize/plan` 是**同步**端点 —— 10k 首、上千目录会把请求挂住几分钟。

**建议**：建"归一化名字 → 目录"反向索引 + 缓存 `normalize_text` / `similarity`；
`has_home_folder` / `find_home` 先按 artist 精确/前缀命中再退回模糊；
阶段 3 的无时长分支先按 `(size, name_key)` 建桶。

### A12（MEDIUM）两处"声明但没生效"的旋钮

| 旋钮 | 状态 |
| --- | --- |
| `plan_by_album(keep_flat_below=...)`（界面"单层/两层"） | 整个实现没有分支读它，`structure="flat"` 与 `"nested"` 生成的方案**完全一致** |
| `pair_sources(block_by_duration=True)` | 函数体里只出现在签名 |
| `plan.build_plan` 的 `thr = settings.name_similarity` | 读完不用（设置项改了没效果） |

**建议**：真正接上线，或删掉参数和界面选项。不要留"改了没效果"的旋钮 ——
这正是 AGENTS.md §8 症状表里那一条。

### A13（MEDIUM）`build_plan` 的 `include_duplicates` 只产出一条 warning

`plan.py` —— `cross` 算好之后**只用了它的长度**去拼一句提示，既不返回也不转成步骤。
对比页那个勾选项写着"同时列出两侧内容完全一致的重复（可清理其中一份）"，
用户勾上之后除了多一行提示什么也看不到。

### A14（MEDIUM）对比页"严格 = 0.5"与语义相反

`views.compare.js` —— 阈值越低配得越多。实测同一对（score 0.6968）在 `0.72` 被拒、
在 `0.62` / `0.50` 被接受。用户为了"少误配"去选"严格"，实际得到**最容易误配**的那一档。

**建议**：把三档 value 反过来映射，或重命名。

### A15（LOW）

| 位置 | 问题 |
| --- | --- |
| `matching.duration_similarity` docstring | 说 `tail = max(28, 3×tolerance)`，代码是 `4×`；还断言"合并门禁是 3×"，实际早已统一成 2× |
| `matching` 的 `same_key` | 恒为 `False`（`groups` 的键就是 `dup_key`），文档列的"三种证据"之一永不成立；相邻还有一段重复判断 + 两个 `continue` |
| `plan.duplicate_steps` | 拿 `path` 比 `did` 形式的 `suggested_keep` → 会把"保留项"也生成 trash。目前**没有生产调用点**（只有 `tests/test_core.py`），一旦复用就是丢文件 |
| `organize` 明细 truncate 到 400 | `album-apply` 用 `a["items"]` 生成 `keep_paths` → 勾选 >400 首的合并组时第 401 首起**不执行却报成功** |

## 七、修这些的时候请守住

1. **每条都先写一个能复现的测试**，再改代码。本轮修的 K1 类问题（F1/F2/F4）都是靠复现脚本
   确认"改之前真的会丢文件、改之后 20 轮都不丢"。
2. 改完跑全量：`python -m unittest discover -s tests -t .`（当前基线 741 项全过）。
3. 涉及文件操作的改动，**先读 `SAFETY.md`**，再看 `AGENTS.md` 第 2 节的硬约束。
4. 不要为了让测试变绿去改测试 —— 先确认那条测试是不是在守护一个刻意决定。

---

## 八、第三方审计报告里**未修**的项

来源：桌面 `AUDIT_REPORT.md`（针对 13:06 的审查包快照）。
其余 8 条已修，见 `CHANGELOG.md`；下面 4 条留着，理由各自写明。

### ~~K28（CRITICAL）`exclusive_guard` 只检查不占位~~ —— **已修**

> 修法见 `CHANGELOG.md`：`guard.py` 新增 `exclusive_slot(kind)`，用 FastAPI 的 `yield` 依赖
> 在请求期间真正持有独占任务，响应结束自动收尾。11 个端点（station×4 / tags×2 / folders×5）
> 已换用它，并有测试断言"必须真占位"。下面保留原始分析以作记录。

**原始分析：**

### `exclusive_guard` 只检查不占位

`app/core/guard.py` —— 它是依赖注入时的**快照检查**，通过之后并不持有独占位。
`station_api` / `tags_api` / `sources/folders_api` 的写端点只挂 `Depends(exclusive_guard)`，
**文件里没有任何一处 `get_jobs().create()`**。于是真正写盘期间 `busy()` 仍是 `None`，
扫描或别的写流程可以合法插进来。

**（已修，见上）**

### K29（MEDIUM）FTP 大文件全量下载到内存

`app/sources/ftp.py::open_read` —— 用 `io.BytesIO()` + `retrbinary` 把整个文件下完
再 `buf.read(length)`。扫描器只需要前 128 KB 解析标签，数 GB 的无损文件会导致内存激增。
**建议**：在 `retrbinary` 回调里累计到 `length` 就中断数据连接。

### K30（LOW）WAV / AIFF / APEv2 两次 `f.save()`

`app/core/tags.py` —— `_apply` 内部已保存一次，外层 `if not isinstance(f, ID3): f.save()`
又整体重写一遍。**建议**：统一由一个地方负责保存。

### K31（性能，未做）`files_list` 访问根目录时的查询

审计指出根目录会 `SELECT * FROM track WHERE source_id=?` 把全部行拉到 Python 里过滤。
**未实测确认**，且当前 2301 首规模无感；**建议**在确认后再改成
`WHERE source_id=? AND parent=?`。

---

## 八·补 3、（已修）第六轮：junction / 跨进程 / 临时文件 / 孤儿 / revision

| 问题 | 后果 | 修法 |
| --- | --- | --- |
| junction 绕过 `inside_root` | 扫描/移动/删除/改标签/上传全部落到源根之外 | `_is_reparse` 跳过 + `inside_root_real` realpath 判定 |
| 跨进程无互斥 | 两个实例同时写，静默丢文件 | `core/proclock.py` 文件锁，接进 `Jobs.create/finish` |
| `.mshpart` 永不清理 | 断电后残留占空间 | 启动后台清 >24h 的 |
| 删源留孤儿索引 | `/api/state` 数字对不上、查重扫到幽灵 | 删源必清索引 |
| `revision` 漏检出 | 前端看不到别人改的元数据 | `state_meta` 计数器 |

> 仍存在的架构级限制：`inside_root_real` 是**按操作**调用的（每次多一次 realpath），
> 大目录批量操作会略慢；跨进程锁是**单文件**语义，极端并发下仍是"后来者被拒"而不是排队。

## 八·补 2、（已修）信任边界缺失：读越界 / 写越界 / 只读绕过

2026-10 地毯式排查发现并修复：

| 问题 | 后果 | 修法 |
| --- | --- | --- |
| `cover-raw` 接受任意绝对路径 / `../` | **读库外任意文件**（实测读到 config.toml 里的密码） | `deps.in_source_rel()` 统一边界 |
| `files/list` 接受库外绝对目录 / `../` | **列库外目录** | 同上 |
| `upload/commit` 的 `dst` 允许 `..` | **写出数据源根外** | 拒绝 `..` 与盘符前缀 |
| `upload/commit` 的 `rel` 反斜杠穿越 | 把批次外文件搬进库并删原文件 | 先归一 `\`→`/` 再查 `..` |
| station 三端点 + `tags/restore` 无只读校验 | **只读保险丝失效**，真写盘 | 补 `require_writable` |
| `overwrite` + 校验失败 | 原件与副本**同时消失** | 覆盖前原件进回收站；坏副本改名 `.bad` |
| `fast` + `hard_delete` | 未校验就**永久删源** | 该组合直接拒绝 |
| 17 处类型混淆 | 客户端传错类型 → **500** | `as_list` / `as_text` / `as_dicts` |

> 教训：`startswith(root)` **不是**边界检查 —— `root/../x` 能通过。
> 判断路径必须在**归一化并拒绝 `..`** 之后做，或者用 realpath 比较。

## 八·补、（已修）盘符根数据源的路径畸形

`app/sources/local.py::_native` 之前把 `Y:\` 根转成 `Y:`（裸盘符 = 盘符相对路径），
`os.path.join` 拼出 `Y:x.mp3`，导致 `rel_path` 带盘符、`inside_root` 永远 False、
扫描漏文件（实测少 7 个）。已修 + 重扫 + 加回归测试，详见 CHANGELOG。

> 教训：Windows 上「盘符根」和「子目录根」不是同一类东西。
> 凡是拿数据源根拼路径的地方，都要确认根以分隔符结尾。

## 九、~~拆解 `app/main.py`~~ —— **已完成**

> 2026-10 已拆：`main.py` 2850 → 370 行，拆成 `deps.py` + `routes/{system,jobs,library,compare,files}.py`。
> 路由总数 94 条不变，892 项测试 + 90 接口冒烟全过。
> 原始分析与执行方案保留在下面，作为"怎么安全拆一个巨石文件"的记录。

## （原）唯一没做的优化项：拆解 `app/main.py`

审计报告 4.2(1) 建议把 `main.py`（2700 行 / 49 路由）拆成
`sources_api` / `compare_api` / `files_browser_api` / `jobs_api`。

**这一项我没有做**，原因和做法都写在这里，交给下一轮或接手的人。

### 为什么没在这一轮做

- 拆路由不是"剪切粘贴"，它要求先判断哪些是**共享状态**。现在 `main.py` 里
  有 8 个模块级变量和约 20 个跨端点复用的 helper（`db` / `require_writable` /
  `_folder_map` / `_cover_cache` / `_step_fingerprint` / `_tracks_capped` …），
  它们必须**先**搬到一个独立模块（`app/deps.py`），否则拆出去的路由模块
  与 `main.py` 形成循环导入。
- 半拆比不拆更糟：一半路由在模块里、一半在 `main.py` 里，共享 helper 又没搬，
  结果是"看代码要在两个地方跳"，比现在更难读。
- 这件事需要一个**可回滚点**（备份已在桌面）和一轮完整回归 —— 值得单独一轮做。

### 已经为它铺好的路

- `main.py` 顶部有**导航索引**：49 个路由按 11 个功能组列出，另有模块级状态清单。
- `tests/test_docstring_index.py`（6 项）守着两件事：**导航索引不许和真实路由分叉**、
  **静态路径必须排在带参路由之前**（FastAPI 按注册顺序匹配）。
  → 拆完之后这条测试会自动帮你确认"没有漏注册、没有顺序错"。
- `tests/test_endpoint_guards.py` 走真实路由表核验守卫 —— 拆完能立刻发现漏挂。

### 建议的执行顺序（每步跑一次全量测试）

1. 新建 `app/deps.py`，把 `db()` / `require_writable()` / `_browse_cache_clear()` /
   `_folder_map` / `_folder_rows` / `_dir_summary` / `_cover_cache` + `_serve_cover_bytes` /
   `_step_fingerprint` / `_steps_fingerprint` / `_track_view` / `_source_view` /
   `_device_view` / `_disk_view` / `_tracks_for` / `_tracks_capped` 搬过去。
   `main.py` 改成 `from .deps import *`（显式列出名字）。
2. 拆第一个组（建议 **files_browser**，约 450 行，依赖最少）：新建
   `app/routes/files_browser.py`，用 `APIRouter()`；模块顶部
   `from ..deps import ...`。
3. 在 `main.py` **最底部**（所有定义之后）`from .routes.files_browser import router; app.include_router(router)`。
   放底部是为了避开循环导入（此时 main 的名字已全部就绪）。
4. 跑 `python -m unittest discover -s tests -t .` + `tools/smoke_api.py`；
   特别确认 `/api/files/list`、`/api/files/folders`、`/api/files/under`、
   `/api/files/select`、`/api/files/search`、`/api/files/facets`、`/api/files/move`
   全部 200。
5. 其余三组同法。每拆一组更新 `main.py` 顶部的导航索引（测试会提醒你）。

> 提醒：`/api/sources/status` 必须排在 `/api/sources/{sid}` 之前。
> 如果拆成两个 router，注册顺序要保证这一点 —— `test_docstring_index.py`
> 的 `RouteOrderTest` 会在拆错时报红。
