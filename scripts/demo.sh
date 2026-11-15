#!/usr/bin/env bash
# Docker Compose 一键演示：构建、启动、跑全部场景。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== 构建并启动 api + source =="
docker compose up -d --build --wait

echo
echo "== 服务信息 =="
echo "  取数 API:  http://localhost:8000  (文档 http://localhost:8000/docs)"
echo "  影像源:    http://localhost:8001"

export API_BASE=http://localhost:8000
export SOURCE_BASE=http://localhost:8001
export SRC_INTERNAL=http://source:8000   # api 容器访问影像源的内部地址
export VERIFY_MODE=docker

bash scripts/checks.sh
