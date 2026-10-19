#!/usr/bin/env bash
# ============================================================
# Music Sync Hub 启动脚本（macOS / Linux）
#
#   bash start.sh                 # 默认 127.0.0.1:8787
#   bash start.sh 0.0.0.0 9000    # 让局域网其它机器也能访问
#
# 依赖：uv（https://docs.astral.sh/uv/）。首次运行会自动创建虚拟环境并装依赖。
# 无论从哪个目录、用相对路径还是绝对路径调用，脚本都能正确定位自己。
# ============================================================
set -euo pipefail

# 用 BASH_SOURCE 拿到脚本自身所在目录（$0 在 `bash start.sh` 时只有文件名）
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

HOST="${1:-127.0.0.1}"
PORT="${2:-8787}"

if ! command -v uv >/dev/null 2>&1; then
  echo "❌ 没有找到 uv。请先安装： curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "→ 同步依赖（首次会下载 Python 与依赖包，请稍候）…"
uv sync --quiet

echo
echo "  Music Sync Hub 已启动"
echo "  界面地址： http://${HOST}:${PORT}"
echo "  数据目录： ${MSH_DATA_DIR:-$HOME/.music-sync-hub}"
echo "  停止服务： 按 Ctrl+C"
echo

exec uv run python -m app.main --host "$HOST" --port "$PORT"
