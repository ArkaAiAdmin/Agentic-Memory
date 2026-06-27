#!/usr/bin/env python3
"""Agentic Memory Dashboard — full operational surface.

Run:
    cd ~/.config/agentic-memory
    venv/bin/streamlit run dashboard.py

Or via CLI:
    agentic-memory dashboard start
"""

import json
import os
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

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
    .nav-section {
        color: #6b7280;
        font-size: 0.7rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-top: 0.8rem;
        margin-bottom: 0.2rem;
        padding-left: 0.5rem;
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
    .stTextInput input { background: #1a1d23; color: #f0f2f6; border: 1px solid #2d3139; }
    .stSelectbox div[data-baseweb="select"] { background: #1a1d23; }
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


# ── DB resolution ────────────────────────────────────────────────────────
@st.cache_resource
def resolve_db() -> Path:
    from infrastructure import resolve_active_memory_dir

    return resolve_active_memory_dir() / "memory.db"


DB = resolve_db()
if not DB.exists():
    st.error(f"Database not found: {DB}")
    st.stop()

_NAV_DEFAULTS: dict[str, str] = {
    "_nav_analytics": "Overview",
    "_nav_manage": "Memories",
    "_nav_system": "Audit Log",
}
_SECTION_KEYS = ("_nav_analytics", "_nav_manage", "_nav_system")


def _make_nav_handler(clicked_key: str):
    def handler() -> None:
        for k in _SECTION_KEYS:
            if k != clicked_key:
                st.session_state.pop(k, None)

    return handler


@st.cache_resource
def get_conn():
    c = sqlite3.connect(
        f"file:{DB}?mode=ro", uri=True, timeout=30, check_same_thread=False
    )
    c.execute("PRAGMA foreign_keys=ON")
    return c


def query(sql: str, params=()) -> pd.DataFrame | None:
    try:
        return pd.read_sql_query(sql, get_conn(), params=params)
    except Exception:
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


def try_count(table: str, where: str | None = None) -> int:
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        r = get_conn().execute(sql).fetchone()
        return r[0] if r else 0
    except Exception:
        return 0


def get_mem_dir() -> Path:
    return DB.parent


_analytics_handler = _make_nav_handler("_nav_analytics")
_manage_handler = _make_nav_handler("_nav_manage")
_system_handler = _make_nav_handler("_nav_system")

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
    r = get_conn().execute("SELECT COUNT(*) FROM kg_facts").fetchone()
    n_facts = r[0]
    r = get_conn().execute("SELECT COUNT(*) FROM memory_audit_log").fetchone()
    n_audit = r[0]

    c1, c2 = st.columns(2)
    c1.metric("Memories", n_mem)
    c2.metric("Entities", n_ent)
    c1.metric("Facts", n_facts)
    c2.metric("Audit", n_audit)

    st.divider()

    # ── Quick actions ────────────────────────────────────────────────────
    st.markdown('<div class="nav-section">Quick Actions</div>', unsafe_allow_html=True)
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Refresh", use_container_width=True):
            st.rerun()
    with col_b:
        if st.button("Doctor", use_container_width=True):
            st.session_state["_nav"] = "Health"

    # ── Navigation ───────────────────────────────────────────────────────
    st.markdown('<div class="nav-section">Analytics</div>', unsafe_allow_html=True)
    analytics_items = ["Overview", "Knowledge Graph", "Embeddings", "Drift", "CTR"]
    for item in analytics_items:
        active = st.session_state.get("_nav_analytics", "Overview") == item
        label = f"**◆ {item}**" if active else item
        if st.button(label, key=f"nav_a_{item}", use_container_width=True):
            st.session_state["_nav_analytics"] = item
            for k in ("_nav_manage", "_nav_system"):
                st.session_state.pop(k, None)
            st.rerun()

    st.markdown('<div class="nav-section">Manage</div>', unsafe_allow_html=True)
    manage_items = ["Memories", "Entities", "Explorer"]
    for item in manage_items:
        active = st.session_state.get("_nav_manage", "Memories") == item
        label = f"**◆ {item}**" if active else item
        if st.button(label, key=f"nav_m_{item}", use_container_width=True):
            st.session_state["_nav_manage"] = item
            for k in ("_nav_analytics", "_nav_system"):
                st.session_state.pop(k, None)
            st.rerun()

    st.markdown('<div class="nav-section">System</div>', unsafe_allow_html=True)
    system_items = ["Audit Log", "Health", "Hooks"]
    for item in system_items:
        active = st.session_state.get("_nav_system", "Audit Log") == item
        label = f"**◆ {item}**" if active else item
        if st.button(label, key=f"nav_s_{item}", use_container_width=True):
            st.session_state["_nav_system"] = item
            for k in ("_nav_analytics", "_nav_manage"):
                st.session_state.pop(k, None)
            st.rerun()

    _saved_nav = st.session_state.pop("_nav", None)
    nav = st.session_state.get("_nav_analytics", "Overview")
    nav2 = st.session_state.get("_nav_manage", "Memories")
    nav3 = st.session_state.get("_nav_system", "Audit Log")

    if _saved_nav:
        page = _saved_nav
    elif nav2 != "Memories":
        page = nav2
    elif nav3 != "Audit Log":
        page = nav3
    else:
        page = nav

# ═══════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════
if page == "Overview":
    st.subheader("Overview")

    r = get_conn().execute("SELECT COUNT(*) FROM memories WHERE pinned=1").fetchone()
    n_pin = r[0]
    try:
        r = (
            get_conn()
            .execute("SELECT COUNT(DISTINCT parent_id) FROM memory_chunks")
            .fetchone()
        )
        n_chk = r[0] or 0
    except sqlite3.OperationalError:
        n_chk = 0
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
            ("Audit Events", try_count("memory_audit_log"), "audit trail"),
            ("Facts", try_count("kg_facts"), "knowledge graph"),
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

    st.caption(
        f"ARC: {try_count('arc_stats')} entries, {try_count('arc_ghosts')} evictions  ·  "
        f"Sync: {try_count('kg_entity_crdt') + try_count('kg_edge_crdt') + try_count('memory_field_crdt')} pending CRDT ops"
    )

    st.markdown("<br>", unsafe_allow_html=True)

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
            fig = px.bar(
                df, x="tier", y="cnt", color="cnt", color_continuous_scale="Viridis"
            )
            fig.update_layout(
                **DARK,
                showlegend=False,
                xaxis_title=None,
                yaxis_title="Count",
                margin=dict(t=10, b=10, l=10, r=10),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No tier data")

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

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### Top Tags")
        df = query("SELECT tags FROM memories WHERE tags != '[]' LIMIT 1000")
        if df is not None and not df.empty:
            c: Counter = Counter()
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

    st.markdown("#### MCP Tool Activity")
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
# MEMORIES — CRUD + detail sheet
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Memories":
    st.subheader("Memories")

    # Filters
    col1, col2, col3 = st.columns(3)
    with col1:
        search = st.text_input("Search content", placeholder="keyword...")
    with col2:
        cat_filter = st.text_input("Category", placeholder="e.g. lessons")
    with col3:
        tier_filter = st.selectbox(
            "Tier", ["All", "hot", "warm", "cold", "unassigned"], index=0
        )

    sql = "SELECT id, substr(content,1,200) preview, category, created_at, pinned, fitness_score, tier FROM memories WHERE 1=1"
    params = []
    if search:
        sql += " AND content LIKE ?"
        params.append(f"%{search}%")
    if cat_filter:
        sql += " AND category = ?"
        params.append(cat_filter)
    if tier_filter != "All":
        sql += " AND COALESCE(tier,'unassigned') = ?"
        params.append(tier_filter)
    sql += " ORDER BY created_at DESC LIMIT 1000"

    df = query(sql, tuple(params))
    if df is None or df.empty:
        st.info("No memories match")
    else:
        st.caption(f"{len(df)} result(s)")
        for _, r in df.iterrows():
            pin = "📌 " if r.get("pinned") else ""
            cat = r.get("category") or "—"
            tier = r.get("tier") or "—"
            fs = (
                f"fitness={r['fitness_score']:.2f}"
                if pd.notna(r.get("fitness_score"))
                else ""
            )
            label = f"{pin}{r['id'][:40]}"
            with st.expander(f"`{cat}` · tier={tier} · {fs} — {r['created_at'][:10]}"):
                st.markdown(f"**ID:** `{r['id']}`")
                st.markdown(f"**Category:** `{cat}`  ·  **Tier:** `{tier}`")
                st.markdown(
                    f"**Pinned:** {bool(r.get('pinned'))}  ·  **Fitness:** {r.get('fitness_score', '?')}"
                )
                full = query("SELECT content FROM memories WHERE id=?", (r["id"],))
                if full is not None and not full.empty:
                    st.text_area(
                        "Content",
                        full["content"].iloc[0],
                        height=200,
                        key=f"mem-{r['id'][:20]}",
                    )
                tags_row = query("SELECT tags FROM memories WHERE id=?", (r["id"],))
                if tags_row is not None and not tags_row.empty:
                    try:
                        tags = json.loads(tags_row["tags"].iloc[0])
                        st.caption("Tags: " + ", ".join(f"`{t}`" for t in tags))
                    except Exception:
                        pass
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Pin/Unpin", key=f"pin-{r['id'][:20]}"):
                        try:
                            with sqlite3.connect(str(DB), timeout=5) as c2:
                                c2.execute(
                                    "UPDATE memories SET pinned=NOT pinned WHERE id=?",
                                    (r["id"],),
                                )
                            st.rerun()
                        except Exception as exc:
                            st.error(exc)
                with c2:
                    tier_opts = ["hot", "warm", "cold", "unassigned"]
                    new_tier = st.selectbox(
                        "Set tier",
                        tier_opts,
                        index=tier_opts.index(tier) if tier in tier_opts else 3,
                        key=f"tier-{r['id'][:20]}",
                    )
                    if new_tier != tier:
                        try:
                            with sqlite3.connect(str(DB), timeout=5) as c2:
                                c2.execute(
                                    "UPDATE memories SET tier=? WHERE id=?",
                                    (new_tier, r["id"]),
                                )
                            st.rerun()
                        except Exception as exc:
                            st.error(exc)


# ═══════════════════════════════════════════════════════════════════════════
# ENTITIES
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Entities":
    st.subheader("Knowledge Graph Entities")

    max_n = st.slider("Show top N", 10, 300, 80)
    ent = query(
        "SELECT id, name, entity_type, mentions "
        "FROM kg_entities ORDER BY mentions DESC LIMIT ?",
        (max_n,),
    )
    if ent is None or ent.empty:
        st.info("No entities yet")
    else:
        st.caption(f"{len(ent)} entities")
        for _, r in ent.iterrows():
            with st.expander(
                f"`{r['entity_type']}` · {r['name']} (mentions: {r['mentions']})"
            ):
                st.markdown(f"**ID:** `{r['id']}`  ·  **Type:** `{r['entity_type']}`")
                efacts = query(
                    "SELECT f.predicate, f.object, f.confidence "
                    "FROM kg_facts f "
                    "WHERE f.subject_entity_id = ? OR "
                    "  (f.subject = ? AND f.subject_entity_id IS NULL) "
                    "ORDER BY f.confidence DESC LIMIT 20",
                    (r["id"], r["name"]),
                )
                if efacts is not None and not efacts.empty:
                    st.markdown("**Facts (as subject):**")
                    for _, e in efacts.iterrows():
                        st.caption(
                            f"  `{e['predicate']}` → {e['object']}  "
                            f"(conf: {e['confidence']:.2f})"
                        )


# ═══════════════════════════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Webhooks":
    st.subheader("Webhooks")
    try:
        table_ok = table("memory_webhooks")
    except Exception as exc:
        table_ok = False
    if not table_ok:
        st.info(
            "No webhooks table yet. Populate from MCP tools when memory_sharing is enabled."
        )
    else:
        try:
            wh = query(
                "SELECT id, url, event_types, active, last_triggered_at, created_at FROM memory_webhooks ORDER BY created_at DESC"
            )
        except Exception as exc:
            st.warning(f"Could not read webhooks table: {exc}")
            wh = None
        if wh is not None and not wh.empty:
            st.markdown(f"**{len(wh)} registered webhook(s)**")
            for _, r in wh.iterrows():
                with st.expander(
                    f"{'🟢' if r.get('active') else '🔴'} `{str(r.get('url', ''))[:70]}`"
                ):
                    st.markdown(
                        f"**ID:** `{r['id']}`  ·  **Active:** {bool(r.get('active'))}"
                    )
                    try:
                        events = json.loads(r.get("event_types") or "[]")
                        st.caption("Events: " + ", ".join(f"`{e}`" for e in events))
                    except Exception:
                        st.caption(f"Events: {r.get('event_types')}")
                    last = r.get("last_triggered_at")
                    st.caption(
                        f"Last triggered: {datetime.fromtimestamp(last).isoformat()[:19] if last else 'never'}"
                    )
                    st.caption(f"Created: {r.get('created_at', '?')}")
                    if st.button("Delete", key=f"delwh-{str(r['id'])[:30]}"):
                        try:
                            with sqlite3.connect(str(DB), timeout=5) as conn:
                                conn.execute(
                                    "DELETE FROM memory_webhooks WHERE id=?",
                                    (str(r["id"]),),
                                )
                            st.rerun()
                        except Exception as exc2:
                            st.error(exc2)
        else:
            st.info("No webhooks registered")

        st.divider()
        st.markdown("#### Register new webhook")
        with st.form("new_webhook"):
            col1, col2 = st.columns([3, 1])
            with col1:
                wh_url = st.text_input(
                    "Webhook URL", placeholder="https://example.com/memory-hook"
                )
            with col2:
                wh_active = st.checkbox("Active", value=True)
            wh_events = st.multiselect(
                "Events",
                [
                    "memory.created",
                    "memory.updated",
                    "memory.deleted",
                    "entity.created",
                    "compaction.done",
                    "error.spike",
                ],
                default=["memory.created", "memory.updated"],
            )
            if st.form_submit_button("Register") and wh_url.strip():
                try:
                    wid = f"wh-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
                    with sqlite3.connect(str(DB), timeout=5) as conn:
                        conn.execute(
                            "INSERT INTO memory_webhooks (id, url, event_types, active, created_at) VALUES (?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER))",
                            (
                                wid,
                                wh_url.strip(),
                                json.dumps(wh_events),
                                int(wh_active),
                            ),
                        )
                    st.success("Webhook registered")
                    st.rerun()
                except Exception as exc:
                    st.error(exc)


# ═══════════════════════════════════════════════════════════════════════════
# HEALTH / SYSTEM
# ═══════════════════════════════════════════════════════════════════════════


elif page == "Health":
    st.subheader("System Health")

    # Try loading doctor report
    doctor_path = get_mem_dir() / "doctor_report.json"
    if doctor_path.exists():
        try:
            report = json.loads(doctor_path.read_text())
            worst = report.get("worst", "ok")
            if worst == "ok":
                st.success(f"All checks passed · {report['ts'][:19]}")
            elif worst == "warning":
                st.warning(f"Warnings found · {report['ts'][:19]}")
            else:
                st.error(f"Failures found · {report['ts'][:19]}")

            for chk in report.get("checks", []):
                sev = str(chk.get("severity", "?"))
                icon = {"ok": "✅", "warning": "⚠️", "failure": "❌", "info": "ℹ️"}.get(
                    sev, "?"
                )
                with st.expander(
                    f"{icon} [{sev.upper()}] {chk['check']}: {chk['detail']}"
                ):
                    st.caption(f"Checked at {str(chk.get('ts', '?'))[:19]}")
                    if chk.get("fixable"):
                        st.caption(
                            "🔧 Auto-repairable with: `agentic-memory doctor --fix`"
                        )
            st.caption(f"Report: {doctor_path}")
        except Exception as exc:
            st.warning(f"Could not read report: {exc}")
    else:
        st.info(f"No doctor report yet. Run: `agentic-memory doctor`")

    st.divider()

    # Live stats
    c1, c2, c3, c4 = st.columns(4)
    db_size_mb = DB.stat().st_size / 1024 / 1024
    c1.metric("DB Size", f"{db_size_mb:.1f} MB")
    c2.metric("Total Memories", n_mem)
    c3.metric(
        "Pinned",
        int(
            get_conn()
            .execute("SELECT COUNT(*) FROM memories WHERE pinned=1")
            .fetchone()[0]
        ),
    )
    c4.metric("Schema", "v22")

    try:
        from migration_runner import SCHEMA_VERSION
        import sqlite3 as sq

        with sq.connect(str(DB), timeout=3) as conn:
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id=1"
            ).fetchone()
            db_ver = row[0] if row else "?"
            match = "✅" if db_ver == SCHEMA_VERSION else "❌"
            st.caption(f"Schema: {match} DB=v{db_ver}, code=v{SCHEMA_VERSION}")
    except Exception as exc:
        st.caption(f"Schema check failed: {exc}")

    st.divider()
    st.markdown("#### Auto-save Queue")
    for f in sorted((get_mem_dir() / "sessions").glob("auto-*.md"))[-10:]:
        st.caption(f"`{f.name}`  ({f.stat().st_size:,} bytes)")


# ═══════════════════════════════════════════════════════════════════════════
# HOOKS
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Hooks":
    st.subheader("Hook Events & Circuit Breakers")

    hook_errors = get_mem_dir() / "hook-errors.jsonl"
    if not hook_errors.exists():
        st.info("No hook errors logged yet")
    else:
        try:
            lines = hook_errors.read_text(errors="replace").splitlines()
            entries = [json.loads(l) for l in lines[-200:] if l.strip()]

            # Circuit breaker summary
            circuit_counts: dict[str, dict] = {}
            for e in entries:
                label = e.get("label", "?")
                count = e.get("failureCount", e.get("failure_count", 0))
                if label not in circuit_counts:
                    circuit_counts[label] = {
                        "failures": 0,
                        "max_failures": 0,
                        "entries": 0,
                    }
                circuit_counts[label]["failures"] = count
                circuit_counts[label]["max_failures"] = max(
                    circuit_counts[label]["max_failures"], count
                )
                circuit_counts[label]["entries"] += 1

            st.markdown("#### Circuit Breaker Status")
            cb_data = []
            for label, info in sorted(circuit_counts.items()):
                is_open = info["max_failures"] >= 10
                cb_data.append(
                    {
                        "Label": label,
                        "Current Failures": info["failures"],
                        "Peak": info["max_failures"],
                        "Open?": "🔴 OPEN" if is_open else "🟢 closed",
                        "Log Entries": info["entries"],
                    }
                )
            if cb_data:
                st.dataframe(
                    pd.DataFrame(cb_data), use_container_width=True, hide_index=True
                )

            # Error timeline
            st.markdown("#### Recent Errors (last 200)")
            errs = [
                e for e in entries if e.get("error") or e.get("failureCount", 0) >= 3
            ]
            if errs:
                edf = pd.DataFrame(
                    [
                        {
                            "ts": datetime.fromtimestamp(e["ts"] / 1000).isoformat()[
                                :19
                            ],
                            "label": e.get("label", "?"),
                            "exit": e.get("code") or e.get("exit_code", "?"),
                            "error": str(e.get("error", ""))[:120],
                            "failures": e.get(
                                "failureCount", e.get("failure_count", 0)
                            ),
                        }
                        for e in reversed(errs[-50:])
                    ]
                )
                st.dataframe(edf, use_container_width=True, hide_index=True)
            else:
                st.success("No recent errors")
        except Exception as exc:
            st.error(f"Could not parse hook-errors.jsonl: {exc}")

    st.divider()
    st.markdown("#### Context Monitor State")
    state_file = get_mem_dir() / "sessions" / ".context_monitor_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Total Tool Calls",
                state.get("total_tool_calls", state.get("tool_call_count", 0)),
            )
            c2.metric("Since Checkpoint", state.get("tools_since_checkpoint", "?"))
            checkpoint_due = state.get("checkpoint_due", False)
            c3.metric("Checkpoint Due", "Yes" if checkpoint_due else "No")
            lc = state.get("last_compaction_time", 0)
            if lc:
                st.caption(
                    f"Last compaction: {datetime.fromtimestamp(lc).isoformat()[:19]}"
                )
        except Exception as exc:
            st.warning(exc)
    else:
        st.info("No state file (no session started)")

    st.divider()
    st.markdown("#### Auto-save Allowlist")
    try:
        from auto_save import _resolve_allowlist, _tool_name_matches

        al = _resolve_allowlist()
        if al is None:
            st.success("Unrestricted (`allowlist=*`) — all tools allowed")
        else:
            st.caption(f"{len(al)} tools in allowlist:")
            st.code("\n".join(sorted(al)), language="text")
    except Exception as exc:
        st.warning(str(exc))


# ═══════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Audit Log":
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
# KNOWLEDGE GRAPH
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Knowledge Graph":
    st.subheader("Knowledge Graph")
    import networkx as nx

    max_n = st.slider("Fact count", 50, 500, 200, key="kg_n")
    facts = query(
        "SELECT f.subject, f.predicate, f.object, f.confidence, "
        "  f.subject_entity_id, f.object_entity_id, "
        "  e1.name AS subj_entity_name, e1.entity_type AS subj_entity_type, "
        "  e2.name AS obj_entity_name, e2.entity_type AS obj_entity_type "
        "FROM kg_facts f "
        "LEFT JOIN kg_entities e1 ON e1.id = f.subject_entity_id "
        "LEFT JOIN kg_entities e2 ON e2.id = f.object_entity_id "
        "ORDER BY f.confidence DESC, f.last_seen DESC LIMIT ?",
        (max_n,),
    )
    if facts is None or facts.empty:
        st.info("No facts")
    else:
        G = nx.Graph()
        node_types: dict[str, str] = {}
        node_counts: Counter = Counter()
        for _, r in facts.iterrows():
            s = r["subj_entity_name"] or r["subject"]
            o = r["obj_entity_name"] or r["object"]
            p = r["predicate"]
            G.add_edge(s, o, relation=p, confidence=r["confidence"])
            node_counts[s] += 1
            node_counts[o] += 1
            if s not in node_types:
                node_types[s] = r["subj_entity_type"] or "concept"
            if o not in node_types:
                node_types[o] = r["obj_entity_type"] or "concept"

        if G.number_of_nodes() == 0:
            st.info("No connected facts")
        else:
            pos = nx.spring_layout(G, k=0.3, seed=42, iterations=60)

            type_colors = {
                "tool": "#ef4444",
                "library": "#10b981",
                "project": "#3b82f6",
                "concept": "#f59e0b",
                "person": "#8b5cf6",
                "framework": "#ec4899",
                "language": "#06b6d4",
                "memory": "#6366f1",
                "organization": "#14b8a6",
            }

            edge_traces = []
            for u, v, d in G.edges(data=True):
                x0, y0 = pos[u]
                x1, y1 = pos[v]
                edge_traces.append(
                    go.Scatter(
                        x=(x0, x1, None),
                        y=(y0, y1, None),
                        mode="lines",
                        line=dict(width=1, color="#374151"),
                        hoverinfo="none",
                    )
                )

            node_x = [pos[n][0] for n in G.nodes()]
            node_y = [pos[n][1] for n in G.nodes()]
            node_labels = [n[:28] for n in G.nodes()]
            node_type_list = [node_types.get(n, "concept") for n in G.nodes()]
            node_deg = [node_counts.get(n, 1) for n in G.nodes()]
            colors = [type_colors.get(t, "#6b7280") for t in node_type_list]
            sizes = [min(32, 6 + d * 2.5) for d in node_deg]

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
                    f"<b>{n}</b><br>type: {t}<br>connections: {d}"
                    for n, t, d in zip(node_labels, node_type_list, node_deg)
                ],
                hoverinfo="text",
            )
            fig = go.Figure(data=edge_traces + [node_trace])
            fig.update_layout(
                title="Knowledge Graph (facts → force-directed)",
                **DARK,
                showlegend=False,
                hovermode="closest",
                xaxis=dict(visible=False),
                yaxis=dict(visible=False),
                height=700,
                margin=dict(t=30, b=10, l=10, r=10),
            )
            st.plotly_chart(fig, use_container_width=True)
            with st.expander("Fact list"):
                st.dataframe(
                    facts[["subject", "predicate", "object", "confidence"]],
                    use_container_width=True,
                    hide_index=True,
                )


