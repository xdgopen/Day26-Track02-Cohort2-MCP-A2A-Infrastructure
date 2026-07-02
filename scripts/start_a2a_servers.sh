#!/usr/bin/env bash
# Khởi động A2A specialists (8001–8003) — dùng Python env hiện tại có google-adk
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs

# shellcheck source=_lab_env.sh
source "$ROOT/scripts/_lab_env.sh"
setup_lab_env "$ROOT"

start_agent() {
  local name="$1" port="$2"
  if lsof -i :"$port" >/dev/null 2>&1; then
    echo "⚠ Cổng $port đang được dùng — dừng process cũ để nạp code mới..."
    lsof -ti :"$port" | xargs kill 2>/dev/null || true
    sleep 1
  fi
  echo "→ Khởi động $name :$port ..."
  nohup "${LAB_UVICORN[@]}" "agents.${name}.agent:a2a_app" --host localhost --port "$port" \
    > "logs/${name}.log" 2>&1 &
  echo $! > "logs/${name}.pid"
}

start_agent search_agent 8001
start_agent database_agent 8002
start_agent synthesis_agent 8003

echo "Đợi server khởi động..."
for i in 1 2 3 4 5; do
  sleep 2
  if curl -sf http://localhost:8001/.well-known/agent-card.json >/dev/null 2>&1 \
     && curl -sf http://localhost:8002/.well-known/agent-card.json >/dev/null 2>&1 \
     && curl -sf http://localhost:8003/.well-known/agent-card.json >/dev/null 2>&1; then
    break
  fi
done

echo ""
echo "Kiểm tra agent card:"
curl -sf http://localhost:8001/.well-known/agent-card.json | head -c 120 && echo " ... (search OK)" || {
  echo "✗ search_agent chưa sẵn sàng — xem logs/search_agent.log"
  tail -5 logs/search_agent.log 2>/dev/null || true
}
curl -sf http://localhost:8002/.well-known/agent-card.json | head -c 120 && echo " ... (database OK)" || {
  echo "✗ database_agent chưa sẵn sàng — xem logs/database_agent.log"
  tail -5 logs/database_agent.log 2>/dev/null || true
}
curl -sf http://localhost:8003/.well-known/agent-card.json | head -c 120 && echo " ... (synthesis OK)" || {
  echo "✗ synthesis_agent chưa sẵn sàng — xem logs/synthesis_agent.log"
  tail -5 logs/synthesis_agent.log 2>/dev/null || true
}
echo ""
echo "Dừng server: bash scripts/stop_a2a_servers.sh"
