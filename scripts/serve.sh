#!/usr/bin/env bash
# Stock Radar local preview launcher.
# Usage: bash scripts/serve.sh [--port 8765] [--no-open]
set -euo pipefail
cd "$(dirname "$0")/.."
# 默认在后台跑 —— 用户经常看到 "Press Ctrl+C to stop." 就下意识按 Ctrl+C 把 server 杀掉,
# 改成 daemonize 后 bash 立刻返回,server 进入独立的 process group,任意键都不会杀它。
# 想前台跑(可以实时看日志)就: SERVE_FOREGROUND=1 bash scripts/serve.sh
if [ "${SERVE_FOREGROUND:-0}" = "1" ]; then
  exec python3 scripts/serve.py "$@"
else
  LOG=/tmp/stock-radar-server.log
  nohup python3 scripts/serve.py "$@" > "$LOG" 2>&1 &
  disown
  PID=$!
  echo "  stock-radar server pid: $PID"
  echo "  url: http://127.0.0.1:8765/"
  echo "  log: tail -F $LOG   (实时看 stdout/stderr)"
  echo "  stop: kill $PID    (或: lsof -ti:8765 | xargs kill)"
  exit 0
fi
