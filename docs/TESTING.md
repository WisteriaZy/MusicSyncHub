# 测试体系

> 面向改代码的人。回答三个问题：**跑了什么**、**没跑什么**、**新加功能该怎么测**。

---

## 一、几条命令

```bash
# 全量单元测试（当前 892 项）
python -m unittest discover -s tests -t .

# 单个文件
python -m unittest tests.test_transfer -v

# 单个用例
python -m unittest tests.test_move.MoveTest.test_move_within_source

# 前端静态检查（两个都要 0 问题）
python tools/check_frontend.py
python tools/audit_frontend.py

# 拆分专项审计（改了 deps / routes / 端点后必跑）
python tools/audit_split.py

# 接口冒烟（需要服务在跑）
python -m app.main       # 另开一个终端
python tools/smoke_api.py
```

### 两种"跳过"不是失败

| 情况 | 现象 | 说明 |
| --- | --- | --- |
| 没装 node | `OK (skipped=5)` | 5 条要真跑 JS 队列逻辑的测试。设 `MSH_NODE=<node路径>` 就能跑 |
| 没有 mutagen / Pillow | 相关用例跳过 | 缺可选依赖时的正常行为 |

**判断标准**：`OK` 就是过。出现 `FAILED` 才是问题。

---

## 二、741 项都测了什么

按覆盖的"风险面"分组，不是按文件。

### 2.1 文件安全（最重要的一组，约 150 项）

| 文件 | 用例 | 覆盖什么 |
| --- | --- | --- |
| `test_move.py` | 24 | 同库移动、跨目录、目标已存在时三种 policy、只读源拒绝 |
| `test_transfer.py` | 25 | 跨库搬运四步、校验失败不删源、标准/快速模式、目标撞车、**未知 op 契约** |
| `test_tag_safety.py` | 8 | 改标签前的备份、失败回滚、原子写 |
| `test_tags.py` | 13 | 标签读写往返、备份与恢复 |
| `test_non_audio.py` | 24 | 非音频文件不进候选区/不跨库传输、**音频扩展名单一来源**、**版本标签单一来源** |

### 2.2 匹配与查重（约 90 项）

| 文件 | 用例 | 覆盖什么 |
| --- | --- | --- |
| `test_dedup_rules.py` | 36 | 相似度阈值、时长容差、艺术家否决、版本不一致 |
| `test_duplicates.py` | 16 | 分组、代表选择、浪费空间统计 |
| `test_library_fuzzy.py` | 10 | 模糊匹配阶段 |
| `test_duplicates_api.py` | 2 | 接口层 |

### 2.3 智能整理（约 120 项）

| 文件 | 用例 | 覆盖什么 |
| --- | --- | --- |
| `test_organize_album.py` | 66 | 按专辑整理、门槛、展开箭头契约、**禁止无条件 transform 过渡** |
| `test_organize_analyze.py` | 8 | 分析阶段 |
| `test_organize_pertrack.py` | 9 | 逐曲整理 |
| `test_search_organize.py` | 41 | 搜索 + 整理联动 |

### 2.4 并发 / 任务 / 恢复（约 75 项）

| 文件 | 用例 | 覆盖什么 |
| --- | --- | --- |
| `test_pause_recover.py` | 64 | 暂停/继续/取消、崩溃恢复、独占位 |
| `test_job_lifecycle.py` | 11 | 任务状态机 |

### 2.5 接口与前端契约（约 120 项）

| 文件 | 用例 | 覆盖什么 |
| --- | --- | --- |
| `test_platform_features.py` | 67 | 各平台特性、设置校验、能力标记 |
| `test_folder_ops.py` | 34 | 目录创建/改名/删除/解散 |
| `test_library_paging.py` | 18 | 分页、排序 |
| `test_files_api.py` / `test_files_tree_ui.py` | 20 | 文件接口与目录树 UI 契约 |
| `test_endpoint_guards.py` | 5 | **写接口必须挂独占守卫**（走真 HTTP） |
| `test_docstring_index.py` | 6 | **main.py 导航索引不许腐烂**、路由顺序 |

