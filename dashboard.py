#!/usr/bin/env python3
# Agentic Memory — Local Agent Memory Dashboard.
#
# Run:
#     cd ~/.config/agentic-memory
#     venv/bin/streamlit run dashboard.py
import logging
import os

import streamlit as st

import dashboard  # noqa: E402
from dashboard import CSS, TABS, resolve_db
from dashboard.sidebar import render_sidebar
from dashboard.tab_dashboard import render_dashboard
from dashboard.tab_memories import render_memories
from dashboard.tab_knowledge import render_knowledge
from dashboard.tab_quality import render_quality
from dashboard.tab_operations import render_operations
from dashboard.tab_compliance import render_compliance
from dashboard.tab_coordination import render_coordination
from dashboard.tab_audit import render_audit
from dashboard.tab_settings import render_settings

logger = logging.getLogger(__name__)

# ── Page config ──────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Agentic Memory",
    page_icon="\U0001f9e0",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Theme CSS ────────────────────────────────────────────────────────────
st.html(CSS)

# ── DB init ──────────────────────────────────────────────────────────────
import dashboard as _dk

_dk.DB = resolve_db()
if not _dk.DB.exists():
    st.error(f"Database not found: {_dk.DB}")
    st.stop()

_dk.MEM_DIR = _dk.DB.parent

# ── API Client ────────────────────────────────────────────────────────────
from dashboard.api_client import ApiClient

base_url = os.environ.get("MEMORY_API_BASE", "http://127.0.0.1:9878")
token = os.environ.get("MEMORY_API_TOKEN", "")

if "api_client" not in st.session_state:
    st.session_state.api_client = ApiClient(base_url=base_url, token=token)

# ── Phase 2: login gate ────────────────────────────────────────────────────
# When no static API token is configured, the operator must sign in to obtain
# a JWT session cookie. Operators who set MEMORY_API_TOKEN keep the legacy
# bearer behaviour and skip this page.
from dashboard.login import render_login, requires_login

if not st.session_state.get("authenticated") and requires_login():
    render_login(base_url)
    st.stop()

# ── Sidebar ──────────────────────────────────────────────────────────────
render_sidebar()

# ── Tabs (9 purpose-driven) ─────────────────────────────────────────────
(
    dashboard_tab,
    memories_tab,
    knowledge_tab,
    quality_tab,
    operations_tab,
    compliance_tab,
    coordination_tab,
    audit_tab,
    settings_tab,
) = st.tabs(TABS)

# ═══════════════════════════════════════════════════════════════════════════
# DASHBOARD (Overview + Health + Activity + Command Palette)
# ═══════════════════════════════════════════════════════════════════════════
with dashboard_tab:
    render_dashboard()

# ═══════════════════════════════════════════════════════════════════════════
# MEMORIES (Browse + Search + Edit + Create)
# ═══════════════════════════════════════════════════════════════════════════
with memories_tab:
    render_memories()

# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE (KG + Facts + Embeddings)
# ═══════════════════════════════════════════════════════════════════════════
with knowledge_tab:
    render_knowledge()

# ═══════════════════════════════════════════════════════════════════════════
# QUALITY (Quality Center + Staleness + Impact + Timeline + Merges + Sandbox + Gaps)
# ═══════════════════════════════════════════════════════════════════════════
with quality_tab:
    render_quality()

# ═══════════════════════════════════════════════════════════════════════════
# OPERATIONS (Cron + Backups + Multi-Agent + Runbook)
# ═══════════════════════════════════════════════════════════════════════════
with operations_tab:
    render_operations()

# ═══════════════════════════════════════════════════════════════════════════
# COMPLIANCE (RBAC + ACL + GDPR + Tenants + Policy + Audit Check)
# ═══════════════════════════════════════════════════════════════════════════
with compliance_tab:
    render_compliance()

# ═══════════════════════════════════════════════════════════════════════════
# COORDINATION (Tasks + File Locks + Messaging + Project State)
# ═══════════════════════════════════════════════════════════════════════════
with coordination_tab:
    render_coordination()

# ═══════════════════════════════════════════════════════════════════════════
# AUDIT (Full audit log + Performance)
# ═══════════════════════════════════════════════════════════════════════════
with audit_tab:
    render_audit()

# ═══════════════════════════════════════════════════════════════════════════
# SETTINGS (Feature Flags + System Info + Onboarding + Export)
# ═══════════════════════════════════════════════════════════════════════════
with settings_tab:
    render_settings()
