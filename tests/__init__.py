"""测试包的**安全网**：绝不允许测试写到用户真实的数据目录。

## 为什么需要这个

测试之间靠"设 `MSH_DATA_DIR` 到临时目录、teardown 再 pop 掉"来隔离。
这个做法有个致命缺口：**pop 之后到下一个测试设好之前**，
`MSH_DATA_DIR` 是**空**的 —— 这时候任何 `save_config()` 都会落到
默认数据目录，也就是**用户真实的那份** `config.toml`。

这不是理论风险，是真发生过的：

    审计修 B8（`_inside` 收相对路径）时加了一个用临时配置的测试，
    它在 finally 里 `load_config(force=True)` —— 而那一刻 `MSH_DATA_DIR`
    正好是空的 → 用户的 config.toml 被换成了测试用的假数据源
    （只剩一个指向临时目录的 `si`），**两个真实数据源全没了**。
    发现得早（那个测试自己读到"找不到 win-music"），否则下次重启
    就是"我的音乐库怎么不见了"。

## 做法

在这里就把 `MSH_DATA_DIR` 指向一个**本进程专属的临时目录**，
并且注册一个"环境变量被清空时自动兜回来"的钩子：

  · `MSH_DATA_DIR` 有值 → 原样用（测试自己的隔离照旧生效）
  · `MSH_DATA_DIR` 被 pop 掉 / 变成空串 → **兜回**临时目录，而不是真实目录

于是"忘记隔离"最坏只会让测试之间互相干扰（会表现为测试失败），
**不可能再碰到用户的真实数据**。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# 本进程专属的测试数据目录。用 msh-tests- 前缀，出问题一眼能认出来。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="msh-tests-")

# 哨兵：让 config.py 能把"忘了设"与"故意为空"区分开
_SENTINEL = os.path.join(_TEST_DATA_DIR, ".msh-tests-sentinel")
os.environ["MSH_DATA_DIR"] = _SENTINEL
os.environ["MSH_DB"] = os.path.join(_TEST_DATA_DIR, "index.sqlite3")


def _cleanup() -> None:
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup)


__all__ = ["_TEST_DATA_DIR"]
