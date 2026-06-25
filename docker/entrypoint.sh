#!/bin/sh
# docker/entrypoint.sh — service selector for the agentic-memory image.
#
# The image contains MCP server, sync server, and cron. The SERVICE
# env var decides which one runs. This keeps the image small and
# lets the operator change services with a single env var.
#
# Valid SERVICE values:
#   mcp     — MCP server (default; runs the FastMCP server)
#   sync    — sync_server.py HTTP server (CRDT peer sync)
#   cron    — cron_runner.py (the containerized cron loop)
#   shell   — drop into a shell for debugging
#
# The script is idempotent: re-running it with a different SERVICE
# just starts a different process. The container's PID 1 is this
# script, so signals (SIGTERM) propagate to the child process.

set -eu

SERVICE="${SERVICE:-mcp}"

# Ensure the DB path is set so the child processes can find it.
export MEMORY_DB_PATH="${MEMORY_DB_PATH:-/data/memory.db}"
export MEMORY_LOCAL_DIR="${MEMORY_LOCAL_DIR:-/data}"
export PYTHONPATH="${PYTHONPATH:-}:/app"

mkdir -p /data
if [ ! -f "$MEMORY_DB_PATH" ]; then
    echo "[entrypoint] initializing empty memory.db at $MEMORY_DB_PATH"
    cd /app
    python cli.py bootstrap || true
fi

case "$SERVICE" in
    mcp)
        echo "[entrypoint] starting MCP server..."
        cd /app
        exec python cli.py server
        ;;
    sync)
        echo "[entrypoint] starting sync server..."
        cd /app
        exec python sync_server.py
        ;;
    cron)
        echo "[entrypoint] starting cron runner..."
        cd /app
        exec python /app/docker/cron_runner.py \
            --schedule /app/docker/schedule.json \
            --scripts-dir /app/cron
        ;;
    shell)
        echo "[entrypoint] dropping into shell (use 'exit' to leave)..."
        exec /bin/sh
        ;;
    *)
        echo "[entrypoint] unknown SERVICE='$SERVICE'" >&2
        echo "Valid: mcp, sync, cron, shell" >&2
        exit 1
        ;;
esac
