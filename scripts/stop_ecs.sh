#!/usr/bin/env bash
# 停止北华校园助手（按端口查找并结束进程）
PORT="${PORT:-8000}"
PIDS=$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | sort -u)
if [ -z "$PIDS" ]; then
  echo "端口 $PORT 上没有检测到服务进程"
  exit 0
fi
echo "停止进程：$PIDS"
kill $PIDS
echo "已停止"
