#!/usr/bin/env bash
# Remote OpenCode Access
# Run this on your Mac to expose OpenCode web via Cloudflare Tunnel.
# Then open the URL on your phone from anywhere.

set -euo pipefail
cd "$(dirname "$0")/../.."

PASSWORD="${OPENCODE_WEB_PASSWORD:-$(openssl rand -hex 8)}"
echo "=== Remote OpenCode Access ==="
echo "Generated password: $PASSWORD"
echo ""

echo "Starting OpenCode web on port 4096..."
export OPENCODE_SERVER_PASSWORD="$PASSWORD"
opencode web --port 4096 --hostname 127.0.0.1 &
OPENCODE_PID=$!
sleep 3

echo ""
echo "Starting Cloudflare Tunnel..."
echo "Please log in to Cloudflare if prompted, or use the URL below from your phone."
echo ""
cloudflared tunnel --url http://localhost:4096

# Cleanup on exit
kill $OPENCODE_PID 2>/dev/null || true
