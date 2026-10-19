"""打两个包：AI 审查包（给人/模型读）和开箱即用测试包（给人动手试）。

## 为什么要两个包

- **审查包**：代码 + 文档 + 测试，目标是"能读、能评、能跑起来验证"。
  带一份 `PROJECT_INDEX.md` 和 `REVIEW_NOTES.md`，让审查者 5 分钟知道该看哪。
  **不含** `.venv`（体积大且与平台绑定）、不含演示音频、不含任何用户数据。

- **测试包**：能双击就跑的演示环境。**含** `.venv`（开箱即用）、含演示音频、
  含包内数据目录，**没有任何真实用户数据**。前端打上测试模式水印。

用法：
    python tools/build_packages.py                # 两个都打
    python tools/build_packages.py --only review  # 只打审查包
    python tools/build_packages.py --only test    # 只打测试包
    python tools/build_packages.py --out D:\\tmp   # 指定输出目录
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = Path.home() / "Desktop"

# ---------------------------------------------------------------- 打包内容规则

# 这些目录/文件**任何包都不该含**（用户数据、运行状态、本机痕迹）
NEVER = [
    ".git", ".github/workflows",  # workflows 不强求，但审查包不需要
    ".music-sync-hub", "userdata", "station",
    ".music-sync-trash", "uploads", "tag-backups", "logs",
    "config.toml", "secrets.toml", "mappings.json",
    "index.sqlite3", "index.sqlite3-shm", "index.sqlite3-wal",
    "*.mshpart", "*.mshtag",
    "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".ecdp2", "ui-shots", "ui-screenshots", "msu2_analysis", "organize_recovery",
    "scratch", "tmp",
    "MusicSyncHub-backup-*.zip", "MusicSyncHub-全量备份-*",
    "*-审查包.zip", "*-测试包.zip", "*.log", "*.err",
]

# 审查包额外排除：虚拟环境、演示音频（体积大、与平台绑定）
REVIEW_EXTRA = [".venv", "demo", "run-test.py"]
# 测试包保留 .venv 和 demo


def _matches(rel: str, patterns: list[str]) -> bool:
    import fnmatch
    parts = rel.split("/")
    for pat in patterns:
        p = pat.rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(parts[-1], p):
            return True
        # 祖先目录命中（目录排除整棵子树）
        for i in range(1, len(parts)):
            if fnmatch.fnmatch(parts[i - 1], p):
                return True
    return False


def collect(extra_excludes: list[str]) -> list[Path]:
    pats = NEVER + extra_excludes
    out: list[Path] = []
    for f in sorted(ROOT.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(ROOT).as_posix()
        if _matches(rel, pats):
            continue
        out.append(f)
    return out


# ---------------------------------------------------------------- 版本信息

def _version() -> dict:
    """从源码里读出版本、资产版本、测试数等，写进索引。"""
    info = {"built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'version\s*=\s*"([^"]+)"', py)
        info["version"] = m.group(1) if m else "?"
    except Exception:
        info["version"] = "?"
    try:
        html = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
        m = re.search(r"app\.css\?v=(\d+)", html)
        info["asset_version"] = m.group(1) if m else "?"
    except Exception:
        info["asset_version"] = "?"
    try:
        n = 0
        for f in (ROOT / "tests").glob("test_*.py"):
            n += len(re.findall(r"^\s+def test_", f.read_text(encoding="utf-8"), re.M))
        info["tests"] = n
    except Exception:
        info["tests"] = "?"
    try:
        # 冒烟脚本里的用例是 `('/api/...', 方法, ...)`，也有 f-string 形式。
        # 用"以 /api 开头的调用"来数，和运行时 `len(CASES)` 对得上。
        # 用 ast 精确数 `CASES = [...]` / `CASES += [...]` 的元素个数，
        # 和运行时 `len(CASES)` 完全一致（正则会把注释和辅助调用也算进去）。
        import ast as _ast
        src = (ROOT / "tools/smoke_api.py").read_text(encoding="utf-8")
        _tree = _ast.parse(src)
        _n = 0
        for node in _tree.body:
            if isinstance(node, _ast.Assign):
                if any(getattr(t2, "id", "") == "CASES" for t2 in node.targets) \
                        and isinstance(node.value, (_ast.List, _ast.Tuple)):
                    _n += len(node.value.elts)
            elif isinstance(node, _ast.AugAssign):
                if getattr(node.target, "id", "") == "CASES" \
                        and isinstance(node.value, (_ast.List, _ast.Tuple)):
                    _n += len(node.value.elts)
        info["smoke"] = _n
    except Exception:
        info["smoke"] = "?"
    try:
        # ★ 路由已经拆到 app/routes/* 和 app/*_api.py，不再只数 main.py。
        #   按**唯一路径**计数（同一路径的 GET/POST 算一条）。
        paths = set()
        for f in (ROOT / "app").rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            src = f.read_text(encoding="utf-8")
            for m in re.finditer(
                    r"@(?:app|router)\.(?:get|post|put|delete|patch)\(\s*[\'\"]([^\'\"]+)",
                    src):
                paths.add(m.group(1))
        info["routes"] = len(paths)
    except Exception:
        info["routes"] = "?"
    return info


# ---------------------------------------------------------------- AI 审查包

PROJECT_INDEX = """# 项目索引 —— 给审查者

