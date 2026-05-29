#!/bin/bash
# Vercel/Cloudflare-Pages build script — injects the backend API URL into the
# frontend/ pages so they call the right API when hosted on a separate origin.
# Set DM_API_URL as a build env var (defaults to the Render API below).

set -e

API_URL="${DM_API_URL:-https://datamapper-ai-api.onrender.com}"

# Build with Vite when the frontend project is present; otherwise serve the raw
# pages. TARGET_DIR is what gets deployed (and where we inject the API URL).
if [ -f frontend/package.json ]; then
  echo "→ Building frontend with Vite…"
  ( cd frontend && (npm ci --no-audit --no-fund || npm install --no-audit --no-fund) && npm run build )
  TARGET_DIR="frontend/dist"
else
  TARGET_DIR="frontend"
fi

echo "→ Injecting API URL: $API_URL"
INJECT="<script>window.__DM_API_URL__='${API_URL}';</script>"
for f in "$TARGET_DIR/index.html" "$TARGET_DIR/login.html"; do
  if [ -f "$f" ]; then
    sed -i "0,/<head[^>]*>/s|<head[^>]*>|&\n${INJECT}|" "$f"
    echo "  ✓ injected into $f"
  fi
done

echo "→ Build complete (output: $TARGET_DIR)"
