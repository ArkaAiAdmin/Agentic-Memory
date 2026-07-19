#!/usr/bin/env python3
"""Operations tab — Scheduled Jobs, Backups, Multi-Agent Sync, Runbook."""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import dashboard
from dashboard import (
    DARK,
    _fmt_date,
    get_conn,
    query,
    try_count,
    table,
)
from dashboard.api_client import (
    _api,
    _query_api,
    _try_count_api,
    _table_exists_api,
)

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def render_operations() -> None:
    """Render the Operations tab with four sub-tabs."""
    st.subheader("Operations")

    # Cache rendered state to avoid re-rendering on every tab switch
    if "ops_rendered" not in st.session_state:
        st.session_state["ops_rendered"] = False

    tab_jobs, tab_backups, tab_sync, tab_runbook = st.tabs(
        ["Scheduled Jobs", "Backups", "Multi-Agent Sync", "Runbook"]
    )

    with tab_jobs:
        _render_scheduled_jobs()

    with tab_backups:
        _render_backups()

    with tab_sync:
        _render_multi_agent()

    with tab_runbook:
        _render_runbook()


# ── 1. Scheduled Jobs ─────────────────────────────────────────────────────


@st.cache_data(ttl=300)
def _cached_extra_jobs(known_task_types_tuple):
    """Cache the extra job discovery query."""
    known_set = set(known_task_types_tuple)
    placeholders = ",".join("?" for _ in known_set)
    return _query_api(
        f"SELECT DISTINCT task_type FROM task_queue WHERE task_type NOT IN ({placeholders})",
        list(known_set),
    )


