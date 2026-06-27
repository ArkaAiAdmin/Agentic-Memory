#!/usr/bin/env python3
"""Agentic Memory Dashboard — state-of-the-art local observability.

Run:
    cd ~/.config/agentic-memory
    venv/bin/streamlit run dashboard.py
"""
import json
import os
import struct
import sqlite3
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
    .stTabs [data-baseweb="tab-list"] { gap: 2px; overflow-x: auto; flex-wrap: nowrap; }
    .stTabs [data-baseweb="tab"] {
        background: #16191f;
        border: 1px solid #2d3139;
        border-bottom: none;
        border-radius: 6px 6px 0 0;
        padding: 0.3rem 0.7rem;
        color: #6b7280;
        font-size: 0.72rem;
        font-weight: 500;
        white-space: nowrap;
        transition: all 0.15s;
    }
    .stTabs [data-baseweb="tab"]:hover {
        background: #1e2128;
        color: #9ca3af;
    }
    .stTabs [aria-selected="true"] {
        background: #0e1117;
        color: #f0f2f6;
        border-bottom: 2px solid #6366f1;
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
    [data-testid="stSidebarNavItems"] { padding-top: 0; }
    section[data-testid="stSidebar"] { width: 260px !important; }

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

    /* Status dot */
    .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
    .dot-green { background: #10b981; }
    .dot-yellow { background: #f59e0b; }
    .dot-red { background: #ef4444; }
    .dot-gray { background: #4b5563; }

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
    from infrastructure import resolve_active_memory_dir

    return resolve_active_memory_dir() / "memory.db"


DB = resolve_db()
if not DB.exists():
    st.error(f"Database not found: {DB}")
    st.stop()

MEM_DIR = DB.parent


@st.cache_resource
def _blob_weight(v):
    if isinstance(v, bytes) and len(v) == 4:
        try: return struct.unpack("<f", v)[0]
        except Exception: return 1.0
    try: return float(v)
    except (ValueError, TypeError): return 1.0


def get_conn():
    """Open a read‑only ephemeral connection. Never migrates the schema."""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c


def query(sql: str, params=()) -> pd.DataFrame | None:
    try:
        return pd.read_sql_query(sql, get_conn(), params=params)
    except Exception as exc:
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
    except Exception:
        return False


def try_count(table_name: str, where: str | None = None) -> int:
    try:
        sql = f"SELECT COUNT(*) FROM {table_name}"
        if where:
            sql += f" WHERE {where}"
        r = get_conn().execute(sql).fetchone()
        return r[0] if r else 0
    except Exception:
        return 0


def _read_doctor() -> dict | None:
    p = MEM_DIR / "doctor_report.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


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
    tags_str = meta.get("tags", "")
    with st.expander(f"**{title or mid}**", expanded=expanded):
        st.markdown(f"`{mid}`", unsafe_allow_html=True)
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
    "<h2 style='margin-bottom:0'>\U0001fa84 Agentic Memory</h2>", unsafe_allow_html=True
)
st.sidebar.caption(f"`{DB.parent.name}/{DB.name}`  &nbsp;·&nbsp; {DB.stat().st_size / 1024 / 1024:.0f} MB")

with st.sidebar:
    st.divider()

    n_mem = try_count("memories")
    n_ent = try_count("kg_entities")
    n_edg = try_count("kg_edges")
    n_audit = try_count("memory_audit_log")
    n_pin = try_count("memories", "pinned=1")
    n_err = try_count("memory_audit_log", "error IS NOT NULL")
    n_facts = try_count("kg_facts")

    c1, c2 = st.columns(2)
    c1.metric("Memories", n_mem)
    c2.metric("Entities", n_ent)
    c1.metric("Facts", n_facts)
    c2.metric("Edges", n_edg)

    st.divider()

    # ── Notifications row ──
    alerts = []
    if n_err > 0:
        alerts.append(("error", f"{n_err} errors"))
    doctor = _read_doctor()
    if doctor and doctor.get("worst") == "failure":
        alerts.append(("failure", "doctor failures"))
    elif doctor and doctor.get("worst") == "warning":
        alerts.append(("warning", "doctor warnings"))

    cc = st.columns(max(len(alerts), 1))
    for i, (sev, label) in enumerate(alerts):
        cc[i].markdown(_badge_html(sev, label), unsafe_allow_html=True)
    if not alerts:
        st.caption("\U0001f7e2 All clear")

    c1, c2 = st.columns(2)
    c1.metric("Pinned", n_pin)
    c2.metric("Audit", n_audit)

    st.divider()
    st.caption("**Refresh**", unsafe_allow_html=True)
    if st.button("↻", key="sidebar_refresh", help="Rerun page"):
        st.rerun()

# ── Helpers ──────────────────────────────────────────────────────────────
MEM_DIR

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
    "Cron",
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
    cron_tab,
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

    cols = st.columns(6)
    for i, (label, val, sub) in enumerate(
        [
            ("Total", n_mem, "memory notes"),
            ("Pinned", n_pin, "hot memory"),
            ("Chunked", n_chk, "split notes"),
            ("Embeddings", n_emb, "vectorized"),
            ("CTR Events", n_ctr, "feedback loop"),
            ("Drift Events", n_dft, "concept shifts"),
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
            st.plotly_chart(fig, use_container_width=True)
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
            st.plotly_chart(fig, use_container_width=True)
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
            margin=dict(t=10, b=10, l=10, r=10),
        )
        st.plotly_chart(fig, use_container_width=True)

    # ── Tags + Fitness ──
    col1, col2 = st.columns(2)
    with col1:
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
                td = pd.DataFrame(c.most_common(20), columns=["tag", "count"])
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
                    margin=dict(t=10, b=10, l=10, r=10),
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No tags found")
        else:
            st.info("No tagged notes")

    with col2:
        st.markdown("#### Fitness Distribution")
        df = query("SELECT fitness_score FROM memories WHERE fitness_score IS NOT NULL")
        if df is not None and not df.empty:
            fig = px.histogram(
                df, x="fitness_score", nbins=40, color_discrete_sequence=["#6366f1"]
            )
            fig.update_layout(
                **DARK,
                bargap=0.1,
                xaxis_title="Fitness",
                yaxis_title="Count",
                margin=dict(t=10, b=10, l=10, r=10),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No fitness scores")

    # ── Activity timeline ──
    st.markdown("#### MCP Activity")
    df = query(
        "SELECT DATE(ts,'unixepoch') day, tool, COUNT(*) calls "
        "FROM memory_audit_log GROUP BY day, tool ORDER BY day"
    )
    if df is not None and not df.empty:
        fig = px.area(
            df, x="day", y="calls", color="tool", line_shape="spline", groupnorm=None
        )
        fig.update_layout(
            **DARK,
            xaxis_title=None,
            yaxis_title="Calls",
            margin=dict(t=10, b=10, l=10, r=10),
            legend=dict(font=dict(size=9)),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No audit log data yet — make some MCP tool calls first")

# ═══════════════════════════════════════════════════════════════════════════
# MEMORIES (table)
# ═══════════════════════════════════════════════════════════════════════════
with memories_tab:
    st.subheader("Memories")
    m_search = st.text_input("\U0001f50d Filter memories", placeholder="content LIKE ...", key="mem_search")
    m_min_fit = st.slider("Min fitness", 0.0, 1.0, 0.0, 0.05, key="mem_fit")
    m_cat_filter = st.selectbox(
        "Category",
        ["all"] + sorted([r[0] for r in get_conn().execute("SELECT DISTINCT category FROM memories WHERE category IS NOT NULL").fetchall() if r[0]]),
        key="mem_cat",
    )

    where_clauses = []
    params = []
    if m_search:
        where_clauses.append("content LIKE ?")
        params.append(f"%{m_search}%")
    if m_min_fit > 0:
        where_clauses.append("COALESCE(fitness_score,0) >= ?")
        params.append(m_min_fit)
    if m_cat_filter and m_cat_filter != "all":
        where_clauses.append("category = ?")
        params.append(m_cat_filter)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1"
    m_df = query(
        f"SELECT id, substr(content,1,250) preview, category, created_at, pinned, fitness_score, tier, importance "
        f"FROM memories WHERE {where_sql} ORDER BY created_at DESC LIMIT 200",
        params,
    )
    if m_df is not None and not m_df.empty:
        st.caption(f"{len(m_df)} memories")
        for _, r in m_df.iterrows():
            _render_memory_content(r["id"])
    else:
        st.info("No memories match the filters")

# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ═══════════════════════════════════════════════════════════════════════════
with kg_tab:
    st.subheader("Knowledge Graph")
    import networkx as nx

    max_n = st.slider("Entity count", 10, 500, 150, key="kg_n")

    ent = query(
        "SELECT id, name, entity_type, mentions FROM kg_entities "
        "ORDER BY mentions DESC LIMIT ?",
        (max_n,),
    )
    if ent is None or ent.empty:
        st.info("No entities")
    else:
        edges = query(
            "SELECT source_id, target_id, relation, weight FROM kg_edges "
            "WHERE source_id IN (SELECT id FROM kg_entities) "
            "AND target_id IN (SELECT id FROM kg_entities) "
            "ORDER BY weight DESC LIMIT 1000"
        )
        if edges is not None and not edges.empty:
            G = nx.Graph()
            eid = set(ent["id"].values)
            for _, r in edges.iterrows():
                if r["source_id"] in eid and r["target_id"] in eid:
                    G.add_edge(
                        r["source_id"],
                        r["target_id"],
                        relation=r.get("relation", ""),
                        weight=_blob_weight(r.get("weight", 1)),
                    )

            if G.number_of_nodes() == 0:
                st.info("No connected entities")
            else:
                pos = nx.spring_layout(G, k=0.3, seed=42, iterations=60)
                name_map = dict(zip(ent["id"], ent["name"]))
                type_map = dict(zip(ent["id"], ent["entity_type"]))
                ment_map = dict(zip(ent["id"], ent["mentions"]))

                type_colors = {
                    "tool": "#ef4444",
                    "library": "#10b981",
                    "project": "#3b82f6",
                    "concept": "#f59e0b",
                    "person": "#8b5cf6",
                    "framework": "#ec4899",
                    "language": "#06b6d4",
                }

                edge_traces = []
                for u, v, d in G.edges(data=True):
                    x0, y0 = pos[u]
                    x1, y1 = pos[v]
                    w = d.get("weight", 1) * 0.4 + 0.2
                    edge_traces.append(
                        go.Scatter(
                            x=(x0, x1, None),
                            y=(y0, y1, None),
                            mode="lines",
                            line=dict(width=w, color="#374151"),
                            hoverinfo="none",
                        )
                    )

                node_x = [pos[n][0] for n in G.nodes()]
                node_y = [pos[n][1] for n in G.nodes()]
                node_labels = [name_map.get(n, str(n))[:28] for n in G.nodes()]
                node_types = [type_map.get(n, "other") for n in G.nodes()]
                node_m = [ment_map.get(n, 1) for n in G.nodes()]

                colors = [type_colors.get(t, "#6b7280") for t in node_types]
                sizes = [min(28, 6 + m * 1.8) for m in node_m]

                node_trace = go.Scatter(
                    x=node_x,
                    y=node_y,
                    mode="markers+text",
                    text=node_labels,
                    textposition="top center",
                    textfont=dict(size=9, color="#d1d5db"),
                    marker=dict(
                        size=sizes, color=colors, line=dict(width=1, color="#1f2937")
                    ),
                    hovertext=[
                        f"<b>{name}</b><br>type: {typ}<br>mentions: {m}"
                        for name, typ, m in zip(node_labels, node_types, node_m)
                    ],
                    hoverinfo="text",
                )

                fig = go.Figure(data=edge_traces + [node_trace])
                fig.update_layout(
                    title="Knowledge Graph (force-directed)",
                    **DARK,
                    showlegend=False,
                    hovermode="closest",
                    xaxis=dict(visible=False),
                    yaxis=dict(visible=False),
                    height=700,
                    margin=dict(t=30, b=10, l=10, r=10),
                )
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No edges found")

# ═══════════════════════════════════════════════════════════════════════════
# EMBEDDINGS
# ═══════════════════════════════════════════════════════════════════════════
with embed_tab:
    st.subheader("Embedding Space")

    n_emb = get_conn().execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    if n_emb == 0:
        st.info("No embeddings")
    else:
        st.caption(f"{n_emb} total embeddings")

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            cat_choices = sorted(
                r[0] for r in get_conn().execute(
                    "SELECT DISTINCT m.category FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id WHERE m.category IS NOT NULL"
                ).fetchall()
            )
            cat_filter = st.multiselect("Categories", cat_choices, default=None, key="emb_cat")
        with col2:
            lim = st.slider("Sample", 50, min(2000, n_emb), min(600, n_emb), key="emb_n")
        with col3:
            dim3d = st.checkbox("3D view", value=False, key="emb_3d")
        with col4:
            show_sim = st.checkbox("Show similar", value=False, key="emb_sim")

        cat_where = ""
        cat_params = []
        if cat_filter:
            placeholders = ",".join("?" for _ in cat_filter)
            cat_where = f"AND m.category IN ({placeholders})"
            cat_params = cat_filter

        df = query(
            "SELECT e.memory_id, e.embedding, e.dim, m.category, m.tier, m.fitness_score "
            "FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id "
            f"WHERE 1=1 {cat_where} LIMIT ?",
            cat_params + [lim],
        )
        if df is not None and not df.empty:
            dim = int(df["dim"].iloc[0])
            vecs, cats, mids, tiers, fits = [], [], [], [], []
            for _, r in df.iterrows():
                try:
                    v = np.frombuffer(r["embedding"], dtype=np.float32)
                    if len(v) == dim:
                        vecs.append(v)
                        cats.append(r.get("category", "?") or "?")
                        mids.append(r["memory_id"])
                        tiers.append(r.get("tier", "") or "")
                        fits.append(float(r["fitness_score"]) if pd.notna(r.get("fitness_score")) else 0.0)
                except Exception:
                    pass

            if len(vecs) >= 3:
                with st.spinner("Computing PCA ..."):
                    mat = np.stack(vecs)
                    mc = mat - mat.mean(axis=0)
                    _, S, Vt = np.linalg.svd(mc, full_matrices=False)
                    n_pc = 3 if dim3d else 2
                    p = mc @ Vt[:n_pc].T
                    var_explained = (S[:n_pc] ** 2) / (S**2).sum() * 100

                pdf = pd.DataFrame({
                    "x": p[:, 0], "y": p[:, 1],
                    "category": cats, "memory_id": mids,
                    "tier": tiers, "fitness": fits,
                })
                if dim3d:
                    pdf["z"] = p[:, 2]
                    fig = px.scatter_3d(
                        pdf, x="x", y="y", z="z",
                        color="category", hover_name="memory_id",
                        opacity=0.7, size=[max(2, f * 12) for f in fits],
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
                    fig = px.scatter(
                        pdf, x="x", y="y",
                        color="category", hover_name="memory_id",
                        opacity=0.7, size=[max(2, f * 12) for f in fits],
                        color_discrete_sequence=px.colors.qualitative.Set2,
                    )
                    fig.update_traces(marker=dict(line=dict(width=0.3, color="#333")))
                    fig.update_layout(
                        title=f"PCA 2D ({len(vecs)} pts, PC1={var_explained[0]:.0f}% PC2={var_explained[1]:.0f}%)",
                        **DARK, height=650, margin=dict(t=30, b=10, l=10, r=10),
                    )

                st.plotly_chart(fig, use_container_width=True)

                # --- Click to inspect ---
                sel_mid = st.selectbox(
                    "\U0001f50d Click a point to inspect (select memory_id)",
                    [""] + mids,
                    key="emb_select",
                )
                if sel_mid:
                    full = query("SELECT content FROM memories WHERE id=?", (sel_mid,))
                    if full is not None and not full.empty:
                        with st.expander(f"Memory: {sel_mid}", expanded=True):
                            st.text(full.iloc[0]["content"])

                    # --- Similarity search ---
                    if show_sim and sel_mid in mids:
                        idx = mids.index(sel_mid)
                        query_vec = vecs[idx]
                        sims = []
                        for j, v in enumerate(vecs):
                            if j == idx:
                                continue
                            cos_sim = float(np.dot(query_vec, v) / (np.linalg.norm(query_vec) * np.linalg.norm(v)))
                            sims.append((cos_sim, mids[j], cats[j]))
                        sims.sort(key=lambda x: -x[0])
                        st.markdown("#### Nearest Neighbors (cosine similarity)")
                        sim_df = pd.DataFrame(sims[:20], columns=["similarity", "memory_id", "category"])
                        sim_df["similarity"] = sim_df["similarity"].round(3)
                        st.dataframe(sim_df, use_container_width=True, hide_index=True)

                        # Allow inspecting neighbors
                        nn_mid = st.selectbox("Inspect a neighbor", [""] + [s[1] for s in sims[:20]], key="emb_nn")
                        if nn_mid:
                            nn_full = query("SELECT content FROM memories WHERE id=?", (nn_mid,))
                            if nn_full is not None and not nn_full.empty:
                                with st.expander(f"Neighbor: {nn_mid}", expanded=True):
                                    st.text(nn_full.iloc[0]["content"])

                # --- Dimension distribution ---
                with st.expander("PCA Dimension Weights (top contributing features)"):
                    top_n = st.slider("Top dimensions per PC", 5, 30, 10, key="emb_topd")
                    for pc_i in range(min(n_pc, 3)):
                            weights = Vt[pc_i]
                            top_idx = np.argsort(np.abs(weights))[-top_n:][::-1]
                            wdf = pd.DataFrame({
                                "dim": top_idx,
                                "weight": weights[top_idx].round(3),
                            })
                            st.caption(f"PC{pc_i+1} ({var_explained[pc_i]:.1f}% variance)")
                            fig_w = px.bar(
                                wdf, x="dim", y="weight",
                                color="weight", color_continuous_scale="RdBu",
                            )
                            fig_w.update_layout(**DARK, height=200, margin=dict(t=5, b=5, l=5, r=5))
                            st.plotly_chart(fig_w, use_container_width=True)

            else:
                st.info(f"Need ≥3 vectors, got {len(vecs)}")

# ═══════════════════════════════════════════════════════════════════════════
# FACTS SEARCH
# ═══════════════════════════════════════════════════════════════════════════
with facts_tab:
    st.subheader("Knowledge Graph Facts")
    if table("kg_facts"):
        n_facts = try_count("kg_facts")
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Facts", n_facts)
        n_locked = try_count("kg_facts", "locked=1")
        c2.metric("Locked", n_locked)
        avg_conf = get_conn().execute("SELECT AVG(confidence) FROM kg_facts").fetchone()[0]
        c3.metric("Avg Confidence", f"{avg_conf:.2f}" if avg_conf else "—")

        st.divider()

        col1, col2 = st.columns([1, 3])
        with col1:
            f_search = st.text_input("\U0001f50d Filter", placeholder="subject, predicate, object...", key="fact_search")
            f_min_conf = st.slider("Min confidence", 0.0, 1.0, 0.0, 0.05, key="fact_conf")
            lock_filter = st.selectbox("Locked", ["all", "locked", "unlocked"], key="fact_lock")
            st.caption(f"Showing top 200 by confidence")

            where_clauses = ["1=1"]
            f_params = []
            if f_search:
                where_clauses.append("(subject LIKE ? OR predicate LIKE ? OR object LIKE ?)")
                like = f"%{f_search}%"
                f_params.extend([like, like, like])
            if f_min_conf > 0:
                where_clauses.append("confidence >= ?")
                f_params.append(f_min_conf)
            if lock_filter == "locked":
                where_clauses.append("locked = 1")
            elif lock_filter == "unlocked":
                where_clauses.append("locked = 0")
            where_sql = " AND ".join(where_clauses)

        with col2:
            f_df = query(
                f"SELECT id, subject, predicate, object, confidence, mention_count, "
                f"first_seen, last_seen, locked "
                f"FROM kg_facts WHERE {where_sql} ORDER BY confidence DESC, mention_count DESC LIMIT 200",
                f_params,
            )
            if f_df is not None and not f_df.empty:
                st.caption(f"{len(f_df)} facts")
                for _, r in f_df.iterrows():
                    conf_col = "#10b981" if r["confidence"] >= 0.7 else "#f59e0b" if r["confidence"] >= 0.4 else "#ef4444"
                    conf_badge = f'<span style="background:{conf_col};color:#fff;padding:0.15rem 0.5rem;border-radius:999px;font-size:0.7rem;font-weight:600;">{r["confidence"]:.2f}</span>'
                    lock_badge = _badge_html("warning" if r.get("locked") else "ok", "LOCKED" if r.get("locked") else "OPEN")
                    with st.expander(f"{r['subject'][:35]} → {r['predicate'][:25]} → {r['object'][:50]}"):
                        st.markdown(f"{lock_badge} &nbsp; {conf_badge} &nbsp; **{r['subject'][:35]}** → **{r['predicate'][:25]}** → **{r['object'][:50]}**", unsafe_allow_html=True)
                        c1, c2, c3 = st.columns(3)
                        c1.markdown(f"**Subject**\n\n`{r['subject']}`")
                        c2.markdown(f"**Predicate**\n\n`{r['predicate']}`")
                        c3.markdown(f"**Object**\n\n`{r['object']}`")
                        st.divider()
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("Confidence", f"{r['confidence']:.2f}")
                        m2.metric("Mentions", r["mention_count"])
                        m3.metric("Locked", "Yes" if r.get("locked") else "No")
                        if pd.notna(r.get("first_seen")):
                            m4.caption(f"First: {datetime.fromtimestamp(r['first_seen'], tz=timezone.utc).strftime('%Y-%m-%d')}")
                        if pd.notna(r.get("last_seen")):
                            st.caption(f"Last seen: {datetime.fromtimestamp(r['last_seen'], tz=timezone.utc).strftime('%Y-%m-%d')}")
            else:
                st.info("No facts match the filters")
    else:
        st.info("Table `kg_facts` not available — enable MEMORY_KNOWLEDGE_GRAPH=1")

# ═══════════════════════════════════════════════════════════════════════════
# CONCEPT DRIFT
# ═══════════════════════════════════════════════════════════════════════════
with drift_tab:
    st.subheader("Concept Drift")

    col1, col2 = st.columns(2)
    with col1:
        has_drift = table("concept_drift")
        has_alarms = table("drift_alarms")
        if has_drift:
            n_drift = try_count("concept_drift")
            n_alarms = try_count("drift_alarms") if has_alarms else 0
            c1, c2 = st.columns(2)
            c1.metric("Drift Events", n_drift)
            c2.metric("Drift Alarms", n_alarms)
    with col2:
        alarm_level_filter = None
        if has_alarms:
            alarm_level_filter = st.selectbox(
                "Alarm level filter", ["all", "info", "warning", "critical"], key="drift_level"
            )

    st.divider()

    # ── Drift metric timeline ──
    if has_drift:
        df = query(
            "SELECT id, drift_metric, drifted_dimensions, triggered_at, acknowledged "
            "FROM concept_drift ORDER BY triggered_at DESC LIMIT 200"
        )
        if df is not None and not df.empty:
            df["ts"] = pd.to_datetime(df["triggered_at"], unit="s", errors="coerce")
            df = df.dropna(subset=["ts"]).sort_values("ts")

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
            fig.update_layout(
                **DARK,
                xaxis_title=None, yaxis_title="Drift Metric",
                margin=dict(t=10, b=10, l=10, r=10),
                height=300,
            )
            st.plotly_chart(fig, use_container_width=True)

            # ── Drifted dimensions visualization ──
            if "drifted_dimensions" in df.columns:
                latest_row = df.iloc[-1]
                if latest_row.get("drifted_dimensions"):
                    try:
                        dims = json.loads(latest_row["drifted_dimensions"])
                        if isinstance(dims, (list, tuple)) and len(dims) > 0:
                            dim_df = pd.DataFrame({
                                "dim": range(len(dims)),
                                "weight": dims,
                            })
                            dim_df["abs"] = dim_df["weight"].abs()
                            dim_df = dim_df.sort_values("abs", ascending=False).head(30)
                            fig_dim = px.bar(
                                dim_df, x="dim", y="weight",
                                color="weight", color_continuous_scale="RdBu",
                                title=f"Top drifted dimensions (latest event: {latest_row.get('id','')})",
                            )
                            fig_dim.update_layout(**DARK, height=250, margin=dict(t=25, b=5, l=5, r=5))
                            st.plotly_chart(fig_dim, use_container_width=True)
                    except (json.JSONDecodeError, TypeError):
                        pass

            with st.expander("All Drift Events"):
                disp = df[["id", "ts", "drift_metric", "acknowledged"]].copy()
                disp.columns = ["ID", "Timestamp", "Drift", "Acknowledged"]
                st.dataframe(disp, use_container_width=True, hide_index=True)
        else:
            st.info("No drift events recorded. Run `memory_check_concept_drift()` first.")
    else:
        st.info("Table `concept_drift` not yet created. Call `memory_check_concept_drift()` to start.")

    # ── Drift Alarms ──
    if has_alarms:
        st.divider()
        st.markdown("#### Drift Alarms")

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
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Alarms", len(alarms_df))
            c2.metric("Unacknowledged", alarms_df["acknowledged_at"].isna().sum())
            c3.metric("Avg Drift Score", f"{alarms_df['drift_score'].mean():.3f}")
            c4.metric("Above Threshold", (alarms_df["drift_score"] > alarms_df["threshold"]).sum())

            for _, r in alarms_df.iterrows():
                ack = bool(pd.notna(r.get("acknowledged_at")))
                alarm_badge = _badge_html(
                    {"info": "ok", "warning": "warning", "critical": "error"}.get(r.get("alarm_level", ""), "warning"),
                    r.get("alarm_level", "?").upper(),
                )
                ack_label = _badge_html("ok" if ack else "warning", "ACK" if ack else "PENDING")
                with st.expander(f"drift={r['drift_score']:.3f} &nbsp;·&nbsp; {r['memory_id'][:50]}"):
                    st.markdown(f"{alarm_badge} &nbsp; {ack_label} &nbsp; **Concept**: {r.get('concept', '—')}", unsafe_allow_html=True)
                    st.markdown(f"**Drift Score**: {r['drift_score']:.3f} (threshold: {r['threshold']})")
                    st.markdown(f"**Detected**: {r.get('detected_at', '—')}")
                    if ack:
                        st.markdown(f"**Acknowledged**: {r['acknowledged_at']} by {r.get('acknowledged_by', '?')}")
                    if r.get("notes"):
                        st.markdown(f"**Notes**: {r['notes']}")

                    # Inline view of the flagged memory
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
    st.subheader("CTR Feedback Loop")

    if not table("memory_ctr_feedback"):
        st.info(
            "Table `memory_ctr_feedback` not yet created. Call `memory_record_ctr_feedback()` to start."
        )
    else:
        n_total = try_count("memory_ctr_feedback")
        n_clicked = try_count("memory_ctr_feedback", "clicked_at IS NOT NULL")
        n_dismissed = try_count("memory_ctr_feedback", "dismissed_at IS NOT NULL")
        ctr_pct = (n_clicked / n_total * 100) if n_total > 0 else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Results Returned", n_total)
        c2.metric("Clicked", n_clicked)
        c3.metric("Dismissed", n_dismissed)
        c4.metric("CTR", f"{ctr_pct:.1f}%")

        st.divider()

        col1, col2 = st.columns([1, 2])
        with col1:
            st.markdown("#### Filters")
            sources = [r[0] for r in get_conn().execute("SELECT DISTINCT COALESCE(source,'unknown') FROM memory_ctr_feedback").fetchall()]
            source_filter = st.multiselect("Source", sources, default=sources, key="ctr_src")
            action_filter = st.selectbox("Action", ["all", "clicked", "dismissed", "neither"], key="ctr_act")
            search_qid = st.text_input("Search query_id", placeholder="partial match...", key="ctr_qid")
            st.caption(f"Showing up to 200 rows")

            action_where = ""
            action_params = []
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

            src_placeholders = ",".join("?" * len(source_filter)) if source_filter else "?"
            src_where = f"AND COALESCE(source,'unknown') IN ({src_placeholders})" if source_filter else ""

            fdf = query(
                f"SELECT id, query_id, returned_at, clicked_at, dismissed_at, source, ranking_params "
                f"FROM memory_ctr_feedback "
                f"WHERE 1=1 {src_where} {action_where} {qid_where} "
                f"ORDER BY returned_at DESC LIMIT 200",
                source_filter + action_params + qid_params,
            )
            if fdf is not None and not fdf.empty:
                fdf["returned"] = pd.to_datetime(fdf["returned_at"], unit="s", errors="coerce")
                fdf["status"] = fdf.apply(lambda r: "clicked" if pd.notna(r["clicked_at"]) else "dismissed" if pd.notna(r["dismissed_at"]) else "pending", axis=1)
                st.markdown(f"**{len(fdf)}** events")

                for _, r in fdf.iterrows():
                    status_badge = _badge_html(
                        {"clicked": "ok", "dismissed": "err", "pending": "warning"}.get(r["status"], "warning"),
                        r["status"].upper(),
                    )
                    ts = r["returned"].strftime("%Y-%m-%d %H:%M") if pd.notna(r["returned"]) else "?"
                with st.expander(f"`{r['query_id'][:24]}` &nbsp;·&nbsp; {ts} &nbsp;·&nbsp; src={r['source']}"):
                    st.markdown(f"{status_badge} **Query ID**: `{r['query_id']}`", unsafe_allow_html=True)
                    st.markdown(f"**Source**: {r['source']}")
                    st.markdown(f"**Status**: {r['status']}")
                    if pd.notna(r["returned_at"]):
                        st.caption(f"Returned: {r['returned'].isoformat()}")
                    if pd.notna(r["clicked_at"]):
                        st.caption(f"Clicked: {pd.to_datetime(r['clicked_at'], unit='s', errors='coerce').isoformat()}")
                    if pd.notna(r["dismissed_at"]):
                        st.caption(f"Dismissed: {pd.to_datetime(r['dismissed_at'], unit='s', errors='coerce').isoformat()}")
                    if r.get("ranking_params"):
                        try:
                            rp = json.loads(r["ranking_params"])
                            st.markdown("**Ranking weights**:")
                            w = rp.get("weights", rp)
                            if isinstance(w, dict):
                                wdf = pd.DataFrame(list(w.items()), columns=["factor", "weight"])
                                fig_w = px.bar(wdf, x="factor", y="weight", color="weight", color_continuous_scale="Viridis")
                                fig_w.update_layout(**DARK, height=180, margin=dict(t=10, b=5, l=5, r=5), showlegend=False)
                                st.plotly_chart(fig_w, use_container_width=True)
                            else:
                                st.json(rp)
                        except Exception:
                            st.code(r["ranking_params"])
            else:
                st.info("No events match the filters")

        with col2:
            st.markdown("#### Timeline")
            tdf = query(
                f"SELECT returned_at, clicked_at, dismissed_at, query_id, source "
                f"FROM memory_ctr_feedback "
                f"WHERE 1=1 {src_where} {action_where} {qid_where} "
                f"ORDER BY returned_at DESC LIMIT 200",
                source_filter + action_params + qid_params,
            )
            if tdf is not None and not tdf.empty:
                events = []
                for _, r in tdf.iterrows():
                    ts = pd.to_datetime(r["returned_at"], unit="s", errors="coerce")
                    if pd.isna(ts):
                        continue
                    events.append({"time": ts, "event": "returned", "qid": r["query_id"][:20], "source": r["source"]})
                    if pd.notna(r["clicked_at"]):
                        cts = pd.to_datetime(r["clicked_at"], unit="s", errors="coerce")
                        events.append({"time": cts, "event": "clicked", "qid": r["query_id"][:20], "source": r["source"]})
                    if pd.notna(r["dismissed_at"]):
                        dts = pd.to_datetime(r["dismissed_at"], unit="s", errors="coerce")
                        events.append({"time": dts, "event": "dismissed", "qid": r["query_id"][:20], "source": r["source"]})

                if events:
                    edf = pd.DataFrame(events)
                    edf = edf.sort_values("time")
                    colors = {"returned": "#6366f1", "clicked": "#10b981", "dismissed": "#ef4444"}
                    fig_t = px.scatter(
                        edf, x="time", y="event", color="event",
                        color_discrete_map=colors,
                        hover_data=["qid", "source"],
                        opacity=0.8,
                    )
                    fig_t.update_traces(marker=dict(size=8, line=dict(width=0.5, color="#1f2937")))
                    fig_t.update_layout(
                        **DARK,
                        height=400,
                        margin=dict(t=10, b=10, l=10, r=10),
                        yaxis_title=None,
                    )
                    st.plotly_chart(fig_t, use_container_width=True)
                else:
                    st.info("No timeline events")
            else:
                st.info("No events to timeline")

            st.divider()
            st.markdown("#### Per-Query Breakdown")
            qdf = query(
                f"SELECT query_id, COUNT(*) total, "
                f"SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) clicks, "
                f"SUM(CASE WHEN dismissed_at IS NOT NULL THEN 1 ELSE 0 END) dismissals "
                f"FROM memory_ctr_feedback "
                f"WHERE 1=1 {src_where} {action_where} {qid_where} "
                f"GROUP BY query_id ORDER BY total DESC LIMIT 20",
                source_filter + action_params + qid_params,
            )
            if qdf is not None and not qdf.empty:
                qdf["ctr"] = (qdf["clicks"] / qdf["total"] * 100).round(1)
                qdf["dismissal_rate"] = (qdf["dismissals"] / qdf["total"] * 100).round(1)
                qdf_disp = qdf[["query_id", "total", "clicks", "dismissals", "ctr", "dismissal_rate"]].copy()
                qdf_disp.columns = ["Query ID", "Shown", "Clicked", "Dismissed", "CTR %", "Dismissal %"]
                st.dataframe(qdf_disp, use_container_width=True, hide_index=True)
            else:
                st.info("No queries match")

# ═══════════════════════════════════════════════════════════════════════════
# CRON
# ═══════════════════════════════════════════════════════════════════════════
with cron_tab:
    st.subheader("Cron / Background Jobs")
    logs = _get_cron_logs()
    log_map = {p.name: p for p in logs}

    jobs = [
        ("heartbeat",      "heartbeat.log",       "Daemon liveness",            "every 1 min",   "🫀"),
        ("integrity",      "integrity.log",        "DB integrity + FTS5 drift",  "every 6 h",     "🔍"),
        ("worker",         "worker.log",           "Background task executor",   "every 5 min",   "⚙️"),
        ("fts-rebuild",    "fts-rebuild.log",      "FTS5 index rebuild",        "on WAL trigger", "📑"),
        ("emb-recompute",  "embedding-recompute.log", "Embedding refresh",       "on schema change","🧠"),
        ("crdt-sync",      "crdt-sync.log",        "CRDT merge sync",           "on conflict",   "🔄"),
        ("digest",         "digest.log",           "Daily session digest",      "nightly",        "📰"),
    ]

    status_counts = Counter()
    job_statuses = []
    for name, filename, desc, trigger, emoji in jobs:
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
        job_statuses.append((name, filename, desc, trigger, emoji, sev, status_label, pct, exists, fp))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Jobs", len(jobs))
    c2.metric("Healthy", status_counts.get("ok", 0))
    c3.metric("Warnings", status_counts.get("warning", 0))
    c4.metric("Errors", status_counts.get("error", 0) + status_counts.get("failure", 0))

    st.divider()

    # ── Job cards ──
    for name, filename, desc, trigger, emoji, sev, status_label, pct, exists, fp in job_statuses:
        dot_cls = {"ok": "dot-green", "warning": "dot-yellow", "error": "dot-red", "failure": "dot-red"}.get(sev, "dot-gray")
        bar_color = {"ok": "#10b981", "warning": "#f59e0b", "error": "#ef4444", "failure": "#ef4444"}.get(sev, "#4b5563")

        st.markdown(f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">{emoji} {name}</span>
            <span class="{dot_cls} dot"></span>
          </div>
          <div class="card-sub">{desc} · `{trigger}`</div>
          <div class="progress-track">
            <div class="progress-fill" style="width:{pct}%;background:{bar_color}"></div>
          </div>
          <div class="card-body" style="margin-top:6px">{status_label} · <code>{filename}</code></div>
        </div>
        """, unsafe_allow_html=True)

        if exists:
            log_text = fp.read_text(errors="replace")
            lines = log_text.strip().split("\n")
            f1, f2 = st.columns([3, 1])
            with f1:
                log_search = st.text_input("Search", placeholder="filter lines...", key=f"ls_{name}", label_visibility="collapsed")
            with f2:
                tail_n = st.slider("Tail", 5, min(200, len(lines)), 40, key=f"tn_{name}", label_visibility="collapsed")
            if log_search:
                lines = [l for l in lines if log_search.lower() in l.lower()]
            shown = lines[-tail_n:] if tail_n > 0 else lines
            st.code("\n".join(shown), language="text")
        else:
            st.info(f"`{filename}` not found in {MEM_DIR.name}/")
# ═══════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════
with health_tab:
    st.subheader("Health & Integrity")
    doctor = _read_doctor()
    if doctor:
        sev = doctor.get("worst", "unknown")
        sev_badge = _badge_html(sev, sev.upper())

        st.markdown(
            f'<div class="card">'
            f'<div class="card-header">'
            f'<span class="card-title">🩺 Doctor Report</span>'
            f'{sev_badge}'
            f'</div>'
            f'<div class="card-sub">{doctor.get("ts","")[:19]} · {doctor.get("db_path","")}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        checks = doctor.get("checks", [])
        if checks:
            summary = Counter(c["severity"] for c in checks)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("✅ OK", summary.get("ok", 0))
            c2.metric("⚠️ Warning", summary.get("warning", 0))
            c3.metric("❌ Failure", summary.get("failure", 0))
            c4.metric("☠️ Error", summary.get("error", 0))

            st.divider()
            st.markdown("#### Checks")

            for c in checks:
                sev = c.get("severity", "ok")
                detail = c.get("detail", "")
                check_name = c.get("check", "?")
                fixable = c.get("fixable", False)

                dot_cls = {"ok": "dot-green", "warning": "dot-yellow", "failure": "dot-red", "error": "dot-red"}.get(sev, "dot-gray")

                with st.expander(f"<span class='{dot_cls} dot'></span> {check_name}", expanded=sev in ("warning", "failure", "error")):
                    c1, c2 = st.columns([3, 1])
                    with c1:
                        st.markdown(f"**Severity**: {sev.upper()}")
                        st.markdown(f"**Detail**: {detail}")
                    with c2:
                        if fixable:
                            st.markdown('<span class="badge-warn" style="display:inline-block;padding:0.15rem 0.5rem;border-radius:999px;font-size:0.65rem;font-weight:600;">🔧 FIXABLE</span>', unsafe_allow_html=True)
        else:
            st.json(doctor)
    else:
        st.info("No doctor report available — run `python memory_integrity.py memory/memory.db`")

    st.divider()
    st.markdown("#### Disk Usage")
    db_size = DB.stat().st_size
    mem_dir_size = sum(f.stat().st_size for f in MEM_DIR.rglob("*") if f.is_file())

    c1, c2, c3 = st.columns(3)
    with c1:
        db_pct = min(100, db_size / (500 * 1024 * 1024) * 100)
        db_color = "#10b981" if db_pct < 50 else "#f59e0b" if db_pct < 80 else "#ef4444"
        st.markdown(f"""
        <div class="card">
          <div class="card-sub">Database size</div>
          <div class="card-title">{db_size / 1024 / 1024:.1f} MB</div>
          <div class="progress-track">
            <div class="progress-fill" style="width:{db_pct}%;background:{db_color}"></div>
          </div>
        </div>
        """, unsafe_allow_html=True)
    with c2:
        dir_pct = min(100, mem_dir_size / (2 * 1024 * 1024 * 1024) * 100)
        dir_color = "#10b981" if dir_pct < 50 else "#f59e0b" if dir_pct < 80 else "#ef4444"
        st.markdown(f"""
        <div class="card">
          <div class="card-sub">Memory directory</div>
          <div class="card-title">{mem_dir_size / 1024 / 1024:.1f} MB</div>
          <div class="progress-track">
            <div class="progress-fill" style="width:{dir_pct}%;background:{dir_color}"></div>
          </div>
        </div>
        """, unsafe_allow_html=True)
    with c3:
        st.markdown(f"""
        <div class="card">
          <div class="card-sub">DB file path</div>
          <div class="card-body" style="word-break:break-all;margin-top:4px"><code>{DB}</code></div>
        </div>
        """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# BACKUPS
# ═══════════════════════════════════════════════════════════════════════════
with backups_tab:
    st.subheader("Backups")
    backup_dir = MEM_DIR / "backups"
    if backup_dir.exists():
        backups = sorted(
            [p for p in backup_dir.glob("*") if p.suffix in (".db", ".gz", ".db.gz") and not p.name.startswith(".")],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if backups:
            st.caption(f"{len(backups)} backup(s)")
            rows = []
            for bp in backups:
                mtime = datetime.fromtimestamp(bp.stat().st_mtime, tz=timezone.utc)
                rows.append(
                    {
                        "name": bp.name,
                        "size": f"{bp.stat().st_size / 1024 / 1024:.1f} MB",
                        "modified": mtime.strftime("%Y-%m-%d %H:%M UTC"),
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            if st.button("\U0001f4e4 Create backup now", use_container_width=True):
                import subprocess, gzip, shutil
                from datetime import date
                backup_name = f"memory-{date.today().isoformat()}.db.gz"
                backup_path = backup_dir / backup_name
                if backup_path.exists():
                    st.warning(f"Backup for today already exists: {backup_name}")
                else:
                    with open(DB, "rb") as fin, gzip.open(backup_path, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    st.success(f"Created {backup_name} ({backup_path.stat().st_size / 1024:.0f} KB)")
                    st.rerun()
        else:
            st.info("No backups found in memory/backups/")
            if st.button("\U0001f4e4 Create backup", use_container_width=True):
                import subprocess, gzip, shutil
                backup_dir.mkdir(parents=True, exist_ok=True)
                from datetime import date
                backup_name = f"memory-{date.today().isoformat()}.db.gz"
                backup_path = backup_dir / backup_name
                with open(DB, "rb") as fin, gzip.open(backup_path, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                st.success(f"Created {backup_name} ({backup_path.stat().st_size / 1024:.0f} KB)")
                st.rerun()
    else:
        st.info("No backups directory — backups not enabled or none taken yet")
        if st.button("\U0001f4e4 Create backup directory & backup", use_container_width=True):
            import gzip, shutil
            backup_dir.mkdir(parents=True, exist_ok=True)
            from datetime import date
            backup_name = f"memory-{date.today().isoformat()}.db.gz"
            backup_path = backup_dir / backup_name
            with open(DB, "rb") as fin, gzip.open(backup_path, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            st.success(f"Created {backup_name} ({backup_path.stat().st_size / 1024:.0f} KB)")
            st.rerun()

# ═══════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════
with audit_tab:
    st.subheader("Audit Log")

    df = query(
        "SELECT ts, tool, latency_ms, results_count, error, args "
        "FROM memory_audit_log ORDER BY ts DESC LIMIT 500"
    )
    if df is None or df.empty:
        st.info("No audit log entries yet.")
    else:
        df["ts_dt"] = pd.to_datetime(df["ts"], unit="s", errors="coerce")
        df["has_err"] = df["error"].notna()

        c1, c2, c3 = st.columns(3)
        c1.metric("Total Calls", len(df))
        c2.metric("Avg Latency", f"{df['latency_ms'].mean():.0f} ms")
        c3.metric("Error Rate", f"{df['has_err'].mean() * 100:.1f}%")

        col1, col2 = st.columns(2)
        with col1:
            tc = df["tool"].value_counts().reset_index()
            tc.columns = ["tool", "count"]
            fig = px.bar(
                tc, x="tool", y="count", color="count", color_continuous_scale="Viridis"
            )
            fig.update_layout(
                **DARK,
                xaxis_title=None,
                yaxis_title="Calls",
                margin=dict(t=10, b=10, l=10, r=10),
            )
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            agg = (
                df.groupby("tool")
                .agg(
                    avg_lat=("latency_ms", "mean"),
                    calls=("latency_ms", "count"),
                    errs=("has_err", "sum"),
                )
                .reset_index()
            )
            fig = px.scatter(
                agg,
                x="calls",
                y="avg_lat",
                size="errs",
                hover_name="tool",
                color="tool",
                title="Latency vs Calls",
            )
            fig.update_layout(**DARK, margin=dict(t=30, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

        errs = df[df["has_err"]]
        if not errs.empty:
            st.markdown("#### Recent Errors")
            st.dataframe(
                errs[["ts_dt", "tool", "error"]],
                use_container_width=True,
                hide_index=True,
            )

        with st.expander("Raw Log"):
            st.dataframe(
                df[["ts_dt", "tool", "latency_ms", "results_count", "error"]],
                use_container_width=True,
                hide_index=True,
            )

# ═══════════════════════════════════════════════════════════════════════════
# EXPLORER
# ═══════════════════════════════════════════════════════════════════════════
with search_tab:
    st.subheader("Memory Explorer")

    q_text = st.text_input(
        "Search (LIKE %term%)", placeholder="e.g. memory, agent, config"
    )
    if q_text:
        df = query(
            "SELECT id, substr(content,1,300) preview, category, "
            "created_at, pinned, fitness_score, tier "
            "FROM memories WHERE content LIKE ? "
            "ORDER BY created_at DESC LIMIT 50",
            (f"%{q_text}%",),
        )
        if df is None or df.empty:
            st.info("No matches")
        else:
            st.caption(f"{len(df)} results")
            for _, r in df.iterrows():
                _render_memory_content(r["id"])
    else:
        df = query(
            "SELECT id FROM memories ORDER BY created_at DESC LIMIT 20"
        )
        if df is not None and not df.empty:
            st.caption(f"Recent {len(df)} notes (enter a search term above)")
            for _, r in df.iterrows():
                _render_memory_content(r["id"])
