#!/usr/bin/env python3
"""Agentic Memory Dashboard — state-of-the-art local observability.

Run:
    cd ~/.config/agentic-memory
    venv/bin/streamlit run dashboard.py
"""

import json
import os
import sys
import sqlite3
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go

sys.path.insert(0, str(Path(__file__).parent))

# ── Page config ──────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Agentic Memory",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Theme CSS ────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    .main > div { padding: 0 1rem; }
    .stApp { background: #0e1117; }
    h1, h2, h3 { color: #f0f2f6 !important; font-weight: 600 !important; }
    .metric-card {
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-radius: 12px;
        padding: 1.2rem;
        text-align: center;
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
    .stTabs [data-baseweb="tab-list"] { gap: 0; }
    .stTabs [data-baseweb="tab"] {
        background: #1a1d23;
        border: 1px solid #2d3139;
        border-bottom: none;
        border-radius: 8px 8px 0 0;
        padding: 0.5rem 1.2rem;
        color: #8b8fa3;
        font-size: 0.85rem;
        font-weight: 500;
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
    /* Fix dark input fields */
    .stTextInput input { background: #1a1d23; color: #f0f2f6; border: 1px solid #2d3139; }
    .stSelectbox div[data-baseweb="select"] { background: #1a1d23; }
    .stSlider [data-baseweb="slider"] { margin-top: 0.5rem; }
    div[data-testid="stDataFrame"] { background: #1a1d23; }
    div[data-testid="stDataFrame"] td { color: #d1d5db; }
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


@st.cache_resource
def get_conn():
    """Open a read‑only ephemeral connection. Never migrates the schema."""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.execute("PRAGMA foreign_keys=ON")
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


# ── Sidebar ─────────────────────────────────────────────────────────────
st.sidebar.markdown(
    "<h2 style='margin-bottom:0'> Agentic Memory</h2>", unsafe_allow_html=True
)
st.sidebar.caption(f"`{DB.parent.name}/{DB.name}`")

with st.sidebar:
    st.divider()
    r = get_conn().execute("SELECT COUNT(*) FROM memories").fetchone()
    n_mem = r[0]
    r = get_conn().execute("SELECT COUNT(*) FROM kg_entities").fetchone()
    n_ent = r[0]
    r = get_conn().execute("SELECT COUNT(*) FROM kg_edges").fetchone()
    n_edg = r[0]

    c1, c2 = st.columns(2)
    c1.metric("Memories", n_mem)
    c2.metric("Entities", n_ent)
    c1.metric("Edges", n_edg)
    r = get_conn().execute("SELECT COUNT(*) FROM memory_audit_log").fetchone()
    c2.metric("Audit Events", r[0])

    st.divider()
    if st.button("Refresh", use_container_width=True):
        st.rerun()

# ── Tab helpers ─────────────────────────────────────────────────────────
TABS = [
    "Overview",
    "Knowledge Graph",
    "Embeddings",
    "Concept Drift",
    "CTR Feedback",
    "Audit Log",
    "Explorer",
]
overview_tab, kg_tab, embed_tab, drift_tab, ctr_tab, audit_tab, search_tab = st.tabs(
    TABS
)

# ═══════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════
with overview_tab:
    st.subheader("Overview")

    # ── Metric cards row ──
    r = get_conn().execute("SELECT COUNT(*) FROM memories WHERE pinned=1").fetchone()
    n_pin = r[0]
    r = (
        get_conn()
        .execute("SELECT COUNT(DISTINCT parent_id) FROM memory_chunks")
        .fetchone()
    )
    n_chk = r[0] or 0
    r = get_conn().execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()
    n_emb = r[0]
    r = get_conn().execute("SELECT COUNT(*) FROM memory_ctr_feedback").fetchone()
    n_ctr = r[0] if r else 0
    r = get_conn().execute("SELECT COUNT(*) FROM concept_drift").fetchone()
    n_dft = r[0] if r else 0

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

    # ── Creation timeline (smooth) ──
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
