#!/bin/bash
# DataMapper: Full startup + pipeline runner (Frontier → Verizon BQ)
# Usage: bash ~/Desktop/Projects/dmapper/start_and_map.sh

set -e
cd "$(dirname "$0")"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DataMapper — Frontier → Verizon BQ (Mockup Pipeline)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Check / install Python deps
echo ""
echo "→ Checking Python dependencies..."
python3 -c "import fastapi, uvicorn, anthropic, requests" 2>/dev/null || {
  echo "→ Installing dependencies..."
  pip3 install -r requirements.txt --break-system-packages -q
  pip3 install requests --break-system-packages -q
  echo "✓ Dependencies installed"
}

# 2. Kill stale server
lsof -ti:7788 | xargs kill -9 2>/dev/null || true
sleep 1

# 3. Start server in background
echo ""
echo "→ Starting DataMapper server on :7788..."
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 7788 > /tmp/datamapper.log 2>&1 &
SERVER_PID=$!
echo "  PID: $SERVER_PID"

# 4. Wait for ready
echo "→ Waiting for server..."
for i in $(seq 1 30); do
  sleep 1
  if curl -s http://localhost:7788/api/health > /dev/null 2>&1; then
    echo "✓ Server ready (${i}s)"
    break
  fi
  if [ $i -eq 30 ]; then
    echo "✗ Server failed to start. Last logs:"
    tail -20 /tmp/datamapper.log
    exit 1
  fi
done

# 5. Run pipeline
echo ""
echo "→ Running Frontier → Verizon mapping pipeline..."
python3 run_pipeline.py

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Done! Output files in ~/Desktop/Projects/dmapper/"
echo "    mapping_export.csv"
echo "    table_mapping_summary.csv"
echo "    generated_mapping.sql"
echo "  Live UI: http://localhost:7788"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
