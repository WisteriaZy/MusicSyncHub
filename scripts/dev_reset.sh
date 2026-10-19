#!/usr/bin/env bash
# ============================================================
# 一键重置本地联调环境：停服务 → 清索引 → 重启 → 重新生成样例库
#
#   bash scripts/dev_reset.sh [端口]
#
# ⚠ 这个脚本会杀掉本机所有 Music Sync Hub 实例（包括你用 start.sh 起的正式实例），
#   只适合开发/演示时用。
#
# 注意：命令行有两种启动方式（uvicorn app.main:app 和 python -m app.main），
# 所以这里既按进程名杀、也按端口占用杀，并在启动前确认端口真的空出来了——
# 否则新进程会静默绑定失败，而旧的进程还握着已被删除的索引文件，
# 导致"扫描到新文件、但界面看到旧数据"这种诡异现象。
# ============================================================
set -euo pipefail
PORT="${1:-8791}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE="${MSH_FIXTURE:-/tmp/msh-e2e}"
DATA="${MSH_DATA_DIR:-/tmp/msh-e2e-data}"

cd "$ROOT"

port_pids() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true; }

echo "[1/4] 停掉旧服务（端口 $PORT）"
pkill -9 -f "app.main:app" 2>/dev/null || true
pkill -9 -f "app\.main" 2>/dev/null || true
for pid in $(port_pids); do kill -9 "$pid" 2>/dev/null || true; done
for _ in $(seq 1 20); do
  [ -z "$(port_pids)" ] && break
  sleep 0.5
done
if [ -n "$(port_pids)" ]; then
  echo "  ✗ 端口 $PORT 仍被占用（PID: $(port_pids)），请手动处理后重试"
  exit 1
fi
echo "  ✓ 端口已释放"

echo "[2/4] 清空索引与样例库"
rm -f "$DATA/index.sqlite3" "$DATA/index.sqlite3-wal" "$DATA/index.sqlite3-shm"
mkdir -p "$DATA"
rm -rf "$FIXTURE"

echo "[3/4] 启动服务"
MSH_DATA_DIR="$DATA" nohup uv run python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  > /tmp/msh-server.log 2>&1 &
for _ in $(seq 1 40); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/system" && break
  sleep 1
done
if ! curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/system"; then
  echo "  ✗ 服务启动失败，日志见 /tmp/msh-server.log"
  tail -20 /tmp/msh-server.log
  exit 1
fi
echo "  ✓ 服务已就绪：http://127.0.0.1:$PORT  (PID $(port_pids))"

echo "[4/4] 生成样例库"
uv run python tests/make_sample_libs.py "$FIXTURE" > /dev/null
WIN=$(find "$FIXTURE/win" -type f \( -name '*.wav' -o -name '*.mp3' \) | wc -l | tr -d ' ')
LIN=$(find "$FIXTURE/linux" -type f \( -name '*.wav' -o -name '*.mp3' \) | wc -l | tr -d ' ')
if [ "$WIN" != "10" ] || [ "$LIN" != "7" ]; then
  echo "  ✗ 样例库数量不对（win=$WIN linux=$LIN，应为 10/7）"
  exit 1
fi
echo "  ✓ win=$WIN 首  linux=$LIN 首"
echo
echo "下一步：uv run python tests/e2e_check.py --base http://127.0.0.1:$PORT"
