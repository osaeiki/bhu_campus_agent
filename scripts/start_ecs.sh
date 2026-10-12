#!/usr/bin/env bash
# 北华校园助手 Linux/ECS 后台启动脚本
# 用法：bash scripts/start_ecs.sh
set -e
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="venv/bin/python"
[ -x "$PY" ] || { echo "找不到虚拟环境 Python，请先安装依赖"; exit 1; }

PORT="${PORT:-8000}"

if [ ! -f .env ]; then
  echo "缺少 .env 文件！请先复制 .env.example 为 .env 并填写："
  echo "  DEEPSEEK_API_KEY=你的DeepSeekKey"
  echo "  AMAP_API_KEY=你的高德Key（可留空）"
  exit 1
fi

mkdir -p logs
echo "启动服务：http://0.0.0.0:${PORT} （后台运行）"
nohup "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" > logs/server.log 2>&1 &
PID=$!
echo "已启动，PID=$PID"
echo "查看日志：tail -f logs/server.log"
echo "停止服务：bash scripts/stop_ecs.sh"
