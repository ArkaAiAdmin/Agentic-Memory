#!/usr/bin/env python3
"""Agentic Memory Dashboard — shared foundation.

6-tab layout: Dashboard, Memories, Knowledge, Operations, Audit, Settings.
"""
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
        transition: border-color 0.2s, transform 0.15s, box-shadow 0.2s;
    }
    .metric-card:hover {
        border-color: #4b5563;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }
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
        font-size: 0.78rem;
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
        animation: pulse-glow 3s ease-in-out infinite;
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
    .stApp {
        transition: background 0.3s ease;
    }
    .stButton button {
        transition: all 0.15s ease, transform 0.1s ease;
    }
    .stButton button:active {
        transform: scale(0.97);
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

    /* Quick action buttons */
    .qa-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-radius: 8px;
        padding: 0.4rem 0.8rem;
        color: #d1d5db;
        font-size: 0.75rem;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.15s;
        margin: 2px;
    }
    .qa-btn:hover {
        background: #2d3139;
        border-color: #6366f1;
        color: #f0f2f6;
    }

    /* Health check item */
    .health-check {
        display: flex;
        align-items: center;
        gap: 10px;
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-radius: 8px;
        padding: 10px 14px;
        margin: 4px 0;
    }
    .health-check .hc-icon { font-size: 1.1rem; }
    .health-check .hc-name { color: #d1d5db; font-size: 0.78rem; font-weight: 600; flex: 1; }
    .health-check .hc-detail { color: #6b7280; font-size: 0.7rem; flex: 2; }
    .health-check .hc-action { flex-shrink: 0; }

    /* Onboarding */
    .onboard-card {
        background: linear-gradient(135deg, #1a1d23 0%, #16191f 100%);
        border: 1px solid #2d3139;
        border-radius: 12px;
        padding: 1.5rem;
        margin: 0.5rem 0;
    }
    .onboard-card h4 { color: #f0f2f6; margin: 0 0 0.5rem 0; font-size: 0.9rem; }
    .onboard-card p { color: #9ca3af; font-size: 0.78rem; margin: 0; }

    /* Keyboard shortcut hint */
    .kbd {
        display: inline-block;
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-radius: 4px;
        padding: 0.1rem 0.4rem;
        font-size: 0.65rem;
        font-family: monospace;
        color: #9ca3af;
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

# ── Tab list (6 purpose-driven tabs) ────────────────────────────────────
TABS = [
    "Dashboard",
    "Memories",
    "Knowledge",
    "Quality",
    "Operations",
    "Compliance",
    "Coordination",
    "Audit",
    "Settings",
]


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


# ── Health check subsystem ──────────────────────────────────────────────
def _run_health_checks() -> list[dict]:
    """Run all health subsystem checks and return structured results."""
    checks = []

    # 1. DB accessibility
    try:
        get_conn().execute("SELECT 1")
        checks.append({"name": "Database", "status": "ok", "detail": "Accessible", "fixable": False})
    except Exception as e:
        checks.append({"name": "Database", "status": "error", "detail": str(e)[:80], "fixable": False})

    # 2. Schema version
    ver = _get_schema_version()
    checks.append({"name": "Schema", "status": "ok" if ver.startswith("v") else "warning", "detail": ver, "fixable": False})

    # 3. Vec index drift
    try:
        n_mem = get_conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        n_emb = get_conn().execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
        ratio = n_emb / n_mem if n_mem > 0 else 0
        if ratio < 0.8 and n_mem > 10:
            checks.append({"name": "Vec Index", "status": "warning", "detail": f"{n_emb}/{n_mem} embedded ({ratio:.0%})", "fixable": True, "fix_label": "Rebuild embeddings"})
        else:
            checks.append({"name": "Vec Index", "status": "ok", "detail": f"{n_emb}/{n_mem} embedded", "fixable": True, "fix_label": "Rebuild embeddings"})
    except Exception:
        checks.append({"name": "Vec Index", "status": "info", "detail": "No embeddings table", "fixable": False})

    # 4. FTS5 sync
    try:
        n_chunks = get_conn().execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
        n_unindexed = get_conn().execute(
            "SELECT COUNT(*) FROM memories m LEFT JOIN memory_chunks c ON m.id=c.parent_id WHERE c.id IS NULL"
        ).fetchone()[0]
        if n_unindexed > 0:
            checks.append({"name": "FTS5 Index", "status": "warning", "detail": f"{n_unindexed} unindexed memories", "fixable": True, "fix_label": "Rebuild FTS"})
        else:
            checks.append({"name": "FTS5 Index", "status": "ok", "detail": f"{n_chunks} chunks indexed", "fixable": True, "fix_label": "Rebuild FTS"})
    except Exception:
        checks.append({"name": "FTS5 Index", "status": "info", "detail": "No chunks table", "fixable": False})

    # 5. Background worker liveness — check task_queue for recent completions
    try:
        last_task = get_conn().execute(
            "SELECT completed_at FROM task_queue "
            "WHERE status='completed' AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        if last_task and last_task[0]:
            from datetime import datetime as _dt
            last_dt = _dt.strptime(last_task[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age_s = (_dt.now(timezone.utc) - last_dt).total_seconds()
            if age_s < 600:
                checks.append({"name": "Worker", "status": "ok", "detail": f"Last task {age_s / 60:.0f}m ago", "fixable": False})
            elif age_s < 3600:
                checks.append({"name": "Worker", "status": "warning", "detail": f"Last task {age_s / 60:.0f}m ago (idle)", "fixable": False})
            else:
                checks.append({"name": "Worker", "status": "error", "detail": f"Last task {age_s / 3600:.1f}h ago (down?)", "fixable": False})
        else:
            checks.append({"name": "Worker", "status": "info", "detail": "No completed tasks yet", "fixable": False})
    except Exception:
        checks.append({"name": "Worker", "status": "warning", "detail": "Cannot check worker status", "fixable": False})

    # 6. Cron jobs — only flag recent failures (last 24h) and pending
    try:
        n_recent_failed = get_conn().execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='failed' "
            "AND completed_at IS NOT NULL AND completed_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        n_pending = get_conn().execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='pending'"
        ).fetchone()[0]
        n_stale_pending = get_conn().execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='pending' "
            "AND created_at < datetime('now', '-1 hour')"
        ).fetchone()[0]
        if n_recent_failed > 0:
            checks.append({"name": "Cron Jobs", "status": "warning", "detail": f"{n_recent_failed} failed in 24h, {n_pending} pending", "fixable": True, "fix_label": "View jobs"})
        elif n_stale_pending > 0:
            checks.append({"name": "Cron Jobs", "status": "warning", "detail": f"{n_stale_pending} stuck pending (>1h)", "fixable": True, "fix_label": "View jobs"})
        else:
            checks.append({"name": "Cron Jobs", "status": "ok", "detail": f"{n_pending} pending", "fixable": True, "fix_label": "View jobs"})
    except Exception:
        checks.append({"name": "Cron Jobs", "status": "info", "detail": "No task_queue table", "fixable": False})

    # 7. Circuit breaker
    try:
        cb_log = MEM_DIR / "circuit_breaker.log" if MEM_DIR else None
        if cb_log and cb_log.exists():
            content = cb_log.read_text()[-500:] if cb_log.stat().st_size > 0 else ""
            if "OPEN" in content.upper():
                checks.append({"name": "Circuit Breaker", "status": "error", "detail": "Breaker is OPEN", "fixable": False})
            else:
                checks.append({"name": "Circuit Breaker", "status": "ok", "detail": "Closed", "fixable": False})
        else:
            checks.append({"name": "Circuit Breaker", "status": "ok", "detail": "No trips logged", "fixable": False})
    except Exception:
        checks.append({"name": "Circuit Breaker", "status": "info", "detail": "Unknown", "fixable": False})

    # 8. Disk space
    try:
        import shutil
        usage = shutil.disk_usage(str(DB.parent if DB else "."))
        free_gb = usage.free / (1024**3)
        if free_gb < 1:
            checks.append({"name": "Disk Space", "status": "error", "detail": f"{free_gb:.1f} GB free (critical)", "fixable": False})
        elif free_gb < 5:
            checks.append({"name": "Disk Space", "status": "warning", "detail": f"{free_gb:.1f} GB free", "fixable": False})
        else:
            checks.append({"name": "Disk Space", "status": "ok", "detail": f"{free_gb:.1f} GB free", "fixable": False})
    except Exception:
        checks.append({"name": "Disk Space", "status": "info", "detail": "Unknown", "fixable": False})

    # 9. KG entities
    n_ent = try_count("kg_entities")
    if n_ent == 0:
        checks.append({"name": "Knowledge Graph", "status": "warning", "detail": "0 entities", "fixable": True, "fix_label": "Run backfill"})
    else:
        checks.append({"name": "Knowledge Graph", "status": "ok", "detail": f"{n_ent} entities", "fixable": True, "fix_label": "Run backfill"})

    return checks


def _compute_health_score(checks: list[dict]) -> tuple[int, str]:
    """Compute health score from checks. Returns (score, label)."""
    score = 100
    for c in checks:
        if c["status"] == "error":
            score -= 15
        elif c["status"] == "warning":
            score -= 5
    score = max(0, min(100, score))
    label = "Healthy" if score >= 80 else "Needs Attention" if score >= 60 else "Critical"
    return score, label


# ── Backward compatibility: _live_health wraps _run_health_checks ────────
def _live_health():
    """Legacy wrapper for old tabs.py — returns dict with 'ts' and 'checks'."""
    checks = _run_health_checks()
    legacy_checks = [(c["name"], c["status"], c["detail"]) for c in checks]
    return {"ts": datetime.now(timezone.utc).isoformat(), "checks": legacy_checks}


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
    "_render_memory_content",
    "_auto_refresh",
    "_blob_weight",
    "_run_health_checks",
    "_compute_health_score",
    # Backward compatibility aliases
    "_live_health",
]
