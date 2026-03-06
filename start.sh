#!/usr/bin/env bash
# start.sh — Launch the Solana Alert Bot (Python TA engine + XMTP AI Agent)
#
# Usage:
#   ./start.sh          — start everything
#   ./start.sh --test   — run a one-shot TA check, then exit

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Validate environment ──────────────────────────────────────────────────────

if [[ ! -f ".env" ]]; then
  echo "❌ .env not found. Copy .env.example → .env and fill in your values."
  exit 1
fi

source .env 2>/dev/null || true

if [[ -z "${XMTP_GROUP_ID:-}" ]]; then
  echo "⚠️  XMTP_GROUP_ID is not set in .env"
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "⚠️  ANTHROPIC_API_KEY is not set — DM AI responses will not work"
fi

# ── Activate Python venv if present ──────────────────────────────────────────

if [[ -d "venv" ]]; then
  source venv/bin/activate
  echo "✓ Python venv activated"
fi

# ── Kill any stale processes ──────────────────────────────────────────────────

echo "Stopping any existing instances…"
pkill -f "node.*xmtp_agent.js" 2>/dev/null || true
pkill -f "python.*bot.py"      2>/dev/null || true
sleep 1
# Free port 3001 if anything is still holding it
lsof -ti :3001 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

# ── One-shot test mode ────────────────────────────────────────────────────────

if [[ "${1:-}" == "--test" ]]; then
  echo "Running one-shot TA check…"
  python bot.py test
  exit 0
fi

# ── Start both processes ───────────────────────────────────────────────────────

echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║         SOLANA ALERT BOT  🚀  + AI AGENT  🤖          ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""
echo "  bot.py          — TA engine (Python)"
echo "  xmtp_agent.js   — XMTP AI agent (Node.js)"
echo "  HTTP port:        3001 (internal)"
echo ""
echo "  Press Ctrl+C to stop both."
echo ""

# Start the XMTP agent first (it binds the HTTP port bot.py posts to)
node xmtp_agent.js &
AGENT_PID=$!
echo "✓ xmtp_agent.js started (PID $AGENT_PID)"
sleep 4   # give the agent time to bind :3001

# Start the TA bot — Ctrl+C here kills both via the trap below
trap "echo ''; echo 'Stopping…'; kill $AGENT_PID 2>/dev/null; exit 0" INT TERM

python bot.py

# If bot.py exits cleanly, also stop the agent
kill $AGENT_PID 2>/dev/null || true