> 自动生成。生成时间：{built_at}　版本：{version}　前端资产版本：?v={asset_version}
> 测试：{tests} 项　接口冒烟：{smoke} 个　API 路由：{routes} 条

## 这个项目是什么

Windows 上的**本地**工具：管理多个音乐库（本地盘 / 映射的网络盘），做
**跨库对比、查漏、查重、安全搬运**。后端 FastAPI + SQLite，前端纯原生 JS（**无构建步骤**）。

设计原则一句话：**工具只做计算和搬运，"要不要动这个文件"永远由人决定。**

它是**个人自用工具**，不是产品：没有账号体系、没有多租户、没有 Docker 化。
这些是设计选择，不是未完成的缺陷（见 `REVIEW_NOTES.md` 第三节）。

## 最快的验证方式

```bash
uv sync                                      # 或 pip install -e .
python -m unittest discover -s tests -t .    # 应 {tests} 项全过
python tools/check_frontend.py               # 应为 0 问题
python tools/audit_frontend.py               # 应为 0 问题
python tools/audit_split.py                  # 模块导入/未定义名字/路由差分，应为 0 问题
python -m app.main                           # 打开 http://127.0.0.1:8787
python tools/smoke_api.py                    # 需服务在跑；应 {smoke} 个接口全过
```

没有 node 时，5 条 JS 队列测试会**干净跳过**（不是失败）。要跑它们：
`set MSH_NODE=<node 路径>`。

## 建议的审查顺序

| 顺序 | 文件 | 为什么先看这个 |
| --- | --- | --- |
| 1 | `SAFETY.md` | 三起真实事故 + 由此定下的规则。**先看这个再批评设计**，很多"看起来可以简化"的地方是有血的教训的 |
| 2 | `docs/TECHNICAL.md` 第 18 章 | 安全不变量清单 + 每条的实现位置，一条一条对代码 |
| 3 | `app/core/ops.py` | 执行器。真正动文件的地方，最容易出事 |
| 4 | `app/core/transfer.py` | 跨库传输：复制→校验→删源、断点续传元数据、失败分支 |
| 5 | `app/sources/local.py` | 回收站（`.music-sync-trash` + `_origin.json`）、断点续传落盘、reparse point 过滤 |
| 6 | `app/core/matching.py` | 匹配算法。多阶段 + 分数下限 + 艺术家否决，逻辑最密 |
| 7 | `app/core/plan.py` | 方案生成。三种方向的语义、镜像删除的互斥规则 |
| 8 | `app/core/jobs.py` + `app/core/guard.py` + `app/core/proclock.py` | 并发控制：进程内独占位 + 跨进程文件锁 |
| 9 | `app/deps.py` + `app/routes/*` | HTTP 层。`main.py` 只剩装配（约 400 行），路由按功能拆成 6 个模块 |
| 10 | `docs/TECHNICAL.md` 第 13 章 | 完整 API 表，配合 `routes/*` 看 |

