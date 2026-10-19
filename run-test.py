"""测试包启动器：一条命令把演示环境跑起来。

## 为什么需要它（而不是直接 start.bat）

测试包要"开箱即用"，就得替人做掉这几件事：

1. 把数据目录指到**包内**，绝不碰 `%USERPROFILE%\\.music-sync-hub`；
2. 生成一份只含**演示库**的配置；
3. 第一次启动时把演示音频**扫进索引**（否则界面上空空如也）；
4. 打开浏览器；
5. 全程不开写真实目录——这一点靠 1 和 2 保证。

## 关于"测试模式"

`MSH_TEST_MODE=1` 会让后端在首页注入常驻水印、并在 `/api/system` 里带
`test_mode: true`（前端据此再显示一条可关闭的横幅）。
**它不改变任何业务行为** —— 不做沙箱、不拦操作。真正的隔离靠
`MSH_DATA_DIR` 指向包内目录、配置里只有演示库。

用法：
    python run-test.py            # 正常启动（8787）
    python run-test.py --port 8899
    python run-test.py --no-browser
    python run-test.py --rescan   # 强制重新扫描演示库
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE                      # 包根 = 本文件所在目录
APP_DIR = PACKAGE_ROOT / "app"
DATA_DIR = PACKAGE_ROOT / "userdata"
DEMO_ROOT = PACKAGE_ROOT / "demo"
DEMO_SOURCE_ID = "demo"


def _banner(title: str, lines: list[str]) -> None:
    print()
    print("=" * 66)
    print(f"  {title}")
    print("=" * 66)
    for ln in lines:
        print(f"  {ln}")
    print("=" * 66)
    print()


def prepare_data_dir(force_rescan: bool) -> None:
    """建好包内数据目录 + 只含演示库的配置。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path = DATA_DIR / "config.toml"
    demo = DEMO_ROOT.as_posix()
    if not DEMO_ROOT.is_dir():
        raise SystemExit(
            f"找不到演示库目录：{DEMO_ROOT}\n"
            f"这个包可能不完整，请重新解压。")

    if force_rescan or not cfg_path.exists():
        # 直接写 TOML。演示库只有一个本地源，字段很少，不需要走 AppConfig。
        cfg_path.write_text(
            "# 由 run-test.py 自动生成 —— 只包含一个演示库，指向包内 demo/\n"
            "# 想换成自己的音乐：把 [[sources]] 的 root 改成你的路径即可。\n"
            "\n"
            "[settings]\n"
            "station_max_gb = 5.0\n"
            "staging_max_gb = 2.0\n"
            "duration_tolerance_sec = 2.0\n"
            "scan_workers = 4\n"
            "\n"
            "[[sources]]\n"
            f'id = "{DEMO_SOURCE_ID}"\n'
            'name = "演示曲库"\n'
            'type = "local"\n'
            f'root = "{demo}"\n'
            'read_only = false\n'
            'enabled = true\n',
            encoding="utf-8")
        print(f"  [1/4] 已生成配置: {cfg_path}")


def _get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def wait_ready(base: str, timeout: float = 90.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            _get_json(base + "/api/system", timeout=3)
            return True
        except Exception:
            time.sleep(0.6)
    return False


def ensure_scanned(base: str, force: bool = False) -> None:
    """把演示库扫进索引（已经有曲目就跳过，除非 --rescan）。"""
    try:
        srcs = _get_json(base + "/api/sources")
    except Exception as exc:                      # noqa: BLE001
        print(f"  [2/4] 读取数据源失败（跳过预扫描）：{exc}")
        return
    demo = next((s for s in srcs if s.get("id") == DEMO_SOURCE_ID), None)
    if demo is None:
        print("  [2/4] 配置里没有演示库，跳过预扫描")
        return
    indexed = int(demo.get("indexed") or 0)
    if indexed > 0 and not force:
        print(f"  [2/4] 演示库已有 {indexed} 首，跳过扫描")
        return
    print(f"  [2/4] 正在扫描演示库（{DEMO_ROOT}）…")
    body = json.dumps({}).encode()
    req = urllib.request.Request(
        base + f"/api/scan/{DEMO_SOURCE_ID}", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=600).read()
    except urllib.error.HTTPError as exc:
        print(f"        扫描接口返回 {exc.code}：{exc.read()[:160]!r}")
        return
    except Exception as exc:                      # noqa: BLE001
        print(f"        扫描失败：{exc}")
        return
    # 等它扫完（后台任务）
    t0 = time.time()
    while time.time() - t0 < 300:
        try:
            if not _get_json(base + "/api/busy").get("busy"):
                break
        except Exception:
            pass
        time.sleep(1.0)
    try:
        srcs = _get_json(base + "/api/sources")
        demo = next((s for s in srcs if s.get("id") == DEMO_SOURCE_ID), None)
        n = int((demo or {}).get("indexed") or 0)
        print(f"        扫描完成：{n} 首")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Music Sync Hub 测试包启动器")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--rescan", action="store_true", help="强制重新扫描演示库")
    args = ap.parse_args(argv)

    base = f"http://{args.host}:{args.port}"

    _banner("Music Sync Hub —— 测试包（演示环境）", [
        "这是**测试包**，数据都在本目录内，不会碰你真实的音乐库。",
        "",
        f"演示曲库： {DEMO_ROOT}",
        f"运行数据： {DATA_DIR}",
        f"访问地址： {base}",
        "",
        "页面右上角会常驻「测试模式」水印 —— 有它才说明包是对的。",
        "关闭窗口 / Ctrl+C 结束。",
    ])

    prepare_data_dir(args.rescan)

    env = dict(os.environ)
    env["MSH_TEST_MODE"] = "1"                       # ★ 水印开关
    env["MSH_DATA_DIR"] = str(DATA_DIR)              # ★ 数据隔离
    env["PYTHONUTF8"] = "1"
    # 让 `import app.main` 找得到包
    env["PYTHONPATH"] = str(PACKAGE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [sys.executable, "-u", "-m", "app.main",
           "--host", args.host, "--port", str(args.port)]
    print("  [3/4] 启动服务…")
    proc = subprocess.Popen(cmd, cwd=str(PACKAGE_ROOT), env=env)

    try:
        if not wait_ready(base):
            print("\n  ★ 服务 90 秒内没起来。看上面的报错，或看 userdata/logs/ 里的日志。")
            return 1
        ensure_scanned(base, force=args.rescan)
        print("  [4/4] 就绪")
        if not args.no_browser:
            threading.Timer(0.8, lambda: webbrowser.open(base)).start()
        print()
        print(f"  浏览器地址： {base}")
        print("  按 Ctrl+C 结束测试环境。")
        print()
        while proc.poll() is None:
            time.sleep(0.5)
        return proc.returncode or 0
    except KeyboardInterrupt:
        print("\n  正在关闭…")
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
