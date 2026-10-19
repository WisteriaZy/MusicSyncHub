# 参与开发

感谢你愿意花时间。这个项目的核心约束只有一条：**绝不弄丢用户的文件**。
任何改动都要先过这一关。

---

## 先读这四份

| 文档 | 读它是为了 |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | **第一份读这个**。10 条硬约束、代码地图、改完怎么验 |
| [`SAFETY.md`](SAFETY.md) | **必须读**。三起真实事故和由此定下的规则 |
| [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) | **动手前读**。已经知道的问题，别重复踩 |
| [`docs/TESTING.md`](docs/TESTING.md) | 测试怎么跑、覆盖到哪、新功能怎么测 |
| [`docs/TECHNICAL.md`](docs/TECHNICAL.md) | 知道代码怎么组织、为什么这么设计 |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | 知道用户看到的是什么、被承诺了什么 |

> `AGENTS.md` 是给接手的人（含 AI 助手）看的操作手册，规则密度最高；
> `SAFETY.md` 是那些规则背后的故事，两者互补。

---

## 环境

```bash
# 1) 装依赖（推荐 uv）
uv sync

# 2) 跑起来
python -m app.main            # 然后打开 http://127.0.0.1:8787
# Windows 也可以直接 start.bat
```

Python **3.12+**（用到 `tomllib`）。`pyproject.toml` 里是全部依赖。

### 可选：node

有 5 条测试要用 node 真跑队列逻辑（把 `app.js` 里那段抠出来执行）。
没装 node 时它们会**干净跳过**，不算失败。给 node 时优先用环境变量：

```bash
MSH_NODE=/path/to/node python -m unittest discover -s tests -t .
```

---

## 改代码前请务必知道

### 1. 音频扩展名只有一个来源

`app/core/paths.py` 的 `AUDIO_EXTS` / `is_audio()`。
**不要在别处抄第二份清单** —— 有一条测试专门扫这个
（`tests/test_non_audio.py::AudioExtSingleSourceTest`）。

前端要判断"是不是音频"时，从 `/api/system` 的 `audio_exts` 拿，
不要在 JS 里写死。

### 2. 路径坐标系统一用"源内相对路径"

`MusicSource.abs_path()` 转绝对、`rel_path()` 转相对。
两个操作同一目标的不同代码路径（比如 `_reserve_targets` 和 `_claim_target`）
**必须用同一套坐标**，否则会出现"占位没生效 → 两个步骤写同一个目标 → 静默丢文件"。
这个 bug 真实发生过，见 `docs/TECHNICAL.md` 第 10 章。

### 3. 独占位不是装饰

`app/core/jobs.py` 的 `EXCLUSIVE_KINDS` + `JobState.exclusive` 决定
"同一时间只允许一个写流程"。新增写操作时想清楚：

- 它会不会改动文件或索引？→ 要挂 `exclusive_guard`
- 它是只读但需要一致快照（比如方案预演）？→ 用 `get_jobs().ensure_free()`
- 它跑在后台线程？→ 用 `bgjob.run_in_background`

**任何一个环节漏了，界面上就会出现"两个流程同时改同一批文件"。**

### 4. 不要给基础规则加 `transform` 过渡

```css
/* 坏 */
.btn { transition: transform .12s; }        /* 只为 :active 的位移服务 */
.btn:active { transform: translateY(1px); }

/* 好 */
.btn { transition: background .12s; }
.btn:active { transform: translateY(1px); }  /* 位移照样有过渡 */
```

写成无条件的话，浏览器会对该元素**任何** transform 变化做动画 ——
包括"被父级布局带着走"，表现就是按钮里的箭头/图标自己滑过去。
这个坑踩过三次（`.btn`、`.tile`、`.stat`），现在有测试守着
（`tests/test_organize_album.py::test_no_unconditional_transform_transition`）。

### 5. 测试不许碰真实数据

`tests/__init__.py` 会把 `MSH_DATA_DIR` 指到进程级临时目录。
新增测试如果要写配置、建库，**自己再指一个临时目录**，别依赖默认值 ——
曾经有一次测试把真实的 `config.toml` 覆盖了，用户所有数据源全部丢失。

---

## 提交前跑这些

```bash
# 单元测试（应为 892 项全过；没装 node 时 5 项跳过）
python -m unittest discover -s tests -t .

# 后端接口冒烟（应为 90 个接口全过）
python tools/smoke_api.py

# 前端静态检查（都应为 0 问题）
python tools/check_frontend.py
python tools/audit_frontend.py

# 拆分专项审计（改了 deps / routes / 端点后必跑）
python tools/audit_split.py
```

`smoke_api.py` 会**临时改设置再恢复**，跑完请确认
`%USERPROFILE%\.music-sync-hub\config.toml` 的哈希没变。

---

## 提交规范

### 分支

- `main` 保持可运行
- 功能/修复开分支：`feat/xxx`、`fix/xxx`

### 提交信息

用中文或英文都行，但要**说清改了什么、为什么**。
涉及安全不变量的改动，请把理由写进 commit message 或代码注释里 ——
下一个人需要知道"这里为什么不能简化"。

```
fix(ops): 目标占位改用相对路径

`_reserve_targets` 存的是相对路径，`_claim_target` 传的是绝对路径，
键永远匹配不上 → 同方案内两条步骤写同一目标时只落一个文件，却报"已检测到撞车"。

对应测试：tests/test_move.py::SameTargetTest
```

### 不要在提交里包含

- `.venv/`、`__pycache__/`
- 任何 `config.toml` / `secrets.toml` / `*.sqlite3`
- 你的真实曲库路径（`.gitignore` 已挡，但写文档时也留意）

---

## 特别欢迎的贡献

| 方向 | 说明 |
| --- | --- |
| **网络数据源实测** | SFTP / WebDAV / HTTP 目录 / SMB / FTP 目前都是**实验性**（没有对真实服务器验证过）。如果你有环境，实测并报告结果非常有价值 |
| **非 Windows 平台** | 目前只在 Windows 上实测过。macOS / Linux 上的路径、换行、编码问题都可能存在 |
| **更大的真实曲库** | 现有测试主要用几百到两千首的库。上万首的性能表现还不清楚 |

---

## 报告问题

请带上：

1. 你做了什么（具体操作顺序）
2. 期望看到什么、实际看到什么
3. **日志**：`<数据目录>/logs/msh.log`，或界面「设置 → 后台日志」里复制
4. 如果涉及文件操作，说清有没有丢文件（这永远优先处理）
