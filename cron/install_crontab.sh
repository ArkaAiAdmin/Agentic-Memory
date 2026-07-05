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
# Each script acquires its own flock at startup
# (\$LOCK_DIR/<cron-name>.lock); see cron/_flock.py.

# Background worker — process task queue (drain mode burns down
# backlog; --once is too slow for 10K+ task backlogs).
# H-fix 2026-06-22: cadence reduced */5 → */15 (the worker now has
# flock protection so it self-skips, but cadence 5min was overkill
# for an idle system; observed 32 zombie workers before the fix).
# --max-tasks reduced 200 → 50 (200 tasks can hold a worker >90s;
# 50 keeps wall time under 30s for normal backlogs).
*/15 *  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/background_worker.py --drain --max-tasks=50 >> $LOG_DIR/worker.log 2>&1

# Health check — FTS drift, KG orphans, circuit breaker, auto-save health
# (every 15 min, staggered 5 min after background_worker).
# Writes .health_status.json for the proactive-context hook to read.
*/15 *  *   *   *    MEMORY_KNOWLEDGE_GRAPH=1 $VENV_PY $ROOT/cron/cron_health_check.py >> $LOG_DIR/health-check.log 2>&1

# Daily digest — rolls auto-saves into one note per day
# Phase B: enqueue via worker task queue
0  0  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_daily_digest --payload '{"args": ["daily-digest"]}' >> $LOG_DIR/digest.log 2>&1

# Promote auto-capture drafts — scan lessons tagged auto-capture+draft and
# promote qualifying notes to curated tier (importance=4, promoted tag).
# Runs every 6 hours so drafts created in the morning session have a chance
# to be promoted the same day.
# Phase B: enqueue via worker task queue
0  */6 *  *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_promote_drafts >> $LOG_DIR/promote-drafts.log 2>&1

# Purge auto-save inbox — clean stale pending auto-saves (daily 00:30)
# Phase B: enqueue via worker task queue
30 0  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_purge_auto_saves >> $LOG_DIR/purge-auto-saves.log 2>&1

# Cleanup raw auto-save tool logs — archive sessions/auto-* older than 30d (daily 00:45)
# Phase B: enqueue via worker task queue
45 0  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_cleanup_auto_logs --payload '{"args": ["--max-age-days", "30"]}' >> $LOG_DIR/cleanup-auto-logs.log 2>&1

# Integrity check — DB health, FTS consistency (Sunday 01:00)
# Phase B: enqueue via worker task queue
0  1  *   *   0    MEMORY_KNOWLEDGE_GRAPH=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_integrity_check >> $LOG_DIR/integrity.log 2>&1

# Log retention — rotate/archive old cron logs (daily 01:00, same slot as
# integrity on Sundays; per-cron flocks serialize against each other)
# Phase B: enqueue via worker task queue
0  1  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_log_retention >> $LOG_DIR/log-retention.log 2>&1

# Incremental backfill — rebuild stale indexes daily (daily 01:30)
# Phase B: enqueue via worker task queue
30 1  *   *   *    MEMORY_KNOWLEDGE_GRAPH=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_backfill_all --payload '{"args": ["--incremental"]}' >> $LOG_DIR/backfill.log 2>&1

# Backup — daily SQLite backup (keeps 7 daily)
# Phase B: enqueue via worker task queue
0  2  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_backup >> $LOG_DIR/backup.log 2>&1

# Backup validation — verify backup integrity (daily 02:15, after backup)
# Phase B: enqueue via worker task queue
15 2  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_backup_validate >> $LOG_DIR/backup-validate.log 2>&1

# Compact — monthly tier migration + consolidation + rebuild + archive
# (H4: shifted from 02:00 to 02:30 on the 1st to avoid racing
# cron_backup at 02:00 daily; flock is the safety net.)
# Phase B: enqueue via worker task queue
30 2  1   *   *    MEMORY_KNOWLEDGE_GRAPH=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_compact >> $LOG_DIR/compact.log 2>&1

# FTS5 rebuild — daily lightweight rebuild
# Phase B: enqueue via worker task queue
33 2  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_rebuild_fts >> $LOG_DIR/fts-rebuild.log 2>&1

# Heartbeat — decay, tier assignment, archive stale notes (daily 03:00)
# Phase B: enqueue via worker task queue
0  3  *   *   *    MEMORY_SELF_DIRECTED=1 MEMORY_KNOWLEDGE_GRAPH=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_heartbeat >> $LOG_DIR/heartbeat.log 2>&1

# Tier migration — on-demand hot/warm/cold migration (Sunday 03:00).
# Uses the per-cron flock to serialize against cron_heartbeat at
# 03:00 daily. On most Sundays, heartbeat finishes first and
# tier_migration runs. If a slow heartbeat holds the lock past
# 03:00:30, tier_migration is skipped that week — safe, not
# lossy.
# Phase B: enqueue via worker task queue
0  3  *   *   0    MEMORY_TEMPORAL_TIERS=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_tier_migration --payload '{"args": ["--once"]}' >> $LOG_DIR/tier-migration.log 2>&1

# Weekly KG backfill — refresh kg_facts/entities/edges (Sunday 03:30)
# Phase B: enqueue via worker task queue
30 3  *   *   0    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_kg_backfill --payload '{"args": ["--incremental"]}' >> $LOG_DIR/kg-backfill-cron.log 2>&1