## 最近一轮改了什么（2026-10）

审查时这些**已经是修过的状态**，不用再报：

- `app/main.py` 从 2850 行拆成 `main.py`(装配) + `deps.py` + `routes/{{system,jobs,library,compare,files,trash}}.py`
- **回收站可以还原**：删除时记 `_origin.json`，`GET/POST /api/trash*`
- **跨库传输支持断点续传**：`.mshpart` + `.mshpart.json` 元数据（src_id/路径/大小三者匹配才续）
- 方案指纹（`plan_hash` + 逐步 `step_hashes`）
- GZip 中间件、目录聚合缓存、根目录 SQL 查询、封面 LRU
- 路径信任边界：`cover-raw` / `files-list` 越界读堵住；`upload/commit` 的 `..` 穿越堵住
- 只读源：station / tags-restore 补上 `require_writable`

## 我最想被挑毛病的地方

1. **网络数据源全是实验性** —— SFTP / WebDAV / HTTP / SMB / FTP 没有对真实服务器验证过
2. **只有 Windows 实测过** —— 盘符根、junction、路径大小写在 macOS/Linux 上行为不同
3. **匹配算法阈值是经验值** —— `artist_conflict` 在名字相似度 0.55~0.72 区间不否决（已知缺口）
4. **断点续传的元数据是按"目标名"存的** —— 只校验 src_id/路径/大小；如果同一目标被两个不同源先后写，
   依赖的是"元数据不匹配就重来"，值得你挑一下这个推理是否足够
5. **`Executor.run` 遇到 `BaseException`** 靠 180 秒陈旧回收释放独占位（不是 try/finally）
6. **`inside_root_real`** 每次操作多一次 realpath —— 大目录批量操作会略慢

## 文件清单

```
{tree}
```

## 统计

| 项 | 值 |
| --- | --- |
| Python 文件 | {pyfiles} |
| JS 文件 | {jsfiles} |
| Python 代码行 | {pylines} |
| JS 代码行 | {jslines} |
| 测试项 | {tests} |
| 冒烟用例 | {smoke} |
| API 路由（全部） | {routes} |
"""

REVIEW_NOTES = """# 审查说明 —— 请先读这页

## 一、怎么快速判断"这玩意儿靠不靠谱"

别先读代码。按这个顺序，20 分钟能有结论：

1. **跑测试**：`python -m unittest discover -s tests -t .`
   → 应 {tests} 项全过。有一条失败就说明这个快照有问题。

2. **看安全不变量**：`docs/TECHNICAL.md` 第 18 章列了每条承诺和它的实现位置。
   挑 5 条去代码里找。**找不到实现的那条就是我的 bug。**

3. **看它会不会删我的文件**：删除只有两个入口 ——
   `app/sources/local.py::remove()`（文件）和 `trash_dir()`（目录），
   都进 `.music-sync-trash/<日期>/`，并把原路径写进同目录的 `_origin.json`。
   找找有没有绕过它们的 `os.remove` / `Path.unlink` / `shutil.rmtree`。

4. **看搬运会不会丢源文件**：`app/core/transfer.py::transfer_one`。
   顺序必须是 复制 → 校验 → 删源；**没校验过不许删源**（`fast` + `hard_delete` 会被拒）。

5. **看断点续传会不会拼错内容**：`app/sources/base.py::_resume_offset` +
   `local.copy_from`。只有 `{{src_id, src_path, src_size}}` 三者全等才续，
   续完再比总大小。

## 二、这个项目的历史包袱（会影响你的判断）

这是个**边用边修**的个人工具，长出来时没有架构设计。所以：

- 前端 18 个 `<script>` 顺序加载、无构建、无模块化 —— 我知道，这是故意的
  （维护者不是程序员，不想引入构建链）。
- 注释里有很多"踩过的坑"记录，读起来啰嗦，但那是有意留的 ——
  **防止后来的人（包括我自己）再把同样的 bug 写回去**。