### 2.6 设置与"接线"（4 项）

| 文件 | 用例 | 覆盖什么 |
| --- | --- | --- |
| `test_settings_wiring.py` | 3 | **每个 Settings 字段必须真被消费**（防"界面能改但没效果"） |
| `test_player_queue.py` | 11 | 播放器队列形状契约、`prune`/`step` 跳过失效条目 |

### 2.7 架构约束（"元测试"，专门防回归）

这几条不测功能，测**约定**。它们是这个项目最值钱的测试：

| 用例 | 守的是什么 |
| --- | --- |
| `test_non_audio.py::AudioExtSingleSourceTest` | 音频扩展名只有 `paths.AUDIO_EXTS` 一份 |
| `test_non_audio.py::VersionLabelSingleSourceTest` | 版本类别只有 `paths.VERSION_KINDS` 一份，前端不许抄 |
| `test_settings_wiring.py::SettingsWiringTest` | 每个设置字段都有消费者 |
| `test_organize_album.py::test_no_unconditional_transform_transition` | 基础 CSS 规则里不许有无条件 `transform` 过渡 |
| `test_docstring_index.py` | `main.py` 顶部导航索引必须和真实路由一致 |
| `test_endpoint_guards.py` | 写接口必须挂独占位 |

---

## 三、**没**测什么（重要，别误以为覆盖了）

诚实清单。这些地方改代码要格外小心，因为测试帮不了你：

| 没覆盖的 | 风险 |
| --- | --- |
| **网络数据源连真实服务器** | SFTP / WebDAV / HTTP / SMB / FTP 全部只有单元测试（mock 级），**从没连过真机** |
| **macOS / Linux 实机** | 只在 Windows 上跑过。路径分隔符、换行、编码差异都可能有问题 |
| **大曲库（>3000 首）性能** | 现有测试都是几十到几百首。O(n²) 的地方在上万首时会暴露 |
| **视觉正确性** | 静态检查只能查"引用了不存在的 id/函数"，查不出"盖住了另一个按钮" |
| **并发压力** | 有并发测试，但不是压力测试（多进程、断盘、断电） |
| **真实音频文件多样性** | 测试用生成的小 mp3/wav，没有真实世界的各种脏元数据 |
| **浏览器兼容性** | 只在 Edge/Chromium 内核上验过 |

### 视觉问题怎么查

测试抓不到，必须用真浏览器。工具在 `tools/`：

```js
// tools/cdp_eval.js —— 在真浏览器里跑一段 JS
// tools/cdp_vp.js   —— 多视口截图
node tools/cdp_vp.js 9224 http://127.0.0.1:8787/ 20000 <探测脚本> 1500 <截图目录>
```

**三个已知的坑**（写在 `AGENTS.md` 第 7 节）：
1. `window.resizeTo` 不生效 → 用 `Emulation.setDeviceMetricsOverride`
2. `setDeviceMetricsOverride` 必须在 `navigate` **之后**
3. `requestAnimationFrame` 在无头标签页会被节流 → 用 `setTimeout`

判断"有没有被遮挡"最可靠的办法是 `document.elementFromPoint(x, y)` 打点。

---

## 四、新加功能该怎么测

### 4.1 加一个数据源类型

1. 在 `app/sources/` 新建类，加 `@register`
2. 至少写一条"构造 + 能力标记"测试：`can_write` / `can_delete` / `experimental` 对不对
3. 如果是网络源，`experimental = True` 且 `verify_note` 写清"没验证到什么程度"

### 4.2 加一个写操作

必须有这几条：

```python
# 1) 正常路径
# 2) 只读源 → 403（端点层，不是执行器的 blocked）
# 3) 有任务在跑 → 409
# 4) 目标已存在 → 按 policy 处理，默认不覆盖
# 5) 中途失败 → 源文件还在
```

