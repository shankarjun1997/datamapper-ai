#!/bin/bash
# DataMapper startup script
# Usage: ./run.sh

set -e
cd "$(dirname "$0")"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DataMapper — Agentic STM Engine"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Install deps if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "→ Installing dependencies…"
  pip3 install -r requirements.txt --break-system-packages -q
fi

echo "→ Loading .env from ../sql gen/.env"
echo "→ Starting server on http://localhost:7788"
echo "→ Open http://localhost:7788 in your browser"
echo ""

python3 server.py
