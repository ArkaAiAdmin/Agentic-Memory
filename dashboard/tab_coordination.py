#!/usr/bin/env python3
"""Coordination tab — Agent status, task lifecycle, messaging, audit timeline."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import dashboard
from dashboard import DARK, get_conn, query, table, try_count
from dashboard.api_client import (
    _api,
    _query_api,
    _try_count_api,
    _table_exists_api,
)

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT

# Timeout thresholds (seconds)
STALE_THRESHOLD = 600  # 10 min — task becomes stale
DEAD_THRESHOLD = 300   # 5 min — agent is dead


def render_coordination():
    """Coordination tab with 4 sub-tabs."""
    t1, t2, t3, t4 = st.tabs([
        "Agents", "Tasks", "Messages", "Audit Log",
    ])
    with t1:
        _render_agents()
    with t2:
        _render_tasks()
    with t3:
        _render_messages()
    with t4:
        _render_audit_log()


# ── 1. Agent Status Panel ──────────────────────────────────────────────

def _render_agents():
    st.subheader("Agent Status")

    if not _table_exists_api("agent_heartbeats"):
        st.info("Coordination tables not yet created.")
        return

    now = time.time()

    # Heartbeats
    heartbeats = _query_api(
        "SELECT agent_id, last_heartbeat, session_id, project_id "
        "FROM agent_heartbeats ORDER BY last_heartbeat DESC"
    )

    # Agent activity from project_state
    activity = _query_api(
        "SELECT key, value, updated_by, updated_at FROM project_state "
        "WHERE key LIKE 'agent:%:status' ORDER BY updated_at DESC"
    )

    # Agent stats
    n_alive = 0
    n_dead = 0
    agent_data = []

    if heartbeats is not None and not heartbeats.empty:
        for _, hb in heartbeats.iterrows():
            agent_id = hb.get("agent_id", "?")
            last_hb = hb.get("last_heartbeat", 0)
            age = now - last_hb if last_hb else 999999
            is_alive = age < DEAD_THRESHOLD
            status = "alive" if is_alive else "dead"
            if is_alive:
                n_alive += 1
            else:
                n_dead += 1

            # Find activity for this agent
            agent_activity = "idle"
            agent_file = None
            if activity is not None and not activity.empty:
                for _, act in activity.iterrows():
                    if act.get("updated_by") == agent_id:
                        try:
                            val = json.loads(act.get("value", "{}"))
                            agent_activity = val.get("activity", "idle")
                            agent_file = val.get("file")
                        except (json.JSONDecodeError, TypeError):
                            pass
                        break

            agent_data.append({
                "agent_id": agent_id,
                "status": status,
                "age_s": age,
                "session_id": hb.get("session_id"),
                "project_id": hb.get("project_id"),
                "activity": agent_activity,
                "file": agent_file,
            })

    # Summary metrics
    c1, c2, c3 = st.columns(3)
    c1.metric("Alive", n_alive, delta=None, delta_color="normal")
    c2.metric("Dead", n_dead, delta=None, delta_color="inverse" if n_dead > 0 else "normal")
    c3.metric("Total Agents", n_alive + n_dead)

    st.divider()

    # Agent cards
    if agent_data:
        for agent in agent_data:
            color = "#10b981" if agent["status"] == "alive" else "#ef4444"
            icon = "●" if agent["status"] == "alive" else "○"
            age_str = _format_age(agent["age_s"])

            activity_str = agent["activity"]
            file_str = f" — `{agent['file']}`" if agent.get("file") else ""

            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:12px 16px;margin:6px 0;'>"
                f"<div style='display:flex;align-items:center;gap:8px;'>"
                f"<span style='color:{color};font-size:1.2rem;'>{icon}</span>"
                f"<span style='color:#f0f2f6;font-size:0.9rem;font-weight:600;'>{agent['agent_id']}</span>"
                f"<span style='color:{color};font-size:0.7rem;margin-left:8px;'>{agent['status'].upper()}</span>"
                f"<span style='color:#6b7280;font-size:0.7rem;margin-left:auto;'>last heartbeat: {age_str}</span>"
                f"</div>"
                f"<div style='color:#9ca3af;font-size:0.75rem;margin-top:4px;'>"
                f"activity: {activity_str}{file_str}"
                f"</div>"
                f"</div>"
            )

        st.divider()

        # Action: cleanup stale agents
        if n_dead > 0:
            if st.button(f"Cleanup {n_dead} stale agent(s)", type="secondary"):
                try:
                    _c = _api()
                    if _c:
                        # Call durability maintenance
                        result = _c.health_check()
                        st.toast("Stale agents cleaned up", icon="✅")
                        st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")
    else:
        st.info("No agents have sent heartbeats yet. Agents register on session start via the coordination hook.")

    # Manual heartbeat
    st.divider()
    st.markdown("#### Register Agent")
    with st.form("register_agent", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            reg_agent = st.text_input("Agent ID", key="reg_agent")
        with col2:
            reg_project = st.text_input("Project", value="default", key="reg_project")
        if st.form_submit_button("Register", type="primary"):
            if reg_agent:
                try:
                    _c = _api()
                    if _c:
                        _c.send_message("dashboard", reg_agent, "heartbeat", json.dumps({"project_id": reg_project}))
                        st.toast(f"Agent {reg_agent} registered", icon="✅")
                        st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    elif seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    elif seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    else:
        return f"{seconds / 86400:.1f}d ago"


# ── 2. Task Board ──────────────────────────────────────────────────────

def _render_tasks():
    st.subheader("Shared Task Board")

    if not _table_exists_api("shared_tasks"):
        st.info("Coordination tables not yet created.")
        return

    now = time.time()

    # Stats
    n_total = _try_count_api("shared_tasks")
    n_pending = _try_count_api("shared_tasks", "status='pending'")
    n_active = _try_count_api("shared_tasks", "status='active'")
    n_completed = _try_count_api("shared_tasks", "status='completed'")
    n_stale = _try_count_api("shared_tasks", f"status='active' AND updated_at < {now - STALE_THRESHOLD}")
    n_abandoned = _try_count_api("shared_tasks", "status='abandoned'")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total", n_total)
    c2.metric("Pending", n_pending)
    c3.metric("Active", n_active)
    c4.metric("Stale", n_stale, delta=f"{n_stale} stuck" if n_stale > 0 else None, delta_color="inverse" if n_stale > 0 else "off")
    c5.metric("Completed", n_completed)
    c6.metric("Abandoned", n_abandoned)

    st.divider()

    # Filters
    col1, col2, col3 = st.columns(3)
    with col1:
        status_filter = st.selectbox("Status", ["all", "pending", "active", "stale_active", "completed", "abandoned"], key="task_filter")
    with col2:
        type_filter = st.text_input("Task type", placeholder="e.g. resolve_contradiction", key="task_type_filter")
    with col3:
        agent_filter = st.text_input("Assigned to", placeholder="agent_id", key="task_agent_filter")

    # Build query
    where_parts = []
    params = []
    if status_filter == "stale_active":
        where_parts.append("status='active' AND updated_at < ?")
        params.append(now - STALE_THRESHOLD)
    elif status_filter != "all":
        where_parts.append("status=?")
        params.append(status_filter)
    if type_filter:
        where_parts.append("task_type=?")
        params.append(type_filter)
    if agent_filter:
        where_parts.append("assigned_to=?")
        params.append(agent_filter)

    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    tasks = _query_api(
        f"SELECT * FROM shared_tasks {where} ORDER BY created_at DESC LIMIT 100",
        params,
    )

    if tasks is not None and not tasks.empty:
        for _, task in tasks.iterrows():
            status = task.get("status", "?")
            task_id = task.get("id", "?")
            created_at = task.get("created_at", 0)
            updated_at = task.get("updated_at", 0)
            age = now - created_at if created_at else 0
            stale = status == "active" and (now - updated_at) > STALE_THRESHOLD if updated_at else False

            icon = {"pending": "⏳", "active": "🔄", "completed": "✅", "abandoned": "❌"}.get(status, "❓")
            if stale:
                icon = "⚠️"

            age_str = _format_age(age)
            assigned = task.get("assigned_to") or "unassigned"
            creator = task.get("created_by", "?")

            st.html(
                f"<div style='background:#1a1d23;border:1px solid {'#f59e0b' if stale else '#2d3139'};"
                f"border-radius:8px;padding:10px 12px;margin:4px 0;display:flex;align-items:center;gap:10px;'>"
                f"<span>{icon}</span>"
                f"<div style='flex:1;'>"
                f"<div style='color:#d1d5db;font-size:0.8rem;font-weight:600;'>"
                f"#{task_id} {task.get('task_type', '?')} · {task.get('project_id', '?')}</div>"
                f"<div style='color:#9ca3af;font-size:0.72rem;'>{str(task.get('description', ''))[:100]}</div>"
                f"<div style='color:#6b7280;font-size:0.65rem;margin-top:2px;'>"
                f"by {creator} → {assigned} · {age_str}"
                f"{' · ⚠️ STALE' if stale else ''}</div>"
                f"</div>"
                f"<span style='color:{_status_color(status)};font-size:0.7rem;'>{status}</span>"
                f"</div>"
            )

            # Action buttons
            cols = st.columns(5)
            with cols[0]:
                if status == "pending":
                    if st.button("Claim", key=f"claim_{task_id}"):
                        _task_action("active", task_id, "dashboard")
            with cols[1]:
                if status == "active":
                    if st.button("Complete", key=f"complete_{task_id}"):
                        _task_action("completed", task_id, "dashboard")
            with cols[2]:
                if status in ("pending", "active"):
                    if st.button("Release", key=f"release_{task_id}"):
                        _task_action("pending", task_id, None)
            with cols[3]:
                if stale:
                    if st.button("Reclaim", key=f"reclaim_{task_id}"):
                        _task_action("pending", task_id, None)
            with cols[4]:
                if status not in ("completed", "abandoned"):
                    if st.button("Abandon", key=f"abandon_{task_id}"):
                        _task_action("abandoned", task_id, None)
    else:
        st.info("No tasks match filters")

    # Bulk actions
    if n_stale > 0:
        st.divider()
        if st.button(f"Reclaim all {n_stale} stale task(s)", type="secondary"):
            try:
                _c = _api()
                if _c:
                    _c.query(f"UPDATE shared_tasks SET status='pending', assigned_to=NULL, updated_at={now} WHERE status='active' AND updated_at < {now - STALE_THRESHOLD}")
                    st.toast(f"Reclaimed {n_stale} stale tasks", icon="✅")
                    st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    # Create task
    st.divider()
    st.markdown("#### Create Task")
    with st.form("create_task", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            task_type = st.selectbox("Type", ["fix", "test", "review", "document", "implement", "resolve_contradiction", "fix_integrity"], key="task_type")
        with col2:
            project_id = st.text_input("Project", value="default", key="task_project")
        with col3:
            assign_to = st.text_input("Assign to (optional)", placeholder="agent_id", key="task_assign")
        description = st.text_area("Description", key="task_desc")
        if st.form_submit_button("Create Task", type="primary"):
            if task_type:
                try:
                    _c = _api()
                    if _c:
                        _c.create_task(project_id, task_type, description, assign_to or None)
                    else:
                        st.error("Write requires the REST API to be running.")
                        return
                    st.toast(f"Task created: {task_type}", icon="✅")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")


def _task_action(status: str, task_id, assign_to):
    try:
        _c = _api()
        if _c:
            _c.update_task(task_id, status, assign_to)
            st.toast(f"Task #{task_id} → {status}", icon="✅")
            st.rerun()
    except Exception as e:
        st.error(f"Failed: {e}")


def _status_color(status: str) -> str:
    return {
        "pending": "#f59e0b",
        "active": "#3b82f6",
        "completed": "#10b981",
        "abandoned": "#6b7280",
    }.get(status, "#6b7280")


# ── 3. Messages ────────────────────────────────────────────────────────

def _render_messages():
    st.subheader("Agent Messaging")

    if not _table_exists_api("agent_messages"):
        st.info("Agent messaging table not yet created.")
        return

    # Stats
    n_total = _try_count_api("agent_messages")
    n_pending = _try_count_api("agent_messages", "status='pending'")
    n_delivered = _try_count_api("agent_messages", "status='delivered'")

    c1, c2, c3 = st.columns(3)
    c1.metric("Total", n_total)
    c2.metric("Pending", n_pending, delta=f"{n_pending} undelivered" if n_pending > 0 else None, delta_color="inverse" if n_pending > 0 else "off")
    c3.metric("Delivered", n_delivered)

    st.divider()

    # Message history
    st.markdown("#### Recent Messages")
    messages = _query_api(
        "SELECT * FROM agent_messages ORDER BY created_at DESC LIMIT 50"
    )

    if messages is not None and not messages.empty:
        for _, msg in messages.iterrows():
            status = msg.get("status", "?")
            status_icon = {"pending": "⏳", "delivered": "✅", "dead_lettered": "❌"}.get(status, "❓")
            ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime("%m-%d %H:%M") if msg.get("created_at") else "?"

            payload = msg.get("payload", "")
            if payload and len(payload) > 100:
                payload = payload[:100] + "..."

            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:8px 12px;margin:4px 0;display:flex;align-items:center;gap:10px;'>"
                f"<span>{status_icon}</span>"
                f"<span style='color:#d1d5db;font-size:0.78rem;'>{msg.get('from_agent', '?')} → {msg.get('to_agent', '?')}</span>"
                f"<span style='color:#8b5cf6;font-size:0.7rem;'>{msg.get('message_type', '?')}</span>"
                f"<span style='color:#6b7280;font-size:0.65rem;margin-left:auto;'>{ts}</span>"
                f"</div>"
            )
            if payload:
                st.caption(f"  payload: {payload}")
    else:
        st.info("No messages")

    # Send message
    st.divider()
    st.markdown("#### Send Message")
    with st.form("send_message", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            msg_from = st.text_input("From", value="dashboard", key="msg_from")
        with col2:
            msg_to = st.text_input("To", placeholder="agent_id or * for broadcast", key="msg_to")
        msg_type = st.selectbox("Type", ["task_assign", "task_complete", "lock_request", "notification", "custom"], key="msg_type")
        msg_payload = st.text_area("Payload (JSON)", key="msg_payload")
        if st.form_submit_button("Send Message", type="primary"):
            if msg_to and msg_type:
                try:
                    _c = _api()
                    if _c:
                        _c.send_message(msg_from, msg_to, msg_type, msg_payload or None)
                    else:
                        st.error("Write requires the REST API to be running.")
                        return
                    st.toast(f"Message sent to {msg_to}", icon="✅")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")


# ── 4. Audit Log ───────────────────────────────────────────────────────

def _render_audit_log():
    st.subheader("Coordination Audit Log")

    if not _table_exists_api("coordination_audit"):
        st.info("Audit log table not yet created.")
        return

    # Stats
    n_total = _try_count_api("coordination_audit")
    st.metric("Total Events", n_total)

    st.divider()

    # Filters
    col1, col2 = st.columns(2)
    with col1:
        action_filter = st.selectbox("Action", [
            "all", "session_start", "session_end", "task_created", "task_claimed",
            "task_status_changed", "task_reaped", "message_delivered",
            "locks_released", "durability_maintenance",
        ], key="audit_action_filter")
    with col2:
        agent_filter = st.text_input("Agent", placeholder="agent_id", key="audit_agent_filter")

    where_parts = ["1=1"]
    params = []
    if action_filter != "all":
        where_parts.append("action=?")
        params.append(action_filter)
    if agent_filter:
        where_parts.append("agent_id=?")
        params.append(agent_filter)

    where = " AND ".join(where_parts)
    audit = _query_api(
        f"SELECT * FROM coordination_audit WHERE {where} ORDER BY timestamp DESC LIMIT 100",
        params,
    )

    if audit is not None and not audit.empty:
        for _, entry in audit.iterrows():
            ts = entry.get("timestamp", 0)
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
            action = entry.get("action", "?")
            agent = entry.get("agent_id", "?")
            target = entry.get("target", "")
            detail = entry.get("detail", "")

            # Color code by action type
            action_color = {
                "session_start": "#10b981",
                "session_end": "#6b7280",
                "task_created": "#3b82f6",
                "task_claimed": "#8b5cf6",
                "task_status_changed": "#f59e0b",
                "task_reaped": "#ef4444",
                "message_delivered": "#10b981",
                "locks_released": "#6b7280",
                "durability_maintenance": "#06b6d4",
            }.get(action, "#9ca3af")

            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:6px;"
                f"padding:6px 10px;margin:2px 0;display:flex;align-items:center;gap:8px;font-size:0.75rem;'>"
                f"<span style='color:#6b7280;min-width:130px;'>{ts_str}</span>"
                f"<span style='color:{action_color};font-weight:600;min-width:140px;'>{action}</span>"
                f"<span style='color:#d1d5db;'>{agent}</span>"
                f"<span style='color:#9ca3af;'>{target}</span>"
                f"</div>"
            )
            if detail:
                st.caption(f"  {detail[:200]}")
    else:
        st.info("No audit entries match filters")
