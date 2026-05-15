#!/bin/bash
# Vercel build script — injects the backend API URL into index.html
# Vercel sets DM_API_URL as an env var; we replace the placeholder before serving.

set -e

API_URL="${DM_API_URL:-https://datamapper-ai-api.onrender.com}"
echo "→ Injecting API URL: $API_URL"

# Replace the window.__DM_API_URL__ placeholder in the built HTML
sed -i "s|window.__DM_API_URL__ \|\| 'http://localhost:7788'|'${API_URL}'|g" index.html

echo "→ Build complete"