第 2、3 条用 `TestClient` 走真 HTTP（守卫是 `Depends()` 注入的，直接调函数不会触发）。

### 4.3 加一个设置项

1. 加进 `Settings` dataclass
2. 加进 `_NUM_LIMITS`（如果是数值）
3. **接线** —— 至少在某个业务逻辑里读它
4. `tests/test_settings_wiring.py` 会强制第 3 条：没消费就红

### 4.4 改前端

1. 升 `?v=NN`
2. 跑 `check_frontend.py` + `audit_frontend.py`
3. **真浏览器看一眼** —— 静态检查抓不到布局问题

### 4.5 改 `matching.py` 的阈值

阈值改动很容易"修好一个、弄坏三个"。改之前先看 `docs/TECHNICAL.md` 第 8 节的阈值链，
改之后至少跑 `test_dedup_rules.py` + `test_duplicates.py` + `test_library_fuzzy.py`。

---

## 五、测试隔离（容易踩）

`tests/__init__.py` 把 `MSH_DATA_DIR` 指到进程级临时目录，但**只在进程启动时**。
测试之间靠"设环境变量 → teardown pop 掉"隔离，**中间有空窗期**。

所以新增测试如果要写配置/建库：

```python
def setUp(self):
    self._tmp = tempfile.TemporaryDirectory(prefix="msh-xxx-")
    self._old = os.environ.get("MSH_DATA_DIR")
    os.environ["MSH_DATA_DIR"] = self._tmp.name

def tearDown(self):
    from app.core.db import Database
    Database.close_all()
    from app.core import jobs as J
    J.reset_for_tests()
    if self._old is None:
        os.environ.pop("MSH_DATA_DIR", None)
    else:
        os.environ["MSH_DATA_DIR"] = self._old
    self._tmp.cleanup()
```

**为什么必须这么小心**：发生过一次测试把用户真实的 `config.toml` 覆盖，
用户两个真实数据源被换成临时假源。详见 `SAFETY.md` 事故 2。

换 `MSH_DB` 时还要 `core.jobs.reset_for_tests()`，否则任务注册表还绑在旧库上。

---

## 六、冒烟测试（`tools/smoke_api.py`）

90 个接口用例，特点是**带设置快照恢复**：

```
跑之前：GET /api/settings 拍快照
跑之后：PUT 回去，逐项核对，不一致就计为失败
```

**为什么**：曾经有一条用例为验证"0 = 不限额"，真的把用户的
`station_max_gb` 从 20 改成 0 —— 把"防止上传写满盘"的保护悄悄关掉了。

**新增冒烟用例时不要写"改了就改不回来"的操作。** 如果非要测写操作，
用"故意带错参数"的方式验证参数校验，别改真实状态。

---

## 七、性能相关的测试

没有真正的性能测试。只有这些间接保护：

| 机制 | 在哪 |
| --- | --- |
| 快盘/慢盘自动降并发 | `core/device.py::effective_workers`、`SLOW_MBPS = 15.0` |
| 大文件不参与全量哈希 | `hash_size_limit_mb`（默认 512） |
| 对比结果上限 | `max_compare_tracks`（默认 8000） |
| 文件浏览缓存 | `_FILE_BROWSE_CACHE`，TTL 20 秒 |

改这些地方时注意：它们都是"防卡死"的闸门，放宽要谨慎。

---

## 八、当前基线（改动后请对比）

```
单元测试    892 项   全过（没装 node 时 5 项跳过）
接口冒烟     90 个   全过
前端检查           0 问题
深度审计           0 问题
```

如果改动后这些数字变了（尤其"全过"变成有失败），**先查自己的改动**，
不要先去改测试 —— 有些测试是在守护刻意做出的决定
（例：`test_unknown_op_still_skipped`）。
