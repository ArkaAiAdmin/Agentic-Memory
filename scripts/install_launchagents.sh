#!/usr/bin/env bash
# Install launchd agents so the agentic-memory kernel API server (9876, the
# Desktop IDE harness bridge) and Streamlit dashboard start at login and
# restart on crash (reboot-stable).
#
#   ./scripts/install_launchagents.sh       # load both
#   ./scripts/install_launchagents.sh stop  # unload both
#
# The old com.agentic-memory.rest-api launchd job instance is retired to prevent
# supervisor contention with the harness kernel for the memory.db flock (this retires
# the persistent background service instance, not the port itself; 9879 remains the
# standalone REST/WS API server port). Its plist is kept on disk as
# com.agentic-memory.rest-api.plist.disabled.
set -euo pipefail
cd "$(dirname "$0")/.."

AGENT_DIR="$HOME/Library/LaunchAgents"
SRC_DIR="scripts/launchd"
PLISTS=(
    "com.agentic-memory.kernel-api.plist"
    "com.agentic-memory.dashboard.plist"
)

if [[ "${1:-load}" == "stop" ]]; then
    for p in "${PLISTS[@]}"; do
        launchctl unload "$AGENT_DIR/$p" 2>/dev/null || true
        echo "unloaded $p"
    done
    exit 0
fi

for p in "${PLISTS[@]}"; do
    if [[ -f "$SRC_DIR/$p" ]]; then
        cp "$SRC_DIR/$p" "$AGENT_DIR/$p"
        echo "installed $p (from scripts/launchd)"
    elif [[ ! -f "$AGENT_DIR/$p" ]]; then
        echo "ERROR: $AGENT_DIR/$p not found" >&2
        exit 1
    fi
    launchctl unload "$AGENT_DIR/$p" 2>/dev/null || true
    launchctl load "$AGENT_DIR/$p"
    echo "loaded $p"
done
echo "Done. Services will start now and at every login."