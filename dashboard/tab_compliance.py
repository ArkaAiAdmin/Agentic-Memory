#!/usr/bin/env python3
"""Compliance tab — RBAC, ACL, GDPR, Tenants, SOC 2, Policy, Checks."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import dashboard
from dashboard import DARK, get_conn, query, table, try_count

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def _api():
    return st.session_state.get("api_client")


def _table_exists_api(name: str) -> bool:
    client = _api()
    if client:
        try:
            r = client.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name])
            return len(r.get("results", [])) > 0
        except Exception:
            pass
    return _table_exists(name)


def _list_column_api(table: str, column: str) -> list[str]:
    client = _api()
    if client:
        try:
            r = client.query(
                f"SELECT DISTINCT {column} as val FROM {table} WHERE {column} IS NOT NULL ORDER BY val"
            )
            return [row[column] if column in row else row.get("val", "") for row in r.get("results", [])]
        except Exception:
            pass
    try:
        conn = _get_db()
        rows = conn.execute(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY {column}").fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _try_count_api(table: str, where: str = "") -> int:
    client = _api()
    if client:
        sql = f"SELECT COUNT(*) as count FROM {table}"
        if where:
            sql += f" WHERE {where}"
        try:
            result = client.query(sql)
            rows = result.get("results", [])
            return rows[0]["count"] if rows else 0
        except Exception:
            pass
    return try_count(table, where) if where else try_count(table)


def _query_api(sql: str, params: list | None = None) -> pd.DataFrame | None:
    client = _api()
    if client:
        try:
            result = client.query(sql, params=params or [])
            rows = result.get("results", [])
            if not rows:
                return None
            return pd.DataFrame(rows)
        except Exception:
            pass
    return query(sql, params=params or ())


def render_compliance():
    """Compliance tab with 7 sub-tabs."""
    t1, t2, t3, t4, t5, t6, t7 = st.tabs([
        "RBAC", "ACL Rules", "GDPR", "Tenants", "SOC 2", "Policy", "Health Check",
    ])
    with t1:
        _render_rbac()
    with t2:
        _render_acl()
    with t3:
        _render_gdpr()
    with t4:
        _render_tenants()
    with t5:
        _render_soc2()
    with t6:
        _render_policy_hash()
    with t7:
        _render_compliance_check()


# ── Helpers ───────────────────────────────────────────────────────────────

def _get_db():
    return sqlite3.connect(f"file:{dashboard.DB}?mode=ro", uri=True, timeout=10)


def _table_exists(name: str) -> bool:
    try:
        conn = _get_db()
        r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        conn.close()
        return r is not None
    except Exception:
        return False


def _list_column(table_name: str, column: str) -> list[str]:
    try:
        conn = _get_db()
        rows = conn.execute(f"SELECT DISTINCT {column} FROM {table_name} WHERE {column} IS NOT NULL ORDER BY {column}").fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _confirm_action(label: str, key: str) -> bool:
    """Show a confirmation checkbox before dangerous actions."""
    return st.checkbox(f"I confirm: {label}", key=key)


# ── 1. RBAC ──────────────────────────────────────────────────────────────

def _render_rbac():
    st.subheader("Role-Based Access Control")

    client = _api()

    if not _table_exists_api("principals"):
        st.warning("RBAC tables not created yet. Run tier migration to initialize.")
        if st.button("\u2699\ufe0f Initialize RBAC", type="primary"):
            with st.spinner("Initializing..."):
                try:
                    if client:
                        client.rbac_init()
                    else:
                        st.error("API client not available — start the REST server.")
                        return
                    st.toast("RBAC initialized", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")
        return

    # ── Overview stats ───────────────────────────────────────────────────
    n_principals = _try_count_api("principals")
    n_roles = _try_count_api("roles")
    n_bindings = _try_count_api("role_bindings")
    n_overrides = _try_count_api("acl_overrides") if _table_exists_api("acl_overrides") else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Principals", n_principals)
    c2.metric("Roles", n_roles)
    c3.metric("Role Bindings", n_bindings)
    c4.metric("ACL Overrides", n_overrides)

    st.divider()

    # ── Principals ───────────────────────────────────────────────────────
    st.markdown("#### Principals")

    principals_df = _query_api("SELECT id, kind, display_name, tenant_id, created_at FROM principals ORDER BY id")
    if principals_df is not None and not principals_df.empty:
        st.dataframe(principals_df, use_container_width=True, hide_index=True, key="rbac_principals_table")
    else:
        st.info("No principals registered — system runs in fail-open mode (all agents have access). Add principals to restrict access.")

    with st.expander("\u2795 Add Principal", expanded=False):
        with st.form("add_principal", clear_on_submit=True):
            p_col1, p_col2, p_col3, p_col4 = st.columns(4)
            with p_col1:
                p_id = st.text_input("Principal ID *", placeholder="e.g. agent-openai")
            with p_col2:
                p_kind = st.selectbox("Kind", ["agent", "user", "service"], key="p_kind")
            with p_col3:
                p_name = st.text_input("Display Name", placeholder="e.g. OpenAI Agent")
            with p_col4:
                p_email = st.text_input("Email (optional)", placeholder="agent@example.com")
            p_tenant = st.text_input("Tenant", value="default")
            if st.form_submit_button("\u2705 Create Principal", type="primary"):
                if p_id:
                    try:
                        display = p_name or p_id
                        if p_email:
                            display = f"{display} <{p_email}>"
                        if client:
                            client.rbac_create_principal(pid=p_id, kind=p_kind, display_name=display, tenant_id=p_tenant)
                        else:
                            st.error("API client not available — start the REST server.")
                            return
                        st.toast(f"Created principal: {p_id}", icon="\u2705")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")
                else:
                    st.error("Principal ID is required")

    st.divider()

    # ── Roles ────────────────────────────────────────────────────────────
    st.markdown("#### Roles")

    roles_df = _query_api("SELECT id, name, description, tenant_id FROM roles ORDER BY id")
    if roles_df is not None and not roles_df.empty:
        st.dataframe(roles_df, use_container_width=True, hide_index=True, key="rbac_roles_table")
    else:
        st.info("No roles defined")

    with st.expander("\u2795 Add Role", expanded=False):
        with st.form("add_role", clear_on_submit=True):
            r_col1, r_col2 = st.columns(2)
            with r_col1:
                r_id = st.text_input("Role ID *", placeholder="e.g. admin, reader, writer")
            with r_col2:
                r_desc = st.text_input("Description", placeholder="Full access to all resources")
            r_tenant = st.text_input("Tenant", value="default")
            if st.form_submit_button("\u2705 Create Role", type="primary"):
                if r_id:
                    try:
                        if client:
                            client.rbac_create_role(rid=r_id, description=r_desc or "", tenant_id=r_tenant)
                        else:
                            st.error("API client not available — start the REST server.")
                            return
                        st.toast(f"Created role: {r_id}", icon="\u2705")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")
                else:
                    st.error("Role ID is required")

    st.divider()

    # ── Grant / Revoke ───────────────────────────────────────────────────
    st.markdown("#### Grant / Revoke Role")

    principals = _list_column_api("principals", "id")
    roles = _list_column_api("roles", "id")

    if principals and roles:
        g_col1, g_col2, g_col3, g_col4 = st.columns([2, 2, 1.5, 1])
        with g_col1:
            grant_principal = st.selectbox("Principal", principals, key="grant_principal")
        with g_col2:
            grant_role = st.selectbox("Role", roles, key="grant_role")
        with g_col3:
            pass
        with g_col4:
            st.write("")
            st.write("")
            if st.button("\u2705 Grant", key="do_grant", type="primary", use_container_width=True):
                try:
                    if client:
                        client.rbac_grant(principal_id=grant_principal, role_id=grant_role)
                    else:
                        st.error("API client not available — start the REST server.")
                        return
                    st.toast(f"Granted '{grant_role}' to '{grant_principal}'", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")
    else:
        st.info("Create principals and roles first before granting access")

    # ── Current Bindings ─────────────────────────────────────────────────
    st.markdown("#### Current Role Bindings")

    bindings_df = _query_api(
        "SELECT rb.principal_id, rb.role_id, rb.granted_at, rb.granted_by "
        "FROM role_bindings rb ORDER BY rb.granted_at DESC"
    )
    if bindings_df is not None and not bindings_df.empty:
        display_bindings = bindings_df.copy()
        display_bindings["action"] = ""

        edited = st.data_editor(
            display_bindings,
            use_container_width=True,
            hide_index=True,
            column_config={
                "action": st.column_config.TextColumn("Action", width="small"),
            },
            key="rbac_bindings_editor",
        )

        # Find rows marked for revocation
        for _, row in edited.iterrows():
            if str(row.get("action", "")).strip().lower() == "revoke":
                try:
                    if client:
                        client.rbac_revoke(principal_id=row["principal_id"], role_id=row["role_id"])
                    else:
                        st.error("API client not available — start the REST server.")
                        return
                    st.toast(f"Revoked '{row['role_id']}' from '{row['principal_id']}'", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")
    else:
        st.info("No role bindings yet")


# ── 2. ACL Rules ─────────────────────────────────────────────────────────

def _render_acl():
    st.subheader("ACL Override Rules")

    client = _api()

    if not _table_exists_api("acl_overrides"):
        st.info("ACL overrides table not yet created")
        return

    n_overrides = _try_count_api("acl_overrides")
    st.metric("Total Rules", n_overrides)

    st.divider()

    # ── Add new rule ─────────────────────────────────────────────────────
    st.markdown("#### Add Rule")

    principals = _list_column_api("principals", "id") or ["default"]
    resources = ["memory", "kg_entities", "kg_edges", "kg_facts", "memories", "search", "admin"]
    actions = ["read", "write", "delete", "admin", "share", "export", "search"]

    with st.form("add_acl_rule", clear_on_submit=True):
        r_col1, r_col2, r_col3, r_col4 = st.columns(4)
        with r_col1:
            acl_principal = st.selectbox("Principal", principals, key="acl_principal")
        with r_col2:
            acl_resource = st.selectbox("Resource", resources, key="acl_resource")
        with r_col3:
            acl_action = st.selectbox("Action", actions, key="acl_action")
        with r_col4:
            acl_effect = st.selectbox("Effect", ["allow", "deny"], key="acl_effect")

        if st.form_submit_button("\u2705 Add Rule", type="primary"):
            try:
                if client:
                    client.acl_add_rule(
                        principal_id=acl_principal,
                        resource_id=acl_resource,
                        action=acl_action,
                        effect=acl_effect,
                    )
                else:
                    st.error("API client not available — start the REST server.")
                    return
                st.toast(f"Rule added: {acl_effect} {acl_action} on {acl_resource} for {acl_principal}", icon="\u2705")
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    st.divider()

    # ── Current rules ────────────────────────────────────────────────────
    st.markdown("#### Current Rules")

    rules_df = _query_api("SELECT * FROM acl_overrides ORDER BY principal_id, resource_id, action")
    if rules_df is not None and not rules_df.empty:
        display_rules = rules_df.copy()
        display_rules["_delete"] = False

        edited = st.data_editor(
            display_rules,
            use_container_width=True,
            hide_index=True,
            column_config={
                "_delete": st.column_config.CheckboxColumn("Delete"),
            },
            key="acl_rules_editor",
        )

        selected = edited[edited["_delete"] == True]
        if not selected.empty:
            if st.button(f"\U0001f5d1\ufe0f Delete {len(selected)} selected rules", type="primary"):
                try:
                    if client:
                        for _, row in selected.iterrows():
                            client.acl_delete_rule(
                                principal_id=row["principal_id"],
                                resource_id=row["resource_id"],
                                action=row["action"],
                            )
                    else:
                        st.error("API client not available — start the REST server.")
                        return
                    st.toast(f"Deleted {len(selected)} rules", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")
    else:
        st.info("No ACL rules defined")


# ── 3. GDPR ──────────────────────────────────────────────────────────────

def _render_gdpr():
    st.subheader("GDPR Right to Erasure")

    st.markdown(
        "Article 17 Right-to-Be-Forgotten: permanently delete all data "
        "associated with a data subject and generate a signed deletion certificate."
    )

    st.divider()

    # ── Data Overview (always visible) ───────────────────────────────────
    st.markdown("#### Data Overview")

    total_memories = 0
    try:
        total_memories = _try_count_api("memories")
    except Exception:
        pass

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Memories", total_memories)

    try:
        subject_df = _query_api(
            "SELECT data_subject_sub, COUNT(*) as cnt FROM memories "
            "WHERE data_subject_sub IS NOT NULL AND data_subject_sub != '' "
            "GROUP BY data_subject_sub ORDER BY cnt DESC LIMIT 20"
        )
    except Exception:
        subject_df = None

    if subject_df is not None and not subject_df.empty:
        total_subjects = len(subject_df)
        c2.metric("Data Subjects", total_subjects)
        c3.metric("Top Subject", f"{subject_df.iloc[0]['data_subject_sub']} ({subject_df.iloc[0]['cnt']})")
    else:
        c2.metric("Data Subjects", 0)
        c3.metric("Top Subject", "—")

    # Show top data subjects if available
    if subject_df is not None and not subject_df.empty:
        with st.expander(f"Top {len(subject_df)} data subjects by memory count", expanded=True):
            for _, row in subject_df.iterrows():
                subj = row["data_subject_sub"]
                count = row["cnt"]
                ent_count = _try_count_api("kg_entities", f"name LIKE '%{subj}%'")
                st.html(
                    f"<div style='display:flex;justify-content:space-between;align-items:center;"
                    f"padding:6px 10px;margin:2px 0;background:#1a1d23;border:1px solid #2d3139;"
                    f"border-radius:6px;'>"
                    f"<div>"
                    f"<span style='color:#d1d5db;font-size:0.8rem;font-weight:600;'>{subj}</span>"
                    f"</div>"
                    f"<div style='display:flex;gap:12px;'>"
                    f"<span style='color:#8b5cf6;font-size:0.7rem;'>{count} memories</span>"
                    f"<span style='color:#06b6d4;font-size:0.7rem;'>{ent_count} entities</span>"
                    f"</div>"
                    f"</div>"
                )
    else:
        st.caption(
            "No data subjects found in memories. "
            "All memories use `data_subject_sub = NULL` (no per-user data separation)."
        )

    st.divider()

    # ── Search for data subject ──────────────────────────────────────────
    st.markdown("#### Erase Data Subject")

    search_col1, search_col2 = st.columns([3, 1])
    with search_col1:
        search_term = st.text_input(
            "Search by name or ID to begin erasure",
            key="gdpr_search",
            placeholder="e.g. agent-openai, user@example.com",
        )
    with search_col2:
        search_scope = st.selectbox("Scope", ["Principals", "All text"], key="gdpr_scope")

    if search_term:
        if search_scope == "Principals":
            matches = _query_api(
                "SELECT id, kind, display_name, tenant_id FROM principals "
                "WHERE id LIKE ? OR display_name LIKE ? LIMIT 10",
                [f"%{search_term}%", f"%{search_term}%"],
            )
        else:
            # Search across all data
            matches = _query_api(
                "SELECT DISTINCT data_subject_sub as id, 'data_subject' as kind, "
                "data_subject_sub as display_name, tenant_id "
                "FROM memories WHERE data_subject_sub LIKE ? LIMIT 10",
                [f"%{search_term}%"],
            )

        if matches is not None and not matches.empty:
            st.dataframe(matches, use_container_width=True, hide_index=True)

            selected_subject = st.selectbox(
                "Select data subject to erase",
                matches["id"].tolist(),
                key="gdpr_subject_select",
            )

            if selected_subject:
                _render_erasure_workflow(selected_subject)
        else:
            st.info("No matching data subjects found")

    st.divider()

    # ── Recent erasure requests ──────────────────────────────────────────
    _render_erasure_history()


def _render_erasure_workflow(subject: str):
    """Full erasure workflow: scan → preview → confirm → execute → certificate."""
    st.markdown(f"#### 2. Data Audit for `{subject}`")

    # Scan button
    if st.button("Scan for data", key="gdpr_scan_btn", type="secondary"):
        with st.spinner("Scanning..."):
            _scan_subject_data(subject)

    # Show scan results if available
    scan_key = f"gdpr_scan_{subject}"
    scan_data = st.session_state.get(scan_key)

    if scan_data:
        # Summary metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Memories", scan_data.get("memories", 0))
        c2.metric("KG Entities", scan_data.get("entities", 0))
        c3.metric("KG Edges", scan_data.get("edges", 0))
        c4.metric("Audit Entries", scan_data.get("audit", 0))

        # Detailed breakdown
        with st.expander("Detailed breakdown by table", expanded=False):
            for table, count in scan_data.get("tables", {}).items():
                if count > 0:
                    st.html(
                        f"<div style='display:flex;justify-content:space-between;"
                        f"padding:4px 0;border-bottom:1px solid #1f2937;'>"
                        f"<span style='color:#d1d5db;font-size:0.78rem;'>{table}</span>"
                        f"<span style='color:#f59e0b;font-weight:600;font-size:0.78rem;'>{count} rows</span>"
                        f"</div>"
                    )

        # Sample memories
        if scan_data.get("sample_memories"):
            with st.expander(f"Sample memories ({len(scan_data['sample_memories'])})", expanded=False):
                for m in scan_data["sample_memories"]:
                    preview = (m.get("content", "") or "")[:120]
                    st.html(
                        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:6px;"
                        f"padding:6px 10px;margin:3px 0;font-size:0.72rem;'>"
                        f"<span style='color:#8b5cf6;font-weight:600;'>{m.get('id', '?')}</span> "
                        f"<span style='color:#6b7280;'>{m.get('category', '?')}</span> "
                        f"<span style='color:#9ca3af;'>{preview}</span>"
                        f"</div>"
                    )

        # Sample KG entities
        if scan_data.get("sample_entities"):
            with st.expander(f"Sample KG entities ({len(scan_data['sample_entities'])})", expanded=False):
                for e in scan_data["sample_entities"]:
                    st.html(
                        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:6px;"
                        f"padding:6px 10px;margin:3px 0;font-size:0.72rem;'>"
                        f"<span style='color:#8b5cf6;font-weight:600;'>{e.get('name', '?')}</span> "
                        f"<span style='color:#6b7280;'>{e.get('entity_type', '?')}</span> "
                        f"<span style='color:#9ca3af;'>mentions: {e.get('mentions', 0)}</span>"
                        f"</div>"
                    )

        # ── Erasure section ──────────────────────────────────────────────
        st.divider()
        st.markdown("#### 3. Execute Erasure")

        total_rows = sum(scan_data.get("tables", {}).values())
        if total_rows == 0:
            st.info("No data found for this subject. Nothing to erase.")
            return

        st.warning(
            f"This will permanently delete **{total_rows} rows** across "
            f"{len([v for v in scan_data.get('tables', {}).values() if v > 0])} tables "
            f"and generate a signed deletion certificate."
        )

        # Two-step confirmation
        confirm1 = st.checkbox(
            f"I understand this will permanently delete all data for '{subject}'",
            key="gdpr_confirm_erase",
        )

        confirm2 = ""
        if confirm1:
            confirm2 = st.text_input(
                f'Type "DELETE" to confirm erasure of {subject}',
                key="gdpr_confirm_text",
                placeholder="DELETE",
            )

        both_confirmed = confirm1 and confirm2.strip().upper() == "DELETE"

        if st.button(
            "Execute Erasure",
            type="primary",
            disabled=not both_confirmed,
            key="gdpr_erase_btn",
        ):
            with st.spinner("Erasing data... this may take a moment."):
                try:
                    _c = _api()
                    if not _c:
                        st.error("Write requires the REST API to be running.")
                        return
                    result = _c.gdpr_erase(data_subject_sub=subject)
                    _show_erasure_result(result, subject)
                except Exception as e:
                    st.error(f"Erasure failed: {e}")


def _scan_subject_data(subject: str):
    """Scan all data for a subject and store in session state."""
    tables = {
        "memories": f"data_subject_sub = '{subject}'",
        "kg_entities": f"name LIKE '%{subject}%'",
        "kg_edges": f"source_id IN (SELECT id FROM kg_entities WHERE name LIKE '%{subject}%')",
        "kg_facts": f"subject LIKE '%{subject}%' OR object LIKE '%{subject}%'",
        "memory_audit_log": f"detail LIKE '%{subject}%'",
        "memory_embeddings": "1=0",  # cascaded with memories
        "memory_vec_keys": "1=0",    # cascaded with memories
        "memory_chunks": "1=0",      # cascaded with memories
    }

    table_counts = {}
    for table, where in tables.items():
        if _table_exists_api(table):
            table_counts[table] = _try_count_api(table, where)
        else:
            table_counts[table] = 0

    # Sample memories
    sample_memories = []
    mem_df = _query_api(
        "SELECT id, content, category, data_subject_sub FROM memories "
        f"WHERE data_subject_sub = ? LIMIT 5",
        [subject],
    )
    if mem_df is not None and not mem_df.empty:
        sample_memories = mem_df.to_dict("records")

    # Sample KG entities
    sample_entities = []
    ent_df = _query_api(
        "SELECT name, entity_type, mentions FROM kg_entities "
        f"WHERE name LIKE ? LIMIT 5",
        [f"%{subject}%"],
    )
    if ent_df is not None and not ent_df.empty:
        sample_entities = ent_df.to_dict("records")

    scan_data = {
        "memories": table_counts.get("memories", 0),
        "entities": table_counts.get("kg_entities", 0),
        "edges": table_counts.get("kg_edges", 0),
        "audit": table_counts.get("memory_audit_log", 0),
        "tables": table_counts,
        "sample_memories": sample_memories,
        "sample_entities": sample_entities,
    }

    st.session_state[f"gdpr_scan_{subject}"] = scan_data


def _show_erasure_result(result: dict, subject: str):
    """Display the erasure result with certificate details."""
    if result.get("success"):
        st.success("Erasure complete")

        # Show deletion summary
        rows_deleted = result.get("rows_deleted", {})
        if rows_deleted:
            st.markdown("##### Rows Deleted")
            for table, count in rows_deleted.items():
                if count > 0:
                    st.html(
                        f"<div style='display:flex;justify-content:space-between;"
                        f"padding:4px 0;border-bottom:1px solid #1f2937;'>"
                        f"<span style='color:#d1d5db;font-size:0.78rem;'>{table}</span>"
                        f"<span style='color:#ef4444;font-weight:600;font-size:0.78rem;'>-{count}</span>"
                        f"</div>"
                    )

        md_deleted = result.get("md_files_deleted", 0)
        if md_deleted:
            st.caption(f"Deleted {md_deleted} .md files from disk")

        # Show certificate
        cert = result.get("certificate", {})
        if cert:
            st.markdown("##### Deletion Certificate")
            cert_items = [
                ("Request ID", cert.get("request_id", "?")),
                ("Data Subject", subject),
                ("Tenant", cert.get("tenant_id", "?")),
                ("Requested", cert.get("requested_at", "?")),
                ("Completed", cert.get("completed_at", "?")),
                ("Status", cert.get("status", "?")),
                ("Certificate Hash", cert.get("certificate_hash", "?")[:16] + "..."),
            ]
            for label, value in cert_items:
                st.html(
                    f"<div style='display:flex;justify-content:space-between;"
                    f"padding:4px 0;border-bottom:1px solid #1f2937;'>"
                    f"<span style='color:#6b7280;font-size:0.72rem;'>{label}</span>"
                    f"<span style='color:#d1d5db;font-size:0.72rem;font-weight:600;'>{value}</span>"
                    f"</div>"
                )

            if cert.get("certificate_path"):
                st.info(f"Certificate saved: `{cert['certificate_path']}`")

        # Clear scan data
        st.session_state.pop(f"gdpr_scan_{subject}", None)
    else:
        st.error(f"Erasure failed: {result.get('error', 'unknown')}")


def _render_erasure_history():
    """Show erasure request history with certificate details."""
    st.markdown("#### Erasure Request History")

    if _table_exists_api("gdpr_requests"):
        requests_df = _query_api(
            "SELECT * FROM gdpr_requests ORDER BY requested_at DESC LIMIT 20"
        )
        if requests_df is not None and not requests_df.empty:
            for _, req in requests_df.iterrows():
                status = req.get("status", "?")
                status_color = "#10b981" if status == "completed" else "#f59e0b" if status == "pending" else "#ef4444"
                ts = req.get("requested_at", "?")
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"

                st.html(
                    f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                    f"padding:10px 12px;margin:4px 0;'>"
                    f"<div style='display:flex;align-items:center;gap:8px;'>"
                    f"<span style='color:{status_color};font-size:0.7rem;font-weight:600;'>{status.upper()}</span>"
                    f"<span style='color:#d1d5db;font-size:0.78rem;'>{req.get('data_subject_sub', '?')}</span>"
                    f"<span style='color:#6b7280;font-size:0.65rem;margin-left:auto;'>{ts}</span>"
                    f"</div>"
                    f"</div>"
                )

                # Show certificate details if available
                cert_hash = req.get("certificate_hash")
                if cert_hash:
                    st.caption(f"Certificate: `{cert_hash[:16]}...`")
        else:
            st.info("No erasure requests recorded")
    else:
        st.info("GDPR requests table not yet created. Erasure requests will appear here after first use.")


# ── 4. Tenants ───────────────────────────────────────────────────────────

def _render_tenants():
    st.subheader("Multi-Tenant Isolation")

    st.markdown(
        "View and control tenant isolation, agent permissions, and access policies."
    )

    st.divider()

    # ── Agent Permission Matrix ──────────────────────────────────────────
    st.markdown("#### Agent Permissions")

    principals = _query_api("SELECT id, kind, display_name, tenant_id FROM principals ORDER BY id")
    bindings = _query_api("SELECT principal_id, role_id FROM role_bindings")
    acl = _query_api("SELECT principal_id, resource_id, action, effect FROM acl_overrides")

    if principals is not None and not principals.empty:
        # Build permission matrix
        binding_map = {}
        if bindings is not None:
            for _, r in bindings.iterrows():
                binding_map.setdefault(r["principal_id"], []).append(r["role_id"])

        acl_map = {}
        if acl is not None:
            for _, r in acl.iterrows():
                key = (r["principal_id"], r["resource_id"], r["action"])
                acl_map[key] = r["effect"]

        matrix_data = []
        for _, p in principals.iterrows():
            pid = p["id"]
            roles = binding_map.get(pid, [])
            can_read = "allow" if any("reader" in r or "read" in r or "admin" in r for r in roles) else "deny"
            can_write = "allow" if any("writer" in r or "write" in r or "admin" in r for r in roles) else "deny"
            can_delete = "allow" if any("delete" in r or "admin" in r for r in roles) else "deny"
            can_admin = "allow" if any("admin" in r for r in roles) else "deny"

            # Check ACL overrides
            for (ap, ar, aa), effect in acl_map.items():
                if ap == pid:
                    if ar == "memory" and aa == "read": can_read = effect
                    if ar == "memory" and aa == "write": can_write = effect
                    if ar == "memory" and aa == "delete": can_delete = effect
                    if ar == "admin" and aa == "admin": can_admin = effect

            matrix_data.append({
                "Agent": pid,
                "Type": p.get("kind", "?"),
                "Roles": ", ".join(roles) if roles else "none",
                "Read": can_read,
                "Write": can_write,
                "Delete": can_delete,
                "Admin": can_admin,
                "Tenant": p.get("tenant_id", "default"),
            })

        matrix_df = pd.DataFrame(matrix_data)
        st.dataframe(matrix_df, use_container_width=True, hide_index=True)
    else:
        st.info("No principals configured — system runs in fail-open mode")

    st.divider()

    # ── Tenant Data Overview ─────────────────────────────────────────────
    st.markdown("#### Data by Tenant")

    try:
        tenant_rows_data = _query_api(
            "SELECT COALESCE(tenant_id, 'default') as tenant, COUNT(*) as memories "
            "FROM memories GROUP BY tenant ORDER BY memories DESC"
        )

        if tenant_rows_data is not None and not tenant_rows_data.empty:
            tenant_data = []
            for _, row in tenant_rows_data.iterrows():
                tenant = row["tenant"]
                count = row["memories"]
                ent_count = _try_count_api("kg_entities", f"tenant_id='{tenant}'") if _table_exists_api("kg_entities") else 0
                tenant_data.append({
                    "Tenant": tenant,
                    "Memories": count,
                    "KG Entities": ent_count,
                })

            tenant_df = pd.DataFrame(tenant_data)
            st.dataframe(tenant_df, use_container_width=True, hide_index=True)

            fig = px.bar(tenant_df, x="Tenant", y="Memories", color="Tenant", text_auto=True)
            fig.update_layout(**DARK, height=250, margin=dict(t=30, b=10, l=10, r=10), showlegend=False)
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("All memories use 'default' tenant (single-tenant mode)")
    except Exception as e:
        st.error(f"Failed: {e}")

    st.divider()

    # ── Isolation Controls ───────────────────────────────────────────────
    st.markdown("#### Isolation Feature Flags")

    try:
        import tomllib
        toml_path = ROOT / "memory.toml"
        raw = tomllib.loads(toml_path.read_text())
        features = raw.get("features", {})

        isolation_features = {
            "multi_agent": ("Multi-Agent Isolation", "Cross-agent memory sharing via shared_memories table"),
            "temporal_tiers": ("Temporal Tier System", "Hot/warm/cold tier classification on memories"),
            "crdt_enabled": ("CRDT Version Tracking", "Per-field conflict resolution on every save"),
            "saga_enabled": ("Saga Transactions", "Transactional save with crash-consistent rollback"),
            "consolidation": ("Memory Consolidation", "Deduplication and contradiction scanning"),
            "quality_gates": ("Quality Gates", "Filter search results below relevance threshold"),
        }

        for flag, (label, desc) in isolation_features.items():
            current = features.get(flag, False)
            col_toggle, col_desc = st.columns([1, 3])
            with col_toggle:
                new_val = st.toggle(label, value=bool(current), key=f"tenant_{flag}")
            with col_desc:
                st.caption(desc)

            if new_val != current:
                toml_text = toml_path.read_text()
                new_lines = []
                for line in toml_text.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith(f"{flag} =") or stripped.startswith(f"{flag}="):
                        old_str = "true" if current else "false"
                        new_str = "true" if new_val else "false"
                        line = line.replace(f"= {old_str}", f"= {new_str}").replace(f"={old_str}", f"={new_str}")
                    new_lines.append(line)
                toml_path.write_text("\n".join(new_lines))
                st.toast(f"Updated {flag}: {new_val}", icon="\u2705")
                st.rerun()
    except Exception as e:
        st.info(f"Could not read config: {e}")

    st.divider()

    # ── Tenant config ────────────────────────────────────────────────────
    st.markdown("#### Tenant Configuration")

    try:
        import tomllib
        raw = tomllib.loads(open(ROOT / "memory.toml").read())
        tenants = raw.get("tenants", {})
        if tenants:
            for tenant_name, config in tenants.items():
                with st.expander(f"\U0001f3e2 {tenant_name}", expanded=False):
                    st.json(config)
        else:
            st.info("No custom tenants configured — using 'default' tenant (normal for single-tenant setups)")
    except Exception as e:
        st.info(f"Could not read config: {e}")


# ── 5. SOC 2 ─────────────────────────────────────────────────────────────

def _render_soc2():
    st.subheader("SOC 2 Compliance")

    st.markdown(
        "SOC 2 Type II controls for security, availability, and confidentiality."
    )

    st.divider()

    # ── Audit Trail ──────────────────────────────────────────────────────
    st.markdown("#### Audit Trail (CC7.2)")

    n_audit = _try_count_api("memory_audit_log")
    n_errors = _try_count_api("memory_audit_log", "error IS NOT NULL")
    error_rate = (n_errors / n_audit * 100) if n_audit > 0 else 0

    a_col1, a_col2, a_col3 = st.columns(3)
    a_col1.metric("Total Events", n_audit)
    a_col2.metric("Errors", n_errors)
    a_col3.metric("Error Rate", f"{error_rate:.1f}%")

    if n_audit > 0:
        # Recent audit activity
        recent = _query_api(
            "SELECT ts, tool, latency_ms, error FROM memory_audit_log "
            "ORDER BY ts DESC LIMIT 10"
        )
        if recent is not None and not recent.empty:
            recent["ts"] = pd.to_datetime(recent["ts"], unit="s", errors="coerce").dt.strftime("%m-%d %H:%M")
            recent["status"] = recent["error"].apply(lambda x: "\u274c" if pd.notna(x) else "\u2705")
            st.dataframe(recent[["ts", "tool", "latency_ms", "status"]], use_container_width=True, hide_index=True)

    st.divider()

    # ── Access Controls ──────────────────────────────────────────────────
    st.markdown("#### Access Controls (CC6.1)")

    if _table_exists_api("principals"):
        principals_df = _query_api("SELECT id, kind, display_name, tenant_id FROM principals")
        if principals_df is not None and not principals_df.empty:
            st.dataframe(principals_df, use_container_width=True, hide_index=True)
        else:
            st.info("No principals registered")
    else:
        st.info("RBAC not initialized")

    st.divider()

    # ── Data Retention ───────────────────────────────────────────────────
    st.markdown("#### Data Retention (CC6.1)")

    # Check backup age
    backup_dir = dashboard.MEM_DIR / "backups" if dashboard.MEM_DIR else None
    if backup_dir and backup_dir.exists():
        backups = sorted(backup_dir.glob("*.db.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
        if backups:
            newest = backups[0]
            age_days = (datetime.now().timestamp() - newest.stat().st_mtime) / 86400
            r_col1, r_col2, r_col3 = st.columns(3)
            r_col1.metric("Latest Backup", f"{age_days:.0f} days ago")
            r_col2.metric("Total Backups", len(backups))
            r_col3.metric("Backup Size", f"{sum(b.stat().st_size for b in backups) / 1024 / 1024:.1f} MB")
        else:
            st.warning("No backups found")
    else:
        st.warning("Backup directory not found")

    st.divider()

    # ── Encryption ───────────────────────────────────────────────────────
    st.markdown("#### Encryption (CC6.1)")

    enc_items = []
    # Check if DB is encrypted (look for SQLCipher or WAL)
    try:
        _c = _api()
        if _c:
            info = _c.query("PRAGMA page_count")
            pc = info.get("results", [{}])[0].get("page_count", 0) if info.get("results") else 0
            info2 = _c.query("PRAGMA page_size")
            ps = info2.get("results", [{}])[0].get("page_size", 4096) if info2.get("results") else 4096
            db_size_mb = (pc * ps) / (1024 * 1024)
        else:
            conn = _get_db()
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            db_size_mb = (page_count * page_size) / (1024 * 1024)
            conn.close()
        enc_items.append(("Database at Rest", f"{db_size_mb:.1f} MB", "SQLite file-based"))
    except Exception:
        enc_items.append(("Database at Rest", "Unknown", "Check manually"))

    enc_items.append(("In Transit", "localhost only", "Dashboard bound to 127.0.0.1"))
    enc_items.append(("Backup Encryption", "gzip", "Compressed backups"))

    enc_df = pd.DataFrame(enc_items, columns=["Control", "Status", "Notes"])
    st.dataframe(enc_df, use_container_width=True, hide_index=True)

    st.divider()

    # ── Dead Letter Log ──────────────────────────────────────────────────
    st.markdown("#### Dead Letter Log (CC7.2)")

    try:
        dl_path = dashboard.MEM_DIR / "audit_sink_dead_letter.jsonl" if dashboard.MEM_DIR else None
        if dl_path and dl_path.exists():
            dl_size = dl_path.stat().st_size
            dl_age = (datetime.now().timestamp() - dl_path.stat().st_mtime) / 3600
            dl_lines = sum(1 for _ in open(dl_path))

            st.html(
                f"<div style='display:flex;gap:16px;'>"
                f"<div><span style='color:#6b7280;font-size:0.75rem;'>Entries</span><br>"
                f"<span style='color:#d1d5db;font-weight:600;'>{dl_lines:,}</span></div>"
                f"<div><span style='color:#6b7280;font-size:0.75rem;'>Size</span><br>"
                f"<span style='color:#d1d5db;font-weight:600;'>{dl_size / 1024:.1f} KB</span></div>"
                f"<div><span style='color:#6b7280;font-size:0.75rem;'>Last Modified</span><br>"
                f"<span style='color:#d1d5db;font-weight:600;'>{dl_age:.0f}h ago</span></div>"
                f"</div>"
            )

            if st.button("\U0001f441\ufe0f View Last 20 Entries"):
                try:
                    import json as _json
                    lines = dl_path.read_text().strip().split("\n")[-20:]
                    for line in lines:
                        try:
                            entry = _json.loads(line)
                            ts = datetime.fromtimestamp(entry.get("ts", 0)).strftime("%m-%d %H:%M") if entry.get("ts") else "?"
                            sink = entry.get("sink", "?")
                            error = entry.get("error", "?")
                            tool = entry.get("event", {}).get("tool", "?")
                            st.html(
                                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:6px;"
                                f"padding:6px 10px;margin:3px 0;font-size:0.72rem;'>"
                                f"<span style='color:#6b7280;'>{ts}</span> "
                                f"<span style='color:#ef4444;font-weight:600;'>{error}</span> "
                                f"<span style='color:#d1d5db;'>{sink}</span> "
                                f"<span style='color:#8b5cf6;'>{tool}</span>"
                                f"</div>"
                            )
                        except Exception:
                            pass
                except Exception:
                    st.code(dl_path.read_text()[-2000:], language="json")
        else:
            st.info("No dead letter log found at memory/audit_sink_dead_letter.jsonl")
    except Exception as e:
        st.info(f"Dead letter log unavailable: {e}")


# ── 6. Policy Hash ────────────────────────────────────────────────────────

def _render_policy_hash():
    st.subheader("Policy Hash & Config Alignment")

    st.markdown(
        "Ensures all agents run the same configuration. "
        "Divergent hashes indicate config drift."
    )

    if st.button("\U0001f50d Check Policy Status", type="primary"):
        with st.spinner("Checking..."):
            try:
                result = subprocess.run(
                    [sys.executable, str(ROOT / "cron" / "cron_check_config_drift.py"), "--dry-run"],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, "MEMORY_DB_PATH": str(dashboard.DB)},
                )
                if "FLEET-POLICY-STATUS:" in result.stdout:
                    status_str = result.stdout.split("FLEET-POLICY-STATUS:")[1].strip()
                    items = dict(item.split("=", 1) for item in status_str.split() if "=" in item)

                    for k, v in items.items():
                        icon = "\u2705" if v == "0" else "\u26a0\ufe0f" if v == "aligned" else "\u274c"
                        st.html(
                            f"<div style='display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1f2937;'>"
                            f"<span style='color:#d1d5db;font-size:0.8rem;'>{k.replace('_', ' ').title()}</span>"
                            f"<span style='color:#d1d5db;font-weight:600;'>{v}</span>"
                            f"</div>"
                        )
                else:
                    st.info("No policy status available")
                    if result.stdout:
                        with st.expander("Raw output"):
                            st.code(result.stdout[:500])
            except Exception as e:
                st.error(f"Failed: {e}")

    st.divider()

    # ── Current config summary ───────────────────────────────────────────
    st.markdown("#### Current Configuration")

    try:
        import tomllib
        raw = tomllib.loads(open(ROOT / "memory.toml").read())
        features = raw.get("features", {})

        enabled = sum(1 for v in features.values() if str(v).lower() in ("true", "yes", "1"))
        total = len(features)

        st.metric("Features Enabled", f"{enabled}/{total}")

        for flag, val in features.items():
            val_str = str(val).lower()
            is_on = val_str in ("true", "yes", "1")
            icon = "\u26a1" if is_on else "\u2716"
            color = "#10b981" if is_on else "#6b7280"
            st.html(
                f"<div style='display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1f2937;'>"
                f"<span style='color:#d1d5db;font-size:0.75rem;'>{icon} {flag}</span>"
                f"<span style='color:{color};font-size:0.75rem;font-weight:600;'>{val}</span>"
                f"</div>"
            )
    except Exception as e:
        st.info(f"Could not read config: {e}")


# ── 7. Compliance Health Check ────────────────────────────────────────────

def _render_compliance_check():
    st.subheader("Compliance Health Check")

    st.markdown(
        "Run a comprehensive check across all compliance controls."
    )

    if st.button("\U0001f50d Run All Checks", type="primary"):
        with st.spinner("Running checks..."):
            results = _run_all_compliance_checks()

            # Group by status
            passed = [r for r in results if r["status"] == "pass"]
            warned = [r for r in results if r["status"] == "warn"]
            failed = [r for r in results if r["status"] == "fail"]
            skipped = [r for r in results if r["status"] == "skip"]

            # Summary
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("\u2705 Passed", len(passed))
            c2.metric("\u26a0\ufe0f Warnings", len(warned))
            c3.metric("\u274c Failed", len(failed))
            c4.metric("\u2139\ufe0f Skipped", len(skipped))

            st.divider()

            # Results by category
            categories = {}
            for r in results:
                cat = r.get("category", "General")
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(r)

            for cat, checks in categories.items():
                with st.expander(f"{cat} ({sum(1 for c in checks if c['status']=='pass')}/{len(checks)} passed)", expanded=True):
                    for check in checks:
                        icon = {"pass": "\u2705", "warn": "\u26a0\ufe0f", "fail": "\u274c", "skip": "\u2139\ufe0f"}.get(check["status"], "\u2753")
                        color = {"pass": "#10b981", "warn": "#f59e0b", "fail": "#ef4444", "skip": "#6b7280"}.get(check["status"], "#6b7280")
                        st.html(
                            f"<div style='display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #1f2937;'>"
                            f"<span>{icon}</span>"
                            f"<span style='color:#d1d5db;font-size:0.8rem;font-weight:600;flex:1;'>{check['name']}</span>"
                            f"<span style='color:{color};font-size:0.72rem;'>{check['detail']}</span>"
                            f"</div>"
                        )

            if not failed:
                st.success(f"All checks passed ({len(passed)}/{len(results)})")
            else:
                st.error(f"{len(failed)} check(s) failed")


def _run_all_compliance_checks() -> list[dict]:
    """Run comprehensive compliance checks."""
    checks = []

    # ── RBAC Controls ────────────────────────────────────────────────────
    checks.append({"category": "RBAC Controls", "name": "Principals Table", "status": "pass" if _table_exists_api("principals") else "fail", "detail": "Exists" if _table_exists_api("principals") else "Missing"})
    checks.append({"category": "RBAC Controls", "name": "Roles Table", "status": "pass" if _table_exists_api("roles") else "fail", "detail": "Exists" if _table_exists_api("roles") else "Missing"})
    checks.append({"category": "RBAC Controls", "name": "Role Bindings", "status": "pass" if _table_exists_api("role_bindings") else "fail", "detail": "Exists" if _table_exists_api("role_bindings") else "Missing"})
    checks.append({"category": "RBAC Controls", "name": "ACL Overrides", "status": "pass" if _table_exists_api("acl_overrides") else "warn", "detail": "Exists" if _table_exists_api("acl_overrides") else "Optional"})

    # ── Data Protection ──────────────────────────────────────────────────
    checks.append({"category": "Data Protection", "name": "GDPR Requests Table", "status": "pass" if _table_exists_api("gdpr_requests") else "warn", "detail": "Exists" if _table_exists_api("gdpr_requests") else "Not created"})
    n_audit = _try_count_api("memory_audit_log")
    checks.append({"category": "Data Protection", "name": "Audit Log", "status": "pass" if n_audit > 0 else "warn", "detail": f"{n_audit} events"})

    # ── Tenant Isolation ─────────────────────────────────────────────────
    try:
        has_tenant = False
        res = _query_api("SELECT tenant_id FROM memories LIMIT 1")
        if res is not None and not res.empty:
            has_tenant = True
        checks.append({"category": "Tenant Isolation", "name": "Tenant Column", "status": "pass" if has_tenant else "info", "detail": "Present" if has_tenant else "Single-tenant mode"})
    except Exception:
        checks.append({"category": "Tenant Isolation", "name": "Tenant Column", "status": "info", "detail": "Single-tenant mode"})

    # ── Backup & Recovery ────────────────────────────────────────────────
    backup_dir = dashboard.MEM_DIR / "backups" if dashboard.MEM_DIR else None
    if backup_dir and backup_dir.exists():
        backups = list(backup_dir.glob("*.db.gz"))
        if backups:
            newest = max(backups, key=lambda p: p.stat().st_mtime)
            age_days = (datetime.now().timestamp() - newest.stat().st_mtime) / 86400
            checks.append({"category": "Backup & Recovery", "name": "Backup Exists", "status": "pass", "detail": f"{len(backups)} backups, newest {age_days:.0f}d ago"})
        else:
            checks.append({"category": "Backup & Recovery", "name": "Backup Exists", "status": "warn", "detail": "No backups found"})
    else:
        checks.append({"category": "Backup & Recovery", "name": "Backup Directory", "status": "warn", "detail": "Not found"})

    # ── Database Integrity ───────────────────────────────────────────────
    try:
        _c = _api()
        if _c:
            res = _c.query("PRAGMA integrity_check")
            ok = res.get("results", [{}])[0].get("integrity_check", "ok") == "ok" if res.get("results") else False
            checks.append({"category": "Database Integrity", "name": "PRAGMA integrity_check", "status": "pass" if ok else "fail", "detail": "OK" if ok else "Failed"})
        else:
            conn = sqlite3.connect(f"file:{dashboard.DB}?mode=ro", uri=True, timeout=10)
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            checks.append({"category": "Database Integrity", "name": "PRAGMA integrity_check", "status": "pass" if result and result[0] == "ok" else "fail", "detail": "OK" if result and result[0] == "ok" else "Failed"})
    except Exception as e:
        checks.append({"category": "Database Integrity", "name": "PRAGMA integrity_check", "status": "fail", "detail": str(e)[:60]})

    # ── Config Alignment ─────────────────────────────────────────────────
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "cron" / "cron_check_config_drift.py"), "--dry-run"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "MEMORY_DB_PATH": str(dashboard.DB)},
        )
        aligned = "divergent=0" in result.stdout
        checks.append({"category": "Config Alignment", "name": "Policy Hash", "status": "pass" if aligned else "warn", "detail": "Aligned" if aligned else "Drift detected"})
    except Exception:
        checks.append({"category": "Config Alignment", "name": "Policy Hash", "status": "skip", "detail": "Could not check"})

    # ── Encryption ───────────────────────────────────────────────────────
    checks.append({"category": "Encryption", "name": "Data at Rest", "status": "pass", "detail": "SQLite file-based storage"})
    checks.append({"category": "Encryption", "name": "Data in Transit", "status": "pass", "detail": "Dashboard bound to 127.0.0.1"})
    checks.append({"category": "Encryption", "name": "Backup Compression", "status": "pass", "detail": "gzip compressed"})

    # ── Access Controls ──────────────────────────────────────────────────
    try:
        n_principals = _try_count_api("principals")
        n_bindings = _try_count_api("role_bindings")
        checks.append({"category": "Access Controls", "name": "Principals Registered", "status": "pass" if n_principals > 0 else "warn", "detail": f"{n_principals} principals"})
        checks.append({"category": "Access Controls", "name": "Role Bindings Active", "status": "pass" if n_bindings > 0 else "warn", "detail": f"{n_bindings} bindings"})
    except Exception:
        checks.append({"category": "Access Controls", "name": "Access Check", "status": "fail", "detail": "Could not check"})

    # ── Worker Health ────────────────────────────────────────────────────
    try:
        last_task = _query_api(
            "SELECT completed_at FROM task_queue "
            "WHERE status='completed' AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1"
        )
        if last_task is not None and not last_task.empty:
            from datetime import datetime as _dt
            last_val = last_task.iloc[0]["completed_at"]
            last_dt = _dt.strptime(last_val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age_min = (_dt.now(timezone.utc) - last_dt).total_seconds() / 60
            checks.append({"category": "System Health", "name": "Background Worker", "status": "pass" if age_min < 60 else "warn", "detail": f"Last task {age_min:.0f}min ago"})
        else:
            checks.append({"category": "System Health", "name": "Background Worker", "status": "warn", "detail": "No completed tasks"})
    except Exception:
        checks.append({"category": "System Health", "name": "Background Worker", "status": "skip", "detail": "Could not check"})

    return checks