# ═══════════════════════════════════════════════════════════════════════════
# EMBEDDINGS
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Embeddings":
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
                with st.spinner("Computing PCA..."):
                    mat = np.stack(vecs)
                    mc = mat - mat.mean(axis=0)
                    _, S, Vt = np.linalg.svd(mc, full_matrices=False)
                    p = mc @ Vt[:2].T

                pdf = pd.DataFrame(
                    {"x": p[:, 0], "y": p[:, 1], "category": cats, "memory_id": mids}
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
                    text=f"PC1: {var[0]:.0f}%  PC2: {var[1]:.0f}%",
                    showarrow=False,
                    font=dict(size=11, color="#9ca3af"),
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info(f"Need ≥3 vectors, got {len(vecs)}")


# ═══════════════════════════════════════════════════════════════════════════
# DRIFT
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Drift":
    st.subheader("Concept Drift")
    if not table("concept_drift"):
        st.info(
            "Table `concept_drift` not yet created. Call `memory_check_concept_drift()` first."
        )
    else:
        df = query(
            "SELECT id, drift_metric, drifted_dimensions, triggered_at, acknowledged "
            "FROM concept_drift ORDER BY triggered_at DESC LIMIT 200"
        )
        if df is None or df.empty:
            st.info("No drift events recorded.")
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
elif page == "CTR":
    st.subheader("CTR Feedback Loop")
    if not table("memory_ctr_feedback"):
        st.info("Table `memory_ctr_feedback` not yet created.")
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
# EXPLORER
# ═══════════════════════════════════════════════════════════════════════════
elif page == "Explorer":
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
                pin = "📌 " if r.get("pinned") else ""
                cat = r.get("category") or "—"
                tier = r.get("tier") or "—"
                fs = (
                    f"fitness={r['fitness_score']:.2f}"
                    if pd.notna(r.get("fitness_score"))
                    else ""
                )
                st.markdown(
                    f"**{pin}{r['id'][:50]}** · `{cat}` · tier={tier} · {fs}  \n"
                    f"_{r['created_at']}_  \n"
                    f"{r['preview']}...  "
                )
                st.divider()
    else:
        df = query(
            "SELECT id, substr(content,1,200) preview, category, "
            "created_at, pinned, fitness_score, tier "
            "FROM memories ORDER BY created_at DESC LIMIT 20"
        )
        if df is not None and not df.empty:
            st.caption(f"Recent {len(df)} notes (enter a search term above)")
            for _, r in df.iterrows():
                pin = "📌 " if r.get("pinned") else ""
                cat = r.get("category") or "—"
                tier = r.get("tier") or "—"
                fs = (
                    f"fitness={r['fitness_score']:.2f}"
                    if pd.notna(r.get("fitness_score"))
                    else ""
                )
                st.markdown(
                    f"**{pin}{r['id'][:50]}** · `{cat}` · tier={tier} · {fs}  \n"
                    f"_{r['created_at']}_  \n"
                    f"{r['preview']}...  "
                )
                st.divider()
