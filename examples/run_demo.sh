#!/usr/bin/env bash
# Start an AgentCiv server plus a 6-player bot game using only the CLI tools.
#   ./examples/run_demo.sh            then open http://localhost:8765/
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"
URL="http://localhost:${PORT}"

python -m agentciv.server --port "$PORT" --data-dir "${DATA_DIR:-data}" &
SERVER=$!
cleanup() { kill $(jobs -p) 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM
until curl -sf "$URL/api" >/dev/null; do sleep 0.2; done

# 4 house bots + 2 open seats; turn_delay slows it down enough to watch.
GAME=$(curl -s -X POST "$URL/api/games" -H 'Content-Type: application/json' \
  -d '{"name":"Shell demo","max_players":6,"bots":["strategist","economist","rusher","turtle"],"turn_timeout":10,"turn_delay":0.4}' \
  | python -c 'import sys, json; print(json.load(sys.stdin)["game_id"])')
echo "Watch: $URL/#/game/$GAME"

# Two remote players connect over HTTP, like your agent would.
python examples/simple_bot.py --url "$URL" --game "$GAME" --name SimpleBot &
python -m agentciv.client --url "$URL" --bot strategist --name remote-strategist --game "$GAME" --quiet
wait
echo "Game over — the server keeps running; press Ctrl-C to stop."
wait $SERVER
