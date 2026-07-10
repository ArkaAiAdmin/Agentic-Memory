#!/usr/bin/env bash
# install_crontab.sh — install scheduled cron jobs for agentic-memory.
#
# Idempotent: re-running replaces the agentic-memory block but leaves
# unrelated user crontab entries alone. The block is delimited by
# marker comments so it can be located and updated.
#
# Usage:
#     bash cron/install_crontab.sh              # install (default)
#     bash cron/install_crontab.sh --uninstall  # remove the block
#     bash cron/install_crontab.sh --show       # print the current block
#     bash cron/install_crontab.sh --dry-run    # show the block without installing
#
# After install, run the background worker / concept-drift cron once
# manually to backfill the data:
#     venv/bin/python cron/cron_concept_drift.py
#     venv/bin/python background_worker.py --drain --max-tasks=20000
set -euo pipefail

# Scenario 10 fix (2026-06-22): prevent concurrent installer runs
# from doubling the crontab block.  Two parallel `bash install_crontab.sh`
# invocations used to both append the same block, doubling scheduled
# jobs and causing 2x background churn.
#
# We use a mkdir-based lock because it is atomic on every POSIX
# system (Linux, macOS, BSD) — the standard ``flock`` is Linux-only.
# ``mkdir`` returns 0 if the directory was created, EEXIST if it
# already existed.  The loser polls the directory and waits.
# (Use INSTALL_LOCK_DIR to avoid shadowing the LOCK_DIR used by
# the per-cron lock file at $ROOT/memory/locks below.)
INSTALL_LOCK_DIR="${TMPDIR:-/tmp}/agentic-memory-install-crontab.lock.d"
INSTALL_LOCK_WAIT_MAX_S=60
_install_lock_start=$SECONDS
_acquire_install_lock() {
    # Atomic mkdir-based lock.  Returns 0 on success, 1 on failure.
    # ``mkdir -p`` would silently succeed if the directory already
    # exists, defeating the lock.  We use plain ``mkdir`` and fall
    # back to /tmp if the parent doesn't exist.
    if mkdir "$1" 2>/dev/null; then
        return 0
    fi
    # Failed — either EEXIST (lock held) or ENOENT (parent missing).
    if [ ! -d "$(dirname "$1")" ]; then
        # Parent missing: fall back to /tmp.
        INSTALL_LOCK_DIR="/tmp/agentic-memory-install-crontab.lock.d"
        if mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}
while ! _acquire_install_lock "$INSTALL_LOCK_DIR"; do
    if [ $((SECONDS - _install_lock_start)) -ge $INSTALL_LOCK_WAIT_MAX_S ]; then
        echo "Timed out waiting for another install_crontab.sh to finish (waited ${INSTALL_LOCK_WAIT_MAX_S}s)." >&2
        # Best-effort: try to take it over by removing and re-creating.
        # If another process really is still running, the next
        # invocations of THIS script will detect the lock and fail
        # loudly.  We don't rm -rf blindly because a still-running
        # process holds this directory.
        exit 1
    fi
    sleep 0.2
done
# Trap to clean up the lock on exit (success, error, or signal).
cleanup_lock() { rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true; }
trap cleanup_lock EXIT INT TERM

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
ROOT="$( cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd )"
VENV_PY="$ROOT/venv/bin/python"
LOG_DIR="$ROOT/memory"
DB_PATH="$ROOT/memory/memory.db"
LOCK_DIR="$ROOT/memory/locks"

# Marker used to find/replace the agentic-memory block.
BLOCK_BEGIN="# BEGIN agentic-memory managed block"
BLOCK_END="# END agentic-memory managed block"

# Build the cron block. All crons run as the current user with
# MEMORY_DB_PATH pointing at the global DB. Times are UTC for
# cross-machine reproducibility (host timezone varies).
#
# Concurrency model (H-fix 2026-06-22):
#   * Every script in cron/cron_*.py acquires its own
#     ``<LOCK_DIR>/<name>.lock`` via ``acquire_lock_or_exit`` at the
#     top of ``main()`` (see ``cron/_flock.py``). So two instances
#     of the SAME cron never run concurrently.
#   * Two DIFFERENT crons can still overlap (different lock files).
#     The SQLite WAL handles write ordering at the DB level.
#   * Time-slot scheduling below minimizes overlap (e.g.
#     cron_compact is at 02:30 on the 1st, not 02:00, so it never
#     races cron_backup at 02:00 daily). The flock is the safety
#     net, not the primary defense.
build_block() {
    cat <<EOF
$BLOCK_BEGIN
# agentic-memory scheduled jobs (managed by cron/install_crontab.sh)
# Schedule is in UTC. The local time will be UTC + host_tz_offset.
# m  h  dom mon dow  command
#
# Consolidated scheduler: replaces 39 individual cron entries with 1.
# The scheduler checks which jobs are due and runs them sequentially.
# Job registry: cron/jobs.py | Scheduler: cron/scheduler.py
# Execution tracking: cron_runs table | Health: memory_system_health MCP tool
*/5 *  *   *   *    MEMORY_DB_PATH=$DB_PATH MEMORY_LLM_EXTRACTION=0 $VENV_PY $ROOT/cron/scheduler.py >> $LOG_DIR/scheduler.log 2>&1
$BLOCK_END
EOF
}

# Strip any existing agentic-memory block (between markers) from stdin.
strip_block() {
    awk -v begin="$BLOCK_BEGIN" -v end="$BLOCK_END" '
        $0 == begin { in_block = 1; next }
        $0 == end   { in_block = 0; next }
        !in_block   { print }
    '
}

case "${1:-install}" in
    --uninstall|uninstall)
        echo "Removing agentic-memory crontab block..."
        tmp="$(mktemp)"
        crontab -l 2>/dev/null | strip_block > "$tmp" || true
        crontab "$tmp"
        rm -f "$tmp"
        echo "Done."
        ;;
    --show|show)
        echo "Current crontab (filtered for agentic-memory):"
        crontab -l 2>/dev/null | awk -v begin="$BLOCK_BEGIN" -v end="$BLOCK_END" '
            $0 == begin { in_block = 1 }
            in_block    { print; if ($0 == end) in_block = 0 }
        ' || echo "  (no crontab installed)"
        ;;
    --dry-run|dry-run)
        echo "Would install the following block:"
        build_block
        ;;
    install|"")
        echo "Installing agentic-memory crontab block..."
        # Make sure the lock dir exists before the first cron fires.
        mkdir -p "$LOCK_DIR"
        tmp="$(mktemp)"
        # Read existing crontab (or empty), strip the old block, append new.
        (crontab -l 2>/dev/null || true) | strip_block > "$tmp"
        echo "" >> "$tmp"
        build_block >> "$tmp"
        crontab "$tmp"
        rm -f "$tmp"
        echo "Done. Installed $(build_block | wc -l | tr -d ' ') lines."
        echo ""
        echo "To populate the task_queue and concept_drift tables now:"
        echo "  venv/bin/python cron/cron_concept_drift.py"
        echo "  venv/bin/python background_worker.py --drain --max-tasks=20000"
        ;;
    *)
        echo "Unknown argument: $1"
        echo "Usage: $0 [install|--uninstall|--show|--dry-run]"
        exit 1
        ;;
esac
