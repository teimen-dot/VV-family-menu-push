#!/bin/bash
# 重启 H5 服务器和菜品管理器，加载最新代码
# 用法: ./restart_servers.sh

cd /Users/vv/WorkBuddy/Claw

# 1. 杀掉旧进程
echo "[1/3] Stopping old servers..."
pkill -f "python.*app\.py" 2>/dev/null
pkill -f "python.*photo_manager\.py" 2>/dev/null
sleep 2

# 2. 验证端口空闲
lsof -i:8090 2>&1 | grep LISTEN
lsof -i:8080 2>&1 | grep LISTEN
if lsof -i:8090 2>&1 | grep -q LISTEN; then
  echo "ERROR: Port 8090 still in use, aborting"
  exit 1
fi
if lsof -i:8080 2>&1 | grep -q LISTEN; then
  echo "ERROR: Port 8080 still in use, aborting"
  exit 1
fi

# 3. 启动新进程
echo "[2/3] Starting new servers..."
nohup /Users/vv/.workbuddy/binaries/python/envs/default/bin/python app.py > server.log 2>&1 &
APP_PID=$!
nohup /Users/vv/.workbuddy/binaries/python/envs/default/bin/python photo_manager.py > photo_manager.log 2>&1 &
PM_PID=$!
echo "  app.py: PID $APP_PID"
echo "  photo_manager.py: PID $PM_PID"

# 4. 验证启动
sleep 3
echo "[3/3] Verifying..."
if lsof -i:8090 2>&1 | grep -q LISTEN; then
  echo "  ✓ H5 server (8090) running"
else
  echo "  ✗ H5 server (8090) FAILED"
  exit 1
fi
if lsof -i:8080 2>&1 | grep -q LISTEN; then
  echo "  ✓ Photo manager (8080) running"
else
  echo "  ✗ Photo manager (8080) FAILED"
  exit 1
fi

echo ""
echo "All servers restarted with fresh code."
