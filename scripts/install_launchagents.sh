#!/usr/bin/env bash
# Install launchd agents so the agentic-memory REST API server and Streamlit
# dashboard start at login and restart on crash (reboot-stable).
#
#   ./scripts/install_launchagents.sh       # load both
#   ./scripts/install_launchagents.sh stop  # unload both
set -euo pipefail
cd "$(dirname "$0")/.."

AGENT_DIR="$HOME/Library/LaunchAgents"
PLISTS=(
    "com.agentic-memory.rest-api.plist"
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
    if [[ ! -f "$AGENT_DIR/$p" ]]; then
        echo "ERROR: $AGENT_DIR/$p not found" >&2
        exit 1
    fi
    launchctl load "$AGENT_DIR/$p"
    echo "loaded $p"
done
echo "Done. Services will start now and at every login."
