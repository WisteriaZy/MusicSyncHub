# 安全须知 —— 别让工具改到你的真实数据

> 这份文档是项目的一部分：它记录**真实发生过的事故**和由此定下的规则。
> 改任何涉及文件写入的代码之前，请先读完。
> 用户能依赖的行为见 [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) 第 17 章；
> 实现位置见 [`docs/TECHNICAL.md`](docs/TECHNICAL.md) 第 18 章。

这份文档是给**在本项目上继续开发/跑脚本的人**看的。
它记录的是**真实发生过的事故**，不是假想风险。

---

## 一句话结论

**跑测试、跑冒烟、跑任何临时脚本之前，确认 `MSH_DATA_DIR` 指向临时目录。**
`app/core/config.py` 里已经加了一层测试兜底，但那只在
"进程里导入了 `tests` 包"时生效 —— 临时脚本不在保护范围内。

---

## 真实事故记录

### 事故 1：冒烟测试把关用户的容量上限关掉了

`tools/smoke_api.py` 里有一条用来说明"0 = 不限额"合法的用例：

```python
('/api/settings', 'PUT', {'station_max_gb': 0}, {200}, '设置(0=不限额)'),
```

它**确实返回 200** —— 于是真的写进了用户的 `config.toml`：
中转站容量上限从 **20 GB 变成 0（不限额）**。

用户的"防止上传把盘写满"这道保护，被一个号称"只做只读或幂等调用"的
冒烟测试**悄悄关掉了**，没有任何提示。那个文件开头的注释写的就是
"只做只读或幂等的调用；写操作一律故意带错参数" —— 它违反了自己的约定。

**现状**：冒烟开始前给设置拍快照，跑完 PUT 回去并逐项核对，不一致就计为失败。

### 事故 2：测试覆盖了用户的 config.toml

测试之间靠"设 `MSH_DATA_DIR` 到临时目录、teardown 再 pop 掉"隔离。
缺口在于：**pop 之后到下一个测试设好之前**它是空的 ——
这期间任何 `save_config()` 都会落到用户真实的那份 `config.toml`。

修 B8 时加的一个测试就在 `finally` 里调 `load_config(force=True)`，
正好撞上这个空窗期，把用户的两个真实数据源（`win-music` 2301 首、
`Linux-music` 只读）换成了临时目录里的假数据源。

**发现方式**：下一项测试报"未找到数据源 win-music"。
晚一轮发现，用户下次重启就是"我的音乐库怎么不见了"。

**现状**：两层防护，见下。

### 事故 3：早期测试往真实 `station/` 里写过文件

9 个垃圾文件被写进用户真实的中转站目录，后来手工清理。

---

## 两层防护（现状）

### 第一层：`tests/__init__.py`

测试包导入时就把 `MSH_DATA_DIR` 指向一个**本进程专属的临时目录**。
测试自己的隔离照旧生效；一旦有人 `pop` 掉它，
下一个读取者拿到的是那个临时路径而不是真实目录。

### 第二层：`app/core/config.py::data_dir()`

```python
if override:            p = Path(override)
elif _in_test_run():    p = Path(tempfile.gettempdir()) / "msh-tests-fallback"
else:                   p = Path.home() / ".music-sync-hub"
```

`_in_test_run()` 看 `sys.modules` 里有没有 `tests` 包 ——
正常启动应用不会有它，所以**生产行为一个字没变**。

实测（模拟那个空窗期）：

```
真实目录基线: 12 个文件
空窗期 mappings / staging / tag-backups 都落在 ...\Temp\msh-tests-fallback
各写一个文件之后: 真实目录变化 = 无
```

---

## ★ 必须遵守的规则

### 规则 1：取数据目录只能调 `config.data_dir()`

**不要**自己写 `os.environ.get("MSH_DATA_DIR") or os.path.expanduser("~/.music-sync-hub")`。

自己拼的那份绕过了第二层防护。项目里已经有**三处**犯过这个错：

| 位置 | 后果 |
|---|---|
| `mappings_api._data_dir()` | 映射规则会写进真实目录（映射丢了用户看不出来）|
| `upload_api` 的暂存区 | 暂存文件会落进真实目录 |
| `tags._backup_dir()` 的兜底分支 | 标签备份会落进真实目录 |

顺带一提，`getattr(cfg, "data_dir", "")` 也是错的 ——
`AppConfig` **没有** `data_dir` 属性，永远拿到空串
（`tags.py` 和 `upload_api.py` 都犯过，即台账 B16）。

### 规则 2：改完代码必须重跑一遍完整测试