- `app/main.py` **已经拆过了**（2850 → 约 400 行）：共享状态在 `app/deps.py`，
  路由在 `app/routes/*`。如果你看到"巨石文件"的印象，那是旧版本的描述。

## 三、已知问题（不用再报，我都知道）

按严重程度排。完整清单见 `docs/KNOWN_ISSUES.md`（里面每条都标了修没修）。

| 严重度 | 问题 | 位置 |
| --- | --- | --- |
| 中 | `_do_transfer` 把调用方的 policy 丢掉了，硬编码 `policy="skip"` | `app/core/ops.py::_do_transfer` |
| 中 | `artist_conflict` 在名字相似度 0.55~0.72 区间不否决 | `app/core/matching.py` |
| 中 | 单步超时 120 s、心跳 180 s：超时后工作线程仍在跑 | `app/core/ops.py` + `jobs.py` |
| 中 | 崩溃后"接着做完"的恢复链有断点（3 处） | `app/core/jobs.py` 等 |
| 中 | FTP `open_read` 会把整个文件读进内存 | `app/sources/ftp.py` |
| 中 | 整理算法若干边界（A1–A14，见 KNOWN_ISSUES） | `app/core/organize.py` / `matching.py` |
| 低 | WAV / AIFF / APEv2 标签保存调了两次 `f.save()` | `app/core/audio.py` |
| 低 | `Executor.run` 遇 `BaseException` 靠 180 s 陈旧回收释放独占位 | `app/core/ops.py` |

### 明确是**设计选择**，不是缺陷

- **没有登录/鉴权、CORS 全开**：这是单用户本地工具，默认只监听 `127.0.0.1`。
  如果你要把它暴露到公网，那是**另一个产品**，需要整套安全模型。
- **没有 Docker / i18n / 移动端适配**：同样，属于"给别人用"才需要的东西。
- **网络数据源标 `experimental`**：代码完整但没连过真实服务器，界面上有警告标记。
- **断点续传依赖元数据**：目标不是本地、或适配器不支持随机读时，自动退回整份复制。

## 四、明确没验证过的（不算 bug，但别当成能用）

- **SFTP / WebDAV / HTTP 目录 / SMB / FTP**：从没连过真实服务器
- **macOS / Linux**：只在 Windows 上实际跑过
- **MTP 便携设备**：只探测、不读写（标准库做不到）
- **GB 级大文件**：续传只测过 2 MiB / 20 MiB，没测过 10 GB+
- **十万级曲库**：真实数据测到 2301 + 2308 首，再大没测
- **浏览器能播的格式是子集**：`.ape` / `.wv` / `.wma` / `.dsf` 服务端只转发不解码，
  点了不会响（文档已写明）

## 五、我最想要你回答的四个问题

1. **有没有哪条路径可能静默丢文件？** 任何"复制/移动/删除"的失败分支都值得看。
2. **独占位有没有漏挂？** 写端点必须挂 `exclusive_guard` / `exclusive_slot`，
   漏一个就可能两个流程同时改同一批文件。
3. **路径坐标系有没有混用？** 源内相对路径 vs 绝对路径混用曾经导致过
   "占位没生效 → 同一目标写两次 → 丢文件"。
4. **断点续传的拼接推理成立吗？** 如果 `.mshpart` 与 `.mshpart.json` 不是同一次复制产生的
   （或被人手动改过），我的"三者匹配才续"是否足够？

## 六、环境

- Python 3.11+（开发用 3.13）
- 依赖见 `pyproject.toml`
- 可选：node（5 条 JS 队列测试需要，设 `MSH_NODE` 指过去）

## 七、如果只想看一个文件

