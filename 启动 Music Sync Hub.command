#!/usr/bin/env bash
# ============================================================
#  双击我即可启动 Music Sync Hub（macOS）
#    · 首次会装依赖（几秒）
#    · 服务起来后自动打开浏览器
#  停止：在这个终端窗口按 Control + C
# ============================================================
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST="127.0.0.1"
PORT="${MSH_PORT:-8787}"

if ! command -v uv >/dev/null 2>&1; then
  echo "❌ 没有找到 uv，请先安装："
  echo '   curl -LsSf https://astral.sh/uv/install.sh | sh'
  echo
  read -n 1 -s -r -p "按任意键关闭…"
  exit 1
fi

# 等服务真的起来再开浏览器，避免打开是空白页
(
  for _ in $(seq 1 240); do
    if curl -s -m 1 -o /dev/null "http://$HOST:$PORT/api/system"; then
      open "http://$HOST:$PORT"
      exit 0
    fi
    sleep 1
  done
) &

exec bash start.sh "$HOST" "$PORT"
