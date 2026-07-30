#!/usr/bin/env bash
#
# install_launchagent.sh — install the agentic-memory background worker
# as a macOS launchd LaunchAgent.
#
# Why: the task_queue is drained by background_worker.py. The cron
# scheduler runs background_worker --drain every 5 min as the primary
# path. The launchd agent provides an independent fallback: it also runs
# --drain with a 300s throttle between invocations, so the queue keeps
# draining even if the cron scheduler is temporarily down.
#
# Design: --drain mode uses the "background_worker_drain" lock (mode-
# specific, separate from the cron scheduler's drain lock, so both paths
# coexist without contention). The old --interval=N persistent mode was
# removed because it held the background_worker flock permanently,
# starving all cron drain ticks and blocking enqueue_task.py inserts
# while a slow task was processing.
#
# Usage:
#     bash cron/install_launchagent.sh           # install + load
#     bash cron/install_launchagent.sh --uninstall
#     bash cron/install_launchagent.sh --show       # print resolved plist path
#     bash cron/install_launchagent.sh --dry-run   # print plist, don't install
#
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
ROOT="$( cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd )"
VENV_PY="$ROOT/venv/bin/python"
DB_PATH="$ROOT/memory/memory.db"
LOG_DIR="$ROOT/memory"

PLISTS=(
    "com.agentic-memory.background-worker"
    "com.agentic-memory.journal-reconciler"
)

# Read the sync token from memory.toml [api] token so worker subprocesses
# can authenticate against the sync-opencode / sync-mimocode servers.
SYNC_TOKEN="$(sed -n 's/^token *= *"\(.*\)"/\1/p' "$ROOT/memory.toml" | head -1)"
if [ -z "$SYNC_TOKEN" ]; then
    echo "WARNING: could not read token from $ROOT/memory.toml" >&2
    echo "         Worker subprocesses will fail to authenticate against sync servers." >&2
    SYNC_TOKEN="__UNSET__"
fi

# Serialize a path for safe embedding inside the plist XML.
xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

render_plist() {
    local plist_name="$1"
    local venv_xml db_xml log_xml root_xml token_xml
    venv_xml="$(xml_escape "$VENV_PY")"
    db_xml="$(xml_escape "$DB_PATH")"
    log_xml="$(xml_escape "$LOG_DIR")"
    root_xml="$(xml_escape "$ROOT")"
    token_xml="$(xml_escape "$SYNC_TOKEN")"
    sed -e "s|__VENV_PY__|$venv_xml|g" \
        -e "s|__DB_PATH__|$db_xml|g" \
        -e "s|__LOG_DIR__|$log_xml|g" \
        -e "s|__ROOT__|$root_xml|g" \
        -e "s|__SYNC_TOKEN__|$token_xml|g" \
        "$SCRIPT_DIR/$plist_name.plist.in"
}

case "${1:-install}" in
    --uninstall|uninstall)
        for p in "${PLISTS[@]}"; do
            plist_dst="$HOME/Library/LaunchAgents/$p.plist"
            echo "Unloading + removing $plist_dst"
            launchctl unload "$plist_dst" 2>/dev/null || true
            rm -f "$plist_dst"
        done
        echo "Done. Workers will stop at next launchd cycle."
        ;;
    --show)
        for p in "${PLISTS[@]}"; do
            echo "$HOME/Library/LaunchAgents/$p.plist"
        done
        ;;
    --dry-run)
        for p in "${PLISTS[@]}"; do
            echo "# Would install to: $HOME/Library/LaunchAgents/$p.plist"
            render_plist "$p"
        done
        ;;
    install|"")
        # Validate python + worker exist before installing a wedged agent.
        if [ ! -x "$VENV_PY" ]; then
            echo "ERROR: venv python not found at $VENV_PY" >&2
            echo "       Run the project's venv bootstrap first." >&2
            exit 1
        fi
        if [ ! -f "$ROOT/background/background_worker.py" ]; then
            echo "ERROR: worker not found at $ROOT/background/background_worker.py" >&2
            exit 1
        fi
        mkdir -p "$HOME/Library/LaunchAgents"
        for p in "${PLISTS[@]}"; do
            plist_dst="$HOME/Library/LaunchAgents/$p.plist"
            render_plist "$p" > "$plist_dst"
            chmod 644 "$plist_dst"
            # Load (or reload if already loaded). Errors here are non-fatal.
            if launchctl load "$plist_dst" 2>/dev/null; then
                echo "Loaded $plist_dst — service starts now + on login/boot."
            else
                # Already loaded? Try unload→load to pick up changes.
                launchctl unload "$plist_dst" 2>/dev/null || true
                if launchctl load "$plist_dst" 2>/dev/null; then
                    echo "Re-loaded $plist_dst."
                else
                    echo "WARN: launchctl load failed for $plist_dst." >&2
                fi
            fi
        done
        ;;
    *)
        echo "Unknown arg: $1" >&2
        echo "Usage: bash cron/install_launchagent.sh [install|--uninstall|--show|--dry-run]" >&2
        exit 1
        ;;
esac