# Skill extraction — turn procedural memories into skills (Mondays 03:45)
# Phase B: enqueue via worker task queue
45 3  *   *   1    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_skill_extraction >> $LOG_DIR/skill-extraction.log 2>&1

# Cross-session learning — extract reusable patterns (Mondays 04:15)
# Phase B: enqueue via worker task queue
15 4  *   *   1    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_cross_session_learn >> $LOG_DIR/cross-session-learn.log 2>&1

# KG backfill monitor — alerts on backfill failures (daily 04:00).
# On Sundays 04:00 this overlaps with cron_consolidate; the per-cron
# flocks let both run and the SQLite WAL serializes their writes.
# Phase B: enqueue via worker task queue
0  4  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_kg_backfill_monitor >> $LOG_DIR/kg-backfill-monitor.log 2>&1

# Embedding recompute — detect model change, re-embed if needed (daily 04:00)
# Phase B: enqueue via worker task queue
0  4  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_embedding_recompute --payload '{"args": ["--once"]}' >> $LOG_DIR/embedding-recompute.log 2>&1

# Consolidation — dedup, detect contradictions (Sunday 04:00)
# Phase B: enqueue via worker task queue
0  4  *   *   0    MEMORY_KNOWLEDGE_GRAPH=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_consolidate >> $LOG_DIR/consolidation.log 2>&1

# Vec drift detection — alerts if vec_keys/vec_idx diverge (daily 04:30)
# Phase B: enqueue via worker task queue
30 4  *   *   *    $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_detect_vec_drift >> $LOG_DIR/drift.log 2>&1

# Rewrite broken wiki links (Sunday 04:30)
# Phase B: enqueue via worker task queue
30 4  *   *   0    MEMORY_KNOWLEDGE_GRAPH=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_rewrite_links >> $LOG_DIR/rewrite-links.log 2>&1

# Pinned decay — auto-unpin stale pinned notes (Sunday 05:00)
# Phase B: enqueue via worker task queue
0  5  *   *   0    MEMORY_KNOWLEDGE_GRAPH=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_pinned_decay >> $LOG_DIR/pinned-decay.log 2>&1

# Concept drift detection — cosine distance between current and
# previous embedding centroid (Sunday 06:00 UTC). Populates the
# concept_drift AND drift_alarms tables.
# Phase B: enqueue via worker task queue
0  6  *   *   0    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_concept_drift >> $LOG_DIR/concept-drift.log 2>&1

# Purge soft-deleted notes older than 30 days (1st of month 06:30).
# (H8: shifted from 06:00 to 06:30 so it never races cron_concept_drift
# on the 1st Sunday/Monday when both are scheduled.)
# Phase B: enqueue via worker task queue
30 6  1   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_purge_expired >> $LOG_DIR/purge.log 2>&1

# Quality gate stats (Mondays 07:00). H8: shifted from 06:00.
# Phase B: enqueue via worker task queue
0  7  *   *   1    MEMORY_QUALITY_GATES=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_quality_filter >> $LOG_DIR/quality.log 2>&1

# Auto-summarize long notes (Mondays 07:30). H8: shifted from 07:00
# to make room for cron_quality_filter at 07:00.
# Phase B: enqueue via worker task queue
30 7  *   *   1    MEMORY_SUMMARIZATION=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_auto_summarize >> $LOG_DIR/summarize.log 2>&1

# Adaptive retention stats (Mondays 08:00)
# Phase B: enqueue via worker task queue
0  8  *   *   1    MEMORY_ADAPTIVE_RETENTION=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_retention_stats >> $LOG_DIR/retention.log 2>&1

# Auto-share — opt-in memories to the shared pool (daily 09:00)
# Phase B: enqueue via worker task queue
0  9  *   *   *    MEMORY_MULTI_AGENT=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_auto_share >> $LOG_DIR/auto-share.log 2>&1

# Sync — single-peer two-way sync (5 min past every hour)
# Phase B: enqueue via worker task queue
5  *  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_sync >> $LOG_DIR/sync.log 2>&1

# CRDT sync — multi-peer two-way sync (15 min past every hour).
# Staggered 10 min after cron_sync so both never run at the same time.
# Phase B: enqueue via worker task queue
15 *  *   *   *    MEMORY_MULTI_AGENT=1 MEMORY_CRDT_ENABLED=1 MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_crdt_sync >> $LOG_DIR/crdt-sync.log 2>&1

# Watchdog — periodic health assertion and daemon uptime check
# (Staggered at :25 and :55 to avoid overlapping background_worker at :00/:15/:30/:45)
25,55 *  *   *   *    $VENV_PY $ROOT/cron/cron_watchdog.py >> $LOG_DIR/watchdog.log 2>&1

# Daemon watchdog — restart auto-save daemon if it has crashed (every 5 min at :03/:18/:33/:48)
3,18,33,48 *  *   *   *    $VENV_PY $ROOT/cron/cron_daemon_watchdog.py >> $LOG_DIR/watchdog-daemon.log 2>&1

# Task queue monitor — alert on backlog depth and stale task types (Phase F)
# Staggered at :10 and :40 to avoid all other operational crons.
# Phase B: enqueue via worker task queue
10,40 *  *   *   *    MEMORY_DB_PATH=$DB_PATH $VENV_PY $ROOT/cron/enqueue_task.py --task-type cron_monitor_task_queue >> $LOG_DIR/task-queue-monitor.log 2>&1
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
