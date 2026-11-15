#!/usr/bin/env bash
# 无 Docker 的本地演示：用同一 Python 环境直接起两个服务跑全部场景。
# 用法: PYTHON=/path/to/python bash scripts/run_local.sh
# 可选: API_PORT（默认 8000）、SOURCE_PORT（默认 8001）
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON=${PYTHON:-python3}
API_PORT=${API_PORT:-8000}
SOURCE_PORT=${SOURCE_PORT:-8001}
DATA_DIR=$(mktemp -d)
LOG_DIR=$(mktemp -d)
cleanup() {
  # 正常环境下 kill 掉两个服务进程；沙箱等限制信号的环境下仅尽力而为
  kill "${API_PID:-}" "${SRC_PID:-}" 2>/dev/null || true
  rm -rf "$DATA_DIR"
  echo "（服务进程已请求停止；日志在 $LOG_DIR）"
}
trap cleanup EXIT

echo "== 生成演示影像到 $DATA_DIR =="
"$PYTHON" source/make_image.py "$DATA_DIR"

echo "== 启动影像源 ($SOURCE_PORT) 与取数 API ($API_PORT) =="
# exec 让子 shell 进程即服务进程（PID 可直接管理）；
# 整组重定向，避免后台进程持有脚本 stdout 导致管道不关闭
(cd source && exec env DATA_DIR="$DATA_DIR" "$PYTHON" -m uvicorn main:app --port "$SOURCE_PORT") >"$LOG_DIR/source.log" 2>&1 &
SRC_PID=$!
(cd app && exec "$PYTHON" -m uvicorn main:app --port "$API_PORT") >"$LOG_DIR/api.log" 2>&1 &
API_PID=$!

for url in "http://localhost:$SOURCE_PORT/health" "http://localhost:$API_PORT/health"; do
  ok=0
  for i in $(seq 1 60); do
    curl -sf "$url" >/dev/null 2>&1 && { ok=1; break; }
    sleep 0.5
  done
  [ "$ok" = 1 ] || { echo "服务启动失败: $url"; cat "$LOG_DIR"/*.log; exit 1; }
done
echo "  API 文档: http://localhost:$API_PORT/docs"

export API_BASE="http://localhost:$API_PORT"
export SOURCE_BASE="http://localhost:$SOURCE_PORT"
export SRC_INTERNAL="$SOURCE_BASE"
export VERIFY_MODE=local
export PYTHON

bash scripts/checks.sh