`app/core/ops.py`。它是唯一真正动文件的地方，其它都是算和看。
"""


def build_review(out_dir: Path) -> Path:
    info = _version()
    files = collect(REVIEW_EXTRA)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"MusicSyncHub-审查包-{stamp}.zip"

    # 统计
    pyf = [f for f in files if f.suffix == ".py"]
    jsf = [f for f in files if f.suffix == ".js"]
    pylines = sum(len(f.read_text(encoding="utf-8", errors="replace").splitlines()) for f in pyf)
    jslines = sum(len(f.read_text(encoding="utf-8", errors="replace").splitlines()) for f in jsf)

    # 目录树
    tree_lines = []
    seen_dirs = set()
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        parts = rel.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i]) + "/"
            if d not in seen_dirs:
                seen_dirs.add(d)
                tree_lines.append("  " * (i - 1) + parts[i - 1] + "/")
        tree_lines.append("  " * (len(parts) - 1) + parts[-1])
    tree = "\n".join(tree_lines[:400])
    if len(tree_lines) > 400:
        tree += f"\n  …（还有 {len(tree_lines) - 400} 项）"

    idx = PROJECT_INDEX.format(
        built_at=info["built_at"], version=info["version"],
        asset_version=info["asset_version"], tests=info["tests"],
        smoke=info["smoke"], routes=info["routes"], tree=tree,
        pyfiles=len(pyf), jsfiles=len(jsf), pylines=pylines, jslines=jslines)
    notes = REVIEW_NOTES.format(tests=info["tests"], routes=info["routes"])

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("PROJECT_INDEX.md", idx)
        z.writestr("REVIEW_NOTES.md", notes)
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())
    return dest


# ---------------------------------------------------------------- 测试包

TEST_README = """# Music Sync Hub —— 测试包（开箱即用）

> **这是测试/演示环境。** 数据都在本目录内，不会碰你真实的音乐库。
> 页面顶部有「测试模式」横幅、右下角有 TEST 角标 —— 有这些才说明包是对的。

---

## 怎么跑

### 一条命令（推荐）

双击同目录下的 **`启动测试.bat`**，或者在命令行里：

```bat
python run-test.py
```

浏览器会自动打开 `http://127.0.0.1:8787`。

第一次启动会自动扫描演示曲库（9 首），大概 3-5 秒。

### 没有 Python？

这个包**已经自带** `.venv`（虚拟环境 + 全部依赖），用 `启动测试.bat` 就行。
它内部用的是包内的 `python.exe`，不需要你另外装。

> 如果提示找不到 python，说明打包时漏了 `.venv`，请联系提供这个包的人。

---

## 里面有什么

```
demo/                    ← 演示曲库（可以随便删、随便改）
  0单曲/                 Loose Single.mp3 / Untagged Track 01.mp3（无元数据）
                        readme.txt（非音乐文件，用来试"非音频怎么处理"）
  Night Drive/           3 首，专辑 Midnight City
  Piano Days/            2 首，专辑 Quiet Room
  山茶花/                2 首，艺术家 かめりあ

userdata/                ← 运行数据（配置、索引、日志），删掉就全部重置
run-test.py              ← 启动器
启动测试.bat              ← 双击这个
docs/USER_GUIDE.md       ← 完整用户手册
```

---

## 建议按这个顺序试

每一步都对应手册里的一章。

### 1. 看它认不认得你的音乐（第 6 章）

左侧「数据源」→ 点「演示曲库」→ 看「已索引 9 首」。
点「扫描」再跑一次，看它多快。

### 2. 浏览和一个一个听（第 7 章）

「文件浏览」→ 左边选目录，右边列文件。
双击一行试听；底部播放器有条进度条可以拖。

### 3. 批量挑歌 → 放进候选区（第 8 章）

勾几首歌 → 「加入候选区」→ 左侧会出现候选区面板。
**试一下把它和「移动」的区别看出来**：候选区只是记着，不搬文件。

### 4. 改标签（第 7 章）

选一首 → 右键 → 「编辑标签」→ 改标题/作者/专辑 → 保存。
回去看文件列表，标签变了。

### 5. 删除 → 从回收站恢复（第 13 章）

选一首 → 删除。然后去 `demo/` 目录看：文件没消失，被移到了
`demo/.music-sync-trash/<日期>/`。
**这就是"删除进回收站"的意思。**

### 6. 查重（第 11 章）

