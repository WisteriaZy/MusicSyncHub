# 推送到 GitHub —— 操作步骤

> 这台机器上**没有装 git**（`git --version` 不存在），也没有 `gh`。
> 所以我没法替你 `git init` / `git push`。下面是你要做的步骤，全部代码文件已经准备好了。

---

## 一、安装 git（2 分钟）

任选一种：

```powershell
# 方式 1：winget（Windows 10 自带）
winget install --id Git.Git -e --source winget

# 方式 2：官网下载
# https://git-scm.com/download/win
```

装完**重开一个终端**，确认：

```powershell
git --version
```

同时建议装 GitHub CLI（后面建仓库用得上）：

```powershell
winget install --id GitHub.cli -e --source winget
```

---

## 二、本地初始化（在项目目录里执行）

```powershell
cd C:\Users\Administrator\Desktop\MusicSyncHub

git init -b main

# ★ 先确认 .gitignore 生效了，这一步很重要
git add -A
git status --short | Measure-Object -Line     # 应该只有一两百行，不是几千行

# 检查有没有把不该提交的东西加进去（下面这些命令都应该没有输出）
git status --short | Select-String "\.venv/"
git status --short | Select-String "config\.toml"
git status --short | Select-String "secrets\.toml"
git status --short | Select-String "\.sqlite3"
git status --short | Select-String "userdata/"
git status --short | Select-String "demo/"
git status --short | Select-String "__pycache__"
```

**如果上面任何一条有输出，先停下来**：说明 `.gitignore` 没生效，
千万别 `git commit`（一旦提交进历史，后面很难彻底清掉）。

预期会提交的文件数量大约 **150~200 个**。主要构成：

| 目录 | 约数 |
| --- | --- |
| `app/` | 52（Python + 前端） |
| `tests/` | 33 |
| `tools/` | 10 |
| `docs/` | 2 |
| `.github/` | 3 |
| 根目录 md / 配置 | 12 |

> `.venv/`（54 MB / 2051 文件）、`demo/`（4.8 MB）、`userdata/`、`station/`
> 都已被 `.gitignore` 排除。别人 clone 下来 `uv sync` 就能跑。

---

## 三、第一次提交

```powershell
git config user.name  "你的名字"
git config user.email "你的邮箱"

git commit -m "feat: Music Sync Hub 初始版本

跨库音乐管理工具：多数据源、双栏对比、查重查漏、安全搬运。
后端 FastAPI + SQLite，前端纯原生 JS（无构建）。

- 文件安全：删除进回收站、绝不覆盖、搬运是复制→校验→删源
- 723 项单元测试 + 90 个接口冒烟用例
- 用户手册 1150 行 / 技术文档 2800 行"
```

---

## 四、建远程仓库并推送

### 用 GitHub CLI（推荐，能建私有仓库）

```powershell
gh auth login                                    # 浏览器授权
gh repo create music-sync-hub --private --source=. --push
```

### 用网页（不想装 gh）

1. 打开 https://github.com/new
2. 仓库名 `music-sync-hub`，选 **Private**（先私有，确认没问题再公开）
3. **不要**勾选 "Add a README file" / ".gitignore" / "license"（本地已有）
4. 建完后按页面提示执行：

```powershell
git remote add origin https://github.com/<你的用户名>/music-sync-hub.git
git push -u origin main
```

---

## 五、推送后立刻确认这三件事

1. **仓库里没有 `config.toml` / `secrets.toml` / `*.sqlite3`**
   打开仓库页面，用 GitHub 的搜索框搜 `secrets`，应该搜不到任何文件。

2. **`.venv` 没有进去**
   仓库首页应该显示很小的体积（几百 KB 级别的代码），不是几十 MB。

3. **CI 跑起来了**
   点仓库的 **Actions** 标签，应该有一个 `tests` 工作流在跑。
   第一次可能因为 `pyproject.toml` 的可编辑安装方式失败 —— 见下面"可能需要修的地方"。

---

## 六、可能需要修的地方

### CI 里的依赖安装

`.github/workflows/tests.yml` 里现在写的是：

```yaml
python -m pip install fastapi "uvicorn[standard]" mutagen pillow httpx paramiko
python -m pip install -e . || python -m pip install .
```

`pyproject.toml` 里没有 `[build-system]`，所以 `pip install -e .` 可能失败、
落到 `pip install .`。如果两个都失败，最简单的改法是删掉那两行，
只保留第一行显式列依赖。

### 想公开发布的话

- 把 `LICENSE` 里的 `Music Sync Hub contributors` 改成你的名字或组织
- 在 `README.md` 顶部加个截图（现在没有）
- 确认 `SAFETY.md` 里提到的路径、人名都做过脱敏

---

## 七、后续开发

```powershell
# 开分支
git checkout -b fix/something

# ... 改代码，跑测试 ...

python -m unittest discover -s tests -t .
python tools/check_frontend.py
python tools/audit_frontend.py
python tools/audit_split.py        # 模块导入 / 未定义名字 / 路由差分

git add -A
git commit -m "fix: 说清改了什么、为什么"
git push -u origin fix/something
```

提交规范与硬约束见 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。

---

## 八、已经在仓库里的东西（不用再准备）

| 文件 | 作用 |
| --- | --- |
| `.gitignore` | 已覆盖依赖、运行数据、密钥、索引、日志、备份包、演示音频 |
| `.gitattributes` | 统一 LF；`.bat` 强制 CRLF；二进制与敏感文件标记 |
| `.editorconfig` | 统一缩进/换行，减少无意义 diff |
| `LICENSE` | MIT |
| `README.md` | 已重写：项目文档索引、三条安全保证、测试模式说明 |
| `CONTRIBUTING.md` | 开发环境、5 条硬约束、提交规范、特别欢迎的贡献方向 |
| `CHANGELOG.md` | 变更记录（本轮所有修复都列了） |
| `SAFETY.md` | 三起真实事故与规则（从工作区搬进项目） |
| `docs/USER_GUIDE.md` | 用户手册 1153 行 |
| `docs/TECHNICAL.md` | 技术文档 2826 行，含 94 个端点 |
| `.github/workflows/tests.yml` | CI：Windows + Ubuntu × Python 3.12/3.13 |
| `.github/ISSUE_TEMPLATE/` | Bug 报告（含"有没有丢文件"必填项）与功能建议模板 |
| `run-test.py` | 测试包启动器 |
| `tools/build_packages.py` | 打审查包 / 测试包 |

---

## 九、还有个更省事的选项

如果你暂时不想折腾 GitHub，桌面上已经有两个可以直接给出去的包：

| 包 | 给谁 | 怎么用 |
| --- | --- | --- |
| `MusicSyncHub-审查包-20261017-130557.zip` | 让人或 AI 审查代码 | 解压即读，`PROJECT_INDEX.md` 是入口 |
| `MusicSyncHub-测试包-20261017-130558.zip` | 让人动手试用 | 解压，双击 `启动测试.bat` |

这两个包都不含 `.venv` 之外的任何私有数据（测试包含 `.venv`，只有一个演示曲库）。