def _render_scheduled_jobs() -> None:
    """Dynamic job list from task_queue table with stats, charts, and controls."""

    # Full job list derived from cron/jobs.py scheduler registry
    _KNOWN_JOBS: list[tuple[str, str, str, str, str]] = [
        # ── 15-minute tier ──
        ("health-check", "cron_health_check", "System health probe", "every 15 min", "\U0001f3e0"),
        ("queue-monitor", "cron_monitor_task_queue", "Task queue monitor", "every 15 min", "\u2699\ufe0f"),
        # ── Hourly tier ──
        ("sync", "cron_sync", "Cross-agent sync", "every 1 h", "\U0001f504"),
        ("crdt-sync", "cron_crdt_sync", "CRDT merge sync", "every 1 h", "\U0001f504"),
        ("auto-retry", "cron_auto_retry_dead_tasks", "Retry dead tasks", "every 1 h", "\U0001f504"),
        ("policy-hash", "cron_policy_hash_status", "Policy alignment check", "every 1 h", "\U0001f512"),
        # ── Daily tier ──
        ("heartbeat", "cron_heartbeat", "Daemon liveness + tier rebalance", "daily", "\U0001fa78"),
        ("digest", "cron_daily_digest", "Daily session digest", "daily", "\U0001f4f0"),
        ("purge-saves", "cron_purge_auto_saves", "Purge stale auto-saves", "daily", "\U0001f5d1\ufe0f"),
        ("cleanup-logs", "cron_cleanup_auto_logs", "Clean old log files", "daily", "\U0001f5d1\ufe0f"),
        ("backfill", "cron_backfill_all", "KG + FTS + embedding backfill", "daily", "\U0001f504"),
        ("backup", "cron_backup", "Database backup", "daily", "\U0001f4be"),
        ("backup-validate", "cron_backup_validate", "Validate backup integrity", "daily", "\u2705"),
        ("emb-recompute", "cron_embedding_recompute", "Embedding refresh", "daily", "\U0001f9e0"),
        ("fts-rebuild", "cron_rebuild_fts", "FTS5 index rebuild", "daily", "\U0001f4d1"),
        ("vec-drift", "cron_detect_vec_drift", "Vector index drift detection", "daily", "\U0001f4d0"),
        ("config-drift", "cron_check_config_drift", "Config drift detection", "daily", "\U0001f527"),
        ("contradictions", "cron_resolve_contradictions", "Resolve KG contradictions", "daily", "\U0001f91d"),
        ("auto-share", "cron_auto_share", "Auto-share high-value memories", "daily", "\U0001f4e4"),
        ("kg-monitor", "cron_kg_backfill_monitor", "KG backfill health monitor", "daily", "\U0001f4ca"),
        ("revalidate", "cron_revalidate_entailments", "Revalidate entailment chains", "daily", "\U0001f504"),
        # ── Weekly tier ──
        ("integrity", "cron_integrity_check", "DB integrity + FTS5 deep check", "weekly", "\U0001f50d"),
        ("log-retention", "cron_log_retention", "Archive old logs", "weekly", "\U0001f4da"),
        ("tier-migration", "cron_tier_migration", "Memory tier promotion/demotion", "weekly", "\U0001f4c8"),
        ("kg-backfill", "cron_kg_backfill", "Full KG backfill", "weekly", "\U0001f504"),
        ("consolidate", "cron_consolidate", "Memory consolidation", "weekly", "\U0001f4c6"),
        ("rewrite-links", "cron_rewrite_links", "Rewrite backlinks", "weekly", "\U0001f517"),
        ("pinned-decay", "cron_pinned_decay", "Pinned memory decay", "weekly", "\U0001f4cc"),
        ("concept-drift", "cron_concept_drift", "Concept drift detection", "weekly", "\U0001f4d0"),
        ("forget-model", "cron_train_forget_model", "Train neural forget model", "weekly", "\U0001f9e0"),
        ("temporal-ssm", "cron_train_temporal_ssm", "Train temporal SSM", "weekly", "\U0001f9e0"),
        ("clusters", "cron_semantic_clusters", "Semantic cluster analysis", "weekly", "\U0001f4ca"),
        ("skill-decay", "cron_skill_decay", "Skill relevance decay", "weekly", "\U0001f9e0"),
        ("skill-extract", "cron_skill_extraction", "Extract reusable skills", "weekly", "\U0001f4d1"),
        ("cross-session", "cron_cross_session_learn", "Cross-session learning", "weekly", "\U0001f4da"),
        ("quality-filter", "cron_quality_filter", "Memory quality filter", "weekly", "\u2705"),
        ("auto-summarize", "cron_auto_summarize", "Auto-summarize long notes", "weekly", "\U0001f4dd"),
        ("retention-stats", "cron_retention_stats", "Retention statistics", "weekly", "\U0001f4ca"),
        ("train-ltr", "cron_train_ltr", "Train LTR ranking model", "weekly", "\U0001f9e0"),
        # ── Monthly tier ──
        ("purge-expired", "cron_purge_expired", "Purge expired memories", "monthly", "\U0001f5d1\ufe0f"),
    ]

    _INTERVAL_S: dict[str, int | None] = {
        "every 15 min": 900,
        "every 1 h": 3600,
        "daily": 86400,
        "weekly": 604800,
        "monthly": 2592000,
    }

    # Also discover any extra task_types not in the known list
    _known_task_types = {jt for _, jt, _, _, _ in _KNOWN_JOBS}
    _extra_df = _cached_extra_jobs(tuple(_known_task_types))
    extra_jobs: list[tuple[str, str, str, str, str]] = []
    if _extra_df is not None and not _extra_df.empty:
        for _, row in _extra_df.iterrows():
            jt = row["task_type"]
            extra_jobs.append((jt, jt, "Auto-discovered task", "dynamic", "\U0001f4e6"))

    all_jobs = _KNOWN_JOBS + extra_jobs

    def _get_task_status(_client, task_type: str) -> dict:
        try:
            r = _client.query(
                "SELECT status, completed_at, error, attempts "
                "FROM task_queue WHERE task_type = ? ORDER BY id DESC LIMIT 1",
                [task_type],
            )
            rows = r.get("results", [])
            if not rows:
                return {"status": "unknown", "last_run": None, "error": None, "attempts": 0}
            row = rows[0]
            return {
                "status": row.get("status") or "unknown",
                "last_run": row.get("completed_at"),
                "error": row.get("error"),
                "attempts": row.get("attempts") or 0,
            }
        except Exception:
            return {"status": "unknown", "last_run": None, "error": None, "attempts": 0}

    def _get_pending_count(_client, task_type: str) -> int:
        try:
            r = _client.query(
                "SELECT COUNT(*) as cnt FROM task_queue WHERE task_type = ? AND status = 'pending'",
                [task_type],
            )
            rows = r.get("results", [])
            return rows[0].get("cnt", 0) if rows else 0
        except Exception:
            return 0

    _client = _api()
    status_counts: Counter[str] = Counter()
    job_data: list[dict] = []
    now = datetime.now(timezone.utc).timestamp()

    for name, task_type, desc, trigger, emoji in all_jobs:
        if _client:
            info = _get_task_status(_client, task_type)
            pending = _get_pending_count(_client, task_type)
        else:
            info = {"status": "unknown", "last_run": None, "error": None, "attempts": 0}
            pending = 0

        ts = info["status"]
        last_run = info["last_run"]
        error = info["error"]

        if ts == "completed":
            if last_run:
                try:
                    comp_dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                    age_s = now - comp_dt.timestamp()
                except Exception:
                    age_s = 0
                interval = _INTERVAL_S.get(trigger, 600)
                if interval is None:
                    sev, status_label = "ok", f"completed {age_s / 60:.0f}min ago (event-triggered)"
                elif age_s > 6 * interval:
                    sev, status_label = "warning", f"completed but stale ({age_s / 3600:.0f}h ago)"
                else:
                    sev, status_label = "ok", f"completed {age_s / 60:.0f}min ago"
            else:
                sev, status_label = "ok", "completed"
        elif ts == "failed":
            err_short = (error[:60] + "...") if error and len(error) > 60 else (error or "unknown error")
            sev, status_label = "error", f"failed: {err_short}"
        elif ts == "processing":
            sev, status_label = "warning", "running now"
        elif ts == "pending":
            sev, status_label = "warning", f"queued ({pending} pending)"
        else:
            sev, status_label = "ok", "scheduled (not yet run)"

        status_counts[sev] += 1
        job_data.append({
            "name": name,
            "task_type": task_type,
            "desc": desc,
            "trigger": trigger,
            "emoji": emoji,
            "sev": sev,
            "status": status_label,
            "error": error,
            "pending": pending,
        })

    if cron_conn:
        cron_conn.close()

    n_ok = status_counts.get("ok", 0)
    n_warn = status_counts.get("warning", 0)
    n_err = status_counts.get("error", 0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Jobs", len(all_jobs))
    c2.metric("Healthy", n_ok)
    c3.metric("Warnings", n_warn)
    c4.metric("Errors", n_err)

    health_df = pd.DataFrame(
        {"Status": ["Healthy", "Warnings", "Errors"], "Count": [n_ok, n_warn, n_err]}
    )
    health_df = health_df[health_df["Count"] > 0]
    if not health_df.empty:
        fig = px.pie(
            health_df,
            names="Status",
            values="Count",
            color="Status",
            color_discrete_map={"Healthy": "#10b981", "Warnings": "#f59e0b", "Errors": "#ef4444"},
        )
        fig.update_layout(
            **DARK,
            height=280,
            margin=dict(t=30, b=60, l=10, r=10),
            showlegend=True,
            legend=dict(orientation="h", yanchor="top", y=-0.2, font=dict(size=8)),
        )
        st.plotly_chart(fig, width="stretch")

    st.divider()

    job_table_df = pd.DataFrame([
        {
            "Job": f"{j['emoji']} {j['name']}",
            "Description": j["desc"],
            "Schedule": j["trigger"],
            "Status": j["status"],
            "Pending": j["pending"],
        }
        for j in job_data
    ])
    st.dataframe(job_table_df, width="stretch", hide_index=True)

    st.divider()

    for job in job_data:
        with st.expander(f"{job['emoji']} {job['name']} \u2014 {job['status']}", expanded=False):
            st.caption(f"{job['desc']} \u00b7 Schedule: `{job['trigger']}`")

            if job["error"] and job["sev"] == "error":
                st.error(f"Last error:\n```\n{job['error'][:500]}\n```")

            col_trig, col_info = st.columns([1, 3])
            with col_trig:
                _drain_key = f"drain_{job['name']}"
                _drain_on = st.session_state.get(_drain_key, False)
                st.checkbox("Drain immediately", key=_drain_key, value=_drain_on)
                if st.button("\u25b6\ufe0f Run now", key=f"run_{job['name']}", type="primary"):
                    try:
                        from infra.db_write_queue import sqlite_write_queue
                        from background.background_queue import init_task_queue, enqueue_task as _enqueue

                        _conn = sqlite_write_queue.start_session(dashboard.MEM_DIR / "memory.db")
                        _task_id = None
                        try:
                            init_task_queue(_conn)
                            _task_id = _enqueue(_conn, job["task_type"], payload={"source": "dashboard"})
                            if isinstance(_task_id, dict):
                                st.warning(f"Rejected: {_task_id.get('reason', '?')}")
                            else:
                                msg = f"Enqueued (id={_task_id})."
                                if st.session_state.get(_drain_key, False):
                                    st.info(f"{msg} Draining worker...")
                                else:
                                    st.success(f"{msg} Worker will process it.")
                        finally:
                            _conn.close()
                        if not isinstance(_task_id, dict) and st.session_state.get(_drain_key, False):
                            _worker_script = str(dashboard.MEM_DIR.parent / "background_worker.py")
                            if not os.path.isfile(_worker_script):
                                _worker_script = str(ROOT / "background_worker.py")
                            _env = os.environ.copy()
                            _env.setdefault("MEMORY_DB_PATH", str(dashboard.MEM_DIR / "memory.db"))
                            _result = subprocess.run(
                                [sys.executable, _worker_script, "--once", "--task-type", job["task_type"]],
                                capture_output=True,
                                text=True,
                                timeout=120,
                                env=_env,
                            )
                            if _result.returncode == 0:
                                _out = _result.stdout.strip()
                                st.success(f"Drained task {_task_id}. Output:\n```\n{_out[:300]}\n```")
                            else:
                                _err = (_result.stdout + "\n" + _result.stderr).strip()[:500]
                                st.error(f"Drain failed:\n```\n{_err}\n```")
                    except subprocess.TimeoutExpired:
                        st.warning("Worker drain timed out (120s). The task may still complete.")
                    except Exception as _e:
                        st.error(f"Failed: {_e}")

            with col_info:
                if job["pending"] > 0:
                    st.caption(f"{job['pending']} tasks pending in queue")


# ── 2. Backups ────────────────────────────────────────────────────────────


def _render_backups() -> None:
    """Backup management with stats, timeline, per-backup cards, and retention."""
    backup_dir = dashboard.MEM_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backups: list[Path] = sorted(
        [p for p in backup_dir.glob("*") if p.suffix in (".db", ".gz", ".db.gz") and not p.name.startswith(".")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if backups:
        total_size = sum(bp.stat().st_size for bp in backups)
        oldest = datetime.fromtimestamp(backups[-1].stat().st_mtime, tz=timezone.utc) if backups else None
        newest = datetime.fromtimestamp(backups[0].stat().st_mtime, tz=timezone.utc) if backups else None

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Backups", len(backups))
        c2.metric("Total Size", f"{total_size / 1024 / 1024:.1f} MB")
        c3.metric("Newest", newest.strftime("%Y-%m-%d") if newest else "\u2014")
        c4.metric("Oldest", oldest.strftime("%Y-%m-%d") if oldest else "\u2014")

        st.divider()

        bp_data = []
        bp: Path
        for bp in backups:
            bp_mtime = datetime.fromtimestamp(bp.stat().st_mtime, tz=timezone.utc)
            bp_data.append({
                "name": bp.name,
                "size_mb": bp.stat().st_size / 1024 / 1024,
                "date": bp_mtime,
            })
        bp_df = pd.DataFrame(bp_data)

        fig_bp = px.bar(
            bp_df,
            x="date",
            y="size_mb",
            color_discrete_sequence=["#6366f1"],
            hover_data=["name"],
            text_auto=".1f",
        )
        fig_bp.update_layout(
            **DARK,
            height=200,
            margin=dict(t=30, b=10, l=10, r=10),
            xaxis_title=None,
            yaxis_title="Size (MB)",
        )
        st.plotly_chart(fig_bp, width="stretch")

        def _validate_backup(path: Path) -> bool:
            for opener in (gzip.open, open):
                try:
                    inner = opener(path, "rb")
                    sig = inner.read(16)
                    inner.close()
                    return sig == b"SQLite format 3\x00" or sig == b""
                except Exception:
                    continue
            return False

        for bp in backups:
            bp_mtime = datetime.fromtimestamp(bp.stat().st_mtime, tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - bp_mtime).days
            valid = _validate_backup(bp)
            valid_badge = "\u2705 OK" if valid else "\u274c Corrupt"
            valid_color = "#10b981" if valid else "#ef4444"
            restore_key = f"restore_op_{bp.name}"

            st.html(
                f"<div style='display:flex;align-items:center;gap:10px;background:#1a1d23;"
                f"border:1px solid #2d3139;border-radius:8px;padding:8px 12px;margin:3px 0;'>"
                f"<span style='flex:2;color:#d1d5db;font-size:0.78rem;'>{bp.name}</span>"
                f"<span style='color:#6b7280;font-size:0.7rem;'>{bp.stat().st_size / 1024 / 1024:.1f} MB</span>"
                f"<span style='color:#6b7280;font-size:0.7rem;'>{bp_mtime.strftime('%Y-%m-%d')} ({age_days}d)</span>"
                f"<span style='background:{valid_color}22;color:{valid_color};padding:0.1rem 0.4rem;"
                f"border-radius:999px;font-size:0.65rem;font-weight:600;'>{valid_badge}</span>"
                f"</div>",
            )
            col_r1, col_r2 = st.columns([1, 8])
            with col_r1:
                if st.button("\U0001f504 Restore", key=restore_key):
                    pre_name = f"pre-restore-{date.today().isoformat()}.db.gz"
                    pre_path = backup_dir / pre_name
                    with open(dashboard.DB, "rb") as fin, gzip.open(pre_path, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    with gzip.open(bp, "rb") as gfin, open(dashboard.DB, "wb") as gfout:
                        shutil.copyfileobj(gfin, gfout)
                    st.success(f"Restored from {bp.name}. Pre-restore backup saved.")
                    st.rerun()
    else:
        st.info("No backups yet")

    st.divider()

    st.markdown("#### Create Backup")
    col_create, col_manage = st.columns([1, 1])

    with col_create:
        if st.button("\U0001f4e4 Create Backup Now", width="stretch", type="primary"):
            backup_name = f"memory-{date.today().isoformat()}-{datetime.now().strftime('%H%M')}.db.gz"
            backup_path = backup_dir / backup_name
            with open(dashboard.DB, "rb") as fin, gzip.open(backup_path, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            st.success(f"Created {backup_name} ({backup_path.stat().st_size / 1024:.0f} KB)")
            st.rerun()

    with col_manage:
        if backups:
            restore_name = st.selectbox(
                "Select backup to restore",
                [bp.name for bp in backups],
                key="restore_select_op",
            )
            if st.button("Restore this backup", type="primary"):
                st.warning(
                    "This will replace the active database with backup contents. "
                    "A pre-restore snapshot will be saved."
                )
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Cancel", key=f"cancel_restore_op_{restore_name}"):
                        st.rerun()
                with col2:
                    if st.button("Confirm Restore", key=f"confirm_restore_op_{restore_name}"):
                        restore_path = backup_dir / restore_name
                        if restore_path.exists():
                            pre_restore_name = f"pre-restore-{date.today().isoformat()}.db.gz"
                            pre_restore_path = backup_dir / pre_restore_name
                            with open(dashboard.DB, "rb") as fin, gzip.open(pre_restore_path, "wb") as fout:
                                shutil.copyfileobj(fin, fout)
                            with gzip.open(restore_path, "rb") as _fin, open(dashboard.DB, "wb") as fout2:
                                shutil.copyfileobj(_fin, fout2)  # type: ignore[arg-type]
                            st.success(
                                f"Restored from {restore_name}. "
                                f"Pre-restore backup saved as {pre_restore_name}"
                            )
                            st.rerun()

    if backups and len(backups) > 7:
        st.divider()
        st.markdown("#### Backup Retention")
        st.caption(f"You have {len(backups)} backups. Consider keeping only the last 7.")
        if st.button("\U0001f5d1\ufe0f Clean Old Backups (keep last 7)", width="stretch"):
            for bp in backups[7:]:
                bp.unlink()
            st.success(f"Deleted {len(backups) - 7} old backups")
            st.rerun()


# ── 3. Multi-Agent Sync ──────────────────────────────────────────────────


def _render_multi_agent() -> None:
    """Shared memories, sync peer config, sync log, direction and success charts."""
    try:
        from infra._lazy_imports import get_config as _cfg
    except ImportError:
        _cfg = None

    st.subheader("Multi-Agent Sync")

    shared_total = _try_count_api("shared_memories") if _table_exists_api("shared_memories") else 0
    _peers = []
    _crdt_enabled = False
    if _cfg is not None:
        try:
            _c = _cfg()
            _peers = list(_c.sync_peers) if _c.sync_enable_server else []
            _crdt_enabled = _c.crdt_enabled
        except Exception:
            pass

    partner_shared = 0
    partner_rows: list[tuple] = []
    _partner_db = None
    try:
        _partner_db = dashboard.DB.parent / dashboard.DB.name.replace("memory.db", "memory-agent-b.db")
        if _partner_db.exists():
            _pc = sqlite3.connect(f"file:{_partner_db}?mode=ro", uri=True)
            if _pc.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='shared_memories'"
            ).fetchone():
                partner_shared = _pc.execute("SELECT COUNT(*) FROM shared_memories").fetchone()[0]
                partner_rows = _pc.execute(
                    "SELECT source_note_id, agent_id, category, shared_with, "
                    "datetime(shared_at, 'unixepoch') as shared "
                    "FROM shared_memories ORDER BY shared_at DESC"
                ).fetchall()
            _pc.close()
    except Exception as _e:
        logger.warning("partner shared pool read failed: %s", _e)

    cols = st.columns(3)
    cols[0].metric("Shared memories", shared_total + partner_shared)
    cols[1].metric("Sync peers (config)", len(_peers))
    cols[2].metric("CRDT enabled", "Yes" if _crdt_enabled else "No")
    if len(_peers) == 0 and shared_total == 0 and partner_shared == 0:
        st.caption(
            "Single-agent install \u2014 no sync peers configured. "
            "Add peers under `[sync]` in `memory.toml` to enable cross-agent sharing."
        )

    st.divider()

    _sync_sql = (
        "SELECT id, peer_name, peer_agent_id, direction, started_at, "
        "completed_at, success, changes_pushed, changes_pulled, "
        "error_message, duration_ms FROM sync_log"
    )
    df = _query_api(_sync_sql + " ORDER BY started_at DESC LIMIT 200")

    _partner_sync = None
    try:
        if _partner_db is not None and _partner_db.exists():
            _psc = sqlite3.connect(f"file:{_partner_db}?mode=ro", uri=True)
            if _psc.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_log'"
            ).fetchone():
                _partner_sync = pd.read_sql_query(
                    _sync_sql + " ORDER BY started_at DESC LIMIT 200", _psc
                )
            _psc.close()
    except Exception as _e:
        logger.warning("partner sync_log read failed: %s", _e)

    if _partner_sync is not None and not _partner_sync.empty:
        df = (
            pd.concat([df, _partner_sync], ignore_index=True)
            if df is not None and not df.empty
            else _partner_sync
        )

    if df is not None and not df.empty:
        st.markdown("#### Recent sync cycles")
        df["started_at"] = pd.to_datetime(df["started_at"], unit="s")
        df["completed_at"] = pd.to_datetime(df["completed_at"], unit="s", errors="coerce")
        df["status"] = df["success"].apply(lambda s: "\u2705" if s else "\u274c")
        df["changes"] = df["changes_pushed"].fillna(0) + df["changes_pulled"].fillna(0)
        display = df[
            [
                "id", "peer_name", "peer_agent_id", "direction",
                "status", "started_at", "completed_at",
                "changes_pushed", "changes_pulled", "duration_ms",
            ]
        ].copy()
        display.columns = [
            "ID", "Peer", "Agent", "Dir",
            "Status", "Started", "Completed",
            "Pushed", "Pulled", "Duration ms",
        ]
        st.dataframe(display, width="stretch", hide_index=True)

        st.divider()
        st.markdown("#### Direction breakdown (last 200 cycles)")
        dir_counts = df["direction"].value_counts().reset_index()
        dir_counts.columns = ["Direction", "Count"]
        fig = px.bar(
            dir_counts,
            x="Direction",
            y="Count",
            color="Direction",
            color_discrete_sequence=["#3b82f6", "#10b981", "#f59e0b"],
        )
        fig.update_layout(**DARK, margin=dict(t=30, b=10, l=10, r=10), showlegend=False, height=300)
        st.plotly_chart(fig, width="stretch")

        st.divider()
        st.markdown("#### Peer success rate (last 200 cycles)")
        peer_status = df.groupby("peer_name")["success"].agg(["mean", "count"]).reset_index()
        peer_status.columns = ["Peer", "Success rate", "Cycles"]
        peer_status["Success rate"] = (peer_status["Success rate"] * 100).round(1)
        peer_status = peer_status.sort_values("Success rate", ascending=True)
        fig2 = px.bar(
            peer_status,
            x="Success rate",
            y="Peer",
            color="Success rate",
            color_continuous_scale="RdYlGn",
            range_color=[0, 100],
            orientation="h",
        )
        fig2.update_layout(
            **DARK,
            margin=dict(t=30, b=10, l=10, r=10),
            height=max(200, len(peer_status) * 40),
        )
        st.plotly_chart(fig2, width="stretch")
    else:
        st.info(
            "No sync cycles recorded. Configure `[[sync.peers]]` in `memory.toml` to get started."
        )

    st.divider()
    st.markdown("#### Shared memory pool")
    if _table_exists_api("shared_memories") or partner_rows:
        local_rows = []
        if _table_exists_api("shared_memories"):
            _lr = _query_api(
                "SELECT source_note_id, agent_id, category, shared_with, "
                "datetime(shared_at, 'unixepoch') as shared "
                "FROM shared_memories ORDER BY shared_at DESC"
            )
            if _lr is not None and not _lr.empty:
                local_rows = _lr.to_dict("records")
        combined = list(local_rows) + [
            dict(zip(["source_note_id", "agent_id", "category", "shared_with", "shared"], r))
            for r in partner_rows
        ]
        if combined:
            df_shared = pd.DataFrame(combined).head(50)
            st.caption(
                f"Showing {len(df_shared)} of {len(combined)} shared entries "
                f"({len(local_rows)} local + {len(partner_rows)} from partner agent)"
            )
            st.dataframe(df_shared, width="stretch", hide_index=True)
        else:
            st.info(
                "Shared pool is empty. "
                "Call `memory_maintenance(operation='share', share_note_id=...)` to publish."
            )
    else:
        st.info("Table `shared_memories` not yet created.")


# ── 4. Runbook ────────────────────────────────────────────────────────────


def _render_runbook() -> None:
    """Guided operational workflows with status indicators and actionable buttons."""
    st.subheader("Operational Runbook")

    # ── Section 1: System not healthy? ─────────────────────────────────────
    with st.expander("\U0001f6e1\ufe0f System not healthy?", expanded=True):
        st.caption("Ordered checklist — diagnose and fix common system issues.")

        # 1. Check DB
        _db_ok = False
        _db_detail = ""
        try:
            _c = _api()
            if _c:
                _c.health()
                _db_ok = True
                _db_detail = "DB accessible"
            else:
                get_conn().execute("SELECT 1")
                _db_ok = True
                _db_detail = "DB accessible"
        except Exception as e:
            _db_detail = f"DB error: {str(e)[:80]}"

        _icon_db = "\u2705" if _db_ok else "\u274c"
        _color_db = "#10b981" if _db_ok else "#ef4444"
        st.html(
            f"<div class='health-check'>"
            f"<span class='hc-icon'>{_icon_db}</span>"
            f"<span class='hc-name'>1. Check Database</span>"
            f"<span class='hc-detail' style='color:{_color_db};'>{_db_detail}</span>"
            f"</div>"
        )
        if not _db_ok:
            if st.button("Run integrity check", key="rb_integrity"):
                try:
                    _c = _api()
                    if _c:
                        report = _c.integrity_check()
                        if report.get("success"):
                            st.toast("Integrity check passed", icon="\u2705")
                        else:
                            errors = report.get("errors", [])
                            st.error(f"Integrity check: {errors}")
                    else:
                        st.error("API client not available")
                except Exception as e:
                    st.error(f"Failed: {e}")

        # 2. Check worker
        _worker_ok = False
        _worker_detail = ""
        heartbeat_log = dashboard.MEM_DIR / "heartbeat.log" if dashboard.MEM_DIR else None
        if heartbeat_log and heartbeat_log.exists():
            import time

            age_s = time.time() - heartbeat_log.stat().st_mtime
            if age_s < 120:
                _worker_ok = True
                _worker_detail = f"Heartbeat {age_s:.0f}s ago"
            elif age_s < 600:
                _worker_detail = f"Heartbeat {age_s:.0f}s ago (stale)"
            else:
                _worker_detail = f"Heartbeat {age_s:.0f}s ago (down?)"
        else:
            _worker_detail = "No heartbeat log found"

        _icon_worker = "\u2705" if _worker_ok else ("\u26a0\ufe0f" if _worker_detail != "No heartbeat log found" else "\u2753")
        _color_worker = "#10b981" if _worker_ok else "#f59e0b"
        st.html(
            f"<div class='health-check'>"
            f"<span class='hc-icon'>{_icon_worker}</span>"
            f"<span class='hc-name'>2. Check Worker</span>"
            f"<span class='hc-detail' style='color:{_color_worker};'>{_worker_detail}</span>"
            f"</div>"
        )

        # 3. Check cron
        _cron_err = 0
        _cron_detail = ""
        try:
            _cron_err = _try_count_api("task_queue", "status='failed'")
            _cron_pending = _try_count_api("task_queue", "status='pending'")
            if _cron_err > 0:
                _cron_detail = f"{_cron_err} failed, {_cron_pending} pending"
            else:
                _cron_detail = f"{_cron_pending} pending, 0 failed"
        except Exception:
            _cron_detail = "No task_queue table"

        _icon_cron = "\u2705" if _cron_err == 0 else "\u26a0\ufe0f"
        _color_cron = "#10b981" if _cron_err == 0 else "#f59e0b"
        st.html(
            f"<div class='health-check'>"
            f"<span class='hc-icon'>{_icon_cron}</span>"
            f"<span class='hc-name'>3. Check Cron Jobs</span>"
            f"<span class='hc-detail' style='color:{_color_cron};'>{_cron_detail}</span>"
            f"</div>"
        )

        # 4. Check disk
        _disk_ok = False
        _disk_detail = ""
        free_gb = 0.0
        try:
            usage = shutil.disk_usage(str(dashboard.DB.parent if dashboard.DB else "."))
            free_gb = usage.free / (1024**3)
            if free_gb < 1:
                _disk_detail = f"{free_gb:.1f} GB free (critical)"
            elif free_gb < 5:
                _disk_detail = f"{free_gb:.1f} GB free (low)"
            else:
                _disk_ok = True
                _disk_detail = f"{free_gb:.1f} GB free"
        except Exception:
            _disk_detail = "Cannot determine"

        _icon_disk = "\u2705" if _disk_ok else "\u274c" if free_gb < 1 else "\u26a0\ufe0f"
        _color_disk = "#10b981" if _disk_ok else "#ef4444" if free_gb < 1 else "#f59e0b"
        st.html(
            f"<div class='health-check'>"
            f"<span class='hc-icon'>{_icon_disk}</span>"
            f"<span class='hc-name'>4. Check Disk Space</span>"
            f"<span class='hc-detail' style='color:{_color_disk};'>{_disk_detail}</span>"
            f"</div>"
        )

    # ── Section 2: Background worker down? ─────────────────────────────────
    with st.expander("\u2699\ufe0f Background worker down?", expanded=False):
        try:
            _c = _api()
            if _c:
                res = _c.query(
                    "SELECT completed_at FROM task_queue "
                    "WHERE status='completed' AND completed_at IS NOT NULL "
                    "ORDER BY completed_at DESC LIMIT 1"
                )
                last_task = res.get("results", [{}])[0] if res.get("results") else None
                last_task = (last_task.get("completed_at"),) if last_task else None
            else:
                last_task = get_conn().execute(
                    "SELECT completed_at FROM task_queue "
                    "WHERE status='completed' AND completed_at IS NOT NULL "
                    "ORDER BY completed_at DESC LIMIT 1"
                ).fetchone()
            if last_task and last_task[0]:
                comp_dt = datetime.strptime(last_task[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - comp_dt).total_seconds()
                if age_s < 600:
                    st.html(
                        "<div style='background:#064e3b;border:1px solid #10b981;border-radius:8px;"
                        "padding:10px;color:#6ee7b7;font-size:0.8rem;'>"
                        "\u2705 Worker is alive (last task {:.0f}min ago)".format(age_s / 60)
                        + "</div>"
                    )
                else:
                    st.html(
                        "<div style='background:#5c3d00;border:1px solid #f59e0b;border-radius:8px;"
                        "padding:10px;color:#fbbf24;font-size:0.8rem;'>"
                        "\u26a0\ufe0f Worker idle ({:.0f}min since last task)".format(age_s / 60)
                        + "</div>"
                    )
            else:
                st.info("No completed tasks yet — worker may not have started.")
        except Exception:
            st.info("Cannot check worker status.")

        st.markdown("**Restart command:**")
        st.code(
            "cd {root}\n"
            "nohup venv/bin/python background_worker.py >> memory/worker.log 2>&1 &\n"
            "tail -f memory/worker.log".format(root=str(ROOT)),
            language="bash",
        )

        if st.button("View worker.log (last 30 lines)", key="rb_worker_log"):
            worker_log = dashboard.MEM_DIR / "worker.log" if dashboard.MEM_DIR else None
            if worker_log and worker_log.exists():
                try:
                    lines = worker_log.read_text().strip().split("\n")[-30:]
                    st.code("\n".join(lines), language="text")
                except Exception:
                    st.info("Cannot read worker.log")
            else:
                st.info("No worker.log found")

    # ── Section 3: Cron job failing? ───────────────────────────────────────
    with st.expander("\u274c Cron job failing?", expanded=False):
        failed_df = _query_api(
            "SELECT task_type, id, error, attempts, created_at, completed_at "
            "FROM task_queue WHERE status = 'failed' "
            "ORDER BY completed_at DESC LIMIT 20"
        )
        if failed_df is not None and not failed_df.empty:
            st.caption(f"{len(failed_df)} failed job(s)")
            for _, row in failed_df.iterrows():
                err_preview = str(row.get("error", ""))[:200]
                st.html(
                    f"<div style='background:#4c0519;border:1px solid #ef4444;border-radius:8px;"
                    f"padding:8px 12px;margin:4px 0;'>"
                    f"<div style='color:#fca5a5;font-weight:600;font-size:0.78rem;'>"
                    f"{row['task_type']}</div>"
                    f"<div style='color:#f87171;font-size:0.72rem;margin-top:2px;'>"
                    f"{err_preview}</div>"
                    f"</div>"
                )
                if st.button(
                    f"Retry {row['task_type']}",
                    key=f"rb_retry_{row['id']}",
                    type="secondary",
                ):
                    try:
                        from infra.db_write_queue import sqlite_write_queue
                        from background.background_queue import init_task_queue, enqueue_task as _enqueue

                        _conn = sqlite_write_queue.start_session(dashboard.MEM_DIR / "memory.db")
                        try:
                            init_task_queue(_conn)
                            _tid = _enqueue(_conn, row["task_type"], payload={"source": "runbook"})
                            if isinstance(_tid, dict):
                                st.warning(f"Rejected: {_tid.get('reason', '?')}")
                            else:
                                st.success(f"Re-enqueued (id={_tid})")
                        finally:
                            _conn.close()
                    except Exception as _e:
                        st.error(f"Failed: {_e}")
        else:
            st.html(
                "<div style='background:#064e3b;border:1px solid #10b981;border-radius:8px;"
                "padding:10px;color:#6ee7b7;font-size:0.8rem;'>"
                "\u2705 No failed cron jobs"
                + "</div>"
            )

    # ── Section 4: Need to migrate? ────────────────────────────────────────
    with st.expander("\U0001f504 Need to migrate?", expanded=False):
        current_ver = 0
        try:
            _c = _api()
            if _c:
                res = _c.query("SELECT version FROM schema_version WHERE id=1")
                if res.get("results"):
                    current_ver = res["results"][0].get("version", 0)
            else:
                r = get_conn().execute("SELECT version FROM schema_version WHERE id=1").fetchone()
                if r:
                    current_ver = r[0]
        except Exception:
            pass

        migration_dir = ROOT / "migrations"
        latest_migration = 0
        pending: list[str] = []
        if migration_dir.exists():
            migration_files = sorted(migration_dir.glob("*.sql"))
            migration_numbers: list[tuple[int, str]] = []
            for f in migration_files:
                if f.name.endswith(".down.sql"):
                    continue
                try:
                    num = int(f.name.split("_")[0])
                    migration_numbers.append((num, f.name))
                except ValueError:
                    continue
            if migration_numbers:
                latest_migration = max(n for n, _ in migration_numbers)
                pending = [
                    name
                    for num, name in migration_numbers
                    if num > current_ver
                ]

        c1, c2, c3 = st.columns(3)
        c1.metric("Current Schema", f"v{current_ver}")
        c2.metric("Latest Migration", f"v{latest_migration}")
        c3.metric("Pending", len(pending))

        if current_ver == 0 and latest_migration == 0:
            st.info("No migrations detected.")
        elif pending:
            st.html(
                "<div style='background:#5c3d00;border:1px solid #f59e0b;border-radius:8px;"
                "padding:10px;color:#fbbf24;font-size:0.8rem;'>"
                f"\u26a0\ufe0f {len(pending)} pending migration(s): "
                + ", ".join(pending[:5])
                + ("..." if len(pending) > 5 else "")
                + "</div>"
            )
            st.markdown("**Run migration:**")
            st.code(
                f"cd {ROOT}\n"
                f"venv/bin/python -m infra.migration_runner --target {latest_migration}",
                language="bash",
            )
        else:
            st.html(
                "<div style='background:#064e3b;border:1px solid #10b981;border-radius:8px;"
                "padding:10px;color:#6ee7b7;font-size:0.8rem;'>"
                "\u2705 Schema is up to date"
                + "</div>"
            )

        with st.expander("Migration files", expanded=False):
            if migration_dir.exists():
                migs = sorted(migration_dir.glob("*.sql"))
                mig_list = []
                for f in migs:
                    if f.name.endswith(".down.sql"):
                        continue
                    try:
                        num = int(f.name.split("_")[0])
                        status = "applied" if num <= current_ver else "pending"
                        mig_list.append({"file": f.name, "version": f"v{num}", "status": status})
                    except ValueError:
                        continue
                if mig_list:
                    mig_df = pd.DataFrame(mig_list)
                    st.dataframe(mig_df, width="stretch", hide_index=True)
                else:
                    st.info("No migration files found")
            else:
                st.info("Migrations directory not found")
