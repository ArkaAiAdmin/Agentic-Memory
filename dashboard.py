#!/usr/bin/env python3
from __future__ import annotations

import logging
logger = logging.getLogger(__name__)
"""Agentic Memory — Local Agent Memory Dashboard.

Run:
    cd ~/.config/agentic-memory
    venv/bin/streamlit run dashboard.py
"""
import html
import json
import struct
import sqlite3
import time
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

# ── Page config ──────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Agentic Memory",
    page_icon="\U0001fa84",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Theme CSS ────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    .main > div { padding: 0 1rem; }
    .stApp { background: #0e1117; }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #0e1117; }
    ::-webkit-scrollbar-thumb { background: #2d3139; border-radius: 3px; }

    h1, h2, h3 { color: #f0f2f6 !important; font-weight: 600 !important; }

    /* Metric cards */
    .metric-card {
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-radius: 12px;
        padding: 1.2rem;
        text-align: center;
        transition: border-color 0.2s;
    }
    .metric-card:hover { border-color: #4b5563; }
    .metric-card .label {
        color: #8b8fa3;
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .metric-card .value {
        color: #f0f2f6;
        font-size: 2rem;
        font-weight: 700;
        line-height: 1.2;
    }
    .metric-card .sub {
        color: #6b7280;
        font-size: 0.7rem;
    }

    /* Status badges */
    .badge-ok {
        display: inline-block;
        background: #064e3b;
        color: #6ee7b7;
        font-size: 0.65rem;
        font-weight: 600;
        padding: 0.15rem 0.5rem;
        border-radius: 999px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .badge-warn {
        display: inline-block;
        background: #5c3d00;
        color: #fbbf24;
        font-size: 0.65rem;
        font-weight: 600;
        padding: 0.15rem 0.5rem;
        border-radius: 999px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .badge-err {
        display: inline-block;
        background: #4c0519;
        color: #fca5a5;
        font-size: 0.65rem;
        font-weight: 600;
        padding: 0.15rem 0.5rem;
        border-radius: 999px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }

    /* Status dot for cron */
    .dot-green { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #10b981; margin-right: 6px; }
    .dot-red { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #ef4444; margin-right: 6px; }
    .dot-yellow { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #f59e0b; margin-right: 6px; }
    .dot-gray { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #4b5563; margin-right: 6px; }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] { gap: 3px; overflow-x: auto; flex-wrap: nowrap; padding: 0 0 2px 0; }
    .stTabs [data-baseweb="tab"] {
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-bottom: 2px solid #2d3139;
        border-radius: 8px 8px 0 0;
        padding: 0.35rem 0.85rem;
        color: #6b7280;
        font-size: 0.72rem;
        font-weight: 600;
        white-space: nowrap;
        letter-spacing: 0.02em;
        transition: all 0.2s ease;
        margin-bottom: -2px;
    }
    .stTabs [data-baseweb="tab"]:hover {
        background: #22262e;
        color: #d1d5db;
        border-color: #4b5563;
    }
    .stTabs [aria-selected="true"] {
        background: #0e1117 !important;
        color: #f0f2f6 !important;
        border: 1px solid #4b5563;
        border-bottom: 2px solid #8b5cf6;
        box-shadow: 0 -2px 8px rgba(139,92,246,0.15);
    }

    blockquote {
        border-left: 3px solid #2d3139;
        padding: 0.5rem 1rem;
        background: #1a1d23;
        border-radius: 0 8px 8px 0;
    }

    /* Dark inputs */
    .stTextInput input { background: #1a1d23; color: #f0f2f6; border: 1px solid #2d3139; }
    .stSelectbox div[data-baseweb="select"] { background: #1a1d23; }
    .stSlider [data-baseweb="slider"] { margin-top: 0.5rem; }
    div[data-testid="stDataFrame"] { background: #1a1d23; }
    div[data-testid="stDataFrame"] td { color: #d1d5db; }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: #0a0b0e !important;
        border-right: 1px solid #1f2937;
    }
    section[data-testid="stSidebar"] .stApp { background: #0a0b0e; }
    [data-testid="stSidebar"] hr { margin: 0.4rem 0; border-color: #1f2937; }
    [data-testid="stSidebar"] h3 {
        color: #e5e7eb !important;
        font-size: 0.85rem !important;
        font-weight: 700 !important;
        letter-spacing: -0.01em;
    }
    [data-testid="stSidebar"] .stMetric {
        background: transparent;
        border: none;
        padding: 0.1rem 0;
    }
    [data-testid="stSidebar"] .stMetric label {
        color: #6b7280 !important;
        font-size: 0.55rem !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        letter-spacing: 0.06em;
    }
    [data-testid="stSidebar"] .stMetric [data-testid="stMetricValue"] {
        color: #e5e7eb !important;
        font-size: 0.95rem !important;
        font-weight: 700;
        line-height: 1.2;
    }
    [data-testid="stSidebar"] .stButton button {
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-radius: 6px;
        color: #9ca3af;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 0.25rem 0.5rem;
        transition: all 0.12s;
    }
    [data-testid="stSidebar"] .stButton button:hover {
        background: #2d3139;
        border-color: #6366f1;
        color: #f0f2f6;
    }
    [data-testid="stSidebar"] .st-emotion-cache-16idsys p {
        font-size: 0.65rem;
        color: #6b7280;
    }

    /* Life & motion */
    @keyframes pulse-glow {
        0%, 100% { box-shadow: 0 0 0 0 rgba(139,92,246,0); }
        50% { box-shadow: 0 0 6px 2px rgba(139,92,246,0.15); }
    }
    .stTabs [aria-selected="true"] {
        animation: pulse-glow 3s ease-in-out infinite;
    }
    .stApp {
        transition: background 0.3s ease;
    }
    .stButton button {
        transition: all 0.15s ease, transform 0.1s ease;
    }
    .stButton button:active {
        transform: scale(0.97);
    }
    .st-emotion-cache-1yiq2ps {
        transition: opacity 0.2s ease;
    }
    .metric-card {
        transition: transform 0.15s ease, border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .metric-card:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }

    /* Small icon buttons */
    button[title="Rerun page"] {
        min-height: 24px !important;
        height: 28px !important;
        padding: 0 0.4rem !important;
        font-size: 0.8rem !important;
        line-height: 1 !important;
    }

    /* Card container */
    .card {
        background: #16191f;
        border: 1px solid #2d3139;
        border-radius: 10px;
        padding: 1rem;
        margin-bottom: 0.6rem;
    }
    .card-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 0.4rem;
    }
    .card-title {
        color: #f0f2f6;
        font-size: 0.85rem;
        font-weight: 600;
        margin: 0;
    }
    .card-sub {
        color: #6b7280;
        font-size: 0.72rem;
        margin: 0;
    }
    .card-body {
        color: #9ca3af;
        font-size: 0.78rem;
    }

    /* Progress bar */
    .progress-track {
        background: #1a1d23;
        border-radius: 4px;
        height: 6px;
        overflow: hidden;
        margin-top: 4px;
    }
    .progress-fill {
        height: 100%;
        border-radius: 4px;
        transition: width 0.4s ease;
    }

    /* Log viewer */
    .log-box {
        background: #0d0f12;
        border: 1px solid #2d3139;
        border-radius: 8px;
        padding: 0.7rem;
        overflow-x: auto;
        max-height: 220px;
        overflow-y: auto;
    }
    .log-box code {
        color: #9ca3af;
        font-size: 0.72rem;
        line-height: 1.5;
        white-space: pre;
    }
</style>
""",
    unsafe_allow_html=True,
)

# ── Plotly style ─────────────────────────────────────────────────────────
px.defaults.template = "plotly_dark"
DARK = dict(
    paper_bgcolor="#0e1117",
    plot_bgcolor="#0e1117",
    font_color="#d1d5db",
    title_font_color="#f0f2f6",
)


# ── DB resolution ───────────────────────────────────────────────────────
@st.cache_resource
def resolve_db() -> Path:
    from infra.infrastructure import resolve_active_memory_dir

    return resolve_active_memory_dir() / "memory.db"


DB = resolve_db()
if not DB.exists():
    st.error(f"Database not found: {DB}")
    st.stop()

MEM_DIR = DB.parent


@st.cache_resource
def _blob_weight(v):
    if isinstance(v, bytes) and len(v) == 4:
        try:
            return struct.unpack("<f", v)[0]
        except Exception as e:
            logger.warning("_blob_weight failed: %s", e)
            return 1.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 1.0


def get_conn():
    """Open a read‑only ephemeral connection. Never migrates the schema."""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def query(sql: str, params=()) -> pd.DataFrame | None:
    try:
        return pd.read_sql_query(sql, get_conn(), params=params)
    except Exception as e:
        logger.warning("query failed: %s", e)
        return None


def table(name: str) -> bool:
    try:
        r = (
            get_conn()
            .execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
            .fetchone()
        )
        return r is not None
    except Exception as e:
        logger.warning("table failed: %s", e)
        return False


def try_count(table_name: str, where: str | None = None) -> int:
    try:
        sql = f"SELECT COUNT(*) FROM {table_name}"
        if where:
            sql += f" WHERE {where}"
        r = get_conn().execute(sql).fetchone()
        return r[0] if r else 0
    except Exception as e:
        logger.warning("try_count failed: %s", e)
        return 0


def _live_health():
    checks = []
    conn = get_conn()
    try:
        r = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        checks.append(("memories", "ok" if r and r[0] > 0 else "error", f"{r[0]} notes"))
    except Exception as e:
        logger.warning("_live_health failed: %s", e)
        checks.append(("memories", "error", str(e)))
    for tbl in ("kg_entities", "kg_edges", "kg_facts", "memory_chunks", "memory_embeddings", "memory_audit_log", "backlinks"):
        try:
            r = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
            checks.append((tbl, "ok" if r and r[0] > 0 else "warning", f"{r[0]} rows"))
        except Exception as e:
            logger.warning("_live_health failed: %s", e)
            checks.append((tbl, "error", str(e)))

    # LTR model check
    ltr_model = Path(__file__).parent / "models" / "ltr" / "model.txt"
    if ltr_model.exists():
        size_kb = ltr_model.stat().st_size / 1024
        checks.append(("ltr_model", "ok", f"{size_kb:.0f} KB"))
    else:
        checks.append(("ltr_model", "warning", "not trained yet"))
    try:
        r = conn.execute("SELECT COUNT(*) FROM memories WHERE pinned=1").fetchone()
        checks.append(("pinned", "ok", f"{r[0]} notes"))
    except Exception as e:
        logger.warning("_live_health failed: %s", e)
    return {"ts": datetime.now(timezone.utc).isoformat(), "checks": checks}


def _render_memory_content(mid: str, expanded: bool = True):
    full = query("SELECT content FROM memories WHERE id=?", (mid,))
    if full is None or full.empty:
        st.info("Could not load full content")
        return
    content = full.iloc[0]["content"]
    meta = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            front = parts[1]
            body = parts[2].strip()
            for line in front.strip().split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip().strip('"').strip("'")
    title = ""
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("# ") and not s.startswith("##"):
            title = s[2:].strip()
            break
    meta.get("tags", "")
    with st.expander(f"**{title or mid}**", expanded=expanded):
        st.markdown(f"`{html.escape(str(mid))}`", unsafe_allow_html=True)
        cols = st.columns(4)
        if meta.get("created"):
            cols[0].caption(f"\U0001f4c5 {meta['created'][:10]}")
        if meta.get("tags"):
            raw_tags = meta["tags"].strip("[]").split(",") if meta["tags"].startswith("[") else [meta["tags"]]
            tags_clean = " ".join(t.strip().strip("\"").strip("'") for t in raw_tags[:5])
            cols[1].caption(f"\U0001f3f7 {tags_clean}")
        if meta.get("pinned") and meta["pinned"].lower() in ("true", "1"):
            cols[2].markdown("\U000f04d3 Pinned", unsafe_allow_html=True)
        if meta.get("importance"):
            cols[3].caption(f"Importance: {meta['importance']}")
        st.divider()
        st.markdown(body, unsafe_allow_html=True)


def _get_cron_logs() -> list[Path]:
    return sorted(MEM_DIR.glob("*.log"))


def _badge_html(severity: str, text: str) -> str:
    cls = {"ok": "badge-ok", "warning": "badge-warn", "failure": "badge-err", "error": "badge-err"}.get(
        severity, "badge-warn"
    )
    return f'<span class="{cls}">{text}</span>'


# ── Sidebar ─────────────────────────────────────────────────────────────
st.sidebar.markdown(
    "<h2 style='margin-bottom:0;color:#f0f2f6;font-weight:700;letter-spacing:-0.02em'>Agentic Memory</h2>",
    unsafe_allow_html=True,
)
st.sidebar.caption(
    f"`{DB.parent.name}`  \u00b7 "
    f"{DB.stat().st_size / 1024 / 1024:.0f} MB"
)

with st.sidebar:
    st.markdown("### System Overview")

    n_mem = try_count("memories")
    n_ent = try_count("kg_entities")
    n_edg = try_count("kg_edges")
    n_audit = try_count("memory_audit_log")
    n_pin = try_count("memories", "pinned=1")
    n_facts = try_count("kg_facts")

    c1, c2, c3 = st.columns(3)
    c1.metric("Memories", n_mem)
    c2.metric("Entities", n_ent)
    c3.metric("Facts", n_facts)

    c1.metric("Edges", n_edg)
    c2.metric("Pinned", n_pin)
    c3.metric("DB", f"{DB.stat().st_size / 1024 / 1024:.0f} MB")

    st.divider()

    st.caption("Quick Actions")
    if st.button("\u21bb Refresh Now", key="sidebar_refresh", width="stretch"):
        st.rerun()
    st.markdown(
        f"<div style='display:flex;align-items:center;gap:6px;margin-top:4px;'>"
        f"<span style='width:6px;height:6px;border-radius:50%;background:#10b981;animation:pulse-dot 2s ease-in-out infinite;display:inline-block;'></span>"
        f"<span style='color:#6b7280;font-size:0.6rem;'>live · {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.markdown("""
    <style>
    @keyframes pulse-dot {
        0%, 100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.5; transform: scale(0.8); }
    }
    </style>
    """, unsafe_allow_html=True)

# ── Helpers ──────────────────────────────────────────────────────────────

def _auto_refresh(interval_secs: int = 30) -> None:
    if st.button("↻", key="top_refresh", help="Rerun page"):
        st.rerun()


# ── Tabs ─────────────────────────────────────────────────────────────────
TABS = [
    "Overview",
    "Memories",
    "Knowledge Graph",
    "Embeddings",
    "Facts",
    "Concept Drift",
    "CTR Feedback",
    "Benchmarks",
    "Cron",
    "Multi-Agent",
    "Health",
    "Backups",
    "Audit Log",
    "Explorer",
]
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
    st.subheader("Overview")

    _auto_refresh()

    n_pin = try_count("memories", "pinned=1")
    n_chk = try_count("memory_chunks", "parent_id IS NOT NULL")
    n_emb = try_count("memory_embeddings")
    n_ctr = try_count("memory_ctr_feedback")
    n_dft = try_count("concept_drift")
    n_ops_today = try_count(
        "memory_audit_log",
        "DATE(ts,'unixepoch') = DATE('now')",
    )
    avg_lat = None
    try:
        r = get_conn().execute(
            "SELECT AVG(latency_ms) FROM memory_audit_log WHERE DATE(ts,'unixepoch') = DATE('now')"
        ).fetchone()
        avg_lat = round(r[0], 1) if r and r[0] else None
    except Exception as e:
        logger.warning("operation failed: %s", e)
    db_size_mb = DB.stat().st_size / 1024 / 1024

    n_mem = try_count("memories")

    cols = st.columns(8)
    for i, (label, val, sub) in enumerate(
        [
            ("Total", n_mem, "memory notes"),
            ("Pinned", n_pin, "hot memory"),
            ("Chunked", n_chk, "split notes"),
            ("DB Size", f"{db_size_mb:.0f} MB", "on disk"),
            ("Ops Today", n_ops_today, "MCP calls"),
            ("Avg Latency", f"{avg_lat or '?'} ms", "today"),
            ("Embeddings", n_emb, "vectorized"),
            ("CTR Impressions", n_ctr, "search results"),
        ]
    ):
        cols[i].markdown(
            f"<div class='metric-card'>"
            f"<div class='label'>{label}</div>"
            f"<div class='value'>{val}</div>"
            f"<div class='sub'>{sub}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── System health indicator ──
    n_entities = try_count("kg_entities")
    n_facts = try_count("kg_facts") if table("kg_facts") else 0
    n_sync = try_count("sync_log") if table("sync_log") else 0
    n_alarms = try_count("drift_alarms") if table("drift_alarms") else 0
    ltr_model = Path(__file__).parent / "models" / "ltr" / "model.txt"

    # Health score: 0-100
    health_score = 100
    if n_alarms > 0: health_score -= min(30, n_alarms * 5)
    if n_entities == 0: health_score -= 20
    if not ltr_model.exists(): health_score -= 10
    health_color = "#10b981" if health_score >= 80 else "#f59e0b" if health_score >= 60 else "#ef4444"
    health_label = "Healthy" if health_score >= 80 else "Needs Attention" if health_score >= 60 else "Critical"

    st.markdown(
        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:12px;padding:1rem 1.5rem;margin-bottom:1rem;display:flex;align-items:center;justify-content:space-between;'>"
        f"<div style='display:flex;align-items:center;gap:12px;'>"
        f"<div style='width:48px;height:48px;border-radius:50%;background:{health_color}22;display:flex;align-items:center;justify-content:center;font-size:1.5rem;'>"
        f"{'🟢' if health_score >= 80 else '🟡' if health_score >= 60 else '🔴'}"
        f"</div>"
        f"<div><div style='color:#f0f2f6;font-weight:700;font-size:1.1rem;'>System Health: {health_label}</div>"
        f"<div style='color:#6b7280;font-size:0.75rem;'>Score: {health_score}/100 · KG: {n_entities} entities · LTR: {'ready' if ltr_model.exists() else 'pending'}</div></div>"
        f"</div>"
        f"<div style='text-align:right;'><div style='color:{health_color};font-size:2rem;font-weight:700;'>{health_score}</div>"
        f"<div style='color:#6b7280;font-size:0.7rem;'>health score</div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Category pie + Tier bar ──
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### Notes by Category")
        df = query(
            "SELECT COALESCE(category,'uncategorized') cat, COUNT(*) cnt "
            "FROM memories GROUP BY cat ORDER BY cnt DESC"
        )
        if df is not None and not df.empty:
            fig = px.pie(
                df,
                names="cat",
                values="cnt",
                color_discrete_sequence=px.colors.sequential.Viridis_r,
            )
            fig.update_layout(
                **DARK,
                margin=dict(t=10, b=10, l=10, r=10),
                showlegend=True,
                legend=dict(font=dict(size=9)),
            )
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
            cmap = {
                "hot": "#ef4444",
                "warm": "#f59e0b",
                "cold": "#3b82f6",
                "unassigned": "#4b5563",
            }
            fig = px.bar(
                df,
                x="tier",
                y="cnt",
                color="tier",
                color_discrete_map=cmap,
                text_auto=True,
            )
            fig.update_layout(
                **DARK,
                showlegend=False,
                xaxis_title=None,
                yaxis_title="Count",
                margin=dict(t=10, b=10, l=10, r=10),
            )
            fig.update_traces(textposition="outside")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No tier data")

    # ── Creation timeline ──
    st.markdown("#### Daily Note Creation")
    df = query(
        "SELECT DATE(created_at) day, COUNT(*) cnt "
        "FROM memories GROUP BY day ORDER BY day"
    )
    if df is not None and len(df) > 1:
        df["day"] = pd.to_datetime(df["day"])
        fig = px.line(df, x="day", y="cnt", markers=True, line_shape="spline")
        fig.update_traces(
            line=dict(width=3, shape="spline", smoothing=1.3), marker=dict(size=6)
        )
        fig.update_layout(
            **DARK,
            xaxis_title=None,
            yaxis_title="Notes",
            margin=dict(t=30, b=10, l=10, r=10),
        )
        st.plotly_chart(fig, width="stretch")

    # ── Tags + Recent Activity ──
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### Top Tags")
        df = query("SELECT tags FROM memories WHERE tags != '[]' LIMIT 1000")
        if df is not None and not df.empty:
            c: Counter[str] = Counter()
            for row in df["tags"]:
                try:
                    c.update(json.loads(row))
                except Exception as e:
                    logger.warning("operation failed: %s", e)
            if c:
                td = pd.DataFrame(c.most_common(15), columns=["tag", "count"])
                fig = px.bar(
                    td,
                    x="count",
                    y="tag",
                    orientation="h",
                    color="count",
                    color_continuous_scale="Viridis",
                )
                fig.update_layout(
                    **DARK,
                    yaxis=dict(autorange="reversed"),
                    margin=dict(t=30, b=10, l=10, r=10),
                    height=350,
                )
                st.plotly_chart(fig, width="stretch")
            else:
                st.info("No tags found")
        else:
            st.info("No tagged notes")

    with col2:
        st.markdown("#### Recent Activity")
        recent_ops = query(
            "SELECT ts, tool, latency_ms, error "
            "FROM memory_audit_log ORDER BY ts DESC LIMIT 10"
        )
        if recent_ops is not None and not recent_ops.empty:
            for _, r in recent_ops.iterrows():
                ts = pd.to_datetime(r["ts"], unit="s", errors="coerce")
                ts_str = ts.strftime("%H:%M:%S") if pd.notna(ts) else "?"
                status = "❌" if pd.notna(r.get("error")) else "✅"
                lat = f"{r['latency_ms']:.0f}ms" if pd.notna(r.get("latency_ms")) else "?"
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:8px;padding:3px 0;border-bottom:1px solid #1f2937;'>"
                    f"<span style='font-size:0.8rem;'>{status}</span>"
                    f"<span style='color:#d1d5db;font-size:0.75rem;font-weight:600;'>{r['tool']}</span>"
                    f"<span style='color:#4b5563;font-size:0.65rem;margin-left:auto;'>{lat}</span>"
                    f"<span style='color:#6b7280;font-size:0.65rem;'>{ts_str}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.info("No recent activity")

    # ── System summary cards ──
    st.markdown("<br>", unsafe_allow_html=True)
    sum_col1, sum_col2, sum_col3 = st.columns(3)
    with sum_col1:
        st.markdown(
            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px;'>"
            f"<div style='color:#8b8fa3;font-size:0.7rem;text-transform:uppercase;'>Knowledge Graph</div>"
            f"<div style='color:#f0f2f6;font-size:1.3rem;font-weight:700;'>{n_entities} entities</div>"
            f"<div style='color:#6b7280;font-size:0.7rem;'>{n_facts} facts · {n_edg} edges</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    with sum_col2:
        ltr_status = "ready" if ltr_model.exists() else "awaiting data"
        ltr_color = "#10b981" if ltr_model.exists() else "#f59e0b"
        st.markdown(
            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px;'>"
            f"<div style='color:#8b8fa3;font-size:0.7rem;text-transform:uppercase;'>LTR Model</div>"
            f"<div style='color:{ltr_color};font-size:1.3rem;font-weight:700;'>{ltr_status}</div>"
            f"<div style='color:#6b7280;font-size:0.7rem;'>{n_ctr} impressions · 29 features</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    with sum_col3:
        sync_status = f"{n_sync} cycles" if n_sync > 0 else "not synced"
        sync_color = "#10b981" if n_sync > 0 else "#6b7280"
        st.markdown(
            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px;'>"
            f"<div style='color:#8b8fa3;font-size:0.7rem;text-transform:uppercase;'>Sync Status</div>"
            f"<div style='color:{sync_color};font-size:1.3rem;font-weight:700;'>{sync_status}</div>"
            f"<div style='color:#6b7280;font-size:0.7rem;'>{n_alarms} drift alarms</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

# ═══════════════════════════════════════════════════════════════════════════
# MEMORIES (table)
# ═══════════════════════════════════════════════════════════════════════════
with memories_tab:
    st.subheader("Memory Management")

    # ── Summary stats ──
    n_total_m = try_count("memories")
    n_pinned_m = try_count("memories", "pinned=1")
    n_cats = len([r[0] for r in get_conn().execute("SELECT DISTINCT category FROM memories WHERE category IS NOT NULL").fetchall() if r[0]])
    avg_fit = get_conn().execute("SELECT AVG(fitness_score) FROM memories WHERE fitness_score IS NOT NULL").fetchone()[0]
    n_hot = try_count("memories", "tier='hot'")
    n_warm = try_count("memories", "tier='warm'")
    n_cold = try_count("memories", "tier='cold'")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total", n_total_m)
    c2.metric("Pinned", n_pinned_m)
    c3.metric("Categories", n_cats)
    c4.metric("Avg Fitness", f"{avg_fit:.2f}" if avg_fit else "—")
    c5.metric("Hot / Warm / Cold", f"{n_hot} / {n_warm} / {n_cold}")

    st.divider()

    # ── Category distribution + Tier breakdown ──
    col_cat, col_tier = st.columns([2, 1])
    with col_cat:
        cat_df = query(
            "SELECT COALESCE(category, 'uncategorized') cat, COUNT(*) cnt "
            "FROM memories GROUP BY cat ORDER BY cnt DESC"
        )
        if cat_df is not None and not cat_df.empty:
            fig_cat = px.bar(
                cat_df, x="cat", y="cnt", color="cnt",
                color_continuous_scale="Viridis", text_auto=True,
            )
            fig_cat.update_layout(**DARK, height=200, margin=dict(t=10, b=10, l=10, r=10), showlegend=False, xaxis_title=None, yaxis_title="Count")
            st.plotly_chart(fig_cat, width="stretch")

    with col_tier:
        tier_df = query(
            "SELECT COALESCE(tier, 'unassigned') tier, COUNT(*) cnt "
            "FROM memories GROUP BY tier ORDER BY cnt DESC"
        )
        if tier_df is not None and not tier_df.empty:
            cmap = {"hot": "#ef4444", "warm": "#f59e0b", "cold": "#3b82f6", "unassigned": "#4b5563"}
            fig_tier = px.pie(
                tier_df, names="tier", values="cnt", color="tier",
                color_discrete_map=cmap,
            )
            fig_tier.update_layout(**DARK, height=200, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig_tier, width="stretch")

    # ── Fitness distribution ──
    fit_df = query("SELECT fitness_score FROM memories WHERE fitness_score IS NOT NULL")
    if fit_df is not None and not fit_df.empty:
        fig_fit = px.histogram(
            fit_df, x="fitness_score", nbins=30, color_discrete_sequence=["#6366f1"],
        )
        fig_fit.update_layout(**DARK, height=180, margin=dict(t=10, b=10, l=10, r=10), bargap=0.1, xaxis_title="Fitness Score", yaxis_title="Count")
        st.plotly_chart(fig_fit, width="stretch")

    st.divider()

    # ── Filters ──
    f_col1, f_col2, f_col3 = st.columns([2, 1, 1])
    with f_col1:
        m_search = st.text_input("\U0001f50d Filter memories", placeholder="content LIKE ...", key="mem_search")
    with f_col2:
        m_min_fit = st.slider("Min fitness", 0.0, 1.0, 0.0, 0.05, key="mem_fit")
    with f_col3:
        cat_options = ["all"] + sorted([r[0] for r in get_conn().execute("SELECT DISTINCT category FROM memories WHERE category IS NOT NULL").fetchall() if r[0]])
        m_cat_filter = st.selectbox("Category", cat_options, key="mem_cat")

    where_clauses = []
    params = []
    if m_search:
        where_clauses.append("content LIKE ?")
        params.append(f"%{m_search}%")
    if m_min_fit > 0:
        where_clauses.append("COALESCE(fitness_score,0) >= ?")
        params.append(str(m_min_fit))
    if m_cat_filter and m_cat_filter != "all":
        where_clauses.append("category = ?")
        params.append(m_cat_filter)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1"
    m_df = query(
        f"SELECT id, substr(content,1,250) preview, category, created_at, pinned, "
        f"COALESCE(fitness_score, 0.5) as fitness, COALESCE(tier, 'unassigned') as tier, "
        f"COALESCE(importance, 3) as importance "
        f"FROM memories WHERE {where_sql} ORDER BY created_at DESC LIMIT 200",
        params,
    )

    if m_df is not None and not m_df.empty:
        st.caption(f"**{len(m_df)}** memories matching filters")

        # ── Interactive table ──
        display_df = m_df[["id", "category", "fitness", "tier", "importance", "pinned"]].copy()
        display_df["preview"] = m_df["preview"].str[:80]
        display_df["created"] = pd.to_datetime(m_df["created_at"], errors="coerce").dt.strftime("%Y-%m-%d")
        display_df.columns = ["ID", "Category", "Fitness", "Tier", "Importance", "Pinned", "Preview", "Created"]

        selected = st.dataframe(
            display_df, width="stretch", hide_index=True,
            column_config={
                "Fitness": st.column_config.ProgressColumn("Fitness", min_value=0, max_value=1, format="%.2f"),
                "Pinned": st.column_config.CheckboxColumn("Pinned"),
                "Preview": st.column_config.TextColumn("Preview", width="large"),
            },
            selection_mode="single",
            key="mem_table",
        )

        # ── Selected memory detail ──
        # st.dataframe returns a DeltaGenerator; selection is in session_state
        sel_rows = st.session_state.get("mem_table", {}).get("selection", {}).get("rows", [])
        if sel_rows:
            sel_idx = sel_rows[0]
            sel_id = m_df.iloc[sel_idx]["id"]
            st.divider()
            st.markdown(f"### {sel_id}")
            _render_memory_content(sel_id, expanded=True)
    else:
        st.info("No memories match the filters")

# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ═══════════════════════════════════════════════════════════════════════════
with kg_tab:
    st.subheader("Knowledge Graph")
    import networkx as nx

    # ── Summary stats row ──
    n_entities = try_count("kg_entities")
    n_edges_total = try_count("kg_edges")
    n_facts = try_count("kg_facts") if table("kg_facts") else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entities", n_entities)
    c2.metric("Edges", n_edges_total)
    c3.metric("Facts", n_facts)
    density = f"{2 * n_edges_total / (n_entities * (n_entities - 1)):.3f}" if n_entities > 1 else "0"
    c4.metric("Density", density, help="edges / possible edges")

    st.divider()

    # ── Controls row ──
    max_n = st.slider("Show top", 10, 500, 150, key="kg_n", help="Number of top entities to load")

    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 1.5, 1.5])
    with col_ctrl1:
        search_entity = st.text_input("\U0001f50d Find entity", placeholder="bash, tool, python\u2026", key="kg_search")
    with col_ctrl2:
        focus_opts = [""] + sorted(set(
            name_map.get(n, str(n))
            for n in (G_sub.nodes() if 'G_sub' in dir() else [])
        ))
        focus_pick = st.selectbox("\U0001f4d1 Focus", focus_opts, key="kg_focus", placeholder="Focus on entity")

    # ── Query entities ──
    ent = query(
        "SELECT id, name, entity_type, mentions FROM kg_entities "
        "ORDER BY mentions DESC LIMIT ?",
        (max_n,),
    )
    if ent is None or ent.empty:
        st.info("No entities yet. Run KG backfill to populate.")
        st.stop()

    eid_list = [int(x) for x in ent["id"].values]
    name_map = dict(zip(ent["id"], ent["name"]))
    type_map = dict(zip(ent["id"], ent["entity_type"]))
    ment_map = dict(zip(ent["id"], ent["mentions"]))

    # ── Entity type distribution chart ──
    type_counts = ent["entity_type"].value_counts().reset_index()
    type_counts.columns = ["Type", "Count"]
    fig_types = px.bar(
        type_counts, x="Type", y="Count", color="Type",
        color_discrete_sequence=px.colors.qualitative.Set2,
        text_auto=True,
    )
    fig_types.update_layout(**DARK, height=220, margin=dict(t=30, b=10, l=10, r=10), showlegend=False)
    st.plotly_chart(fig_types, width="stretch")

    # ── Type filter (compact) ──
    all_types = sorted(ent["entity_type"].unique())
    with st.popover(f"Filter types ({len(all_types)})", use_container_width=False):
        sel_types = []
        for t in all_types:
            if st.checkbox(t, value=True, key=f"kg_t_{t}"):
                sel_types.append(t)
        if not sel_types:
            sel_types = all_types

    # ── Query edges ──
    placeholders = ",".join("?" for _ in eid_list)
    edges_df = query(
        f"SELECT source_id, target_id, relation, weight FROM kg_edges "
        f"WHERE source_id IN ({placeholders}) "
        f"AND target_id IN ({placeholders}) "
        f"ORDER BY weight DESC LIMIT 1000",
        eid_list + eid_list,
    )

    # ── Edge statistics ──
    if edges_df is not None and not edges_df.empty:
        col_e1, col_e2, col_e3 = st.columns(3)
        col_e1.metric("Visible Edges", len(edges_df))
        rel_counts = edges_df["relation"].value_counts()
        top_rel = rel_counts.index[0] if len(rel_counts) > 0 else "—"
        col_e2.metric("Top Relation", top_rel)
        avg_w = edges_df["weight"].mean() if "weight" in edges_df.columns else 0
        col_e3.metric("Avg Weight", f"{avg_w:.2f}")

        # Relation distribution chart
        rel_df = rel_counts.head(10).reset_index()
        rel_df.columns = ["Relation", "Count"]
        fig_rel = px.bar(
            rel_df, x="Relation", y="Count", color="Count",
            color_continuous_scale="Viridis", text_auto=True,
        )
        fig_rel.update_layout(**DARK, height=200, margin=dict(t=30, b=10, l=10, r=10), showlegend=False)
        st.plotly_chart(fig_rel, width="stretch")
    else:
        st.info("No edges connect the selected entities.")

    # ── Build graph ──
    G = nx.Graph()
    if edges_df is not None and not edges_df.empty:
        for _, r in edges_df.iterrows():
            G.add_edge(
                r["source_id"], r["target_id"],
                relation=r.get("relation", ""),
                weight=_blob_weight(r.get("weight", 1)),
            )

    # ── Subset by type filter ──
    filtered_nodes = [n for n in G.nodes() if type_map.get(n, "other") in sel_types]
    if not filtered_nodes:
        st.info("No entities match the type filter.")
        st.stop()

    G_sub = G.subgraph(filtered_nodes).copy()

    # ── Compute layout once ──
    if "kg_layout" not in st.session_state or st.session_state.get("kg_layout_n") != len(filtered_nodes):
        with st.spinner("Laying out graph \u2026"):
            st.session_state["kg_layout"] = nx.spring_layout(G_sub, k=0.4, seed=42, iterations=50)
            st.session_state["kg_layout_n"] = len(filtered_nodes)
    pos = st.session_state["kg_layout"]

    # ── Resolve focus ──
    focus_id = None
    focus_name = None
    if search_entity:
        q = search_entity.lower()
        best = None
        best_score = 0
        for n in G_sub.nodes():
            nm = name_map.get(n, "")
            score = nm.lower().count(q)
            if score > best_score:
                best_score = score
                best = n
        if best is not None and best_score > 0:
            focus_id = best
            focus_name = name_map.get(best, str(best))
        else:
            for n in G_sub.nodes():
                nm = name_map.get(n, "")
                if q in nm.lower():
                    focus_id = n
                    focus_name = nm
                    break
    if focus_pick and focus_id is None:
        for n in G_sub.nodes():
            if name_map.get(n, str(n)) == focus_pick:
                focus_id = n
                focus_name = focus_pick
                break

    # ── Highlight neighbors ──
    neighbor_ids = set()
    if focus_id is not None and focus_id in G_sub:
        neighbor_ids = set(G_sub.neighbors(focus_id))
        neighbor_ids.add(focus_id)

    # ── Build plot ──
    type_colors = {
        "tool": "#ef4444", "library": "#10b981", "project": "#3b82f6",
        "concept": "#f59e0b", "person": "#8b5cf6", "framework": "#ec4899",
        "language": "#06b6d4", "other": "#6b7280",
    }

    # Edge traces
    edge_traces = []
    for u, v, d in G_sub.edges(data=True):
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        w = d.get("weight", 1) * 0.4 + 0.2
        is_focus = focus_id is not None and (u == focus_id or v == focus_id)
        edge_traces.append(go.Scatter(
            x=(x0, x1, None), y=(y0, y1, None),
            mode="lines",
            line=dict(
                width=w * (2.5 if is_focus else 1.2),
                color="#8b5cf6" if is_focus else "rgba(100,116,139,0.3)",
            ),
            hoverinfo="text",
            hovertext=f"{name_map.get(u, u)} \u2014[{d.get('relation', '')}]\u2014> {name_map.get(v, v)}",
            showlegend=False,
        ))

    def _node_trace(nodes, size_mult, color_override=None, text_visible=False, opacity=0.9):
        if not nodes:
            return None
        xs, ys, labels, types, ments = [], [], [], [], []
        for n in nodes:
            xs.append(pos[n][0])
            ys.append(pos[n][1])
            labels.append(name_map.get(n, str(n))[:24])
            types.append(type_map.get(n, "other"))
            ments.append(ment_map.get(n, 1))
        cols = [color_override or type_colors.get(t, "#6b7280") for t in types]
        sz = [min(32, 8 + m * 2.0) * size_mult for m in ments]
        return go.Scatter(
            x=xs, y=ys,
            mode="markers+text" if text_visible else "markers",
            text=labels if text_visible else None,
            textposition="top center",
            textfont=dict(size=11, color="#f0f2f6", weight=700),
            marker=dict(
                size=sz, color=cols,
                line=dict(width=2 if text_visible else 0.5, color="#f0f2f6" if text_visible else "#1f2937"),
                opacity=opacity,
            ),
            hovertext=[f"<b>{name_map.get(n, n)}</b><br>type: {type_map.get(n, '?')}<br>mentions: {ment_map.get(n, 0)}<br>edges: {G_sub.degree(n)}" for n in nodes],
            hoverinfo="text",
            showlegend=False,
        )

    hl_self, hl_nbr, hl_other = [], [], []
    for n in G_sub.nodes():
        if focus_id is not None and n == focus_id:
            hl_self.append(n)
        elif focus_id is not None and n in neighbor_ids:
            hl_nbr.append(n)
        else:
            hl_other.append(n)

    traces = list(edge_traces)
    if hl_other:
        traces.append(_node_trace(hl_other, 0.8, opacity=0.15 if focus_id else 0.4))
    if hl_nbr:
        traces.append(_node_trace(hl_nbr, 1.2, opacity=0.9))
    if hl_self:
        traces.append(_node_trace(hl_self, 1.8, color_override="#8b5cf6", text_visible=True, opacity=1.0))

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=f"{G_sub.number_of_nodes()} nodes, {G_sub.number_of_edges()} edges"
        + (f" \u00b7 focused: {focus_name}" if focus_name else ""),
        **DARK, showlegend=False, hovermode="closest",
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        height=650,
        margin=dict(t=30, b=10, l=10, r=10),
    )
    st.plotly_chart(fig, width="stretch")

    # ── Detail panel ──
    if focus_id is not None and focus_name:
        st.divider()
        col_d1, col_d2, col_d3 = st.columns([1, 1.5, 1.5])
        with col_d1:
            st.markdown(f"**{focus_name}**")
            st.caption(f"Type: `{type_map.get(focus_id, '?')}` | Mentions: {ment_map.get(focus_id, 0)} | Connections: {G_sub.degree(focus_id)}")
            ns = list(G_sub.neighbors(focus_id))
            # Sort neighbors by edge weight
            ns_weighted = []
            for n in ns:
                w = 0
                for _, _, d in G_sub.edges([focus_id, n], data=True):
                    w = d.get("weight", 0)
                ns_weighted.append((n, w))
            ns_weighted.sort(key=lambda x: -x[1])
            st.markdown("**Connections**")
            for n, w in ns_weighted[:15]:
                rel = next((d.get("relation", "") for _, _, d in G_sub.edges([focus_id], data=True) if n in d), "")
                rel_badge = f"<span style='background:#1e293b;color:#94a3b8;padding:0.1rem 0.4rem;border-radius:4px;font-size:0.65rem;margin-left:4px;'>{rel}</span>" if rel else ""
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:6px;font-size:0.78rem;padding:2px 0;'>"
                    f"<span style='color:{type_colors.get(type_map.get(n, ''), '#6b7280')};font-size:1.1rem;'>\u25cf</span>"
                    f"<span style='color:#d1d5db;'>{name_map.get(n, str(n))}</span>"
                    f"{rel_badge}"
                    f"<span style='color:#4b5563;font-size:0.65rem;'>w={w:.1f}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            if len(ns) > 15:
                st.caption(f"\u2026 and {len(ns) - 15} more")

        with col_d2:
            st.markdown("**Related Memories**")
            mems = query(
                "SELECT id, substr(content,1,150) preview, category, "
                "COALESCE(fitness_score, 0.5) as fitness "
                "FROM memories WHERE content LIKE ? "
                "ORDER BY fitness DESC, created_at DESC LIMIT 8",
                (f"%{focus_name}%",),
            )
            if mems is not None and not mems.empty:
                for _, r in mems.iterrows():
                    cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b", "sessions": "#8b5cf6"}.get(r["category"], "#6b7280")
                    st.markdown(
                        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;padding:8px;margin:4px 0;'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                        f"<span style='background:{cat_color}22;color:{cat_color};padding:0.1rem 0.5rem;border-radius:999px;font-size:0.65rem;font-weight:600;'>{r['category']}</span>"
                        f"<span style='color:#4b5563;font-size:0.65rem;'>fitness: {r['fitness']:.2f}</span>"
                        f"</div>"
                        f"<div style='color:#9ca3af;font-size:0.72rem;margin-top:4px;'>{r['preview'][:120]}</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("No memories reference this entity")

        with col_d3:
            st.markdown("**Top Entities by Mentions**")
            top_entities = ent.head(15)
            fig_top = px.bar(
                top_entities, x="mentions", y="name", orientation="h",
                color="entity_type", color_discrete_map=type_colors,
            )
            fig_top.update_layout(**DARK, height=320, margin=dict(t=20, b=10, l=10, r=10), showlegend=False, yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig_top, width="stretch")

    # ── Full entity table ──
    with st.expander(f"All {len(ent)} entities", expanded=False):
        st.dataframe(
            ent[["name", "entity_type", "mentions"]].rename(columns={"name": "Name", "entity_type": "Type", "mentions": "Mentions"}),
            width="stretch", hide_index=True,
        )

# ═══════════════════════════════════════════════════════════════════════════
# EMBEDDINGS
# ═══════════════════════════════════════════════════════════════════════════
with embed_tab:
    st.subheader("Embedding Space")
    n_emb = get_conn().execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    if n_emb == 0:
        st.info("No embeddings")
    else:
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            cat_choices = sorted(
                r[0] for r in get_conn().execute(
                    "SELECT DISTINCT m.category FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id WHERE m.category IS NOT NULL"
                ).fetchall()
            )
            cat_filter = st.multiselect("Filter category", cat_choices, default=None, key="emb_cat")
        with col2:
            lim = st.slider("Sample", 50, min(2000, n_emb), min(600, n_emb), key="emb_n")
        with col3:
            dim3d = st.checkbox("3D view", value=False, key="emb_3d")

        cat_where = ""
        cat_params = []
        if cat_filter:
            placeholders = ",".join("?" for _ in cat_filter)
            cat_where = f"AND m.category IN ({placeholders})"
            cat_params = cat_filter

        df = query(
            "SELECT e.memory_id, e.embedding, e.dim, m.category, m.tier, m.fitness_score, SUBSTR(m.content, 1, 120) as preview "
            "FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id "
            f"WHERE 1=1 {cat_where} LIMIT ?",
            cat_params + [lim],
        )
        if df is not None and not df.empty:
            dim = int(df["dim"].iloc[0])
            vecs, cats, mids, tiers, fits, previews = [], [], [], [], [], []
            for _, r in df.iterrows():
                try:
                    v = np.frombuffer(r["embedding"], dtype=np.float32)
                    if len(v) == dim:
                        vecs.append(v)
                        cats.append(r.get("category", "?") or "?")
                        mids.append(r["memory_id"])
                        tiers.append(r.get("tier", "") or "")
                        fits.append(float(r["fitness_score"]) if pd.notna(r.get("fitness_score")) else 0.0)
                        previews.append((r.get("preview") or "")[:100])
                except Exception as e:
                    logger.warning("operation failed: %s", e)

            if len(vecs) >= 3:
                with st.spinner("Computing PCA ..."):
                    mat = np.stack(vecs)
                    mc = mat - mat.mean(axis=0)
                    _, S, Vt = np.linalg.svd(mc, full_matrices=False)
                    n_pc = 3 if dim3d else 2
                    p = mc @ Vt[:n_pc].T
                    var_explained = (S[:n_pc] ** 2) / (S**2).sum() * 100

                # --- Semantic search ---
                search_q = st.text_input("\U0001f50d Search memories by text", placeholder="e.g. 'database migration'", key="emb_search")
                search_hit_idx = None
                search_hit_vec = None
                if search_q:
                    query_text = search_q.lower()
                    scored = []
                    for i, pid in enumerate(previews):
                        score = pid.lower().count(query_text)
                        if score > 0:
                            scored.append((score, i))
                    scored.sort(key=lambda x: -x[0])
                    if scored:
                        search_hit_idx = scored[0][1]
                        search_hit_vec = vecs[search_hit_idx]
                        st.caption(f"\U0001f50d Matching memory: {mids[search_hit_idx]}"[:80])

                # --- Compute neighbor lines for selected point ---
                sel_for_lines = None
                if not search_q:
                    sel_mid_dd = st.selectbox(
                        "\U0001f50d Highlight a memory + its neighbors on the plot",
                        [""] + mids, key="emb_select",
                    )
                    if sel_mid_dd and sel_mid_dd in mids:
                        sel_for_lines = mids.index(sel_mid_dd)
                else:
                    sel_for_lines = search_hit_idx

                # --- Build plot ---
                pdf = pd.DataFrame({
                    "x": p[:, 0], "y": p[:, 1],
                    "category": cats, "memory_id": mids,
                    "tier": tiers, "fitness": fits,
                    "preview": previews,
                })
                edge_traces = []
                if sel_for_lines is not None:
                    qv = vecs[sel_for_lines]
                    cos_dot = np.dot(vecs, qv)
                    qn = np.linalg.norm(qv)
                    vn = np.linalg.norm(vecs, axis=1)
                    sims = cos_dot / (qn * vn)
                    top5 = np.argsort(sims)[-6:-1][::-1]  # exclude self
                    highlight_ids = set([sel_for_lines] + list(top5))
                    pdf["highlight"] = ["highlight" if i in highlight_ids else "dim" for i in range(len(vecs))]
                    for ni in top5:
                        edge_traces.append(
                            go.Scatter(
                                x=[p[sel_for_lines, 0], p[ni, 0], None],
                                y=[p[sel_for_lines, 1], p[ni, 1], None],
                                mode="lines",
                                line=dict(width=1, color="rgba(139,92,246,0.4)"),
                                hoverinfo="none",
                                showlegend=False,
                            )
                        )
                else:
                    pdf["highlight"] = "all"

                marker_sizes = []
                for f in fits:
                    s = 4 + f * 14
                    marker_sizes.append(min(s, 28))

                if dim3d:
                    pdf["z"] = p[:, 2]
                    fig = px.scatter_3d(
                        pdf, x="x", y="y", z="z",
                        color="category",
                        hover_name="preview",
                        hover_data={"memory_id": True, "fitness": True, "category": True, "x": False, "y": False, "z": False},
                        opacity=0.8,
                        size=[s * 1.2 for s in marker_sizes],
                        color_discrete_sequence=px.colors.qualitative.Set2,
                    )
                    fig.update_traces(marker=dict(line=dict(width=0.3, color="#333")))
                    fig.update_layout(
                        title=f"PCA 3D ({len(vecs)} pts, PC1={var_explained[0]:.0f}% PC2={var_explained[1]:.0f}% PC3={var_explained[2]:.0f}%)",
                        **DARK, height=650, margin=dict(t=30, b=10, l=10, r=10),
                        scene=dict(
                            xaxis=dict(visible=False), yaxis=dict(visible=False), zaxis=dict(visible=False),
                            bgcolor="#0e1117",
                        ),
                    )
                else:
                    pdf["z"] = 0
                    fig = go.Figure()
                    if sel_for_lines is not None:
                        for et in edge_traces:
                            fig.add_trace(et)
                        dimmed = pdf[pdf["highlight"] == "dim"]
                        hl = pdf[pdf["highlight"] == "highlight"]
                        fig.add_trace(go.Scatter(
                            x=dimmed["x"], y=dimmed["y"],
                            mode="markers",
                            marker=dict(
                                size=[marker_sizes[i] for i in dimmed.index],
                                color="#2d3139",
                                opacity=0.25,
                                line=dict(width=0),
                            ),
                            text=dimmed["preview"],
                            hoverinfo="text",
                            name="other",
                        ))
                        fig.add_trace(go.Scatter(
                            x=hl["x"], y=hl["y"],
                            mode="markers+text",
                            marker=dict(
                                size=[marker_sizes[i] * 1.5 for i in hl.index],
                                color=px.colors.qualitative.Set2[hl.index.to_series().map({i: cats[i] for i in range(len(cats))}).map(
                                    {c: px.colors.qualitative.Set2[i % len(px.colors.qualitative.Set2)] for i, c in enumerate(sorted(set(cats)))}
                                ).fillna("#8b5cf6")],
                                opacity=0.95,
                                line=dict(width=1.5, color="#8b5cf6"),
                            ),
                            text=hl["memory_id"],
                            textposition="top center",
                            textfont=dict(size=9, color="#e5e7eb"),
                            hovertext=[previews[i] for i in hl.index],
                            hoverinfo="text",
                            name="selected",
                        ))
                    else:
                        fig.add_trace(go.Scatter(
                            x=pdf["x"], y=pdf["y"],
                            mode="markers",
                            marker=dict(
                                size=marker_sizes,
                                color=[px.colors.qualitative.Set2[cats.index(c) % len(px.colors.qualitative.Set2)] for c in cats],
                                opacity=0.7,
                                line=dict(width=0.3, color="#333"),
                            ),
                            text=[f"<b>{mids[i]}</b><br>{previews[i][:60]}..." for i in range(len(mids))],
                            hoverinfo="text",
                        ))
                    fig.update_layout(
                        title=f"PCA 2D ({len(vecs)} pts, PC1={var_explained[0]:.0f}% PC2={var_explained[1]:.0f}%)",
                        **DARK, height=650, margin=dict(t=30, b=10, l=10, r=10),
                    )
                st.plotly_chart(fig, width="stretch")

                # --- Selected memory detail ---
                if sel_for_lines is not None:
                    mid = mids[sel_for_lines]
                    st.markdown("---")
                    col_info, col_nn = st.columns([1, 1])
                    with col_info:
                        st.markdown(f"**{mid}**")
                        st.caption(f"Category: {cats[sel_for_lines]} | Fitness: {fits[sel_for_lines]:.3f}")
                        preview_text = query("SELECT content FROM memories WHERE id=?", (mid,))
                        if preview_text is not None and not preview_text.empty:
                            st.text(preview_text.iloc[0]["content"][:500])
                    with col_nn:
                        qv = vecs[sel_for_lines]
                        cos_dot = np.dot(vecs, qv)
                        qn = np.linalg.norm(qv)
                        vn = np.linalg.norm(vecs, axis=1)
                        all_sims = cos_dot / (qn * vn)
                        ranked = sorted(
                            [(all_sims[j], mids[j], cats[j], previews[j]) for j in range(len(vecs)) if j != sel_for_lines],
                            key=lambda x: -x[0],
                        )[:15]
                        st.markdown("**Nearest Neighbors**")
                        nn_df = pd.DataFrame(ranked, columns=["similarity", "memory_id", "category", "preview"])
                        nn_df["similarity"] = nn_df["similarity"].round(3)
                        st.dataframe(nn_df, width="stretch", hide_index=True, column_config={
                            "preview": st.column_config.TextColumn("preview", width="large"),
                        })

                # --- Cluster pulse ---
                if len(cats) >= 20:
                    st.markdown("---")
                    st.markdown("#### Category Concentration")
                    cat_counts = Counter(cats)
                    total = sum(cat_counts.values())
                    cat_df = pd.DataFrame([
                        {"category": c, "count": n, "pct": round(n / total * 100, 1)}
                        for c, n in cat_counts.most_common(10)
                    ])
                    st.dataframe(cat_df, width="stretch", hide_index=True)

                # --- PCA weights ---
                with st.expander("PCA Dimension Weights (top contributing features)"):
                    top_n = st.slider("Top dimensions per PC", 5, 30, 10, key="emb_topd")
                    for pc_i in range(min(n_pc, 3)):
                        weights = Vt[pc_i]
                        top_idx = np.argsort(np.abs(weights))[-top_n:][::-1]
                        wdf = pd.DataFrame({"dim": top_idx, "weight": weights[top_idx].round(3)})
                        st.caption(f"PC{pc_i+1} ({var_explained[pc_i]:.1f}% variance)")
                        fig_w = px.bar(wdf, x="dim", y="weight", color="weight", color_continuous_scale="RdBu")
                        fig_w.update_layout(**DARK, height=200, margin=dict(t=5, b=5, l=5, r=5))
                        st.plotly_chart(fig_w, width="stretch")
            else:
                st.info(f"Need ≥3 vectors, got {len(vecs)}")

# ═══════════════════════════════════════════════════════════════════════════
# FACTS SEARCH
# ═══════════════════════════════════════════════════════════════════════════
with facts_tab:
    st.subheader("Fact Management")
    if table("kg_facts"):
        n_facts = try_count("kg_facts")
        n_locked = try_count("kg_facts", "locked=1")
        avg_conf = get_conn().execute("SELECT AVG(confidence) FROM kg_facts").fetchone()[0]
        n_high_conf = try_count("kg_facts", "confidence >= 0.7")
        n_subjects = try_count("kg_facts", "1=1")

        # ── Summary stats ──
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Facts", n_facts)
        c2.metric("Locked", n_locked)
        c3.metric("Avg Confidence", f"{avg_conf:.2f}" if avg_conf else "—")
        c4.metric("High Confidence", n_high_conf)
        c5.metric("Open", n_facts - n_locked)

        st.divider()

        # ── Confidence distribution + Top predicates ──
        col_conf, col_pred = st.columns([1, 1])

        with col_conf:
            conf_dist = query(
                "SELECT "
                "CASE WHEN confidence >= 0.7 THEN 'high (>=0.7)' "
                "WHEN confidence >= 0.4 THEN 'medium (0.4-0.7)' "
                "ELSE 'low (<0.4)' END as bucket, "
                "COUNT(*) cnt FROM kg_facts GROUP BY bucket ORDER BY bucket"
            )
            if conf_dist is not None and not conf_dist.empty:
                cmap = {"high (>=0.7)": "#10b981", "medium (0.4-0.7)": "#f59e0b", "low (<0.4)": "#ef4444"}
                fig_conf = px.pie(
                    conf_dist, names="bucket", values="cnt", color="bucket",
                    color_discrete_map=cmap,
                )
                fig_conf.update_layout(**DARK, height=220, margin=dict(t=10, b=10, l=10, r=10), title="Confidence Distribution")
                st.plotly_chart(fig_conf, width="stretch")

        with col_pred:
            pred_dist = query(
                "SELECT predicate, COUNT(*) cnt FROM kg_facts "
                "GROUP BY predicate ORDER BY cnt DESC LIMIT 10"
            )
            if pred_dist is not None and not pred_dist.empty:
                fig_pred = px.bar(
                    pred_dist, x="cnt", y="predicate", orientation="h",
                    color="cnt", color_continuous_scale="Viridis", text_auto=True,
                )
                fig_pred.update_layout(**DARK, height=220, margin=dict(t=10, b=10, l=10, r=10), showlegend=False, yaxis=dict(autorange="reversed"), xaxis_title="Count", title="Top Predicates")
                st.plotly_chart(fig_pred, width="stretch")

        st.divider()

        # ── Filters ──
        f_col1, f_col2, f_col3 = st.columns([2, 1, 1])
        with f_col1:
            f_search = st.text_input("\U0001f50d Filter", placeholder="subject, predicate, object...", key="fact_search")
        with f_col2:
            f_min_conf = st.slider("Min confidence", 0.0, 1.0, 0.0, 0.05, key="fact_conf")
        with f_col3:
            lock_filter = st.selectbox("Locked", ["all", "locked", "unlocked"], key="fact_lock")

        where_clauses = ["1=1"]
        f_params = []
        if f_search:
            where_clauses.append("(subject LIKE ? OR predicate LIKE ? OR object LIKE ?)")
            like = f"%{f_search}%"
            f_params.extend([like, like, like])
        if f_min_conf > 0:
            where_clauses.append("confidence >= ?")
            f_params.append(str(f_min_conf))
        if lock_filter == "locked":
            where_clauses.append("locked = 1")
        elif lock_filter == "unlocked":
            where_clauses.append("locked = 0")
        where_sql = " AND ".join(where_clauses)

        # ── Interactive fact table ──
        f_df = query(
            f"SELECT id, subject, predicate, object, confidence, mention_count, "
            f"first_seen, last_seen, locked "
            f"FROM kg_facts WHERE {where_sql} ORDER BY confidence DESC, mention_count DESC LIMIT 200",
            f_params,
        )

        if f_df is not None and not f_df.empty:
            st.caption(f"**{len(f_df)}** facts matching filters")

            # Confidence badge column
            def _conf_badge(conf):
                color = "#10b981" if conf >= 0.7 else "#f59e0b" if conf >= 0.4 else "#ef4444"
                return f'<span style="background:{color};color:#fff;padding:0.1rem 0.4rem;border-radius:999px;font-size:0.65rem;font-weight:600;">{conf:.2f}</span>'

            display_f = f_df[["subject", "predicate", "object", "confidence", "mention_count", "locked"]].copy()
            display_f["confidence"] = display_f["confidence"].apply(lambda x: f"{x:.2f}")
            display_f.columns = ["Subject", "Predicate", "Object", "Confidence", "Mentions", "Locked"]

            selected_f = st.dataframe(
                display_f, width="stretch", hide_index=True,
                column_config={
                    "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=1, format="%.2f"),
                    "Locked": st.column_config.CheckboxColumn("Locked"),
                    "Subject": st.column_config.TextColumn("Subject", width="medium"),
                    "Predicate": st.column_config.TextColumn("Predicate", width="small"),
                    "Object": st.column_config.TextColumn("Object", width="large"),
                },
                selection_mode="single",
                key="fact_table",
            )

            # ── Selected fact detail ──
            sel_fact_rows = st.session_state.get("fact_table", {}).get("selection", {}).get("rows", [])
            if sel_fact_rows:
                sel_idx = sel_fact_rows[0]
                sel_row = f_df.iloc[sel_idx]
                st.divider()

                # Fact header with badges
                conf_val = float(sel_row["confidence"])
                conf_color = "#10b981" if conf_val >= 0.7 else "#f59e0b" if conf_val >= 0.4 else "#ef4444"
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:10px;'>"
                    f"<span style='background:{conf_color};color:#fff;padding:0.2rem 0.7rem;border-radius:999px;font-size:0.8rem;font-weight:700;'>{conf_val:.2f}</span>"
                    f"{'🔒 LOCKED' if sel_row.get('locked') else '🔓 OPEN'}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # Subject → Predicate → Object
                st.markdown(
                    f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:1.2rem;margin:0.8rem 0;'>"
                    f"<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap;'>"
                    f"<span style='background:#3b82f622;color:#60a5fa;padding:0.3rem 0.8rem;border-radius:8px;font-weight:600;'>{html.escape(str(sel_row['subject']))}</span>"
                    f"<span style='color:#6b7280;font-size:1.2rem;'>→</span>"
                    f"<span style='background:#8b5cf622;color:#a78bfa;padding:0.3rem 0.8rem;border-radius:8px;font-size:0.85rem;'>{html.escape(str(sel_row['predicate']))}</span>"
                    f"<span style='color:#6b7280;font-size:1.2rem;'>→</span>"
                    f"<span style='background:#10b98122;color:#34d399;padding:0.3rem 0.8rem;border-radius:8px;font-weight:600;'>{html.escape(str(sel_row['object']))}</span>"
                    f"</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # Metrics row
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Confidence", f"{conf_val:.2f}")
                m2.metric("Mentions", sel_row["mention_count"])
                m3.metric("Locked", "Yes" if sel_row.get("locked") else "No")
                if pd.notna(sel_row.get("first_seen")):
                    m4.metric("First Seen", datetime.fromtimestamp(sel_row['first_seen'], tz=timezone.utc).strftime('%Y-%m-%d'))

                # Related memories
                mems = query(
                    "SELECT id, substr(content,1,150) preview, category FROM memories "
                    "WHERE content LIKE ? ORDER BY created_at DESC LIMIT 5",
                    (f"%{sel_row['subject']}%",),
                )
                if mems is not None and not mems.empty:
                    st.markdown("**Related Memories**")
                    for _, mr in mems.iterrows():
                        cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b"}.get(mr["category"], "#6b7280")
                        st.markdown(
                            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;padding:8px;margin:3px 0;'>"
                            f"<span style='background:{cat_color}22;color:{cat_color};padding:0.1rem 0.4rem;border-radius:999px;font-size:0.6rem;'>{mr['category']}</span> "
                            f"<span style='color:#9ca3af;font-size:0.72rem;'>{mr['preview'][:100]}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
        else:
            st.info("No facts match the filters")
    else:
        st.info("Table `kg_facts` not available — enable MEMORY_KNOWLEDGE_GRAPH=1")

# ═══════════════════════════════════════════════════════════════════════════
# CONCEPT DRIFT
# ═══════════════════════════════════════════════════════════════════════════
with drift_tab:
    st.subheader("Concept Drift & Quality")

    has_drift = table("concept_drift")
    has_alarms = table("drift_alarms")

    # ── Summary stats ──
    n_drift = try_count("concept_drift") if has_drift else 0
    n_alarms = try_count("drift_alarms") if has_alarms else 0
    n_unack = 0
    avg_drift = 0
    if has_alarms:
        try:
            row = get_conn().execute("SELECT AVG(drift_score) FROM drift_alarms").fetchone()
            avg_drift = row[0] if row and row[0] else 0
            n_unack = try_count("drift_alarms", "acknowledged_at IS NULL")
        except Exception:
            pass

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Drift Events", n_drift)
    c2.metric("Alarms", n_alarms)
    c3.metric("Unacknowledged", n_unack)
    c4.metric("Avg Drift Score", f"{avg_drift:.3f}" if avg_drift else "—")

    st.divider()

    # ── Drift timeline + Alarm distribution ──
    if has_drift:
        df = query(
            "SELECT id, drift_metric, drifted_dimensions, triggered_at, acknowledged "
            "FROM concept_drift ORDER BY triggered_at DESC LIMIT 200"
        )
        if df is not None and not df.empty:
            df["ts"] = pd.to_datetime(df["triggered_at"], unit="s", errors="coerce")
            df = df.dropna(subset=["ts"]).sort_values("ts")

            col_timeline, col_dist = st.columns([2, 1])

            with col_timeline:
                fig = px.line(
                    df, x="ts", y="drift_metric", markers=True, line_shape="spline"
                )
                fig.update_traces(
                    line=dict(width=3, color="#ef4444", shape="spline", smoothing=1.3),
                    marker=dict(size=6, color="#ef4444"),
                )
                fig.add_hline(
                    y=0.15, line_dash="dash", line_color="#f59e0b",
                    annotation_text="threshold (0.15)",
                )
                fig.add_hrect(y0=0, y1=0.15, fillcolor="rgba(16,185,129,0.06)", line_width=0)
                fig.add_hrect(y0=0.15, y1=0.3, fillcolor="rgba(245,158,11,0.06)", line_width=0)
                fig.add_hrect(y0=0.3, y1=1, fillcolor="rgba(239,68,68,0.06)", line_width=0)
                fig.update_layout(
                    **DARK,
                    xaxis_title=None, yaxis_title="Drift Metric",
                    margin=dict(t=10, b=10, l=10, r=10),
                    height=300,
                )
                st.plotly_chart(fig, width="stretch")

            with col_dist:
                if has_alarms:
                    alarm_dist = query(
                        "SELECT alarm_level, COUNT(*) cnt FROM drift_alarms GROUP BY alarm_level"
                    )
                    if alarm_dist is not None and not alarm_dist.empty:
                        cmap = {"info": "#3b82f6", "warning": "#f59e0b", "critical": "#ef4444"}
                        fig_dist = px.pie(
                            alarm_dist, names="alarm_level", values="cnt",
                            color="alarm_level", color_discrete_map=cmap,
                        )
                        fig_dist.update_layout(**DARK, height=250, margin=dict(t=10, b=10, l=10, r=10), title="Alarm Distribution")
                        st.plotly_chart(fig_dist, width="stretch")

            # ── Drifted dimensions ──
            if "drifted_dimensions" in df.columns:
                latest_row = df.iloc[-1]
                if latest_row.get("drifted_dimensions"):
                    try:
                        dims = json.loads(latest_row["drifted_dimensions"])
                        if isinstance(dims, (list, tuple)) and len(dims) > 0:
                            dim_df = pd.DataFrame({"dim": range(len(dims)), "weight": dims})
                            dim_df["abs"] = dim_df["weight"].abs()
                            dim_df = dim_df.sort_values("abs", ascending=False).head(20)
                            fig_dim = px.bar(
                                dim_df, x="dim", y="weight",
                                color="weight", color_continuous_scale="RdBu",
                                title=f"Top drifted dimensions (latest event)",
                            )
                            fig_dim.update_layout(**DARK, height=220, margin=dict(t=30, b=10, l=10, r=10))
                            st.plotly_chart(fig_dim, width="stretch")
                    except (json.JSONDecodeError, TypeError):
                        pass
        else:
            st.info("No drift events recorded.")
    else:
        st.info("Table `concept_drift` not yet created.")

    # ── Drift Alarms ──
    if has_alarms:
        st.divider()
        st.markdown("#### Drift Alarms")

        alarm_level_filter = st.selectbox(
            "Filter by level", ["all", "info", "warning", "critical"], key="drift_level"
        )

        where = ""
        params = []
        if alarm_level_filter and alarm_level_filter != "all":
            where = "WHERE alarm_level = ?"
            params = [alarm_level_filter]

        alarms_df = query(
            f"SELECT id, memory_id, concept, drift_score, threshold, alarm_level, "
            f"detected_at, acknowledged_at, acknowledged_by, notes "
            f"FROM drift_alarms {where} ORDER BY detected_at DESC LIMIT 200",
            params,
        )
        if alarms_df is not None and not alarms_df.empty:
            # Interactive table
            display_alarms = alarms_df[["alarm_level", "concept", "drift_score", "threshold", "memory_id"]].copy()
            display_alarms["acknowledged"] = alarms_df["acknowledged_at"].apply(lambda x: pd.notna(x))
            display_alarms.columns = ["Level", "Concept", "Drift Score", "Threshold", "Memory ID", "Acknowledged"]
            display_alarms["Memory ID"] = display_alarms["Memory ID"].str[:40]

            sel_alarm = st.dataframe(
                display_alarms, width="stretch", hide_index=True,
                selection_mode="single",
                key="alarm_table",
            )

            # Selected alarm detail
            sel_alarm_rows = st.session_state.get("alarm_table", {}).get("selection", {}).get("rows", [])
            if sel_alarm_rows:
                sel_idx = sel_alarm_rows[0]
                r = alarms_df.iloc[sel_idx]
                st.divider()

                level_color = {"info": "#3b82f6", "warning": "#f59e0b", "critical": "#ef4444"}.get(r.get("alarm_level", ""), "#6b7280")
                ack = bool(pd.notna(r.get("acknowledged_at")))

                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:10px;'>"
                    f"<span style='background:{level_color};color:#fff;padding:0.2rem 0.7rem;border-radius:999px;font-size:0.8rem;font-weight:700;'>{r.get('alarm_level', '?').upper()}</span>"
                    f"{'✅ ACK' if ack else '⏳ PENDING'}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                st.markdown(f"**Concept**: {html.escape(str(r.get('concept', '—')))}")
                m1, m2, m3 = st.columns(3)
                m1.metric("Drift Score", f"{r['drift_score']:.3f}")
                m2.metric("Threshold", f"{r['threshold']}")
                m3.metric("Memory", r['memory_id'][:30])

                if r.get("notes"):
                    st.markdown(f"**Notes**: {r['notes']}")

                mem_id = r["memory_id"]
                if mem_id:
                    mem_full = query("SELECT substr(content,1,500) preview FROM memories WHERE id=?", (mem_id,))
                    if mem_full is not None and not mem_full.empty:
                        st.markdown("**Memory preview**:")
                        st.text(mem_full.iloc[0]["preview"])
        else:
            st.info("No drift alarms match the filter")

# ═══════════════════════════════════════════════════════════════════════════
# CTR FEEDBACK
# ═══════════════════════════════════════════════════════════════════════════
with ctr_tab:
    st.subheader("CTR Feedback & Search Quality")

    if not table("memory_ctr_feedback"):
        st.info(
            "Table `memory_ctr_feedback` not yet created. Call `memory_record_ctr_feedback()` to start."
        )
    else:
        n_total = try_count("memory_ctr_feedback")
        n_clicked = try_count("memory_ctr_feedback", "clicked_at IS NOT NULL")
        n_dismissed = try_count("memory_ctr_feedback", "dismissed_at IS NOT NULL")
        ctr_pct = (n_clicked / n_total * 100) if n_total > 0 else 0
        n_queries = len([r[0] for r in get_conn().execute("SELECT DISTINCT query_id FROM memory_ctr_feedback").fetchall()])

        # ── Summary stats ──
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Impressions", n_total)
        c2.metric("Clicked", n_clicked)
        c3.metric("Dismissed", n_dismissed)
        c4.metric("CTR", f"{ctr_pct:.1f}%")
        c5.metric("Unique Queries", n_queries)

        st.divider()

        # ── CTR trend + Source breakdown ──
        col_trend, col_source = st.columns([2, 1])

        with col_trend:
            st.markdown("#### CTR Over Time")
            trend_df = query(
                "SELECT DATE(returned_at, 'unixepoch') day, "
                "COUNT(*) as impressions, "
                "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) as clicks "
                "FROM memory_ctr_feedback GROUP BY day ORDER BY day"
            )
            if trend_df is not None and len(trend_df) > 1:
                trend_df["day"] = pd.to_datetime(trend_df["day"])
                trend_df["ctr"] = (trend_df["clicks"] / trend_df["impressions"] * 100).round(1)
                fig_trend = go.Figure()
                fig_trend.add_trace(go.Bar(
                    x=trend_df["day"], y=trend_df["impressions"],
                    name="Impressions", marker_color="#6366f1", opacity=0.6,
                ))
                fig_trend.add_trace(go.Bar(
                    x=trend_df["day"], y=trend_df["clicks"],
                    name="Clicks", marker_color="#10b981", opacity=0.9,
                ))
                fig_trend.add_trace(go.Scatter(
                    x=trend_df["day"], y=trend_df["ctr"],
                    name="CTR %", yaxis="y2", mode="lines+markers",
                    line=dict(color="#f59e0b", width=2),
                ))
                fig_trend.update_layout(
                    **DARK, barmode="overlay", height=250,
                    margin=dict(t=10, b=10, l=10, r=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=9)),
                    yaxis=dict(title="Count"),
                    yaxis2=dict(title="CTR %", overlaying="y", side="right", showgrid=False),
                )
                st.plotly_chart(fig_trend, width="stretch")
            else:
                st.info("Not enough data for trend chart")

        with col_source:
            st.markdown("#### By Source")
            src_df = query(
                "SELECT COALESCE(source, 'unknown') source, COUNT(*) cnt, "
                "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) clicks "
                "FROM memory_ctr_feedback GROUP BY source ORDER BY cnt DESC"
            )
            if src_df is not None and not src_df.empty:
                src_df["ctr"] = (src_df["clicks"] / src_df["cnt"] * 100).round(1)
                fig_src = px.bar(
                    src_df, x="source", y="cnt", color="source",
                    text_auto=True, color_discrete_sequence=px.colors.qualitative.Set2,
                )
                fig_src.update_layout(**DARK, height=250, margin=dict(t=10, b=10, l=10, r=10), showlegend=False, xaxis_title=None, yaxis_title="Impressions")
                st.plotly_chart(fig_src, width="stretch")
            else:
                st.info("No source data")

        # ── nDCG section ──
        if n_clicked > 0:
            try:
                from math import log2
                qdf_quality = query(
                    "SELECT query_id, id, clicked_at, dismissed_at, returned_at "
                    "FROM memory_ctr_feedback ORDER BY query_id, returned_at"
                )
                if qdf_quality is not None and not qdf_quality.empty:
                    ndcg_scores = []
                    for qid, grp in qdf_quality.groupby("query_id"):
                        grp = grp.sort_values("returned_at")
                        rels = [1 if pd.notna(r["clicked_at"]) else 0 for _, r in grp.iterrows()]
                        dcg = sum(rel / log2(i + 2) for i, rel in enumerate(rels))
                        ideal = sorted(rels, reverse=True)
                        idcg = sum(r / log2(i + 2) for i, r in enumerate(ideal))
                        ndcg = dcg / idcg if idcg > 0 else 0.0
                        ndcg_scores.append({"query_id": qid, "nDCG@10": round(ndcg, 4), "results": len(rels), "clicks": sum(rels)})
                    ndcg_df = pd.DataFrame(ndcg_scores)
                    avg_ndcg = ndcg_df["nDCG@10"].mean()
                    col_q1, col_q2, col_q3 = st.columns(3)
                    col_q1.metric("Avg nDCG@10", f"{avg_ndcg:.4f}")
                    col_q2.metric("Queries with clicks", f"{len(ndcg_df[ndcg_df['clicks'] > 0])}/{len(ndcg_df)}")
                    col_q3.metric("Total queries", len(ndcg_df))

                    # nDCG distribution
                    fig_ndcg = px.histogram(ndcg_df, x="nDCG@10", nbins=20, color_discrete_sequence=["#6366f1"])
                    fig_ndcg.update_layout(**DARK, height=200, margin=dict(t=30, b=10, l=10, r=10), bargap=0.1, xaxis_title="nDCG@10", yaxis_title="Queries")
                    st.plotly_chart(fig_ndcg, width="stretch")
            except Exception as e:
                st.caption(f"nDCG computation failed: {e}")

        # ── Offline baseline ──
        bench_file = Path(__file__).parent / "eval" / "results" / "retrieval-baseline.json"
        if bench_file.exists():
            try:
                baseline = json.loads(bench_file.read_text())
                with st.expander("Offline retrieval baseline (eval/gold/v1.jsonl)"):
                    st.caption("Different eval set and metric (nDCG@5) — not directly comparable to live nDCG@10 above.")
                    bc1, bc2 = st.columns(2)
                    bc1.metric("nDCG@5", f"{baseline.get('ndcg_at_5', 0):.4f}")
                    bc2.metric("MRR", f"{baseline.get('mrr', 0):.4f}")
            except Exception:
                pass

        st.divider()

        # ── Filters ──
        f_col1, f_col2, f_col3 = st.columns([2, 1, 1])
        with f_col1:
            sources = [r[0] for r in get_conn().execute("SELECT DISTINCT COALESCE(source,'unknown') FROM memory_ctr_feedback").fetchall()]
            source_filter = st.multiselect("Source", sources, default=sources, key="ctr_src")
        with f_col2:
            action_filter = st.selectbox("Action", ["all", "clicked", "dismissed", "neither"], key="ctr_act")
        with f_col3:
            search_qid = st.text_input("Search query_id", placeholder="partial match...", key="ctr_qid")

        action_where = ""
        action_params: list[str] = []
        if action_filter == "clicked":
            action_where = "AND clicked_at IS NOT NULL"
        elif action_filter == "dismissed":
            action_where = "AND dismissed_at IS NOT NULL"
        elif action_filter == "neither":
            action_where = "AND clicked_at IS NULL AND dismissed_at IS NULL"

        qid_where = ""
        qid_params = []
        if search_qid:
            qid_where = "AND query_id LIKE ?"
            qid_params = [f"%{search_qid}%"]

        src_placeholders = ",".join("?" * len(source_filter)) if source_filter else ""
        src_where = f"AND COALESCE(source,'unknown') IN ({src_placeholders})" if source_filter else ""

        # ── Interactive table ──
        fdf = query(
            f"SELECT id, query_id, returned_at, clicked_at, dismissed_at, source, ranking_params "
            f"FROM memory_ctr_feedback "
            f"WHERE 1=1 {src_where} {action_where} {qid_where} "
            f"ORDER BY returned_at DESC LIMIT 200",
            source_filter + action_params + qid_params,
        )

        if fdf is not None and not fdf.empty:
            st.caption(f"**{len(fdf)}** events")

            # Status column
            fdf["status"] = fdf.apply(lambda r: "clicked" if pd.notna(r["clicked_at"]) else "dismissed" if pd.notna(r["dismissed_at"]) else "pending", axis=1)
            fdf["returned_ts"] = pd.to_datetime(fdf["returned_at"], unit="s", errors="coerce").dt.strftime("%Y-%m-%d %H:%M")

            display_ctr = fdf[["query_id", "source", "status", "returned_ts"]].copy()
            display_ctr.columns = ["Query ID", "Source", "Status", "Time"]

            sel_ctr = st.dataframe(
                display_ctr, width="stretch", hide_index=True,
                column_config={
                    "Query ID": st.column_config.TextColumn("Query ID", width="medium"),
                    "Source": st.column_config.TextColumn("Source", width="small"),
                    "Status": st.column_config.TextColumn("Status", width="small"),
                },
                selection_mode="single",
                key="ctr_table",
            )

            # Selected event detail
            sel_ctr_rows = st.session_state.get("ctr_table", {}).get("selection", {}).get("rows", [])
            if sel_ctr_rows:
                sel_idx = sel_ctr_rows[0]
                sel_row = fdf.iloc[sel_idx]
                st.divider()

                # Event header
                status_color = {"clicked": "#10b981", "dismissed": "#ef4444", "pending": "#f59e0b"}.get(sel_row["status"], "#6b7280")
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:10px;'>"
                    f"<span style='background:{status_color};color:#fff;padding:0.2rem 0.7rem;border-radius:999px;font-size:0.8rem;font-weight:700;'>{sel_row['status'].upper()}</span>"
                    f"<span style='color:#9ca3af;font-size:0.85rem;'>{sel_row['source']}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                st.markdown(f"**Query ID**: `{sel_row['query_id']}`")
                if pd.notna(sel_row["returned_at"]):
                    st.caption(f"Returned: {pd.to_datetime(sel_row['returned_at'], unit='s').isoformat()}")

                # Ranking weights
                if sel_row.get("ranking_params"):
                    try:
                        rp = json.loads(sel_row["ranking_params"])
                        w = rp.get("weights", rp)
                        if isinstance(w, dict):
                            wdf = pd.DataFrame(list(w.items()), columns=["Factor", "Weight"])
                            fig_w = px.bar(wdf, x="Factor", y="Weight", color="Weight", color_continuous_scale="Viridis")
                            fig_w.update_layout(**DARK, height=200, margin=dict(t=10, b=10, l=10, r=10), showlegend=False)
                            st.plotly_chart(fig_w, width="stretch")
                    except Exception:
                        pass

                # Click/Dismiss buttons
                btn_cols = st.columns(2)
                if btn_cols[0].button("👍 Click", key=f"click_{sel_row['query_id']}_{sel_row['id']}", use_container_width=True):
                    try:
                        from search.feedback import record_ctr_feedback_db
                        record_ctr_feedback_db(str(DB), id=sel_row["id"], query_id=sel_row["query_id"], action="clicked")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")
                if btn_cols[1].button("👎 Dismiss", key=f"dismiss_{sel_row['query_id']}_{sel_row['id']}", use_container_width=True):
                    try:
                        from search.feedback import record_ctr_feedback_db
                        record_ctr_feedback_db(str(DB), id=sel_row["id"], query_id=sel_row["query_id"], action="dismissed")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")
        else:
            st.info("No events match the filters")

# ═══════════════════════════════════════════════════════════════════════════
# BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════
with benchmarks_tab:
    st.subheader("Performance Benchmarks")

    bench_dir = Path(__file__).parent / "eval" / "results"
    perf_paths = sorted(bench_dir.glob("perf-envelope*.json"))
    bench_paths = sorted(bench_dir.glob("bench-*.json"))
    retri_paths = sorted(bench_dir.glob("retrieval-baseline*.json"))

    if not perf_paths and not bench_paths:
        st.info(
            "No benchmark results found. Run:\n\n"
            "```\n./venv/bin/python eval/perf_envelope.py\n"
            "./venv/bin/python eval/perf_envelope_v3.py\n"
            "```"
        )
    else:
        rows: list[dict] = []
        build_ops: list[dict] = []
        for fp in perf_paths:
            try:
                data = json.loads(fp.read_text())
            except Exception as e:
                logger.warning("operation failed: %s", e)
                continue
            for corp in data.get("corpora", []):
                sz = corp.get("corpus_size", 0)
                for m in corp.get("measurements", []):
                    rows.append(
                        {
                            "source": fp.stem,
                            "name": m["name"],
                            "corpus_size": sz,
                            "p50_s": m.get("p50_s", 0),
                            "p95_s": m.get("p95_s", 0),
                            "max_s": m.get("max_s", 0),
                            "mean_s": m.get("mean_s", 0),
                            "iterations": m.get("iterations", 1),
                            "notes": m.get("notes", ""),
                        }
                    )
                if "rebuild_index_s" in corp:
                    build_ops.append({"op": "rebuild_index", "corpus_size": sz, "time_s": corp["rebuild_index_s"]})
                if "rebuild_vec_index_s" in corp:
                    build_ops.append({"op": "rebuild_vec_index", "corpus_size": sz, "time_s": corp["rebuild_vec_index_s"]})
                if "initial_build_s" in corp:
                    build_ops.append({"op": "initial_build", "corpus_size": sz, "time_s": corp["initial_build_s"]})
        for fp in bench_paths:
            try:
                data = json.loads(fp.read_text())
            except Exception as e:
                logger.warning("operation failed: %s", e)
                continue
            results_block = data.get("results", {})
            for size_str, modes in results_block.items():
                sz = int(size_str)
                for mode_name, stats in modes.items():
                    if not isinstance(stats, dict):
                        continue
                    rows.append(
                        {
                            "source": fp.stem,
                            "name": f"search_{mode_name}",
                            "corpus_size": sz,
                            "p50_s": stats.get("p50", 0) / 1000.0,
                            "p95_s": stats.get("p95", 0) / 1000.0,
                            "max_s": stats.get("max", 0) / 1000.0,
                            "mean_s": stats.get("mean", 0) / 1000.0,
                            "iterations": 5,
                            "notes": "",
                        }
                    )

        if rows:
            df = pd.DataFrame(rows)
            latest = df.sort_values("corpus_size", ascending=False).groupby("name").first().reset_index()
            max_sz = latest["corpus_size"].max()

            op_order = ["save", "fts5_search", "semantic_search", "indexed_search", "rerank",
                        "contradiction_phrase", "contradiction_semantic", "pinned_decay_dry_run"]
            op_labels = {
                "save": "Save", "fts5_search": "FTS5 Search", "semantic_search": "Semantic Search",
                "indexed_search": "Indexed Search", "rerank": "Rerank",
                "contradiction_phrase": "Contradiction (Phrase)", "contradiction_semantic": "Contradiction (Semantic)",
                "pinned_decay_dry_run": "Pinned Decay",
            }
            fast_ops = [o for o in op_order if o in latest["name"].values and latest.loc[latest["name"] == o, "p50_s"].values[0] < 0.01]
            slow_build = sorted(build_ops, key=lambda x: (x["corpus_size"], x["op"]))

            st.markdown("#### Latency at Largest Corpus")
            cols = st.columns(min(len(fast_ops) + 1, 8))
            for i, op_name in enumerate(fast_ops):
                r = latest[latest["name"] == op_name].iloc[0]
                p50_ms = r["p50_s"] * 1000
                p95_ms = r["p95_s"] * 1000
                max_ms = r["max_s"] * 1000
                cols[i].markdown(
                    f"""<div class="metric-card">
                        <div class="label">{op_labels.get(op_name, op_name)}</div>
                        <div class="value">{p50_ms:.2f} ms</div>
                        <div class="sub">p95 {p95_ms:.2f} · max {max_ms:.2f} @ {int(max_sz):,}</div>
                    </div>""",
                    unsafe_allow_html=True,
                )

            st.markdown("#### Index Build Time")
            if slow_build:
                build_cols = st.columns(3)
                for sz_group in sorted(set(b["corpus_size"] for b in slow_build)):
                    build_df = pd.DataFrame([b for b in slow_build if b["corpus_size"] == sz_group])
                    with st.container():
                        st.caption(f"**{int(sz_group):,} notes**")
                        for _, br in build_df.iterrows():
                            label = br["op"].replace("_", " ").title()
                            st.markdown(
                                f"""<div style="display:flex;justify-content:space-between;
                                background:#1a1d23;padding:0.3rem 0.7rem;border-radius:6px;margin:2px 0;
                                font-size:0.8rem;">
                                    <span style="color:#9ca3af;">{label}</span>
                                    <span style="color:#f0f2f6;font-weight:600;">{br['time_s']:.1f}s</span>
                                </div>""",
                                unsafe_allow_html=True,
                            )

            st.markdown("---")

            chart_df = df[df["p50_s"] > 0].copy()
            chart_df["p50_ms"] = (chart_df["p50_s"] * 1000).round(2)
            chart_df["label"] = chart_df["name"].str.replace("_", " ").str.title()
            chart_df["throughput"] = (1 / chart_df["p50_s"].replace(0, float("nan"))).round(0)
            chart_df["throughput_label"] = chart_df["throughput"].apply(lambda v: f"{v:.0f} ops/s" if pd.notna(v) else "")

            col1, col2 = st.columns(2)
            with col1:
                fast_chart = chart_df[chart_df["p50_ms"] < 100]
                fig = px.bar(
                    fast_chart,
                    x="corpus_size",
                    y="p50_ms",
                    color="label",
                    barmode="group",
                    title="p50 Latency (sub-100ms ops)",
                    labels={"corpus_size": "Corpus Size", "p50_ms": "p50 (ms)", "label": "Operation"},
                )
                fig.update_layout(**DARK, margin=dict(t=40, b=10, l=10, r=10), legend=dict(font=dict(size=9)))
                st.plotly_chart(fig, width="stretch")

            with col2:
                slow_chart = chart_df[chart_df["p50_ms"] >= 100]
                if not slow_chart.empty:
                    fig2 = px.bar(
                        slow_chart,
                        x="corpus_size",
                        y="p50_ms",
                        color="label",
                        barmode="group",
                        title="p50 Latency (expensive ops)",
                        labels={"corpus_size": "Corpus Size", "p50_ms": "p50 (ms)", "label": "Operation"},
                    )
                    fig2.update_layout(**DARK, margin=dict(t=40, b=10, l=10, r=10), legend=dict(font=dict(size=9)))
                    st.plotly_chart(fig2, width="stretch")
                else:
                    st.info("All operations are sub-100ms — no expensive ops to show separately")

            col1, col2 = st.columns(2)
            with col1:
                fig3 = px.scatter(
                    chart_df,
                    x="corpus_size",
                    y="p50_ms",
                    color="label",
                    size="max_s",
                    trendline="lowess",
                    title="p50 vs Corpus Size (bubble = max latency)",
                    labels={"corpus_size": "Corpus Size", "p50_ms": "p50 (ms)", "label": "Operation"},
                )
                fig3.update_layout(**DARK, margin=dict(t=40, b=10, l=10, r=10), legend=dict(font=dict(size=9)))
                st.plotly_chart(fig3, width="stretch")

            with col2:
                fig4 = px.scatter(
                    chart_df,
                    x="p95_s",
                    y="p50_ms",
                    color="label",
                    size="max_s",
                    hover_data=["corpus_size", "throughput_label"],
                    title="p95 vs p50 (bubble = max latency)",
                    labels={"p95_s": "p95 (s)", "p50_ms": "p50 (ms)", "label": "Operation"},
                )
                fig4.update_layout(**DARK, margin=dict(t=40, b=10, l=10, r=10), legend=dict(font=dict(size=9)))
                st.plotly_chart(fig4, width="stretch")

            with st.expander("Raw Measurements"):
                disp = df.copy()
                disp["p50_ms"] = (disp["p50_s"] * 1000).round(2)
                disp["p95_ms"] = (disp["p95_s"] * 1000).round(2)
                disp["max_ms"] = (disp["max_s"] * 1000).round(2)
                disp["throughput_ops_s"] = (1 / disp["p50_s"].replace(0, float("nan"))).round(0)
                disp_disp = disp[["source", "name", "corpus_size", "p50_ms", "p95_ms", "max_ms", "throughput_ops_s", "iterations"]].copy()
                disp_disp.columns = ["Source", "Operation", "Corpus Size", "p50 (ms)", "p95 (ms)", "Max (ms)", "Throughput (ops/s)", "Iters"]
                st.dataframe(disp_disp, width="stretch", hide_index=True)
        else:
            st.info("No measurements parsed from benchmark files")

        st.markdown("---")
        st.subheader("Retrieval Quality")
        if retri_paths:
            retri_tabs = st.tabs([p.stem for p in retri_paths])
            for rtab, rfp in zip(retri_tabs, retri_paths):
                with rtab:
                    try:
                        rdata = json.loads(rfp.read_text())
                    except Exception as e:
                        logger.warning("operation failed: %s", e)
                        st.error(f"Failed to parse {rfp.name}")
                        continue
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("nDCG@5", rdata.get("nDCG@5", "—"))
                    c2.metric("MRR", rdata.get("MRR", "—"))
                    c3.metric("Zero-Result Rate", rdata.get("zero_result_rate", "—"))
                    c4.metric("Wall Time (ms)", f'{rdata.get("wall_time_s", 0) * 1000:.0f}')
                    if "worst_keyword" in rdata:
                        st.caption(f'Worst keyword: {rdata["worst_keyword"]} (nDCG@5={rdata.get("worst_ndcg", "?")})')
                    per_query = rdata.get("per_query", [])
                    if per_query:
                        pdf = pd.DataFrame(per_query)
                        fig = px.bar(pdf, x="keyword", y="nDCG@5", title="Per-Query nDCG@5",
                                     color="nDCG@5", color_continuous_scale="Viridis")
                        fig.update_layout(**DARK, margin=dict(t=40, b=80, l=10, r=10))
                        st.plotly_chart(fig, width="stretch")
        else:
            st.info("No retrieval quality baselines found. Run:\n\n```\n.venv/bin/python eval/retrieval_check.py --gold eval/gold/v1.jsonl\n```")

# ═══════════════════════════════════════════════════════════════════════════
# CRON
# ═══════════════════════════════════════════════════════════════════════════
with cron_tab:
    st.subheader("Scheduled Jobs")

    jobs = [
        ("heartbeat",      "heartbeat.log",       "Daemon liveness",            "every 1 min",   "\U0001fa78"),
        ("integrity",      "integrity.log",        "DB integrity + FTS5 drift",  "every 6 h",     "\U0001f50d"),
        ("worker",         "worker.log",           "Background task executor",   "every 5 min",   "\u2699\ufe0f"),
        ("fts-rebuild",    "fts-rebuild.log",      "FTS5 index rebuild",        "on WAL trigger", "\U0001f4d1"),
        ("emb-recompute",  "embedding-recompute.log", "Embedding refresh",       "on schema change","\U0001f9e0"),
        ("crdt-sync",      "crdt-sync.log",        "CRDT merge sync",           "on conflict",   "\U0001f504"),
        ("digest",         "digest.log",           "Daily session digest",      "nightly",        "\U0001f4f0"),
        ("ltr-trainer",    None,                   "LambdaMART LTR training",   "weekly Mon 05:00","🎯"),
    ]

    # ── Compute job statuses ──
    status_counts: Counter[str] = Counter()
    job_data = []
    for name, filename, desc, trigger, emoji in jobs:
        if filename is None:
            model_path = Path(__file__).parent / "models" / "ltr" / "model.txt"
            if model_path.exists():
                sev, status_label, pct = "ok", "model trained", 60
            else:
                sev, status_label, pct = "warning", "awaiting training data", 10
            status_counts[sev] += 1
            job_data.append({"name": name, "desc": desc, "trigger": trigger, "emoji": emoji, "sev": sev, "status": status_label, "pct": pct, "log_file": None, "log_path": model_path})
            continue
        fp = MEM_DIR / filename
        exists = fp.exists()
        size = fp.stat().st_size if exists else 0
        if not exists:
            sev, status_label, pct = "warning", "no activity", 0
        elif size < 50:
            sev, status_label, pct = "warning", "empty", 5
        elif size < 5000:
            sev, status_label, pct = "ok", f"{size/1024:.0f} KB", 40
        elif size < 100000:
            sev, status_label, pct = "ok", f"{size/1024:.0f} KB", 75
        else:
            sev, status_label, pct = "warning", f"{size/1024:.0f} KB (large)", 95
        status_counts[sev] += 1
        job_data.append({"name": name, "desc": desc, "trigger": trigger, "emoji": emoji, "sev": sev, "status": status_label, "pct": pct, "log_file": filename, "log_path": fp})

    # ── Summary stats ──
    n_ok = status_counts.get("ok", 0)
    n_warn = status_counts.get("warning", 0)
    n_err = status_counts.get("error", 0) + status_counts.get("failure", 0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Jobs", len(jobs))
    c2.metric("Healthy", n_ok)
    c3.metric("Warnings", n_warn)
    c4.metric("Errors", n_err)

    # ── Health summary chart ──
    health_df = pd.DataFrame({"Status": ["Healthy", "Warnings", "Errors"], "Count": [n_ok, n_warn, n_err]})
    health_df = health_df[health_df["Count"] > 0]
    if not health_df.empty:
        fig_health = px.pie(
            health_df, names="Status", values="Count", color="Status",
            color_discrete_map={"Healthy": "#10b981", "Warnings": "#f59e0b", "Errors": "#ef4444"},
        )
        fig_health.update_layout(**DARK, height=220, margin=dict(t=30, b=10, l=10, r=10), showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, font=dict(size=9)))
        st.plotly_chart(fig_health, width="stretch")

    st.divider()

    # ── Job status table ──
    job_table_df = pd.DataFrame([
        {"Job": f"{j['emoji']} {j['name']}", "Description": j["desc"], "Schedule": j["trigger"], "Status": j["status"], "Health": j["sev"]}
        for j in job_data
    ])
    st.dataframe(job_table_df, width="stretch", hide_index=True, column_config={
        "Health": st.column_config.TextColumn("Health", width="small"),
    })

    st.divider()

    # ── Expandable job details ──
    for job in job_data:
        status_color = {"ok": "#10b981", "warning": "#f59e0b", "error": "#ef4444"}.get(job["sev"], "#6b7280")
        with st.expander(f"{job['emoji']} {job['name']} — {job['status']}", expanded=False):
            st.caption(f"{job['desc']} · Schedule: `{job['trigger']}`")

            if job["log_path"] and job["log_path"].exists():
                log_text = job["log_path"].read_text(errors="replace")
                lines = log_text.strip().split("\n")
                st.caption(f"Log: {len(lines)} lines, {job['log_path'].stat().st_size / 1024:.0f} KB")

                col_s, col_t = st.columns([3, 1])
                with col_s:
                    log_search = st.text_input("Filter", placeholder="search lines...", key=f"ls_{job['name']}", label_visibility="collapsed")
                with col_t:
                    tail_n = st.slider("Last N lines", 5, min(200, len(lines)), 20, key=f"tn_{job['name']}", label_visibility="collapsed")

                if log_search:
                    lines = [line for line in lines if log_search.lower() in line.lower()]
                shown = lines[-tail_n:] if tail_n > 0 else lines
                st.code("\n".join(shown), language="text")
            else:
                st.info(f"No log file yet")

# ═══════════════════════════════════════════════════════════════════════════
# MULTI-AGENT SYNC
# ═══════════════════════════════════════════════════════════════════════════
with multi_agent_tab:
    from infra._lazy_imports import get_config as _cfg

    st.subheader("Multi-Agent Sync")

    shared_total = try_count("shared_memories")
    _peers = list(_cfg().sync_peers) if _cfg().sync_enable_server else []
    cols = st.columns(3)
    cols[0].metric("Shared memories", shared_total)
    cols[1].metric("Sync peers (config)", len(_peers))
    cols[2].metric("CRDT enabled", "Yes" if _cfg().crdt_enabled else "No")

    st.divider()

    if table("sync_log"):
        df = query(
            "SELECT id, peer_name, peer_agent_id, direction, started_at, "
            "completed_at, success, changes_pushed, changes_pulled, "
            "error_message, duration_ms "
            "FROM sync_log ORDER BY started_at DESC LIMIT 200"
        )
        if df is not None and not df.empty:
            st.markdown("#### Recent sync cycles")
            df["started_at"] = pd.to_datetime(df["started_at"], unit="s")
            df["completed_at"] = pd.to_datetime(df["completed_at"], unit="s", errors="coerce")
            df["status"] = df["success"].apply(lambda s: "✅" if s else "❌")
            df["changes"] = df["changes_pushed"].fillna(0) + df["changes_pulled"].fillna(0)
            display = df[[
                "id", "peer_name", "peer_agent_id", "direction",
                "status", "started_at", "completed_at",
                "changes_pushed", "changes_pulled", "duration_ms",
            ]].copy()
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
            fig2.update_layout(**DARK, margin=dict(t=30, b=10, l=10, r=10), height=max(200, len(peer_status) * 40))
            st.plotly_chart(fig2, width="stretch")
        else:
            st.info("No sync cycles recorded. Configure `[[sync.peers]]` in `memory.toml` to get started.")
    else:
        st.info("Table `sync_log` not yet created. Run `memory_check_integrity()` to bootstrap.")

    st.divider()
    st.markdown("#### Shared memory pool")
    if table("shared_memories"):
        df_shared = query(
            "SELECT source_note_id, agent_id, category, shared_with, "
            "datetime(shared_at, 'unixepoch') as shared "
            "FROM shared_memories ORDER BY shared_at DESC LIMIT 50"
        )
        if df_shared is not None and not df_shared.empty:
            st.caption(f"Showing {len(df_shared)} most recent shared entries")
            st.dataframe(df_shared, width="stretch", hide_index=True)
        else:
            st.info("Shared pool is empty. Call `memory_maintenance(operation='share', share_note_id=...)` to publish.")
    else:
        st.info("Table `shared_memories` not yet created.")

# ═══════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════
with health_tab:
    st.subheader("System Health & Integrity")
    live = _live_health()
    checks = live.get("checks", [])
    summary = Counter(s for _, s, _ in checks)

    # ── Health summary with pie chart ──
    n_ok = summary.get("ok", 0)
    n_warn = summary.get("warning", 0)
    n_fail = summary.get("failure", 0)
    n_err = summary.get("error", 0)
    total_checks = n_ok + n_warn + n_fail + n_err

    col_stats, col_pie = st.columns([1, 1])
    with col_stats:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("✅ OK", n_ok)
        c2.metric("⚠️ Warning", n_warn)
        c3.metric("❌ Failure", n_fail)
        c4.metric("☠️ Error", n_err)
        st.caption(f"Checked at {live['ts'][:19]} · {total_checks} total checks")

        # Health score
        health_score = round(n_ok / total_checks * 100) if total_checks > 0 else 0
        health_color = "#10b981" if health_score >= 80 else "#f59e0b" if health_score >= 60 else "#ef4444"
        st.markdown(
            f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px;margin-top:8px;'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
            f"<span style='color:#8b8fa3;font-size:0.75rem;'>Health Score</span>"
            f"<span style='color:{health_color};font-size:1.5rem;font-weight:700;'>{health_score}%</span>"
            f"</div>"
            f"<div class='progress-track' style='margin-top:6px;'><div class='progress-fill' style='width:{health_score}%;background:{health_color};'></div></div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    with col_pie:
        if total_checks > 0:
            pie_data = pd.DataFrame({"Status": ["OK", "Warning", "Failure", "Error"], "Count": [n_ok, n_warn, n_fail, n_err]})
            pie_data = pie_data[pie_data["Count"] > 0]
            fig_pie = px.pie(
                pie_data, names="Status", values="Count", color="Status",
                color_discrete_map={"OK": "#10b981", "Warning": "#f59e0b", "Failure": "#ef4444", "Error": "#dc2626"},
            )
            fig_pie.update_layout(**DARK, height=220, margin=dict(t=10, b=10, l=10, r=10), showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, font=dict(size=9)))
            st.plotly_chart(fig_pie, width="stretch")

    st.divider()

    # ── Table health cards ──
    st.markdown("#### Table Health")
    cols_per_row = 4
    for i in range(0, len(checks), cols_per_row):
        row_checks = checks[i:i+cols_per_row]
        cols = st.columns(cols_per_row)
        for j, (name, sev, detail) in enumerate(row_checks):
            color = {"ok": "#10b981", "warning": "#f59e0b", "failure": "#ef4444", "error": "#dc2626"}.get(sev, "#6b7280")
            icon = {"ok": "✅", "warning": "⚠️", "failure": "❌", "error": "☠️"}.get(sev, "❓")
            cols[j].markdown(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;padding:10px;text-align:center;'>"
                f"<div style='font-size:1.2rem;'>{icon}</div>"
                f"<div style='color:#d1d5db;font-size:0.75rem;font-weight:600;margin:4px 0;'>{name}</div>"
                f"<div style='color:#6b7280;font-size:0.65rem;'>{detail}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.divider()

    # ── System info + Feature flags ──
    col_sys, col_feat = st.columns(2)

    with col_sys:
        st.markdown("#### System Info")
        import platform
        sys_info = {
            "Python": platform.python_version(),
            "Platform": f"{platform.system()} {platform.release()}",
            "DB Path": str(DB),
            "Schema": "v61",
            "DB Size": f"{DB.stat().st_size / 1024 / 1024:.1f} MB",
            "Memory Dir": f"{sum(f.stat().st_size for f in MEM_DIR.rglob('*') if f.is_file()) / 1024 / 1024:.1f} MB",
        }
        for k, v in sys_info.items():
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1f2937;'>"
                f"<span style='color:#8b8fa3;font-size:0.75rem;'>{k}</span>"
                f"<span style='color:#d1d5db;font-size:0.75rem;font-weight:600;'>{v}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

    with col_feat:
        st.markdown("#### Feature Flags")
        try:
            from infra._lazy_imports import get_config
            cfg = get_config()
            features = {
                "Knowledge Graph": getattr(cfg, "knowledge_graph_enabled", False),
                "Adaptive Retention": getattr(cfg, "adaptive_retention_enabled", False),
                "Temporal KG": getattr(cfg, "temporal_kg_enabled", False),
                "CTR Tuning": os.environ.get("MEMORY_CTR_TUNING") == "1",
                "LLM Extraction": getattr(cfg, "llm_extraction_enabled", False),
                "Session Memory": getattr(cfg, "session_memory", False),
                "CRDT Sync": getattr(cfg, "crdt_enabled", False),
                "LTR Model": ltr_model.exists() if 'ltr_model' in dir() else (Path(__file__).parent / "models" / "ltr" / "model.txt").exists(),
            }
            for feat_name, enabled in features.items():
                status_color = "#10b981" if enabled else "#4b5563"
                status_text = "ON" if enabled else "OFF"
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #1f2937;'>"
                    f"<span style='color:#d1d5db;font-size:0.75rem;'>{feat_name}</span>"
                    f"<span style='background:{status_color}22;color:{status_color};padding:0.1rem 0.4rem;border-radius:999px;font-size:0.65rem;font-weight:600;'>{status_text}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        except Exception:
            st.info("Could not load feature flags")

# ═══════════════════════════════════════════════════════════════════════════
# BACKUPS
# ═══════════════════════════════════════════════════════════════════════════
with backups_tab:
    st.subheader("Backup Management")

    backup_dir = MEM_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backups = sorted(
        [p for p in backup_dir.glob("*") if p.suffix in (".db", ".gz", ".db.gz") and not p.name.startswith(".")],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )

    # ── Backup stats ──
    if backups:
        total_size = sum(bp.stat().st_size for bp in backups)
        oldest = datetime.fromtimestamp(backups[-1].stat().st_mtime, tz=timezone.utc) if backups else None
        newest = datetime.fromtimestamp(backups[0].stat().st_mtime, tz=timezone.utc) if backups else None

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Backups", len(backups))
        c2.metric("Total Size", f"{total_size / 1024 / 1024:.1f} MB")
        c3.metric("Newest", newest.strftime("%Y-%m-%d") if newest else "—")
        c4.metric("Oldest", oldest.strftime("%Y-%m-%d") if oldest else "—")

        st.divider()

        # ── Backup timeline chart ──
        bp_data = []
        for bp in backups:
            mtime = datetime.fromtimestamp(bp.stat().st_mtime, tz=timezone.utc)
            bp_data.append({"name": bp.name, "size_mb": bp.stat().st_size / 1024 / 1024, "date": mtime})
        bp_df = pd.DataFrame(bp_data)

        fig_bp = px.bar(
            bp_df, x="date", y="size_mb",
            color_discrete_sequence=["#6366f1"],
            hover_data=["name"],
            text_auto=".1f",
        )
        fig_bp.update_layout(**DARK, height=200, margin=dict(t=10, b=10, l=10, r=10), xaxis_title=None, yaxis_title="Size (MB)")
        st.plotly_chart(fig_bp, width="stretch")

        # ── Backup table ──
        rows = []
        for bp in backups:
            mtime = datetime.fromtimestamp(bp.stat().st_mtime, tz=timezone.utc)
            age_days = (datetime.now(timezone.utc) - mtime).days
            rows.append({
                "Name": bp.name,
                "Size": f"{bp.stat().st_size / 1024 / 1024:.1f} MB",
                "Created": mtime.strftime("%Y-%m-%d %H:%M UTC"),
                "Age": f"{age_days}d ago" if age_days > 0 else "today",
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info("No backups yet")

    st.divider()

    # ── Create backup ──
    st.markdown("#### Create Backup")
    col_create, col_manage = st.columns([1, 1])

    with col_create:
        if st.button("\U0001f4e4 Create Backup Now", width="stretch", type="primary"):
            import gzip
            import shutil
            from datetime import date
            backup_name = f"memory-{date.today().isoformat()}-{datetime.now().strftime('%H%M')}.db.gz"
            backup_path = backup_dir / backup_name
            with open(DB, "rb") as fin, gzip.open(backup_path, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            st.success(f"Created {backup_name} ({backup_path.stat().st_size / 1024:.0f} KB)")
            st.rerun()

    with col_manage:
        if backups:
            # ── Restore from backup ──
            restore_name = st.selectbox(
                "Select backup to restore",
                [bp.name for bp in backups],
                key="restore_select",
            )
            if st.button("\U0001f504 Restore from Backup", width="stretch"):
                restore_path = backup_dir / restore_name
                if restore_path.exists():
                    import gzip
                    import shutil
                    # Create a backup of current DB first
                    from datetime import date
                    pre_restore_name = f"pre-restore-{date.today().isoformat()}.db.gz"
                    pre_restore_path = backup_dir / pre_restore_name
                    with open(DB, "rb") as fin, gzip.open(pre_restore_path, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    # Restore
                    with gzip.open(restore_path, "rb") as fin, open(DB, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    st.success(f"Restored from {restore_name}. Pre-restore backup saved as {pre_restore_name}")
                    st.rerun()

    # ── Backup retention ──
    if backups and len(backups) > 7:
        st.divider()
        st.markdown("#### Backup Retention")
        st.caption(f"You have {len(backups)} backups. Consider keeping only the last 7.")
        if st.button("\U0001f5d1\ufe0f Clean Old Backups (keep last 7)", width="stretch"):
            for bp in backups[7:]:
                bp.unlink()
            st.success(f"Deleted {len(backups) - 7} old backups")
            st.rerun()

# ═══════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════
with audit_tab:
    st.subheader("Audit Log & API Performance")

    df = query(
        "SELECT ts, tool, latency_ms, results_count, error, args "
        "FROM memory_audit_log ORDER BY ts DESC LIMIT 500"
    )
    if df is None or df.empty:
        st.info("No audit log entries yet.")
    else:
        df["ts_dt"] = pd.to_datetime(df["ts"], unit="s", errors="coerce")
        df["has_err"] = df["error"].notna()
        df["day"] = df["ts_dt"].dt.date

        # ── Summary stats ──
        p50 = df["latency_ms"].quantile(0.5)
        p95 = df["latency_ms"].quantile(0.95)
        err_count = df["has_err"].sum()
        n_tools = df["tool"].nunique()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Calls", len(df))
        c2.metric("Avg Latency", f"{df['latency_ms'].mean():.0f} ms")
        c3.metric("p50 / p95", f"{p50:.0f} / {p95:.0f} ms")
        c4.metric("Error Rate", f"{df['has_err'].mean() * 100:.1f}%")
        c5.metric("Unique Tools", n_tools)

        st.divider()

        # ── Tool calls + Latency trend ──
        col_calls, col_trend = st.columns([1, 2])

        with col_calls:
            tc = df["tool"].value_counts().reset_index()
            tc.columns = ["Tool", "Calls"]
            fig_calls = px.bar(
                tc, x="Calls", y="Tool", orientation="h",
                color="Calls", color_continuous_scale="Viridis", text_auto=True,
            )
            fig_calls.update_layout(**DARK, height=300, margin=dict(t=10, b=10, l=10, r=10), showlegend=False, yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig_calls, width="stretch")

        with col_trend:
            # Latency trend by day
            daily = df.groupby("day").agg(
                avg_lat=("latency_ms", "mean"),
                p95_lat=("latency_ms", lambda x: x.quantile(0.95)),
                calls=("latency_ms", "count"),
                errors=("has_err", "sum"),
            ).reset_index()
            daily["error_rate"] = (daily["errors"] / daily["calls"] * 100).round(1)

            fig_trend = go.Figure()
            fig_trend.add_trace(go.Scatter(
                x=daily["day"], y=daily["avg_lat"],
                name="Avg Latency", mode="lines+markers",
                line=dict(color="#6366f1", width=2),
            ))
            fig_trend.add_trace(go.Scatter(
                x=daily["day"], y=daily["p95_lat"],
                name="p95 Latency", mode="lines+markers",
                line=dict(color="#f59e0b", width=2, dash="dot"),
            ))
            fig_trend.add_trace(go.Bar(
                x=daily["day"], y=daily["errors"],
                name="Errors", marker_color="#ef4444", opacity=0.5, yaxis="y2",
            ))
            fig_trend.update_layout(
                **DARK, height=300,
                margin=dict(t=10, b=10, l=10, r=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, font=dict(size=9)),
                yaxis=dict(title="Latency (ms)"),
                yaxis2=dict(title="Errors", overlaying="y", side="right", showgrid=False),
            )
            st.plotly_chart(fig_trend, width="stretch")

        # ── Tool performance table ──
        st.markdown("#### Tool Performance")
        tool_perf = df.groupby("tool").agg(
            calls=("latency_ms", "count"),
            avg_lat=("latency_ms", "mean"),
            p50_lat=("latency_ms", lambda x: x.quantile(0.5)),
            p95_lat=("latency_ms", lambda x: x.quantile(0.95)),
            errors=("has_err", "sum"),
        ).reset_index()
        tool_perf["error_rate"] = (tool_perf["errors"] / tool_perf["calls"] * 100).round(1)
        tool_perf = tool_perf.sort_values("calls", ascending=False)

        display_perf = tool_perf[["tool", "calls", "avg_lat", "p50_lat", "p95_lat", "error_rate"]].copy()
        display_perf.columns = ["Tool", "Calls", "Avg (ms)", "p50 (ms)", "p95 (ms)", "Error %"]
        display_perf["Avg (ms)"] = display_perf["Avg (ms)"].round(0)
        display_perf["p50 (ms)"] = display_perf["p50 (ms)"].round(0)
        display_perf["p95 (ms)"] = display_perf["p95 (ms)"].round(0)

        st.dataframe(display_perf, width="stretch", hide_index=True, column_config={
            "Error %": st.column_config.ProgressColumn("Error %", min_value=0, max_value=100, format="%.1f%%"),
        })

        # ── Recent errors ──
        errs = df[df["has_err"]]
        if not errs.empty:
            st.markdown("#### Recent Errors")
            err_display = errs[["ts_dt", "tool", "error"]].copy()
            err_display.columns = ["Time", "Tool", "Error"]
            st.dataframe(err_display, width="stretch", hide_index=True)

# ═══════════════════════════════════════════════════════════════════════════
# EXPLORER
# ═══════════════════════════════════════════════════════════════════════════
with search_tab:
    st.subheader("Memory Explorer")

    # ── Search bar with category filter ──
    s_col1, s_col2 = st.columns([3, 1])
    with s_col1:
        q_text = st.text_input(
            "\U0001f50d Search memories", placeholder="e.g. 'database migration', 'search quality', 'CTR feedback'",
            key="explorer_search",
        )
    with s_col2:
        search_cat = st.selectbox(
            "Category",
            ["all"] + sorted([r[0] for r in get_conn().execute("SELECT DISTINCT category FROM memories WHERE category IS NOT NULL").fetchall() if r[0]]),
            key="explorer_cat",
        )

    # ── Search or browse ──
    if q_text:
        cat_where = "AND category = ?" if search_cat and search_cat != "all" else ""
        cat_params = [search_cat] if search_cat and search_cat != "all" else []

        import time as _time
        _t0 = _time.time()
        df = query(
            f"SELECT id, substr(content,1,400) preview, category, "
            f"created_at, pinned, COALESCE(fitness_score, 0.5) as fitness, "
            f"COALESCE(tier, 'unassigned') as tier, COALESCE(importance, 3) as importance "
            f"FROM memories WHERE content LIKE ? {cat_where} "
            f"ORDER BY created_at DESC LIMIT 50",
            [f"%{q_text}%"] + cat_params,
        )
        _elapsed = (_time.time() - _t0) * 1000

        if df is None or df.empty:
            st.info(f"No matches for '{q_text}'")
        else:
            # ── Search stats ──
            c1, c2, c3 = st.columns(3)
            c1.metric("Results", len(df))
            c2.metric("Search Time", f"{_elapsed:.0f} ms")
            c3.metric("Category", search_cat if search_cat != "all" else "All")

            st.divider()

            # ── Category breakdown of results ──
            if len(df) > 3:
                res_cats = df["category"].value_counts().reset_index()
                res_cats.columns = ["Category", "Count"]
                fig_res = px.bar(
                    res_cats, x="Category", y="Count", color="Count",
                    color_continuous_scale="Viridis", text_auto=True,
                )
                fig_res.update_layout(**DARK, height=160, margin=dict(t=10, b=10, l=10, r=10), showlegend=False, xaxis_title=None)
                st.plotly_chart(fig_res, width="stretch")

            # ── Results as cards ──
            for _, r in df.iterrows():
                cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b", "sessions": "#8b5cf6", "concepts": "#ec4899", "preferences": "#06b6d4"}.get(r["category"], "#6b7280")
                tier_bg = {"hot": "#ef444422", "warm": "#f59e0b22", "cold": "#3b82f622"}.get(r["tier"], "#4b556322")
                tier_fg = {"hot": "#fca5a5", "warm": "#fbbf24", "cold": "#93c5fd"}.get(r["tier"], "#9ca3af")
                tier_badge = f"<span style='background:{tier_bg};color:{tier_fg};padding:0.1rem 0.4rem;border-radius:999px;font-size:0.6rem;margin-left:4px;'>{r['tier']}</span>"
                pinned_badge = " 📌" if r.get("pinned") else ""

                st.markdown(
                    f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:12px 16px;margin:6px 0;'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'>"
                    f"<div>"
                    f"<span style='background:{cat_color}22;color:{cat_color};padding:0.15rem 0.5rem;border-radius:999px;font-size:0.65rem;font-weight:600;'>{r['category']}</span>"
                    f"{tier_badge}{pinned_badge}"
                    f"</div>"
                    f"<span style='color:#4b5563;font-size:0.65rem;'>{r.get('created', '—')}</span>"
                    f"</div>"
                    f"<div style='color:#9ca3af;font-size:0.78rem;line-height:1.4;'>{r['preview'][:200]}</div>"
                    f"<div style='display:flex;gap:12px;margin-top:6px;'>"
                    f"<span style='color:#4b5563;font-size:0.65rem;'>fitness: {r['fitness']:.2f}</span>"
                    f"<span style='color:#4b5563;font-size:0.65rem;'>importance: {r['importance']}</span>"
                    f"</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
    else:
        # ── Browse mode: recent memories ──
        st.markdown("#### Recent Memories")
        recent = query(
            "SELECT id, substr(content,1,200) preview, category, created_at, "
            "pinned, COALESCE(fitness_score, 0.5) as fitness, COALESCE(tier, 'unassigned') as tier "
            "FROM memories ORDER BY created_at DESC LIMIT 20"
        )
        if recent is not None and not recent.empty:
            for _, r in recent.iterrows():
                cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b", "sessions": "#8b5cf6"}.get(r["category"], "#6b7280")
                pinned_badge = " 📌" if r.get("pinned") else ""
                st.markdown(
                    f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;padding:10px 14px;margin:4px 0;'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                    f"<span style='background:{cat_color}22;color:{cat_color};padding:0.1rem 0.4rem;border-radius:999px;font-size:0.65rem;font-weight:600;'>{r['category']}</span>{pinned_badge}"
                    f"<span style='color:#4b5563;font-size:0.65rem;'>{r.get('created', '—')[:10]}</span>"
                    f"</div>"
                    f"<div style='color:#9ca3af;font-size:0.72rem;margin-top:4px;'>{r['preview'][:120]}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.info("No memories yet — save some notes first")
