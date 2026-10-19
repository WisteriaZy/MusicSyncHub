# 第三方依赖与许可证（THIRD PARTY NOTICES）

本项目以 **MIT** 许可发布（见 [LICENSE](LICENSE)）。它依赖以下第三方库，
各自的许可证如下（以实际安装版本的元数据为准）。

## 直接依赖

| 依赖 | 许可证 | 说明 |
| --- | --- | --- |
| fastapi | MIT | Web 框架 |
| starlette | BSD-3-Clause | ASGI 工具集（fastapi 依赖） |
| uvicorn | BSD-3-Clause | ASGI 服务器 |
| httpx | BSD-3-Clause | HTTP 客户端（含测试客户端） |
| python-multipart | Apache-2.0 | 表单/文件上传解析 |
| **mutagen** | **GPL-2.0-or-later** | 音频标签解析/写入 —— **注意：GPL** |
| Pillow | MIT-CMU | 封面缩略图 |
| **paramiko** | **LGPL-2.1** | SFTP 数据源（可选） |
| smbprotocol | MIT | SMB 数据源（可选） |

## 关于 GPL / LGPL 的说明（重要）

- 本项目**只通过 `import` 使用** mutagen / paramiko，**不修改、不内联**它们的源码。
  按社区通行理解（例如 beets 也是 MIT + mutagen），这属于"独立程序间调用"，
  本项目自身的 MIT 许可是成立的。
- **发布源码 / 源码仓库**：不包含上述库的二进制，无需附带其源码。
- **发布包含 `.venv` 的"测试包"**：那是在**分发 mutagen / paramiko 的二进制**。
  此时必须随包附带它们的许可证文本（本仓库 `LICENSES/` 下有副本），
  并能指明对应版本与源码来源：
  - mutagen: <https://github.com/quodlibet/mutagen>（GPL-2.0-or-later）
  - paramiko: <https://github.com/paramiko/paramiko>（LGPL-2.1）
- **如果你要做"单文件 exe"**（PyInstaller 把依赖打进同一个二进制）：
  那个二进制通常必须整体按 GPL 兼容许可发布。本项目目前不做这种打包。

## 许可证原文

`LICENSES/` 目录下是从已安装依赖的 `dist-info` 里收集的许可证文件
（文件名为 `<包名>.txt`）。如果某个包没有附带许可证文件，以上面的项目主页为准。