```powershell
cd C:\Users\Administrator\Desktop\MusicSyncHub
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

跑完顺手看一眼 `config.toml` 有没有被动：

```powershell
(Get-FileHash "$env:USERPROFILE\.music-sync-hub\config.toml" -Algorithm SHA256).Hash
```

### 规则 3：绝不用 PowerShell 写源码文件

`Set-Content` / `Out-File` 会按系统代码页（cp936）写，
把中文注释变成乱码、还可能吞掉换行 —— 项目里因此毁过一次
`app/core/organize.py`（后来从 `.pyc` 重建）。

用编辑器工具，或 Python：

```python
Path(f).write_text(text, encoding="utf-8")
```

（`Get-Content` 显示 UTF-8 文件时看着像乱码是**正常的**，
文件本身没问题 —— 用 `read` 工具确认。）

### 规则 4：临时脚本自己设数据目录

临时的验证脚本**不在**测试兜底范围内（进程里没有 `tests` 包）：

```python
import os, tempfile
os.environ["MSH_DATA_DIR"] = tempfile.mkdtemp(prefix="msh-scratch-")
os.environ["MSH_DB"] = os.path.join(os.environ["MSH_DATA_DIR"], "i.sqlite3")
# 必须在 import app.* **之前**设好
```

### 规则 5：在真实库上做验证，用一次性文件

要验证搬运/整理/删除这类链路时，**自己造一个测试文件**，
不要拿用户的真歌试。测完把两边（含回收站）都清干净。

判断一个东西是不是"测试残留"的方法：

- 看大小和内容（测试文件常是 `200` 字节、或内容是 `01 02 03 04...`）
- 看修改时间是否对得上某次测试
- **拿不准就不要删** —— `Y:\1.txt` 就差点被当成残留删掉，
  实际是用户自己 143 KB 的文件清单

---

## 与文件安全有关的产品约定（改代码时别破坏）

| 约定 | 位置 |
|---|---|
| 删除一律进 `<root>/.music-sync-trash/日期/`，只有 `hard=true` 才真删 | `sources/local.py::remove` |
| 写文件先写 `.mshpart` 再改名，中断不留半截文件 | `sources/base.py` / `local.py` |
| 同名冲突默认**跳过**，绝不自动覆盖 | `Executor` 的 `policy` |
| 跨库搬运是"复制 → 校验 → 再删源"，任何一步失败源都不消失 | `core/transfer.py` |
| 校验不过时删掉目标那份、**保留源** | 同上 |
| 删源失败时**绝不回滚目标**（回滚会把好文件删掉） | 同上 |
| 越界路径（`..`、别的盘）一律拒绝，不放宽 | `ops._inside_root`、`folders_api._inside` |
| 只读数据源上所有写操作返回 403，读操作不受影响 | `main.require_writable` |
| 同一方案内两条步骤写同一目标时**自动改名**，绝不覆盖 | `ops._reserve_targets` + `_claim_target` |
| 整理方案的指纹包含源文件**大小** → 文件被换掉会要求重新生成 | `search_api._plan_hash` |

---

## 已知项（**尚未**防护，改到相关代码时留意）

| # | 内容 | 风险 |
|---|---|---|
| 1 | `/api/plan` + `/api/execute`（对比页路径）**没有方案指纹** | 对比方案放久了再执行可能基于过期信息。缓解：`Executor` 会逐步骤重查（源不存在→error、目标已存在→按 `policy` 跳过）|
| 2 | `artist_conflict` 在"文件名完全一样 + 艺术家相似度 0.55~0.72"时不否决 | 缺口窄（例：`Windows Only` vs `Linux Only` = 0.636）。误配的后果是这两首从"独有"列表消失 |
| 3 | `call_with_timeout` 超时后线程仍在跑（daemon，杀不掉） | Python 没有安全的杀线程手段；界面文案已说明"超时 = 可能后台仍在写" |
| 4 | 暂存区清理是**真删**（`os.remove`/`rmtree`） | 有意为之：暂存区是"这次上传流程的中间态"的显式删除 UI，中转站才走回收站 |

---

## 验证这套防护是否还成立

随时可以跑这一段（它会**故意尝试**污染真实数据目录，然后核对有没有改动）：

```powershell
cd C:\Users\Administrator\Desktop\MusicSyncHub
# 记下基线
Get-ChildItem "$env:USERPROFILE\.music-sync-hub" | Select-Object Name,Length
# 跑完再核对一次，长度应完全一致
```

或者直接跑完整回归（719 项单元测试 + 90 个冒烟接口）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe tools\smoke_api.py
(Get-FileHash "$env:USERPROFILE\.music-sync-hub\config.toml" -Algorithm SHA256).Hash
```

**跑完 `config.toml` 的哈希必须和跑之前一样。** 不一样就说明防护破了，
要立刻查是哪条路径绕过了 `data_dir()`。
