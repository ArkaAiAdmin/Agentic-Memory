#!/usr/bin/env python3
"""Coordination tab — Multi-agent task management, file locks, messaging, state."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

import dashboard
from dashboard import DARK, get_conn, query, table, try_count

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def render_coordination():
    """Coordination tab with 4 sub-tabs."""
    t1, t2, t3, t4 = st.tabs([
        "Tasks", "File Locks", "Messaging", "Project State",
    ])
    with t1:
        _render_tasks()
    with t2:
        _render_file_locks()
    with t3:
        _render_messaging()
    with t4:
        _render_project_state()


def _get_conn():
    return sqlite3.connect(str(dashboard.DB), timeout=10)


# ── 1. Tasks ─────────────────────────────────────────────────────────────

def _render_tasks():
    st.subheader("Shared Task Board")

    if not table("shared_tasks"):
        st.info("Coordination tables not yet created. Run migration 069.")
        return

    # Stats
    n_total = try_count("shared_tasks")
    n_pending = try_count("shared_tasks", "status='pending'")
    n_active = try_count("shared_tasks", "status='active'")
    n_completed = try_count("shared_tasks", "status='completed'")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", n_total)
    c2.metric("Pending", n_pending)
    c3.metric("Active", n_active)
    c4.metric("Completed", n_completed)

    st.divider()

    # Create task
    st.markdown("#### Create Task")
    with st.form("create_task", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            task_type = st.selectbox("Type", ["fix", "test", "review", "document", "implement"], key="task_type")
        with col2:
            project_id = st.text_input("Project", value="default", key="task_project")
        with col3:
            assign_to = st.text_input("Assign to (optional)", placeholder="agent_id", key="task_assign")

        description = st.text_area("Description", key="task_desc")

        if st.form_submit_button("\u2705 Create Task", type="primary"):
            if task_type:
                try:
                    conn = _get_conn()
                    now = time.time()
                    cursor = conn.execute(
                        "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (project_id, task_type, description, assign_to,
                         "active" if assign_to else "pending", "dashboard", now, now),
                    )
                    conn.commit()
                    conn.close()
                    st.toast(f"Task created: {task_type}", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")

    st.divider()

    # Task list
    st.markdown("#### Tasks")

    status_filter = st.selectbox("Filter", ["all", "pending", "active", "completed"], key="task_filter")
    where = "" if status_filter == "all" else f"WHERE status='{status_filter}'"
    tasks = query(f"SELECT * FROM shared_tasks {where} ORDER BY created_at DESC LIMIT 50")

    if tasks is not None and not tasks.empty:
        for _, task in tasks.iterrows():
            status = task.get("status", "?")
            icon = {"pending": "\u23f3", "active": "\U0001f504", "completed": "\u2705"}.get(status, "\u2753")
            color = {"pending": "#f59e0b", "active": "#3b82f6", "completed": "#10b981"}.get(status, "#6b7280")

            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:10px 12px;margin:4px 0;display:flex;align-items:center;gap:10px;'>"
                f"<span>{icon}</span>"
                f"<div style='flex:1;'>"
                f"<div style='color:#d1d5db;font-size:0.8rem;font-weight:600;'>{task.get('task_type', '?')} \u00b7 {task.get('project_id', '?')}</div>"
                f"<div style='color:#9ca3af;font-size:0.72rem;'>{str(task.get('description', ''))[:80]}</div>"
                f"</div>"
                f"<span style='color:{color};font-size:0.7rem;'>{status}</span>"
                f"</div>"
            )

            # Action buttons
            cols = st.columns(4)
            with cols[0]:
                if task.get("status") == "pending":
                    if st.button("Claim", key=f"claim_{task['id']}"):
                        try:
                            conn = _get_conn()
                            conn.execute("UPDATE shared_tasks SET assigned_to='dashboard', status='active', updated_at=? WHERE id=?",
                                         (time.time(), task["id"]))
                            conn.commit()
                            conn.close()
                            st.toast("Task claimed", icon="\u2705")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed: {e}")
            with cols[1]:
                if task.get("status") == "active":
                    if st.button("Complete", key=f"complete_{task['id']}"):
                        try:
                            conn = _get_conn()
                            conn.execute("UPDATE shared_tasks SET status='completed', updated_at=? WHERE id=?",
                                         (time.time(), task["id"]))
                            conn.commit()
                            conn.close()
                            st.toast("Task completed", icon="\u2705")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed: {e}")
            with cols[2]:
                if task.get("status") in ("pending", "active"):
                    if st.button("Release", key=f"release_{task['id']}"):
                        try:
                            conn = _get_conn()
                            conn.execute("UPDATE shared_tasks SET assigned_to=NULL, status='pending', updated_at=? WHERE id=?",
                                         (time.time(), task["id"]))
                            conn.commit()
                            conn.close()
                            st.toast("Task released", icon="\u2705")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed: {e}")
    else:
        st.info("No tasks yet")


# ── 2. File Locks ────────────────────────────────────────────────────────

def _render_file_locks():
    st.subheader("File Locks")

    if not table("file_locks"):
        st.info("File locks table not yet created")
        return

    # List active locks
    st.markdown("#### Active Locks")
    locks = query("SELECT * FROM file_locks ORDER BY locked_at DESC")

    if locks is not None and not locks.empty:
        now = time.time()
        active = []
        expired = []
        for _, lock in locks.iterrows():
            if lock.get("expires_at") and lock["expires_at"] < now:
                expired.append(lock)
            else:
                active.append(lock)

        if active:
            for lock in active:
                remaining = max(0, (lock.get("expires_at", 0) - now)) if lock.get("expires_at") else 0
                st.html(
                    f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                    f"padding:8px 12px;margin:4px 0;display:flex;align-items:center;gap:10px;'>"
                    f"<span style='color:#10b981;'>\U0001f512</span>"
                    f"<span style='color:#d1d5db;font-size:0.78rem;flex:1;'>{lock.get('file_path', '?')}</span>"
                    f"<span style='color:#6b7280;font-size:0.7rem;'>{lock.get('locked_by', '?')}</span>"
                    f"<span style='color:#f59e0b;font-size:0.65rem;'>{remaining:.0f}s left</span>"
                    f"</div>"
                )
                if st.button("Release", key=f"unlock_{lock.get('file_path', '')}"):
                    try:
                        conn = _get_conn()
                        conn.execute("DELETE FROM file_locks WHERE file_path=?", (lock.get("file_path"),))
                        conn.commit()
                        conn.close()
                        st.toast("Lock released", icon="\u2705")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")
        else:
            st.info("No active locks")

        if expired:
            st.caption(f"{len(expired)} expired locks (auto-cleaned on next access)")
    else:
        st.info("No locks")

    # Manual lock
    st.divider()
    st.markdown("#### Acquire Lock")
    with st.form("acquire_lock", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            lock_file = st.text_input("File path", key="lock_file")
        with col2:
            lock_agent = st.text_input("Agent ID", value="dashboard", key="lock_agent")
        lock_ttl = st.slider("TTL (seconds)", 30, 3600, 300, key="lock_ttl")

        if st.form_submit_button("\U0001f512 Acquire Lock", type="primary"):
            if lock_file:
                try:
                    conn = _get_conn()
                    now = time.time()
                    conn.execute(
                        "INSERT OR REPLACE INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
                        (lock_file, lock_agent, now, now + lock_ttl),
                    )
                    conn.commit()
                    conn.close()
                    st.toast(f"Locked: {lock_file}", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")


# ── 3. Messaging ─────────────────────────────────────────────────────────

def _render_messaging():
    st.subheader("Agent Messaging")

    if not table("agent_messages"):
        st.info("Agent messaging table not yet created")
        return

    # Stats
    n_total = try_count("agent_messages")
    n_pending = try_count("agent_messages", "status='pending'")
    n_delivered = try_count("agent_messages", "status='delivered'")

    c1, c2, c3 = st.columns(3)
    c1.metric("Total", n_total)
    c2.metric("Pending", n_pending)
    c3.metric("Delivered", n_delivered)

    st.divider()

    # Send message
    st.markdown("#### Send Message")
    with st.form("send_message", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            msg_from = st.text_input("From", value="dashboard", key="msg_from")
        with col2:
            msg_to = st.text_input("To", placeholder="agent_id or * for broadcast", key="msg_to")

        msg_type = st.selectbox("Type", ["task_assign", "task_complete", "lock_request", "notification", "custom"], key="msg_type")
        msg_payload = st.text_area("Payload (JSON)", key="msg_payload")

        if st.form_submit_button("\U0001f4e8 Send Message", type="primary"):
            if msg_to and msg_type:
                try:
                    conn = _get_conn()
                    now = time.time()
                    payload = msg_payload if msg_payload else None
                    conn.execute(
                        "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at) "
                        "VALUES (?, ?, ?, ?, 'pending', ?)",
                        (msg_from, msg_to, msg_type, payload, now),
                    )
                    conn.commit()
                    conn.close()
                    st.toast(f"Message sent to {msg_to}", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")

    st.divider()

    # Message history
    st.markdown("#### Recent Messages")
    messages = query("SELECT * FROM agent_messages ORDER BY created_at DESC LIMIT 20")

    if messages is not None and not messages.empty:
        for _, msg in messages.iterrows():
            status_icon = {"pending": "\u23f3", "delivered": "\u2705"}.get(msg.get("status"), "\u2753")
            ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime("%m-%d %H:%M") if msg.get("created_at") else "?"

            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:8px 12px;margin:4px 0;display:flex;align-items:center;gap:10px;'>"
                f"<span>{status_icon}</span>"
                f"<span style='color:#d1d5db;font-size:0.78rem;'>{msg.get('from_agent', '?')} \u2192 {msg.get('to_agent', '?')}</span>"
                f"<span style='color:#8b5cf6;font-size:0.7rem;'>{msg.get('message_type', '?')}</span>"
                f"<span style='color:#6b7280;font-size:0.65rem;margin-left:auto;'>{ts}</span>"
                f"</div>"
            )
    else:
        st.info("No messages")


# ── 4. Project State ─────────────────────────────────────────────────────

def _render_project_state():
    st.subheader("Project State")

    if not table("project_state"):
        st.info("Project state table not yet created")
        return

    # Project selector
    projects = query("SELECT DISTINCT project_id FROM project_state ORDER BY project_id")
    project_list = ["default"]
    if projects is not None and not projects.empty:
        project_list = projects["project_id"].tolist()

    selected_project = st.selectbox("Project", project_list, key="ps_project")

    st.divider()

    # Current state
    st.markdown(f"#### State for `{selected_project}`")
    state = query("SELECT * FROM project_state WHERE project_id=? ORDER BY key", (selected_project,))

    if state is not None and not state.empty:
        for _, s in state.iterrows():
            try:
                value = json.loads(s.get("value", "{}")) if s.get("value") else None
                value_display = json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)
            except Exception:
                value_display = str(s.get("value", ""))

            ts = datetime.fromtimestamp(s.get("updated_at", 0)).strftime("%m-%d %H:%M") if s.get("updated_at") else "?"

            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:10px 12px;margin:4px 0;'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                f"<span style='color:#d1d5db;font-size:0.8rem;font-weight:600;'>{s.get('key', '?')}</span>"
                f"<span style='color:#6b7280;font-size:0.65rem;'>{s.get('updated_by', '?')} \u00b7 {ts}</span>"
                f"</div>"
                f"<div style='color:#9ca3af;font-size:0.72rem;margin-top:4px;white-space:pre-wrap;'>{value_display[:200]}</div>"
                f"</div>"
            )
    else:
        st.info(f"No state for project '{selected_project}'")

    # Update state
    st.divider()
    st.markdown("#### Update State")
    with st.form("update_state", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            state_key = st.text_input("Key", placeholder="e.g. current_branch, active_file", key="state_key")
        with col2:
            state_value = st.text_area("Value (JSON)", key="state_value")
        state_agent = st.text_input("Agent", value="dashboard", key="state_agent")

        if st.form_submit_button("\U0001f4be Update", type="primary"):
            if state_key:
                try:
                    conn = _get_conn()
                    now = time.time()
                    try:
                        value = json.loads(state_value) if state_value else None
                    except json.JSONDecodeError:
                        value = state_value
                    value_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value) if value is not None else None

                    conn.execute(
                        "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (selected_project, state_key, value_str, state_agent, now),
                    )
                    conn.commit()
                    conn.close()
                    st.toast(f"State updated: {state_key}", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")
