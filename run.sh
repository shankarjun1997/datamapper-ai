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

echo "→ Starting server on http://localhost:7788"
echo "→ Open http://localhost:7788 in your browser"
echo ""
echo "  Demo credentials (if login page appears):"
echo "    Workspace : demo"
echo "    Email     : demo@xref.ai"
echo "    Password  : demo"
echo "  Or click 'Quick Demo Access' to skip manual entry."
echo ""

# Use the modular app package (app/main.py) if present, fall back to legacy server.py
if [ -f "app/main.py" ]; then
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 7788 --reload
else
  python3 server.py
fi
