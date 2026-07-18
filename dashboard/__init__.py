#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import logging
import os
import struct
import sqlite3
import sys
import typing
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Theme CSS ────────────────────────────────────────────────────────────
CSS = """
<style>
    .main > div { padding: 0 1rem; }
    .stApp { background: #0e1117; }

    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #0e1117; }
    ::-webkit-scrollbar-thumb { background: #2d3139; border-radius: 3px; }

    h1, h2, h3 { color: #f0f2f6 !important; font-weight: 600 !important; }

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

    .dot-green { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #10b981; margin-right: 6px; }
    .dot-red { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #ef4444; margin-right: 6px; }
    .dot-yellow { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #f59e0b; margin-right: 6px; }
    .dot-gray { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #4b5563; margin-right: 6px; }

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

    .stTextInput input { background: #1a1d23; color: #f0f2f6; border: 1px solid #2d3139; }
    .stSelectbox div[data-baseweb="select"] { background: #1a1d23; }
    .stSlider [data-baseweb="slider"] { margin-top: 0.5rem; }
    div[data-testid="stDataFrame"] { background: #1a1d23; }
    div[data-testid="stDataFrame"] td { color: #d1d5db; }

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

    button[title="Rerun page"] {
        min-height: 24px !important;
        height: 28px !important;
        padding: 0 0.4rem !important;
        font-size: 0.8rem !important;
        line-height: 1 !important;
    }

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
"""

# ── Plotly style ─────────────────────────────────────────────────────────
px.defaults.template = "plotly_dark"
DARK = dict(
    paper_bgcolor="#0e1117",
    plot_bgcolor="#0e1117",
    font_color="#d1d5db",
    title_font_color="#f0f2f6",
)

# ── DB globals (set by dashboard.py at startup) ──────────────────────────
DB: Path = None  # type: ignore[assignment]
MEM_DIR: Path = None  # type: ignore[assignment]


# ── DB resolution ───────────────────────────────────────────────────────
@st.cache_resource
def resolve_db() -> Path:
    from infra.infrastructure import resolve_active_memory_dir
    return resolve_active_memory_dir() / "memory.db"


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


def get_conn(db_path: "Path | str | None" = None):
    path = Path(db_path) if db_path else DB
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def query(sql: str, params=(), db_path: Path | None = None) -> pd.DataFrame | None:
    try:
        return pd.read_sql_query(sql, get_conn(db_path), params=params)
    except Exception as e:
        logger.warning("query failed: %s", e)
        return None


@st.cache_data(ttl=30)
def table(name: str, db_path: str | None = None) -> bool:
    try:
        r = (
            get_conn(db_path)
            .execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
            .fetchone()
        )
        return r is not None
    except Exception as e:
        logger.warning("table failed: %s", e)
        return False


@st.cache_data(ttl=30)
def try_count(table_name: str, where: str | None = None, db_path: str | None = None) -> int:
    try:
        sql = f"SELECT COUNT(*) FROM {table_name}"
        if where:
            sql += f" WHERE {where}"
        r = get_conn(db_path).execute(sql).fetchone()
        return r[0] if r else 0
    except Exception as e:
        logger.warning("try_count failed: %s", e)
        return 0


_EXPECTED_EMPTY_TABLES = frozenset({
    "concept_drift", "drift_alarms", "memory_ctr_feedback",
    "sync_log", "shared_memories", "kg_edges",
})


def _table_status(name: str) -> tuple[str, str]:
    try:
        r = get_conn().execute(f"SELECT COUNT(*) FROM {name}").fetchone()
        n = r[0] if r else 0
        if n > 0:
            return ("ok", f"{n} rows")
        if name in _EXPECTED_EMPTY_TABLES:
            return ("info", "0 rows (expected)")
        return ("warning", "0 rows (unexpected)")
    except Exception as e:
        return ("error", str(e))


@st.cache_data(ttl=60)
def _get_schema_version() -> str:
    try:
        r = get_conn().execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        if r:
            return "v" + str(r[0])
    except Exception:
        pass
    return "? (pre-migration)"


def _fmt_date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return str(ts)[:10] if ts else "\u2014"


def _live_health():
    checks = []
    core_tables = {
        "memories": "error",
        "kg_entities": "warning",
        "kg_facts": "warning",
        "memory_chunks": "warning",
        "memory_embeddings": "warning",
        "memory_audit_log": "warning",
        "backlinks": "warning",
    }
    for tbl, default_if_empty in core_tables.items():
        sev, detail = _table_status(tbl)
        if sev in ("ok", "error"):
            pass
        elif sev == "info":
            sev = "info"
        elif sev == "warning":
            sev = default_if_empty
        checks.append((tbl, sev, detail))
    for tbl in ("kg_edges", "concept_drift", "drift_alarms", "memory_ctr_feedback", "sync_log", "shared_memories"):
        checks.append((tbl,) + _table_status(tbl))
    ltr_model = _REPO_ROOT / "models" / "ltr" / "model.txt"
    if ltr_model.exists():
        size_kb = ltr_model.stat().st_size / 1024
        checks.append(("ltr_model", "ok", f"{size_kb:.0f} KB"))
    else:
        checks.append(("ltr_model", "warning", "not trained yet"))
    try:
        r = get_conn().execute("SELECT COUNT(*) FROM memories WHERE pinned=1").fetchone()
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
    with st.expander(f"**{title or mid}**", expanded=expanded):
        st.html(f"`{html.escape(str(mid))}`")
        cols = st.columns(4)
        if meta.get("created"):
            cols[0].caption(f"\U0001f4c5 {meta['created'][:10]}")
        if meta.get("tags"):
            raw_tags = meta["tags"].strip("[]").split(",") if meta["tags"].startswith("[") else [meta["tags"]]
            tags_clean = " ".join(t.strip().strip("\"").strip("'") for t in raw_tags[:5])
            cols[1].caption(f"\U0001f3f7 {tags_clean}")
        if meta.get("pinned") and meta["pinned"].lower() in ("true", "1"):
            cols[2].html("\U0001f4cc Pinned")
        if meta.get("importance"):
            cols[3].caption(f"Importance: {meta['importance']}")
        st.divider()
        st.html(body)


def _auto_refresh(interval_secs: int = 30) -> None:
    if st.button("\u21bb", key="top_refresh", help="Rerun page"):
        st.rerun()


# ── Tab list ─────────────────────────────────────────────────────────────
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

__all__ = [
    "CSS",
    "DARK",
    "TABS",
    "DB",
    "MEM_DIR",
    "ROOT",
    "resolve_db",
    "get_conn",
    "query",
    "table",
    "try_count",
    "_table_status",
    "_get_schema_version",
    "_fmt_date",
    "_live_health",
    "_render_memory_content",
    "_auto_refresh",
    "_blob_weight",
]