「查重」→ 阈值拉到最松 → 开始扫描。
演示库里没有重复文件，所以结果是空的 —— **空结果也是结果**。
你可以手动复制一份 mp3 到 `demo/` 里再扫，就能看到分组。

### 7. 智能整理（第 12 章）

「曲库浏览」→「智能整理」→ 分析。
注意看 **「无元数据」那一栏**：`Untagged Track 01.mp3` 会被单独列出来，
**不会**被自动归类。这就是手册里说的"必须人工处理"。
先只「预演」，看清方案再决定要不要执行。

### 8. 双栏对比（第 10 章）

需要两个数据源才能对比。你可以：
「数据源」→「添加数据源」→ 再指向 `demo/` 下的某个子目录 → 然后去「对比」。

### 9. 看日志（第 19 章）

「设置」→ 往下找到「后台日志」。里面记了每次扫描、每个任务。
出问题时把这里的内容贴给别人看。

---

## 它不会做的事（免得你误会）

- **不会**自动删除你的文件。所有删除先进回收站。
- **不会**静默覆盖目标文件。冲突会停下来问你。
- **不会**在搬运失败时删掉源文件。顺序是"复制 → 校验 → 删源"。

## 已知限制

- 只在 Windows 上实测过
- SFTP / WebDAV / HTTP 目录 / SMB / FTP 是**实验性**功能，没对真实服务器验证
- `.wma` / `.mka` / `.caf` 能播放但不能改标签
- MTP（手机/U 盘直连）只探测，不读写

## 玩坏了怎么办

删掉 `userdata/` 目录，重新双击 `启动测试.bat`。
演示曲库也可以删 —— 这个包里的是示例音频，不是你的东西。

除非你手动往 `demo/` 里放过自己的歌，否则没有任何东西会丢。
"""

TEST_BAT = """@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo   Music Sync Hub - 测试包（演示环境）
echo ============================================================
echo.

REM 优先用包内自带的虚拟环境（开箱即用）
if exist ".venv\\Scripts\\python.exe" (
    set "PY=.venv\\Scripts\\python.exe"
    echo   使用包内 Python: %PY%
) else (
    set "PY=python"
    echo   包内没有 .venv，使用系统 Python
)

if not exist "run-test.py" (
    echo   [错误] 找不到 run-test.py，这个包可能不完整。
    pause
    exit /b 1
)

"%PY%" run-test.py %*
echo.
echo 服务已退出。
pause
"""


def build_test(out_dir: Path) -> Path:
    info = _version()
    files = collect([])                       # 测试包保留 .venv 和 demo
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"MusicSyncHub-测试包-{stamp}.zip"

    # 测试包不能带任何已有的运行数据
    files = [f for f in files if "userdata/" not in f.relative_to(ROOT).as_posix()]

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("README-测试包.md", TEST_README.format(**{"version": info["version"]}))
        z.writestr("启动测试.bat", TEST_BAT.lstrip("\n"))
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())
    return dest


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打 AI 审查包 / 测试包")
    ap.add_argument("--only", choices=["review", "test"], default=None)
    ap.add_argument("--out", default=None, help="输出目录（默认桌面）")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else OUT_DEFAULT
    out.mkdir(parents=True, exist_ok=True)
    print(f"  输出目录: {out}")
    made = []
    if args.only in (None, "review"):
        p = build_review(out)
        made.append(p)
        print(f"  [审查包] {p.name}  {p.stat().st_size / 1024 / 1024:.2f} MB")
    if args.only in (None, "test"):
        p = build_test(out)
        made.append(p)
        print(f"  [测试包] {p.name}  {p.stat().st_size / 1024 / 1024:.2f} MB")

    # 校验：每个包都能列出条目、且不含敏感文件
    SENSITIVE = ("config.toml", "secrets.toml", "index.sqlite3", "mappings.json")
    for p in made:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            bad = [n for n in names
                   if n.split("/")[-1] in SENSITIVE
                   and not n.startswith(("demo/",))]
            print(f"  {p.name}: {len(names)} 个条目，敏感文件 {bad or '无'}")
    print()
    print("  完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
