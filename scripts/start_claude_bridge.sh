#!/usr/bin/env bash
# Start the Claude Code OAuth bridge for nemesis-harness.
#
# Exposes an Anthropic-compatible endpoint on CLAUDE_BRIDGE_PORT that forwards
# to api.anthropic.com using the local Claude Code subscription token (macOS
# Keychain service "Claude Code-credentials"), so both the in-process adapters
# (src/llm/claude_bridge.py) and the OpenHands runtime container can call
# Opus 4.8 without an API key.
#
#   ./scripts/start_claude_bridge.sh            # run in foreground
#   ./scripts/start_claude_bridge.sh --check    # verify credentials, then exit
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# shellcheck disable=SC1091
set -a
[ -f .env ] && . ./.env
set +a

PY="${PY:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "error: $PY not found. Create it with: uv venv --python 3.13 .venv" >&2
  exit 1
fi

HOST="${CLAUDE_BRIDGE_HOST:-0.0.0.0}"
PORT="${CLAUDE_BRIDGE_PORT:-8765}"

if [ -z "${WCB_CC_BRIDGE_SECRET:-}" ]; then
  echo "warning: WCB_CC_BRIDGE_SECRET is empty — the bridge will accept any" >&2
  echo "         local (and, on 0.0.0.0, any LAN) caller. Set it in .env." >&2
fi

echo "[bridge] root=$ROOT"
echo "[bridge] model=${CLAUDE_BRIDGE_MODEL:-claude-opus-4-8}  host=$HOST  port=$PORT"
exec "$PY" -m src.claude_oauth --host "$HOST" --port "$PORT" "$@"
