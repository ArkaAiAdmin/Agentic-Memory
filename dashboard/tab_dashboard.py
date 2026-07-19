#!/usr/bin/env python3
"""Dashboard tab — Overview + Health + Activity Feed + Command Palette."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import dashboard
from dashboard import (
    DARK, _auto_refresh, _compute_health_score, _fmt_date, _get_schema_version,
    _run_health_checks, get_conn, query, try_count,
)
from dashboard.api_client import _api, _query_api

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def _render_command_palette():
    """Render the quick-action command palette."""
    st.html("<div style='height:8px;'></div>")
    st.html(
        "<div style='color:#8b8fa3;font-size:0.7rem;text-transform:uppercase;"
        "letter-spacing:0.06em;font-weight:600;margin-bottom:6px;'>Quick Actions</div>"
    )

    cols = st.columns(6)
    actions = [
        ("\U0001f50d Integrity Check", "check_integrity"),
        ("\U0001f4d1 Rebuild FTS", "rebuild_fts"),
        ("\U0001f9e0 Rebuild Embeddings", "rebuild_embeddings"),
        ("\U0001f4be Compact DB", "compact"),
        ("\U0001f4c2 Create Backup", "backup"),
        ("\U0001f504 Run Backfill", "backfill"),
    ]

    for i, (label, action) in enumerate(actions):
        if cols[i].button(label, key=f"qa_{action}", use_container_width=True):
            _execute_quick_action(action)


def _execute_quick_action(action: str):
    """Execute a quick action from the command palette."""
    db_path = str(dashboard.DB)
    mem_dir = str(dashboard.MEM_DIR)

    if action == "check_integrity":
        with st.spinner("Running integrity check..."):
            try:
                client = _api()
                if client:
                    report = client.integrity_check()
                    if report.get("success"):
                        st.toast("Integrity check passed", icon="\u2705")
                    else:
                        errors = report.get("errors", [])
                        st.error(f"Integrity check failed: {errors}")
                else:
                    st.error("API client unavailable — start the REST server first.")
            except Exception as e:
                st.error(f"Failed: {e}")

    elif action == "rebuild_fts":
        with st.spinner("Rebuilding FTS5 index..."):
            try:
                import subprocess, sys
                result = subprocess.run(
                    [sys.executable, str(ROOT / "cron" / "cron_rebuild_fts.py")],
                    capture_output=True, text=True, timeout=120,
                    env={**__import__("os").environ, "MEMORY_DB_PATH": db_path},
                )
                if result.returncode == 0:
                    st.toast("FTS5 index rebuilt", icon="\u2705")
                else:
                    st.error(f"FTS rebuild failed: {result.stderr[:200]}")
            except Exception as e:
                st.error(f"Failed: {e}")

    elif action == "rebuild_embeddings":
        with st.spinner("Rebuilding embeddings..."):
            try:
                import subprocess, sys
                result = subprocess.run(
                    [sys.executable, str(ROOT / "cron" / "cron_embedding_recompute.py")],
                    capture_output=True, text=True, timeout=300,
                    env={**__import__("os").environ, "MEMORY_DB_PATH": db_path},
                )
                if result.returncode == 0:
                    st.toast("Embeddings rebuilt", icon="\u2705")
                else:
                    st.error(f"Embedding rebuild failed: {result.stderr[:200]}")
            except Exception as e:
                st.error(f"Failed: {e}")

    elif action == "compact":
        with st.spinner("Compacting database..."):
            try:
                client = _api()
                if client:
                    result = client.compact()
                    st.toast("Database compacted", icon="\u2705")
                else:
                    st.error("API client unavailable — start the REST server first.")
            except Exception as e:
                st.error(f"Failed: {e}")

    elif action == "backup":
        import gzip
        from datetime import date
        backup_dir = dashboard.MEM_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = f"memory-{date.today().isoformat()}-{datetime.now().strftime('%H%M')}.db.gz"
        path = backup_dir / name
        with open(dashboard.DB, "rb") as fin, gzip.open(path, "wb") as fout:
            import shutil
            shutil.copyfileobj(fin, fout)
        st.toast(f"Backup created: {name}", icon="\u2705")

    elif action == "backfill":
        with st.spinner("Running backfill..."):
            try:
                import subprocess, sys
                result = subprocess.run(
                    [sys.executable, str(ROOT / "backfill_all.py")],
                    capture_output=True, text=True, timeout=600,
                    env={**__import__("os").environ, "MEMORY_DB_PATH": db_path},
                )
                if result.returncode == 0:
                    st.toast("Backfill complete", icon="\u2705")
                else:
                    st.error(f"Backfill failed: {result.stderr[:200]}")
            except Exception as e:
                st.error(f"Failed: {e}")


def _render_health_panel():
    """Render the health check panel with subsystem status and fix buttons."""
    checks = _run_health_checks()
    score, label = _compute_health_score(checks)
    color = "#10b981" if score >= 80 else "#f59e0b" if score >= 60 else "#ef4444"
    emoji = "\U0001f7e2" if score >= 80 else "\U0001f7e1" if score >= 60 else "\U0001f534"

    st.html(
        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:12px;"
        f"padding:1rem 1.5rem;margin-bottom:1rem;display:flex;align-items:center;justify-content:space-between;'>"
        f"<div style='display:flex;align-items:center;gap:12px;'>"
        f"<div style='width:48px;height:48px;border-radius:50%;background:{color}22;"
        f"display:flex;align-items:center;justify-content:center;font-size:1.5rem;'>{emoji}</div>"
        f"<div><div style='color:#f0f2f6;font-weight:700;font-size:1.1rem;'>System Health: {label}</div>"
        f"<div style='color:#6b7280;font-size:0.75rem;'>{len(checks)} subsystems checked</div></div></div>"
        f"<div style='text-align:right;'><div style='color:{color};font-size:2rem;font-weight:700;'>{score}</div>"
        f"<div style='color:#6b7280;font-size:0.7rem;'>health score</div></div></div>",
    )

    # Render checks in a 2-column tiled grid
    col_a, col_b = st.columns(2)
    for i, c in enumerate(checks):
        col = col_a if i % 2 == 0 else col_b
        icon = {"ok": "\u2705", "warning": "\u26a0\ufe0f", "error": "\u274c", "info": "\u2139\ufe0f"}.get(c["status"], "\u2753")
        c_color = {"ok": "#10b981", "warning": "#f59e0b", "error": "#ef4444", "info": "#6b7280"}.get(c["status"], "#6b7280")

        with col:
            st.html(
                f"<div class='health-check'>"
                f"<span style='font-size:0.85rem;'>{icon}</span>"
                f"<span style='color:#d1d5db;font-size:0.78rem;font-weight:600;margin-left:6px;'>{c['name']}</span>"
                f"<span style='color:{c_color};font-size:0.72rem;margin-left:auto;'>{c['detail']}</span>"
                f"</div>"
            )
            if c.get("fixable") and c.get("fix_label"):
                if st.button(
                    c["fix_label"],
                    key=f"fix_{c['name']}",
                    help=f"Run fix for {c['name']}",
                    use_container_width=True,
                ):
                    _execute_fix(c["name"])


def _execute_fix(check_name: str):
    """Execute a fix action for a health check."""
    db_path = str(dashboard.DB)
    mem_dir = str(dashboard.MEM_DIR)

    if check_name == "FTS5 Index":
        with st.spinner("Rebuilding FTS5 index..."):
            try:
                import subprocess, sys
                result = subprocess.run(
                    [sys.executable, str(ROOT / "cron" / "cron_rebuild_fts.py")],
                    capture_output=True, text=True, timeout=120,
                    env={**__import__("os").environ, "MEMORY_DB_PATH": db_path},
                )
                if result.returncode == 0:
                    st.toast("FTS5 index rebuilt", icon="\u2705")
                    st.rerun()
                else:
                    st.error(f"FTS rebuild failed: {result.stderr[:200]}")
            except Exception as e:
                st.error(f"Failed: {e}")

    elif check_name == "Vec Index":
        with st.spinner("Rebuilding embeddings..."):
            try:
                import subprocess, sys
                result = subprocess.run(
                    [sys.executable, str(ROOT / "cron" / "cron_embedding_recompute.py")],
                    capture_output=True, text=True, timeout=300,
                    env={**__import__("os").environ, "MEMORY_DB_PATH": db_path},
                )
                if result.returncode == 0:
                    st.toast("Embeddings rebuilt", icon="\u2705")
                    st.rerun()
                else:
                    st.error(f"Embedding rebuild failed: {result.stderr[:200]}")
            except Exception as e:
                st.error(f"Failed: {e}")

    elif check_name == "Knowledge Graph":
        with st.spinner("Running backfill..."):
            try:
                import subprocess, sys
                result = subprocess.run(
                    [sys.executable, str(ROOT / "backfill_all.py")],
                    capture_output=True, text=True, timeout=600,
                    env={**__import__("os").environ, "MEMORY_DB_PATH": db_path},
                )
                if result.returncode == 0:
                    st.toast("Backfill complete", icon="\u2705")
                    st.rerun()
                else:
                    st.error(f"Backfill failed: {result.stderr[:200]}")
            except Exception as e:
                st.error(f"Failed: {e}")

    elif check_name == "Cron Jobs":
        st.markdown("**Cron Job Status**")
        try:
            rows = _query_api(
                "SELECT task_type, status, COUNT(*) cnt, "
                "MAX(completed_at) last_run "
                "FROM task_queue GROUP BY task_type, status ORDER BY cnt DESC"
            )
            if rows is not None and not rows.empty:
                for _, row in rows.iterrows():
                    task_type = row["task_type"]
                    status = row["status"]
                    cnt = row["cnt"]
                    last_run = row["last_run"]
                    icon = {"completed": "\u2705", "failed": "\u274c", "pending": "\u23f3"}.get(status, "\u2753")
                    st.html(
                        f"<div style='display:flex;align-items:center;gap:8px;padding:3px 0;'>"
                        f"<span>{icon}</span>"
                        f"<span style='color:#d1d5db;font-size:0.75rem;font-weight:600;'>{task_type}</span>"
                        f"<span style='color:#6b7280;font-size:0.65rem;'>{status}: {cnt}</span>"
                        f"<span style='color:#4b5563;font-size:0.65rem;margin-left:auto;'>{last_run or ''}</span>"
                        f"</div>"
                    )
            else:
                st.info("No tasks in queue")
        except Exception as e:
            st.error(f"Failed: {e}")

    elif check_name == "Database":
        with st.spinner("Running integrity check..."):
            try:
                client = _api()
                if client:
                    report = client.integrity_check()
                    if report.get("success"):
                        st.toast("Integrity check passed", icon="\u2705")
                    else:
                        errors = report.get("errors", [])
                        st.error(f"Integrity check failed: {errors}")
                else:
                    st.error("API client unavailable — start the REST server first.")
            except Exception as e:
                st.error(f"Failed: {e}")


def _render_activity_feed():
    """Render the real-time activity feed from audit_log + recent task completions."""
    # Merge MCP audit log and recent task completions into one feed
    audit_df = query(
        "SELECT ts, tool as name, latency_ms, error, 'mcp' as source "
        "FROM memory_audit_log ORDER BY ts DESC LIMIT 30"
    )

    # Get task completions
    tasks = query(
        "SELECT completed_at, task_type, status, error "
        "FROM task_queue WHERE completed_at IS NOT NULL "
        "ORDER BY completed_at DESC LIMIT 20"
    )

    items = []

    # Add audit log entries
    if audit_df is not None and not audit_df.empty:
        for _, r in audit_df.iterrows():
            ts = int(r["ts"]) if pd.notna(r["ts"]) else 0
            items.append({
                "ts": ts,
                "name": r["name"],
                "source": "MCP",
                "error": r.get("error"),
            })

    # Add task completions (only completed, not failed — show recent history)
    if tasks is not None and not tasks.empty:
        for _, r in tasks.iterrows():
            try:
                ts = int(pd.Timestamp(r["completed_at"]).timestamp())
            except Exception:
                ts = 0
            items.append({
                "ts": ts,
                "name": r["task_type"],
                "source": "Worker",
                "error": None,  # completed tasks are OK regardless of error field
            })

    # Sort by timestamp descending
    items.sort(key=lambda x: x["ts"], reverse=True)

    if not items:
        st.info("No recent activity")
        return

    for item in items[:20]:
        ts_dt = datetime.fromtimestamp(item["ts"], tz=timezone.utc) if item["ts"] else None
        ts_str = ts_dt.strftime("%m-%d %H:%M") if ts_dt else "?"
        status = "\u274c" if item.get("error") else "\u2705"
        source_badge = f"<span style='color:#6b7280;font-size:0.6rem;background:#1f2937;padding:0.1rem 0.3rem;border-radius:4px;margin-left:4px;'>{item['source']}</span>"

        st.html(
            f"<div style='display:flex;align-items:center;gap:8px;padding:4px 0;"
            f"border-bottom:1px solid #1f2937;'>"
            f"<span style='font-size:0.8rem;'>{status}</span>"
            f"<span style='color:#d1d5db;font-size:0.75rem;font-weight:600;'>{item['name']}</span>"
            f"{source_badge}"
            f"<span style='color:#4b5563;font-size:0.65rem;margin-left:auto;'>{ts_str}</span>"
            f"</div>",
        )


def render_dashboard():
    """Main dashboard tab — health, metrics, activity, command palette."""
    st.subheader("Dashboard")

    _auto_refresh()

    # ── Command Palette ──────────────────────────────────────────────────
    _render_command_palette()

    st.html("<div style='height:8px;'></div>")

    # ── Key Metrics ──────────────────────────────────────────────────────
    n_mem = try_count("memories")
    n_pin = try_count("memories", "pinned=1")
    n_emb = try_count("memory_embeddings")
    n_ent = try_count("kg_entities")
    n_facts = try_count("kg_facts")
    n_ops_today = try_count("memory_audit_log", "DATE(ts,'unixepoch') = DATE('now')")
    db_size_mb = dashboard.DB.stat().st_size / 1024 / 1024

    avg_lat = None
    try:
        r = get_conn().execute(
            "SELECT AVG(latency_ms) FROM memory_audit_log WHERE DATE(ts,'unixepoch') = DATE('now')"
        ).fetchone()
        avg_lat = round(r[0], 1) if r and r[0] else None
    except Exception:
        pass

    cols = st.columns(6)
    for i, (label, val, sub) in enumerate([
        ("Total", n_mem, "memory notes"),
        ("Pinned", n_pin, "hot memory"),
        ("Entities", n_ent, "in KG"),
        ("Facts", n_facts, "extracted"),
        ("Ops Today", n_ops_today, "MCP calls"),
        ("Avg Latency", f"{avg_lat or '?'} ms", "today"),
    ]):
        cols[i].html(
            f"<div class='metric-card'>"
            f"<div class='label'>{label}</div>"
            f"<div class='value'>{val}</div>"
            f"<div class='sub'>{sub}</div>"
            f"</div>",
        )

    st.html("<div style='height:8px;'></div>")

    # ── Health Panel ─────────────────────────────────────────────────────
    _render_health_panel()

    st.html("<div style='height:8px;'></div>")

    # ── Charts Row ───────────────────────────────────────────────────────
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### Notes by Category")
        df = query(
            "SELECT COALESCE(category,'uncategorized') cat, COUNT(*) cnt "
            "FROM memories GROUP BY cat ORDER BY cnt DESC"
        )
        if df is not None and not df.empty:
            fig = px.pie(
                df, names="cat", values="cnt",
                color_discrete_sequence=px.colors.sequential.Viridis_r,
            )
            fig.update_layout(**DARK, margin=dict(t=30, b=10, l=10, r=10), showlegend=True, legend=dict(font=dict(size=9)))
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No category data")

    with col2:
        st.markdown("#### Memory Tier Distribution")
        df = query(
            "SELECT COALESCE(tier,'unassigned') tier, COUNT(*) cnt "
            "FROM memories GROUP BY tier ORDER BY cnt DESC"
        )
        if df is not None and not df.empty:
            cmap = {"hot": "#ef4444", "warm": "#f59e0b", "cold": "#3b82f6", "unassigned": "#4b5563"}
            fig = px.bar(df, x="tier", y="cnt", color="tier", color_discrete_map=cmap, text_auto=True)
            fig.update_layout(**DARK, showlegend=False, xaxis_title=None, yaxis_title="Count", margin=dict(t=30, b=10, l=10, r=10))
            fig.update_traces(textposition="outside")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No tier data")

    # ── Daily Note Creation ──────────────────────────────────────────────
    st.markdown("#### Daily Note Creation")
    df = query(
        "SELECT DATE(created_at) day, COUNT(*) cnt "
        "FROM memories GROUP BY day ORDER BY day"
    )
    if df is not None and len(df) > 1:
        df["day"] = pd.to_datetime(df["day"])
        fig = px.line(df, x="day", y="cnt", markers=True, line_shape="spline")
        fig.update_traces(line=dict(width=3, shape="spline", smoothing=1.3), marker=dict(size=6))
        fig.update_layout(**DARK, xaxis_title=None, yaxis_title="Notes", margin=dict(t=30, b=10, l=10, r=10))
        st.plotly_chart(fig, width="stretch")

    # ── Activity Feed ────────────────────────────────────────────────────
    col_act, col_tags = st.columns([1, 1])

    with col_act:
        st.markdown("#### Recent Activity")
        _render_activity_feed()

    with col_tags:
        st.markdown("#### Top Tags")
        df = query("SELECT tags FROM memories WHERE tags != '[]' LIMIT 1000")
        if df is not None and not df.empty:
            c: Counter[str] = Counter()
            for row in df["tags"]:
                try:
                    c.update(json.loads(row))
                except Exception:
                    pass
            if c:
                td = pd.DataFrame(c.most_common(15), columns=["tag", "count"])
                fig = px.bar(td, x="count", y="tag", orientation="h", color="count", color_continuous_scale="Viridis")
                fig.update_layout(**DARK, yaxis=dict(autorange="reversed"), margin=dict(t=30, b=10, l=10, r=10), height=350)
                st.plotly_chart(fig, width="stretch")
            else:
                st.info("No tags found")
        else:
            st.info("No tagged notes")

    # ── Subsystem Summary ────────────────────────────────────────────────
    st.html("<br>")
    s1, s2, s3 = st.columns(3)

    with s1:
        ltr_model = ROOT / "models" / "ltr" / "model.txt"
        ltr_status = "ready" if ltr_model.exists() else "awaiting data"
        ltr_color = "#10b981" if ltr_model.exists() else "#f59e0b"
        n_ctr = try_count("memory_ctr_feedback")
        st.html(
            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px;'>"
            f"<div style='color:#8b8fa3;font-size:0.7rem;text-transform:uppercase;'>LTR Model</div>"
            f"<div style='color:{ltr_color};font-size:1.3rem;font-weight:700;'>{ltr_status}</div>"
            f"<div style='color:#6b7280;font-size:0.7rem;'>{n_ctr} impressions \u00b7 29 features</div>"
            f"</div>",
        )

    with s2:
        n_sync = try_count("sync_log") if table("sync_log") else 0
        sync_status = f"{n_sync} cycles" if n_sync > 0 else "not synced"
        sync_color = "#10b981" if n_sync > 0 else "#6b7280"
        n_alarms = try_count("drift_alarms")
        st.html(
            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px;'>"
            f"<div style='color:#8b8fa3;font-size:0.7rem;text-transform:uppercase;'>Sync Status</div>"
            f"<div style='color:{sync_color};font-size:1.3rem;font-weight:700;'>{sync_status}</div>"
            f"<div style='color:#6b7280;font-size:0.7rem;'>{n_alarms} drift alarms</div>"
            f"</div>",
        )

    with s3:
        schema = _get_schema_version()
        st.html(
            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px;'>"
            f"<div style='color:#8b8fa3;font-size:0.7rem;text-transform:uppercase;'>Schema</div>"
            f"<div style='color:#60a5fa;font-size:1.3rem;font-weight:700;'>{schema}</div>"
            f"<div style='color:#6b7280;font-size:0.7rem;'>DB: {db_size_mb:.0f} MB</div>"
            f"</div>",
        )


def table(name: str) -> bool:
    """Check if table exists (local wrapper for this module)."""
    return dashboard.table(name)
