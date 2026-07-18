#!/usr/bin/env python3
# Agentic Memory — Local Agent Memory Dashboard.
#
# Run:
#     cd ~/.config/agentic-memory
#     venv/bin/streamlit run dashboard.py
import logging

import streamlit as st

import dashboard  # noqa: E402
from dashboard import CSS, DARK, TABS, resolve_db
from dashboard.sidebar import render_sidebar
from dashboard.tabs import (
    render_audit_log,
    render_backups,
    render_benchmarks,
    render_concept_drift,
    render_cron,
    render_ctr_feedback,
    render_embeddings,
    render_explorer,
    render_facts,
    render_health,
    render_knowledge_graph,
    render_memories,
    render_multi_agent,
    render_overview,
)

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

# ── Sidebar ──────────────────────────────────────────────────────────────
render_sidebar()

# ── Tabs ─────────────────────────────────────────────────────────────────
(
    overview_tab,
    memories_tab,
    kg_tab,
    embed_tab,
    facts_tab,
    drift_tab,
    ctr_tab,
    benchmarks_tab,
    cron_tab,
    multi_agent_tab,
    health_tab,
    backups_tab,
    audit_tab,
    search_tab,
) = st.tabs(TABS)

# ═══════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════
with overview_tab:
    render_overview()

# ═══════════════════════════════════════════════════════════════════════════
# MEMORIES (table)
# ═══════════════════════════════════════════════════════════════════════════
with memories_tab:
    render_memories()

# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ═══════════════════════════════════════════════════════════════════════════
with kg_tab:
    render_knowledge_graph()

# ═══════════════════════════════════════════════════════════════════════════
# EMBEDDINGS
# ═══════════════════════════════════════════════════════════════════════════
with embed_tab:
    render_embeddings()

# ═══════════════════════════════════════════════════════════════════════════
# FACTS SEARCH
# ═══════════════════════════════════════════════════════════════════════════
with facts_tab:
    render_facts()

# ═══════════════════════════════════════════════════════════════════════════
# CONCEPT DRIFT
# ═══════════════════════════════════════════════════════════════════════════
with drift_tab:
    render_concept_drift()

# ═══════════════════════════════════════════════════════════════════════════
# CTR FEEDBACK
# ═══════════════════════════════════════════════════════════════════════════
with ctr_tab:
    render_ctr_feedback()

# ═══════════════════════════════════════════════════════════════════════════
# BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════
with benchmarks_tab:
    render_benchmarks()

# ═══════════════════════════════════════════════════════════════════════════
# CRON
# ═══════════════════════════════════════════════════════════════════════════
with cron_tab:
    render_cron()

# ═══════════════════════════════════════════════════════════════════════════
# MULTI-AGENT SYNC
# ═══════════════════════════════════════════════════════════════════════════
with multi_agent_tab:
    render_multi_agent()

# ═══════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════
with health_tab:
    render_health()

# ═══════════════════════════════════════════════════════════════════════════
# BACKUPS
# ═══════════════════════════════════════════════════════════════════════════
with backups_tab:
    render_backups()

# ═══════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════
with audit_tab:
    render_audit_log()

# ═══════════════════════════════════════════════════════════════════════════
# EXPLORER
# ═══════════════════════════════════════════════════════════════════════════
with search_tab:
    render_explorer()
