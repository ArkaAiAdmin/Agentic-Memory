"""Job registry for the consolidated cron scheduler.

Each job defines:
- freq: frequency tier ("5m", "15m", "1h", "1d", "1w", "1m")
- script: path to the script to run (relative to repo root)
- args: optional command-line arguments
- env: optional extra environment variables
- timeout: max seconds before the job is killed (default 300).
    For enqueue_task.py entries (most jobs), this timeout applies to the
    enqueue INSERT (~0.1s), NOT the actual task execution.  Execution
    timeout is configured per task type in the cron_task_timeouts table
    (migration 063).  For direct-script jobs, this timeout IS the
    subprocess kill timeout.
- offset_min: minute offset within the frequency window (for staggering)

The scheduler reads this registry and determines which jobs are due
based on the current time.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

JOBS: dict[str, dict] = {
    # ── Immediate tier: every 5 minutes ──────────────────────────────
    # Primary drain path for the task queue.  Runs
    # ``background_worker --drain --max-tasks=50`` which acquires the
    # ``background_worker_drain`` lock, processes up to 50 pending tasks,
    # and exits.  The launchd daemon (cron/install_launchagent.sh) runs
    # the same --drain mode on a 300s throttle as an independent
    # fallback — because the lock names differ (``background_worker_drain``
    # vs ``background_worker_persistent``), both paths coexist without
    # contention.
    #
    # The old ``--interval=N`` persistent mode was removed because it held
    # the ``background_worker`` flock permanently, starving all cron drain
    # ticks and blocking ``enqueue_task.py`` inserts while a slow task was
    # processing (see background/background_worker.py:main for the lock
    # separation change).
    "background_worker": {
        "freq": "5m",
        "script": "background_worker.py",
        "args": ["--drain", "--max-tasks=50"],
        "timeout": 60,
    },
    # Journal reconciler: drains the CQRS write-journal in a separate
    # process (was previously an inline thread in the MCP server process,
    # competing with MCP tool calls for the SQLiteWriteQueue thread).
    "journal_reconciler": {
        "freq": "5m",
        "offset_min": 2,
        "script": "-m",
        "args": ["background.journal_reconciler", "--drain", "--max-entries=50"],
        "timeout": 120,
    },
    "pipeline_health": {
        "freq": "15m",
        "offset_min": 1,
        "script": "cron/cron_pipeline_health.py",
        "timeout": 60,
    },
    "health_check": {
        "freq": "15m",
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_health_check"],
        "timeout": 60,
    },
    "daemon_watchdog": {
        "freq": "15m",
        "offset_min": 3,
        "script": "cron/cron_daemon_watchdog.py",
        "timeout": 30,
    },
    "reap_stale_tasks": {
        "freq": "15m",
        "offset_min": 5,
        "script": "cron/cron_reap_stale_tasks.py",
        "timeout": 30,
    },
    "task_queue_monitor": {
        "freq": "15m",
        "offset_min": 10,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_monitor_task_queue"],
        "timeout": 60,
    },
    "watchdog": {
        "freq": "30m",
        "offset_min": 25,
        "script": "cron/cron_watchdog.py",
        "timeout": 60,
    },

    # ── Hourly tier ──────────────────────────────────────────────────
    "sync": {
        "freq": "1h",
        "offset_min": 5,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_sync"],
        "timeout": 60,
    },
    "crdt_sync": {
        "freq": "1h",
        "offset_min": 15,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_crdt_sync"],
        "env": {"MEMORY_MULTI_AGENT": "1", "MEMORY_CRDT_ENABLED": "1"},
        "timeout": 60,
    },
    "auto_retry_dead_tasks": {
        "freq": "1h",
        "offset_min": 7,
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_retry_dead_tasks"],
        "timeout": 60,
    },
    "policy_hash_status": {
        "freq": "1h",
        "offset_min": 0,
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_policy_hash_status", "--payload", '{"args": ["--alert-stdout"]}'],
        "timeout": 60,
    },
    "sync_usage": {
        "freq": "15m",
        "offset_min": 7,
        "script": "cron/cron_sync_usage.py",
        "timeout": 60,
    },

    # ── Daily tier ───────────────────────────────────────────────────
    "daily_digest": {
        "freq": "1d",
        "offset_min": 0,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_daily_digest", "--payload", '{"args": ["daily-digest"]}'],
        "timeout": 120,
    },
    "promote_drafts": {
        "freq": "6h",
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_promote_drafts"],
        "timeout": 120,
    },
    "repair_unindexed": {
        "freq": "6h",
        "offset_min": 30,
        "script": "cron/cron_repair_unindexed.py",
        "timeout": 120,
        "description": "Re-enqueue indexing tasks for memories that lack embeddings/vec_keys",
    },
    "purge_auto_saves": {
        "freq": "1d",
        "offset_min": 30,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_purge_auto_saves"],
        "timeout": 120,
    },
    "cleanup_auto_logs": {
        "freq": "1d",
        "offset_min": 45,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_cleanup_auto_logs", "--payload", '{"args": ["--max-age-days", "30"]}'],
        "timeout": 120,
    },
    "backfill_all": {
        "freq": "1d",
        "offset_min": 90,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_backfill_all", "--payload", '{"args": ["--incremental"]}'],
        "env": {"MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 300,
    },
    "backup": {
        "freq": "1d",
        "offset_min": 120,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_backup"],
        "timeout": 300,
    },
    "backup_validate": {
        "freq": "1d",
        "offset_min": 135,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_backup_validate"],
        "timeout": 120,
    },
    "heartbeat": {
        "freq": "1d",
        "offset_min": 180,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_heartbeat"],
        "env": {"MEMORY_SELF_DIRECTED": "1", "MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 300,
    },
    "kg_backfill_monitor": {
        "freq": "1d",
        "offset_min": 240,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_kg_backfill_monitor"],
        "timeout": 120,
    },
    # CHANGE 3: graph analytics (PageRank + betweenness + communities + snapshot)
    # moved OFF the save path and onto a scheduled job.  Before this, every
    # memory save triggered a full update_graph_analytics() recompute
    # (kg_db.py), a consistent O(V*(V+E)) tail-latency source.  This daily job
    # maintains centrality off the write path.
    "kg_analytics": {
        "freq": "1d",
        "offset_min": 255,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_kg_analytics"],
        "env": {"MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 300,
    },
    "embedding_recompute": {
        "freq": "1d",
        "offset_min": 240,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_embedding_recompute", "--payload", '{"args": ["--once"]}'],
        "timeout": 300,
    },
    "detect_vec_drift": {
        "freq": "1d",
        "offset_min": 270,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_detect_vec_drift"],
        "timeout": 120,
    },
    "config_drift": {
        "freq": "1d",
        "offset_min": 270,
        "script": "cron/enqueue_task.py",
        "args": [
            "--task-type", "cron_check_config_drift",
            "--payload", '{"args": ["--severity-floor", "stability", "--reload-policy", "--apply-tier-patches", "--alert-stdout"]}',
        ],
        "timeout": 60,
    },
    "resolve_contradictions": {
        "freq": "1d",
        "offset_min": 300,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_resolve_contradictions"],
        "env": {"MEMORY_TEMPORAL_KG": "1"},
        "timeout": 300,
    },
    "auto_share": {
        "freq": "1d",
        "offset_min": 540,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_auto_share"],
        "env": {"MEMORY_MULTI_AGENT": "1"},
        "timeout": 120,
    },

    # ── Weekly tier (Sunday) ─────────────────────────────────────────
    "integrity_check": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 60,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_integrity_check"],
        "env": {"MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 300,
    },
    "log_retention": {
        "freq": "1w",
        "dow": 6,
        "offset_min": 60,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_log_retention"],
        "timeout": 120,
    },
    "tier_migration": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 180,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_tier_migration", "--payload", '{"args": ["--once"]}'],
        "env": {"MEMORY_TEMPORAL_TIERS": "1"},
        "timeout": 300,
    },
    "kg_backfill": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 210,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_kg_backfill", "--payload", '{"args": ["--incremental"]}'],
        "timeout": 600,
    },
    "consolidate": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 240,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_consolidate"],
        "env": {"MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 300,
    },
    "rewrite_links": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 270,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_rewrite_links"],
        "env": {"MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 120,
    },
    "pinned_decay": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 300,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_pinned_decay"],
        "env": {"MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 120,
    },
    "concept_drift": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 360,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_concept_drift"],
        "timeout": 300,
    },
    "train_forget_model": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 330,
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_train_forget_model"],
        "timeout": 60,
    },
    "train_temporal_ssm": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 345,
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_train_temporal_ssm"],
        "timeout": 60,
    },
    # ── Weekly orphans (INFRASTRUCTURE_AUDIT G2 — registered in code, never scheduled) ──
    "answer_rerank": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 390,
        "script": "cron/cron_answer_rerank.py",
        "timeout": 600,
    },
    "recompute_temporal_priors": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 375,
        "script": "cron/cron_recompute_temporal_priors.py",
        "timeout": 120,
    },
    "semantic_clusters": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 1320,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_semantic_clusters"],
        "timeout": 300,
    },
    "skill_decay": {
        "freq": "1w",
        "dow": 0,
        "offset_min": 1350,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_skill_decay"],
        "timeout": 120,
    },
    "skill_extraction": {
        "freq": "1w",
        "dow": 1,
        "offset_min": 225,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_skill_extraction"],
        "timeout": 300,
    },
    "cross_session_learn": {
        "freq": "1w",
        "dow": 1,
        "offset_min": 255,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_cross_session_learn"],
        "timeout": 300,
    },
    "tune_rewrites": {
        "freq": "1w",
        "dow": 1,
        "offset_min": 315,
        "script": "cron/cron_tune_rewrites.py",
        "timeout": 600,
    },
    "quality_filter": {
        "freq": "1w",
        "dow": 1,
        "offset_min": 420,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_quality_filter"],
        "env": {"MEMORY_QUALITY_GATES": "1"},
        "timeout": 120,
    },
    "auto_summarize": {
        "freq": "1w",
        "dow": 1,
        "offset_min": 450,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_auto_summarize"],
        "env": {"MEMORY_SUMMARIZATION": "1"},
        "timeout": 300,
    },
    "retention_stats": {
        "freq": "1w",
        "dow": 1,
        "offset_min": 480,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_retention_stats"],
        "env": {"MEMORY_ADAPTIVE_RETENTION": "1"},
        "timeout": 120,
    },

    # ── Monthly tier (1st of month) ──────────────────────────────────
    "compact": {
        "freq": "1m",
        "dom": 1,
        "offset_min": 150,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_compact"],
        "env": {"MEMORY_KNOWLEDGE_GRAPH": "1"},
        "timeout": 600,
    },
    "purge_expired": {
        "freq": "1m",
        "dom": 1,
        "offset_min": 390,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_purge_expired"],
        "timeout": 120,
    },
    "rebuild_fts": {
        "freq": "1d",
        "offset_min": 153,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_rebuild_fts"],
        "timeout": 300,
    },
    "revalidate_entailments": {
        "freq": "1d",
        "offset_min": 360,
        # timeout applies to enqueue insert only — see cron_task_timeouts for execution timeout
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_revalidate_entailments"],
        "timeout": 300,
    },
    "train_ltr": {
        "freq": "1w",
        "dow": 1,
        "offset_min": 300,
        "script": "cron/enqueue_task.py",
        "args": ["--task-type", "cron_train_ltr"],
        "timeout": 60,
    },
    "review_beliefs": {
        "freq": "1d",
        "offset_min": 420,
        "script": "cron/cron_review_beliefs.py",
        "timeout": 300,
    },
}
