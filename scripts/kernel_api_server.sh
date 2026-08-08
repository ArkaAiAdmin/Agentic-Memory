#!/usr/bin/env bash
# Kernel API server supervisor for the desktop harness (agentic-memory-ide).
# Durability rules:
#   1. long-running supervisor owned by launchd (com.agentic-memory.kernel-api),
#      so there is always a launchd-managed process guarding port 9876.
#   2. Reads the live Bearer token from memory/.api_token (the same file the
#      harness's get_or_create_api_token() uses), so auth always matches.
#   3. If 127.0.0.1:9876 already answers /health, does nothing and re-checks
#      in a few seconds (no double-bind, no fight with a harness-owned kernel).
#   4. When the port is dead (harness died/never started/crashed), spawns the
#      kernel exactly like the harness would (same --db, --port, env) and
#      foreground-waits on it, respawning on every crash.
#   5. On SIGTERM/SIGINT (launchctl bootout / shutdown), tears down the child
#      and exits cleanly -> no fighting launchd teardown.
set -euo pipefail

HOST=127.0.0.1
PORT=9876
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="$REPO_DIR/memory/memory.db"
TOKEN_FILE="$REPO_DIR/memory/.api_token"
CHECK_INTERVAL=15

cleanup() {
    if [[ -n "${CHILD_PID:-}" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT SIGHUP

resolve_token() {
    # Token: use the harness-owned file; fall back to memory.toml token, else generate.
    if [[ -f "$TOKEN_FILE" ]]; then
        tr -d '[:space:]' < "$TOKEN_FILE"
    elif [[ -s "$REPO_DIR/memory.toml" ]]; then
        sed -n 's/^[[:space:]]*token[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$REPO_DIR/memory.toml" | head -1
    fi
}

port_healthy() {
    local tok="$1"
    curl -sf -m 2 -H "Authorization: Bearer $tok" "http://$HOST:$PORT/health" >/dev/null 2>&1
}

while true; do
    TOKEN="$(resolve_token || true)"
    if [[ -z "$TOKEN" ]]; then
        TOKEN="$(/usr/bin/python3 -c 'import secrets,string; print(secrets.choice(string.hexdigits*8))' 2>/dev/null || true)"
        if [[ -n "$TOKEN" ]]; then
            umask 077
            printf '%s' "$TOKEN" > "$TOKEN_FILE"
        fi
    fi

    if port_healthy "$TOKEN"; then
        sleep "$CHECK_INTERVAL"
        continue
    fi

    umask 077
    export AGENTIC_MEMORY_DIR="$REPO_DIR"
    export MEMORY_AGENT_ID=ami
    export MEMORY_SYNC_LISTEN_PORT=9880
    export MEMORY_AUTH_MODE=token
    export MEMORY_API_TOKEN="$TOKEN"
    export PYTHONUNBUFFERED=1
    export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
    export HF_HUB_OFFLINE=1
    export PYTHONPATH="$REPO_DIR"

    "$REPO_DIR/venv/bin/python" "$REPO_DIR/cli.py" api \
        --db "$DB_PATH" --port "$PORT" --host "$HOST" &
    CHILD_PID=$!
    wait "$CHILD_PID" || true
    unset CHILD_PID
    sleep 2
done