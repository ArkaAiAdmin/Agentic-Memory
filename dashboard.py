#!/usr/bin/env python3
"""Agentic Memory Dashboard — state-of-the-art local observability.

Run:
    cd ~/.config/agentic-memory
    venv/bin/streamlit run dashboard.py
"""

import json
import os
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
    .stTabs [data-baseweb="tab-list"] { gap: 0; overflow-x: auto; flex-wrap: nowrap; }
    .stTabs [data-baseweb="tab"] {
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-bottom: none;
        border-radius: 8px 8px 0 0;
        padding: 0.4rem 0.9rem;
        color: #8b8fa3;
        font-size: 0.8rem;
        font-weight: 500;
        white-space: nowrap;
    }
    .stTabs [aria-selected="true"] {
        background: #0e1117;
        color: #f0f2f6;
        border-bottom: 2px solid #6b7280;
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
def get_conn():
    """Open a read‑only ephemeral connection. Never migrates the schema."""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
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
    if st.button("\U0001f504 Refresh", use_container_width=True):
        st.rerun()

# ── Helpers ──────────────────────────────────────────────────────────────
MEM_DIR

def _auto_refresh(interval_secs: int = 30) -> None:
    if st.button("\U0001f504 Refresh", key="top_refresh", use_container_width=True):
        st.rerun()


# ── Tabs ─────────────────────────────────────────────────────────────────
TABS = [
    "Overview",
    "Memories",
    "Sessions",
    "Knowledge Graph",
    "Embeddings",
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
    sessions_tab,
    kg_tab,
    embed_tab,
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
            pin_icon = "\U000f04d3 " if r.get("pinned") else ""
            cat_tag = f"`{r.get('category','—')}`" if r.get("category") else ""
            tier_tag = f"tier={r.get('tier','—')}" if r.get("tier") else ""
            fit_tag = f"f={r['fitness_score']:.2f}" if pd.notna(r.get("fitness_score")) else ""
            imp_tag = f"imp={r['importance']}" if pd.notna(r.get("importance")) else ""
            tags = " &nbsp;·&nbsp; ".join(t for t in [cat_tag, tier_tag, fit_tag, imp_tag] if t)
            st.markdown(
                f"**{pin_icon}{r['id'][:48]}**  \n"
                f"_{r['created_at']}_ &nbsp;·&nbsp; {tags}  \n"
                f"{r['preview']}..."
            )
            st.divider()
    else:
        st.info("No memories match the filters")

# ═══════════════════════════════════════════════════════════════════════════
# SESSIONS
# ═══════════════════════════════════════════════════════════════════════════
with sessions_tab:
    st.subheader("Sessions")
    if table("memory_sessions"):
        s_df = query(
            "SELECT id, agent_id, started_at, ended_at, context_query, note_count, status "
            "FROM memory_sessions ORDER BY started_at DESC LIMIT 100"
        )
        if s_df is not None and not s_df.empty:
            s_df["started"] = pd.to_datetime(s_df["started_at"], unit="s", errors="coerce")
            s_df["duration"] = "—"
            for i, r in s_df.iterrows():
                if pd.notna(r.get("ended_at")):
                    dur = r["ended_at"] - r["started_at"]
                    if dur > 3600:
                        s_df.at[i, "duration"] = f"{dur/3600:.1f}h"
                    elif dur > 60:
                        s_df.at[i, "duration"] = f"{dur/60:.0f}m"
                    else:
                        s_df.at[i, "duration"] = f"{dur:.0f}s"
                else:
                    s_df.at[i, "duration"] = "active"

            s_disp = s_df[["id", "agent_id", "started", "duration", "context_query", "note_count", "status"]].copy()
            s_disp.columns = ["ID", "Agent", "Started", "Duration", "Query", "Notes", "Status"]
            st.dataframe(s_disp, use_container_width=True, hide_index=True)

            active = (s_df["status"] == "active").sum()
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Sessions", len(s_df))
            c2.metric("Active", active)
            c3.metric("Total Notes", int(s_df["note_count"].sum()))
        else:
            st.info("No sessions recorded")
    else:
        st.info("Table `memory_sessions` not available yet")

# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ═══════════════════════════════════════════════════════════════════════════
with kg_tab:
    st.subheader("Knowledge Graph")
    import networkx as nx

    max_n = st.slider("Entity count", 10, 200, 80, key="kg_n")

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
                        weight=float(r.get("weight", 1)),
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
        lim = st.slider("Sample", 50, min(1000, n_emb), min(400, n_emb), key="emb_n")

        df = query(
            "SELECT e.memory_id, e.embedding, e.dim, m.category "
            "FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id "
            f"LIMIT {lim}"
        )
        if df is not None and not df.empty:
            dim = int(df["dim"].iloc[0])
            vecs, cats, mids = [], [], []
            for _, r in df.iterrows():
                try:
                    v = np.frombuffer(r["embedding"], dtype=np.float32)
                    if len(v) == dim:
                        vecs.append(v)
                        cats.append(r.get("category", "?") or "?")
                        mids.append(r["memory_id"])
                except Exception:
                    pass

            if len(vecs) >= 3:
                with st.spinner("Computing PCA ..."):
                    mat = np.stack(vecs)
                    mc = mat - mat.mean(axis=0)
                    _, S, Vt = np.linalg.svd(mc, full_matrices=False)
                    p = mc @ Vt[:2].T

                pdf = pd.DataFrame(
                    {
                        "x": p[:, 0],
                        "y": p[:, 1],
                        "category": cats,
                        "memory_id": mids,
                    }
                )
                fig = px.scatter(
                    pdf,
                    x="x",
                    y="y",
                    color="category",
                    hover_name="memory_id",
                    opacity=0.7,
                    color_discrete_sequence=px.colors.qualitative.Set2,
                )
                fig.update_traces(
                    marker=dict(size=5, line=dict(width=0.5, color="#333"))
                )
                fig.update_layout(
                    title=f"PCA ({len(vecs)} pts, {dim}D → 2D)",
                    **DARK,
                    height=650,
                    margin=dict(t=30, b=10, l=10, r=10),
                )
                var = (S[:2] ** 2) / (S**2).sum() * 100
                fig.add_annotation(
                    xref="paper",
                    yref="paper",
                    x=0,
                    y=1.08,
                    text=f"PC1: {var[0]:.0f}% &nbsp; PC2: {var[1]:.0f}%",
                    showarrow=False,
                    font=dict(size=11, color="#9ca3af"),
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info(f"Need ≥3 vectors, got {len(vecs)}")

# ═══════════════════════════════════════════════════════════════════════════
# CONCEPT DRIFT
# ═══════════════════════════════════════════════════════════════════════════
with drift_tab:
    st.subheader("Concept Drift")

    if not table("concept_drift"):
        st.info(
            "Table `concept_drift` not yet created. Call `memory_check_concept_drift()` to start."
        )
    else:
        df = query(
            "SELECT id, drift_metric, drifted_dimensions, triggered_at, acknowledged "
            "FROM concept_drift ORDER BY triggered_at DESC LIMIT 200"
        )
        if df is None or df.empty:
            st.info(
                "No drift events recorded. Run `memory_check_concept_drift()` first."
            )
        else:
            df["ts"] = pd.to_datetime(df["triggered_at"], unit="s", errors="coerce")
            df = df.dropna(subset=["ts"]).sort_values("ts")

            c1, c2, c3 = st.columns(3)
            c1.metric("Events", len(df))
            latest = df["drift_metric"].iloc[-1]
            c2.metric("Latest Drift", f"{latest:.3f}")
            c3.metric("Above Threshold", sum(df["drift_metric"] > 0.15))

            fig = px.line(
                df, x="ts", y="drift_metric", markers=True, line_shape="spline"
            )
            fig.update_traces(
                line=dict(width=3, color="#ef4444", shape="spline", smoothing=1.3),
                marker=dict(size=6, color="#ef4444"),
            )
            fig.add_hline(
                y=0.15,
                line_dash="dash",
                line_color="#f59e0b",
                annotation_text="threshold (0.15)",
            )
            fig.update_layout(
                **DARK,
                xaxis_title=None,
                yaxis_title="Drift Metric",
                margin=dict(t=10, b=10, l=10, r=10),
            )
            st.plotly_chart(fig, use_container_width=True)

            with st.expander("Drift Events Detail"):
                disp = df[["id", "ts", "drift_metric", "acknowledged"]].copy()
                disp.columns = ["ID", "Timestamp", "Drift", "Acknowledged"]
                st.dataframe(disp, use_container_width=True, hide_index=True)

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
        df = query(
            "SELECT id, query_id, returned_at, clicked_at, dismissed_at, source "
            "FROM memory_ctr_feedback ORDER BY returned_at DESC LIMIT 500"
        )
        if df is None or df.empty:
            st.info("No CTR data yet.")
        else:
            total = len(df)
            clk = df["clicked_at"].notna().sum()
            dsm = df["dismissed_at"].notna().sum()
            ctr = clk / total * 100 if total > 0 else 0

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Results Returned", total)
            c2.metric("Clicked", clk)
            c3.metric("Dismissed", dsm)
            c4.metric("CTR", f"{ctr:.1f}%")

            fd = pd.DataFrame(
                {
                    "stage": ["Returned", "Clicked", "Dismissed"],
                    "count": [total, clk, dsm],
                }
            )
            fig = px.funnel(
                fd,
                x="count",
                y="stage",
                color_discrete_sequence=["#6366f1", "#10b981", "#ef4444"],
            )
            fig.update_layout(**DARK, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

            src = query(
                "SELECT COALESCE(source,'unknown') src, COUNT(*) total, "
                "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) clicks, "
                "SUM(CASE WHEN dismissed_at IS NOT NULL THEN 1 ELSE 0 END) dismissals "
                "FROM memory_ctr_feedback GROUP BY src"
            )
            if src is not None and not src.empty:
                src["ctr"] = (src["clicks"] / src["total"] * 100).round(1)
                fig = px.bar(
                    src,
                    x="src",
                    y=["total", "clicks", "dismissals"],
                    barmode="group",
                    title="By Source",
                    color_discrete_sequence=["#6366f1", "#10b981", "#ef4444"],
                )
                fig.update_layout(
                    **DARK,
                    margin=dict(t=30, b=10, l=10, r=10),
                    xaxis_title=None,
                    yaxis_title="Count",
                )
                st.plotly_chart(fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════
# CRON
# ═══════════════════════════════════════════════════════════════════════════
with cron_tab:
    st.subheader("Cron / Background Jobs")
    logs = _get_cron_logs()
    if logs:
        selected = st.selectbox("Log file", [p.name for p in logs], key="cron_file")
        if selected:
            log_path = MEM_DIR / selected
            log_text = log_path.read_text(errors="replace")
            lines = log_text.strip().split("\n")
            tail_lines = st.slider("Tail (last N lines)", 10, min(500, len(lines)), 50, key="cron_tail")
            shown = lines[-tail_lines:] if tail_lines > 0 else lines
            st.code("\n".join(shown), language="text")
    else:
        st.info("No log files found in memory directory")

    st.divider()
    st.markdown("#### Cron-Style Status Overview")
    jobs_info = [
        ("heartbeat", "heartbeat.log", "Check daemon liveness"),
        ("integrity", "integrity.log", "Data integrity checks"),
        ("worker", "worker.log", "Background worker"),
        ("rebuild", ".rebuild.lock", "Index rebuild lock"),
        ("vec_rebuild", ".vec_rebuild.lock", "Vector rebuild lock"),
    ]
    for name, filename, desc in jobs_info:
        fp = MEM_DIR / filename
        exists = fp.exists()
        status = "ok" if exists else "gray"
        label = "file present" if exists else "not found"
        size_hint = f" ({fp.stat().st_size / 1024:.1f} KB)" if exists and filename.endswith(".log") else ""
        st.markdown(
            f"<span class='dot-{status}'></span>"
            f"<strong>{name}</strong> &nbsp;–&nbsp; {desc}{size_hint}",
            unsafe_allow_html=True,
        )

# ═══════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════
with health_tab:
    st.subheader("Health & Integrity")
    doctor = _read_doctor()
    if doctor:
        sev = doctor.get("worst", "unknown")
        st.markdown(
            f"**Doctor Report** &nbsp;·&nbsp; "
            f"{_badge_html(sev, sev.upper())} ",
            unsafe_allow_html=True,
        )
        sections = doctor.get("sections", {})
        for sec_name, sec_data in sections.items():
            with st.expander(f"{sec_name}", expanded=sec_data.get("severity") == "failure"):
                st.json(sec_data)
    else:
        st.info("No doctor report available — run `python memory_integrity.py memory/memory.db`")

    st.divider()
    st.markdown("#### Disk Usage")
    db_size = DB.stat().st_size
    st.metric("Database size", f"{db_size / 1024 / 1024:.1f} MB")
    mem_dir_size = sum(f.stat().st_size for f in MEM_DIR.rglob("*") if f.is_file())
    st.metric("Memory directory size", f"{mem_dir_size / 1024 / 1024:.1f} MB")
    st.metric("DB file path", str(DB))

# ═══════════════════════════════════════════════════════════════════════════
# BACKUPS
# ═══════════════════════════════════════════════════════════════════════════
with backups_tab:
    st.subheader("Backups")
    backup_dir = MEM_DIR / "backups"
    if backup_dir.exists():
        backups = sorted(backup_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
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
        else:
            st.info("No backups found in memory/backups/")
    else:
        st.info("No backups directory — backups not enabled or none taken yet")

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
                pin = "" if not r.get("pinned") else ""
                cat = r.get("category") or "—"
                tier = r.get("tier") or "—"
                fs = (
                    f"fitness={r['fitness_score']:.2f}"
                    if pd.notna(r.get("fitness_score"))
                    else ""
                )
                st.markdown(
                    f"**{pin}{r['id'][:50]}** &nbsp;·&nbsp; `{cat}` &nbsp;·&nbsp; tier={tier} &nbsp;·&nbsp; {fs}  \n"
                    f"_{r['created_at']}_  \n"
                    f"{r['preview']}...  "
                )
                st.divider()
    else:
        # Show recent notes as default
        df = query(
            "SELECT id, substr(content,1,200) preview, category, "
            "created_at, pinned, fitness_score, tier "
            "FROM memories ORDER BY created_at DESC LIMIT 20"
        )
        if df is not None and not df.empty:
            st.caption(f"Recent {len(df)} notes (enter a search term above)")
            for _, r in df.iterrows():
                pin = "" if not r.get("pinned") else ""
                cat = r.get("category") or "—"
                tier = r.get("tier") or "—"
                fs = (
                    f"fitness={r['fitness_score']:.2f}"
                    if pd.notna(r.get("fitness_score"))
                    else ""
                )
                st.markdown(
                    f"**{pin}{r['id'][:50]}** &nbsp;·&nbsp; `{cat}` &nbsp;·&nbsp; tier={tier} &nbsp;·&nbsp; {fs}  \n"
                    f"_{r['created_at']}_  \n"
                    f"{r['preview']}...  "
                )
                st.divider()
