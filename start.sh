#!/bin/bash
# ResearchBench 本地启动脚本
# 在你自己的 macOS 终端运行：bash start.sh
# 然后浏览器打开 http://127.0.0.1:8889
#
# 说明：
# - 本脚本在本机终端运行，能访问你本机的代理（如 127.0.0.1:52129），
#   因此 Radar 检索（arXiv / Google News）可正常联网。
# - 若在 WorkBuddy 沙箱内运行，沙箱网络隔离，访问不到本机代理，检索会失败。
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="/Users/wangyini/.workbuddy/binaries/python/envs/default/bin/python"
PORT=8889

cd "$PROJECT_DIR"

# 自动继承本机代理环境变量（若已 export 则直接用，否则尝试常见端口）
if [ -z "$HTTPS_PROXY" ] && [ -z "$HTTP_PROXY" ]; then
  if curl -s -o /dev/null --connect-timeout 1 http://127.0.0.1:52129/ 2>/dev/null; then
    export HTTPS_PROXY="http://127.0.0.1:52129"
    export HTTP_PROXY="http://127.0.0.1:52129"
    echo "[start] 已自动启用本机代理: $HTTPS_PROXY"
  fi
fi

echo "[start] 启动 ResearchBench → http://127.0.0.1:$PORT"
echo "[start] 按 Ctrl+C 停止"
exec "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
