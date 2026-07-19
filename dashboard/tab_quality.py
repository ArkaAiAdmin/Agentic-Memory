#!/usr/bin/env python3
"""Quality tab — Memory Quality Center, Staleness, Impact, Timeline, Merges, Search Sandbox, Gap Detector."""
from __future__ import annotations

import difflib
import json
import logging
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import dashboard
from dashboard import DARK, get_conn, query, table, try_count
from dashboard.api_client import (
    _api,
    _query_api,
    _try_count_api,
    _table_exists_api,
    _list_column_api,
    _get_conn_api,
)

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def render_quality():
    """Quality tab with 8 sub-tabs."""
    t1, t2, t3, t4, t5, t6, t7, t8 = st.tabs([
        "Quality Center", "Staleness", "Impact Score",
        "Timeline", "Merge", "Search Sandbox", "Gap Detector", "Optimize",
    ])
    with t1:
        _render_quality_center()
    with t2:
        _render_staleness()
    with t3:
        _render_impact_score()
    with t4:
        _render_timeline()
    with t5:
        _render_merge_suggestions()
    with t6:
        _render_search_sandbox()
    with t7:
        _render_gap_detector()
    with t8:
        _render_optimize()


# ── 1. Memory Quality Center ──────────────────────────────────────────────

def _render_quality_center():
    st.subheader("Memory Quality Center")

    n_total = _try_count_api("memories")
    try:
        _c = _api()
        if _c:
            res = _c.query("SELECT AVG(fitness_score) as avg_fit FROM memories WHERE fitness_score IS NOT NULL")
            avg_fit = res.get("results", [{}])[0].get("avg_fit") if res.get("results") else None
        else:
            avg_fit = get_conn().execute("SELECT AVG(fitness_score) FROM memories WHERE fitness_score IS NOT NULL").fetchone()[0]
    except Exception:
        avg_fit = None
    n_low = _try_count_api("memories", "COALESCE(fitness_score, 0.5) < 0.3")
    n_high = _try_count_api("memories", "COALESCE(fitness_score, 0.5) >= 0.7")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", n_total)
    c2.metric("Avg Fitness", f"{avg_fit:.2f}" if avg_fit else "\u2014")
    c3.metric("Low Quality", n_low)
    c4.metric("High Quality", n_high)

    st.divider()

    f1, f2, f3 = st.columns(3)
    with f1:
        _c = _api()
        if _c:
            res = _c.query("SELECT DISTINCT category FROM memories WHERE category IS NOT NULL")
            cats = [r["category"] for r in res.get("results", []) if r.get("category")] if res.get("results") else []
        else:
            cats = [r[0] for r in get_conn().execute(
                "SELECT DISTINCT category FROM memories WHERE category IS NOT NULL"
            ).fetchall() if r[0]]
        cat_filter = st.selectbox("Category", ["all"] + sorted(cats), key="qc_cat")
    with f2:
        tier_filter = st.selectbox("Tier", ["all", "hot", "warm", "cold"], key="qc_tier")
    with f3:
        fit_range = st.slider("Fitness range", 0.0, 1.0, (0.0, 1.0), 0.05, key="qc_fit")

    where = ["1=1"]
    params = []
    if cat_filter != "all":
        where.append("category = ?")
        params.append(cat_filter)
    if tier_filter != "all":
        where.append("tier = ?")
        params.append(tier_filter)
    where.append("COALESCE(fitness_score, 0.5) >= ?")
    params.append(fit_range[0])
    where.append("COALESCE(fitness_score, 0.5) <= ?")
    params.append(fit_range[1])

    df = _query_api(
        f"SELECT id, category, COALESCE(fitness_score, 0.5) as fitness, "
        f"COALESCE(importance, 3) as importance, COALESCE(tier, 'warm') as tier, "
        f"pinned, SUBSTR(content, 1, 120) as preview, created_at "
        f"FROM memories WHERE {' AND '.join(where)} "
        f"ORDER BY fitness ASC LIMIT 200",
        params,
    )

    if df is None or df.empty:
        st.info("No memories match filters")
        return

    st.session_state["qc_df"] = df
    display = df[["id", "category", "fitness", "importance", "tier", "pinned", "preview", "created_at"]].copy()
    display["preview"] = display["preview"].str[:80]
    display["created_at"] = pd.to_datetime(display["created_at"], errors="coerce").dt.strftime("%Y-%m-%d")
    display["select"] = False
    display.columns = ["ID", "Category", "Fitness", "Importance", "Tier", "Pinned", "Preview", "Created", "Select"]

    edited = st.data_editor(
        display, use_container_width=True, hide_index=True,
        column_config={
            "Fitness": st.column_config.ProgressColumn("Fitness", min_value=0, max_value=1, format="%.2f"),
            "Pinned": st.column_config.CheckboxColumn("Pinned"),
            "Preview": st.column_config.TextColumn("Preview", width="large"),
            "Select": st.column_config.CheckboxColumn("Select"),
        },
        key="qc_editor",
    )

    # Detect pin/unpin toggles
    if "Pinned" in edited.columns:
        for _, row in edited.iterrows():
            mem_id = row["ID"]
            orig_pinned = int(display[display["ID"] == mem_id]["Pinned"].iloc[0]) if mem_id in display["ID"].values else 0
            new_pinned = 1 if row["Pinned"] else 0
            if orig_pinned != new_pinned:
                try:
                    _c = _api()
                    if _c:
                        _c.query(f"UPDATE memories SET pinned={new_pinned} WHERE id='{mem_id}'")
                    else:
                        conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                        conn.execute("UPDATE memories SET pinned=? WHERE id=?", (new_pinned, mem_id))
                        conn.commit()
                        conn.close()
                except Exception:
                    pass

    selected_ids = edited[edited["Select"] == True]["ID"].tolist() if "Select" in edited.columns else []
    if selected_ids:
        st.markdown(f"**{len(selected_ids)} selected**")
        b1, b2, b3, b4 = st.columns(4)
        with b1:
            if st.button("\U0001f4e6 Archive Selected", use_container_width=True):
                _bulk_archive(selected_ids)
        with b2:
            if st.button("\U0001f5d1\ufe0f Delete Selected", use_container_width=True):
                _bulk_delete(selected_ids)
        with b3:
            new_tier = st.selectbox("Tier", ["hot", "warm", "cold"], key="qc_bulk_tier", label_visibility="collapsed")
            if st.button("Set Tier", use_container_width=True):
                _bulk_set_tier(selected_ids, new_tier)
        with b4:
            new_cat = st.selectbox("Category", ["lessons", "decisions", "projects", "sessions", "preferences"], key="qc_bulk_cat", label_visibility="collapsed")
            if st.button("Set Category", use_container_width=True):
                _bulk_set_category(selected_ids, new_cat)


def _bulk_archive(ids):
    try:
        for mid in ids:
            _c = _api()
            if _c:
                _c.delete_memory(mid)
            else:
                conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
                if row:
                    cols = [d[1] for d in conn.execute("PRAGMA table_info(memories)").fetchall()]
                    conn.execute(
                        f"INSERT OR IGNORE INTO memory_archive ({','.join(cols)}) SELECT * FROM memories WHERE id=?",
                        (mid,),
                    )
                    conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                conn.commit()
                conn.close()
        st.toast(f"Archived {len(ids)} memories", icon="\u2705")
        st.rerun()
    except Exception as e:
        st.error(f"Archive failed: {e}")


def _bulk_delete(ids):
    try:
        for mid in ids:
            _c = _api()
            if _c:
                _c.delete_memory(mid)
            else:
                conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                conn.commit()
                conn.close()
        st.toast(f"Deleted {len(ids)} memories", icon="\u2705")
        st.rerun()
    except Exception as e:
        st.error(f"Delete failed: {e}")


def _bulk_set_tier(ids, tier):
    try:
        for mid in ids:
            _c = _api()
            if _c:
                _c.query(f"UPDATE memories SET tier='{tier}' WHERE id='{mid}'")
            else:
                conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                conn.execute("UPDATE memories SET tier=? WHERE id=?", (tier, mid))
                conn.commit()
                conn.close()
        st.toast(f"Set {len(ids)} memories to {tier}", icon="\u2705")
        st.rerun()
    except Exception as e:
        st.error(f"Failed: {e}")


def _bulk_set_category(ids, cat):
    try:
        for mid in ids:
            _c = _api()
            if _c:
                _c.query(f"UPDATE memories SET category='{cat}' WHERE id='{mid}'")
            else:
                conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                conn.execute("UPDATE memories SET category=? WHERE id=?", (cat, mid))
                conn.commit()
                conn.close()
        st.toast(f"Set {len(ids)} memories to {cat}", icon="\u2705")
        st.rerun()
    except Exception as e:
        st.error(f"Failed: {e}")


# ── 2. Staleness Report ──────────────────────────────────────────────────

def _render_staleness():
    st.subheader("Staleness Report")

    df = _query_api(
        "SELECT id, category, COALESCE(fitness_score, 0.5) as fitness, "
        "created_at, SUBSTR(content, 1, 100) as preview "
        "FROM memories ORDER BY created_at ASC"
    )
    if df is None or df.empty:
        st.info("No memories")
        return

    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True).dt.tz_localize(None)
    now = pd.Timestamp.now()
    df["age_days"] = (now - df["created_at"]).dt.days

    def bucket(d):
        if d <= 7: return "0-7d"
        if d <= 30: return "7-30d"
        if d <= 90: return "30-90d"
        return "90d+"

    df["age_bucket"] = df["age_days"].apply(bucket)

    bucket_counts = df["age_bucket"].value_counts().reindex(["0-7d", "7-30d", "30-90d", "90d+"]).fillna(0)
    fig = px.bar(
        x=bucket_counts.index, y=bucket_counts.values,
        color=bucket_counts.index,
        color_discrete_map={"0-7d": "#10b981", "7-30d": "#3b82f6", "30-90d": "#f59e0b", "90d+": "#ef4444"},
        title="Memories by Age",
    )
    fig.update_layout(**DARK, showlegend=False, xaxis_title="Age", yaxis_title="Count", margin=dict(t=40, b=10, l=10, r=10))
    st.plotly_chart(fig, width="stretch")

    c1, c2, c3 = st.columns(3)
    c1.metric("Total", len(df))
    c2.metric("Avg Age", f"{df['age_days'].mean():.0f} days")
    c3.metric("Oldest", f"{df['age_days'].max():.0f} days")

    st.divider()

    stale = df[df["fitness"] < 0.3].sort_values("fitness")
    st.markdown(f"#### Cleanup Candidates ({len(stale)} memories with fitness < 0.3)")

    if not stale.empty:
        display = stale[["id", "category", "fitness", "age_days", "preview"]].head(50).copy()
        display["preview"] = display["preview"].str[:60]
        display.columns = ["ID", "Category", "Fitness", "Age (days)", "Preview"]
        st.dataframe(display, use_container_width=True, hide_index=True)

        if st.button("\U0001f4e6 Archive All Cleanup Candidates", type="primary"):
            _bulk_archive(stale["id"].tolist())
    else:
        st.success("No cleanup candidates")


# ── 3. Memory Impact Score ────────────────────────────────────────────────

def _render_impact_score():
    st.subheader("Memory Impact Score")

    df = _query_api(
        "SELECT id, category, COALESCE(fitness_score, 0.5) as fitness, "
        "COALESCE(pinned, 0) as pinned, created_at, "
        "SUBSTR(content, 1, 200) as preview "
        "FROM memories LIMIT 500"
    )
    if df is None or df.empty:
        st.info("No memories")
        return

    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True).dt.tz_localize(None)
    now = pd.Timestamp.now()
    df["days_old"] = ((now - df["created_at"]).dt.days).fillna(365)
    df["recency"] = 1.0 / (1.0 + df["days_old"] / 30.0)
    df["pinned_bonus"] = df["pinned"].apply(lambda x: 2.0 if x else 1.0)

    previews = df["preview"].fillna("").tolist()
    ids = df["id"].tolist()
    n = len(previews)
    peer_counts = []
    for i in range(n):
        count = 0
        a = previews[i][:100]
        if not a:
            peer_counts.append(0)
            continue
        for j in range(n):
            if i == j:
                continue
            b = previews[j][:100]
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if ratio > 0.5:
                count += 1
        peer_counts.append(count)

    df["peers"] = peer_counts
    df["impact"] = df["fitness"] * df["recency"] * df["pinned_bonus"] * (1 + df["peers"] * 0.1)
    df["impact"] = df["impact"] / df["impact"].max() if df["impact"].max() > 0 else 0

    fig = px.histogram(df, x="impact", nbins=30, color_discrete_sequence=["#6366f1"], title="Impact Score Distribution")
    fig.update_layout(**DARK, height=250, margin=dict(t=40, b=10, l=10, r=10), xaxis_title="Impact Score", yaxis_title="Count")
    st.plotly_chart(fig, width="stretch")

    c1, c2, c3 = st.columns(3)
    c1.metric("Avg Impact", f"{df['impact'].mean():.3f}")
    c2.metric("High Impact (>0.7)", len(df[df["impact"] > 0.7]))
    c3.metric("Low Impact (<0.2)", len(df[df["impact"] < 0.2]))

    st.divider()

    col_high, col_low = st.columns(2)
    with col_high:
        st.markdown("#### Top 20 Highest Impact")
        top = df.nlargest(20, "impact")[["id", "category", "fitness", "impact", "preview"]].copy()
        top["preview"] = top["preview"].str[:60]
        top["impact"] = top["impact"].round(3)
        st.dataframe(top, use_container_width=True, hide_index=True)

    with col_low:
        st.markdown("#### Bottom 20 Lowest Impact")
        bot = df.nsmallest(20, "impact")[["id", "category", "fitness", "impact", "preview"]].copy()
        bot["preview"] = bot["preview"].str[:60]
        bot["impact"] = bot["impact"].round(3)
        st.dataframe(bot, use_container_width=True, hide_index=True)


# ── 4. Memory Story Timeline ──────────────────────────────────────────────

def _render_timeline():
    st.subheader("Memory Story Timeline")

    df = _query_api(
        "SELECT DATE(created_at) as day, category, COUNT(*) as cnt "
        "FROM memories WHERE created_at IS NOT NULL "
        "GROUP BY day, category ORDER BY day"
    )
    if df is None or df.empty:
        st.info("No timeline data")
        return

    df["day"] = pd.to_datetime(df["day"])
    pivot = df.pivot_table(index="day", columns="category", values="cnt", fill_value=0)

    fig = go.Figure()
    for cat in pivot.columns:
        fig.add_trace(go.Scatter(
            x=pivot.index, y=pivot[cat],
            name=cat, mode="lines", stackgroup="one",
            line=dict(width=0.5),
        ))
    fig.update_layout(
        **DARK, title="Knowledge Growth by Category",
        xaxis_title=None, yaxis_title="Memories",
        margin=dict(t=40, b=10, l=10, r=10), height=400,
        legend=dict(font=dict(size=9)),
    )
    st.plotly_chart(fig, width="stretch")

    total_df = df.groupby("day")["cnt"].sum().reset_index()
    total_df["week"] = total_df["day"].dt.to_period("W").apply(lambda r: r.start_time)
    weekly = total_df.groupby("week")["cnt"].sum().reset_index()

    if len(weekly) > 2:
        fig2 = px.line(weekly, x="week", y="cnt", title="Weekly Creation Rate", markers=True)
        fig2.update_layout(**DARK, height=250, margin=dict(t=40, b=10, l=10, r=10), xaxis_title=None, yaxis_title="Memories/week")
        st.plotly_chart(fig2, width="stretch")

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Memories", df["cnt"].sum())
    c2.metric("Categories", df["category"].nunique())
    avg_weekly = weekly["cnt"].mean() if len(weekly) > 0 else 0
    c3.metric("Avg Weekly", f"{avg_weekly:.1f}")


# ── 5. Memory Merge Suggestions ──────────────────────────────────────────

def _render_merge_suggestions():
    st.subheader("Memory Merge Suggestions")

    df = _query_api(
        "SELECT id, category, SUBSTR(content, 1, 200) as preview, "
        "COALESCE(fitness_score, 0.5) as fitness "
        "FROM memories LIMIT 300"
    )
    if df is None or df.empty:
        st.info("No memories")
        return

    if st.button("\U0001f504 Scan for Duplicates", type="primary"):
        with st.spinner("Scanning..."):
            pairs = []
            previews = df["preview"].fillna("").tolist()
            ids = df["id"].tolist()
            fitness = df["fitness"].tolist()
            cats = df["category"].tolist()
            n = len(previews)
            for i in range(n):
                for j in range(i + 1, min(n, i + 50)):
                    a = previews[i]
                    b = previews[j]
                    if not a or not b:
                        continue
                    ratio = difflib.SequenceMatcher(None, a[:150], b[:150]).ratio()
                    if ratio > 0.6:
                        pairs.append((ids[i], a[:80], ids[j], b[:80], round(ratio, 3), fitness[i], cats[i], fitness[j], cats[j]))
            pairs.sort(key=lambda x: -x[4])
            st.session_state["merge_pairs"] = pairs[:30]

    pairs = st.session_state.get("merge_pairs", [])
    if pairs:
        st.markdown(f"#### {len(pairs)} potential duplicates found")
        for i, (id1, p1, id2, p2, sim, f1, c1, f2, c2) in enumerate(pairs):
            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:10px;margin:6px 0;'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                f"<span style='color:#8b5cf6;font-size:0.7rem;font-weight:600;'>sim: {sim}</span>"
                f"<span style='color:#4b5563;font-size:0.65rem;'>{c1} (f={f1:.2f}) vs {c2} (f={f2:.2f})</span>"
                f"</div>"
                f"<div style='display:flex;gap:12px;margin-top:6px;'>"
                f"<div style='flex:1;color:#d1d5db;font-size:0.72rem;'>{p1}</div>"
                f"<div style='flex:1;color:#d1d5db;font-size:0.72rem;'>{p2}</div>"
                f"</div></div>"
            )
            if st.button(f"\U0001f504 Merge pair {i+1}", key=f"merge_pair_{i}"):
                _merge_memories(id1, id2)
    else:
        st.info("Click 'Scan for Duplicates' to find merge candidates")


def _merge_memories(keep_id, remove_id):
    try:
        _c = _api()
        if _c:
            keep_res = _c.query(f"SELECT content, fitness_score FROM memories WHERE id='{keep_id}'")
            remove_res = _c.query(f"SELECT content FROM memories WHERE id='{remove_id}'")
            keep_data = keep_res.get("results", [{}])[0] if keep_res.get("results") else {}
            remove_data = remove_res.get("results", [{}])[0] if remove_res.get("results") else {}
            keep_content = keep_data.get("content", "")
            keep_fitness = keep_data.get("fitness_score", 0.5)
            remove_content = remove_data.get("content", "")
            if keep_content and remove_content:
                new_content = keep_content + "\n\n---\n\n" + remove_content
                new_fitness = max(keep_fitness or 0.5, 0.5)
                _c.query(f"UPDATE memories SET content='{new_content.replace(chr(39), chr(39)*2)}', fitness_score={new_fitness} WHERE id='{keep_id}'")
                _c.query(f"DELETE FROM memories WHERE id='{remove_id}'")
        else:
            conn = sqlite3.connect(str(dashboard.DB), timeout=10)
            keep = conn.execute("SELECT content, fitness_score FROM memories WHERE id=?", (keep_id,)).fetchone()
            remove = conn.execute("SELECT content FROM memories WHERE id=?", (remove_id,)).fetchone()
            if keep and remove:
                new_content = keep[0] + "\n\n---\n\n" + remove[0]
                new_fitness = max(keep[1] or 0.5, 0.5)
                conn.execute("UPDATE memories SET content=?, fitness_score=? WHERE id=?", (new_content, new_fitness, keep_id))
                conn.execute("DELETE FROM memories WHERE id=?", (remove_id,))
                conn.commit()
            conn.close()
        st.toast("Merged memories", icon="\u2705")
        st.rerun()
    except Exception as e:
        st.error(f"Merge failed: {e}")


# ── 6. Memory Search Sandbox ──────────────────────────────────────────────

def _render_search_sandbox():
    st.subheader("Memory Search Sandbox")

    query_text = st.text_input("\U0001f50d Test query", placeholder="e.g. 'database migration best practices'", key="sandbox_q")

    if query_text:
        with st.spinner("Searching..."):
            try:
                from search.orchestrator import search_memories
                t0 = __import__("time").time()
                result = search_memories(
                    db_path=dashboard.DB,
                    query=query_text,
                    limit=20,
                    light=True,
                    include_global=True,
                )
                elapsed = (__import__("time").time() - t0) * 1000
                items = result.get("results", [])
            except Exception as e:
                st.error(f"Search failed: {e}")
                items = []
                elapsed = 0

        c1, c2, c3 = st.columns(3)
        c1.metric("Results", len(items))
        c2.metric("Time", f"{elapsed:.0f} ms")
        c3.metric("Query", query_text[:30])

        if items:
            rows = []
            for i, r in enumerate(items):
                rows.append({
                    "rank": i + 1,
                    "id": r.get("id", ""),
                    "score": round(r.get("score", 0), 4),
                    "category": r.get("category", ""),
                    "fitness": round(r.get("fitness_score", 0.5), 3),
                    "preview": (r.get("content") or "")[:100],
                })
            rdf = pd.DataFrame(rows)
            st.dataframe(rdf, use_container_width=True, hide_index=True, column_config={
                "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=1, format="%.4f"),
                "preview": st.column_config.TextColumn("Preview", width="large"),
            })

            st.markdown("#### Top 5 Agent Would Use")
            for i, r in enumerate(items[:5]):
                cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b", "sessions": "#8b5cf6"}.get(r.get("category", ""), "#6b7280")
                st.html(
                    f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                    f"padding:10px;margin:4px 0;'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                    f"<span style='background:{cat_color}22;color:{cat_color};padding:0.1rem 0.4rem;"
                    f"border-radius:999px;font-size:0.6rem;font-weight:600;'>{r.get('category', '')}</span>"
                    f"<span style='color:#8b5cf6;font-size:0.65rem;'>score: {r.get('score', 0):.4f}</span>"
                    f"</div>"
                    f"<div style='color:#9ca3af;font-size:0.72rem;margin-top:4px;'>{(r.get('content') or '')[:150]}</div>"
                    f"</div>"
                )
                if st.button(f"\U0001f44d Useful", key=f"sandbox_up_{i}"):
                    try:
                        from search.feedback import record_ctr_feedback_db
                        record_ctr_feedback_db(str(dashboard.DB), id=r.get("id", ""), query_id=f"sandbox_{query_text}", action="clicked")
                        st.toast("Recorded", icon="\u2705")
                    except Exception:
                        pass
        else:
            st.info("No results")


# ── 7. Memory Gap Detector ────────────────────────────────────────────────

def _render_gap_detector():
    st.subheader("Memory Gap Detector")

    if not _table_exists_api("kg_entities"):
        st.info("No KG entities. Run KG backfill first.")
        return

    if st.button("\U0001f50d Scan for Gaps", type="primary"):
        with st.spinner("Scanning for knowledge gaps..."):
            entities = _query_api("SELECT id, name, entity_type FROM kg_entities ORDER BY mentions DESC LIMIT 100")
            if entities is None or entities.empty:
                st.info("No entities")
                return

            existing_edges = set()
            edges = _query_api("SELECT source_id, target_id FROM kg_edges")
            if edges is not None and not edges.empty:
                for _, r in edges.iterrows():
                    existing_edges.add((r["source_id"], r["target_id"]))
                    existing_edges.add((r["target_id"], r["source_id"]))

            name_map = dict(zip(entities["id"], entities["name"]))
            gaps = []
            ent_ids = entities["id"].tolist()
            ent_names = entities["name"].tolist()

            for i in range(min(len(ent_ids), 50)):
                for j in range(i + 1, min(len(ent_ids), 50)):
                    if (ent_ids[i], ent_ids[j]) in existing_edges:
                        continue
                    name_a = ent_names[i]
                    name_b = ent_names[j]
                    if not name_a or not name_b or name_a == name_b:
                        continue
                    co_occur = _try_count_api("memories", f"content LIKE '%{name_a}%' AND content LIKE '%{name_b}%'")
                    if co_occur >= 2:
                        gaps.append((name_a, name_b, co_occur, entities.iloc[i]["entity_type"], entities.iloc[j]["entity_type"]))

            gaps.sort(key=lambda x: -x[2])
            st.session_state["gaps"] = gaps[:30]

    gaps = st.session_state.get("gaps", [])
    if gaps:
        total_edges = _try_count_api("kg_edges")
        total_entities = _try_count_api("kg_entities")
        coverage = (total_edges / (total_entities * (total_entities - 1) / 2) * 100) if total_entities > 1 else 0

        c1, c2, c3 = st.columns(3)
        c1.metric("Gaps Found", len(gaps))
        c2.metric("Existing Edges", total_edges)
        c3.metric("Coverage", f"{coverage:.1f}%")

        st.markdown("#### Top Knowledge Gaps")
        for i, (a, b, count, ta, tb) in enumerate(gaps):
            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:8px 12px;margin:4px 0;display:flex;align-items:center;gap:8px;'>"
                f"<span style='color:#d1d5db;font-size:0.78rem;font-weight:600;'>{a}</span>"
                f"<span style='color:#4b5563;font-size:0.7rem;'>({ta})</span>"
                f"<span style='color:#6b7280;font-size:0.7rem;'>\u2194</span>"
                f"<span style='color:#d1d5db;font-size:0.78rem;font-weight:600;'>{b}</span>"
                f"<span style='color:#4b5563;font-size:0.7rem;'>({tb})</span>"
                f"<span style='color:#8b5cf6;font-size:0.65rem;margin-left:auto;'>co-occur: {count}</span>"
                f"</div>"
            )
            if st.button(f"\U0001f517 Create Edge", key=f"gap_edge_{i}", use_container_width=False):
                try:
                    _c = _api()
                    a_id = entities[entities["name"] == a]["id"].iloc[0]
                    b_id = entities[entities["name"] == b]["id"].iloc[0]
                    if _c:
                        _c.query(
                            f"INSERT INTO kg_edges (source_id, target_id, relation, weight) "
                            f"VALUES ({int(a_id)}, {int(b_id)}, 'co-occurs', {count * 0.1})"
                        )
                    else:
                        conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                        conn.execute(
                            "INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, 'co-occurs', ?)",
                            (int(a_id), int(b_id), count * 0.1),
                        )
                        conn.commit()
                        conn.close()
                    st.toast(f"Created edge: {a} \u2194 {b}", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed: {e}")
    else:
        st.info("Click 'Scan for Gaps' to find knowledge gaps in your memory system")


# ── 8. One-click Optimization ─────────────────────────────────────────────

def _render_optimize():
    st.subheader("One-click Optimization")

    st.markdown("Run a full optimization pass on your memory system. Each step can run individually or all together.")

    # Before stats
    st.markdown("#### Current State")
    n_mem = _try_count_api("memories")
    db_size = dashboard.DB.stat().st_size / (1024 * 1024) if dashboard.DB and dashboard.DB.exists() else 0
    n_emb = _try_count_api("memory_embeddings")
    n_chunks = _try_count_api("memory_chunks")
    n_entities = _try_count_api("kg_entities")
    n_edges = _try_count_api("kg_edges")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Memories", n_mem)
    c2.metric("DB Size", f"{db_size:.1f} MB")
    c3.metric("Embeddings", n_emb)
    c4.metric("Entities", n_entities)
    c5.metric("Edges", n_edges)

    st.divider()

    # Individual steps
    st.markdown("#### Individual Steps")

    steps = [
        ("1. Compact Database", "VACUUM to reclaim space", _opt_compact),
        ("2. Rebuild FTS5 Index", "Rebuild full-text search index", _opt_rebuild_fts),
        ("3. Rebuild Embeddings", "Recompute all embeddings", _opt_rebuild_embeddings),
        ("4. Deduplicate KG Entities", "Merge similar entities", _opt_dedup_kg),
        ("5. Archive Stale Memories", "Archive memories with fitness < 0.3 and age > 90d", _opt_archive_stale),
        ("6. Run Backfill", "KG + FTS + embedding backfill", _opt_backfill),
    ]

    for label, desc, func in steps:
        col_label, col_btn = st.columns([3, 1])
        with col_label:
            st.html(
                f"<div style='padding:8px 0;'>"
                f"<div style='color:#d1d5db;font-size:0.85rem;font-weight:600;'>{label}</div>"
                f"<div style='color:#6b7280;font-size:0.72rem;'>{desc}</div>"
                f"</div>"
            )
        with col_btn:
            if st.button("Run", key=f"opt_{label}", use_container_width=True):
                func()

    st.divider()

    # Full optimization
    st.markdown("#### Full Optimization")
    st.caption("Runs all steps in sequence. May take several minutes.")
    if st.button("\u26a1 Run Full Optimization", type="primary", use_container_width=True):
        _run_full_optimization()

    # After stats (if optimization was run)
    if st.session_state.get("opt_done"):
        st.divider()
        st.markdown("#### After Optimization")
        n_mem2 = _try_count_api("memories")
        db_size2 = dashboard.DB.stat().st_size / (1024 * 1024) if dashboard.DB and dashboard.DB.exists() else 0
        n_emb2 = _try_count_api("memory_embeddings")
        n_entities2 = _try_count_api("kg_entities")
        n_edges2 = _try_count_api("kg_edges")

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Memories", n_mem2, delta=n_mem2 - n_mem)
        c2.metric("DB Size", f"{db_size2:.1f} MB", delta=f"{db_size2 - db_size:.1f} MB")
        c3.metric("Embeddings", n_emb2, delta=n_emb2 - n_emb)
        c4.metric("Entities", n_entities2, delta=n_entities2 - n_entities)
        c5.metric("Edges", n_edges2, delta=n_edges2 - n_edges)


def _opt_compact():
    with st.spinner("Compacting database..."):
        try:
            _c = _api()
            if _c:
                _c.compact()
            else:
                conn = sqlite3.connect(str(dashboard.DB), timeout=30)
                conn.execute("VACUUM")
                conn.close()
            st.toast("Database compacted", icon="\u2705")
        except Exception as e:
            st.error(f"Compact failed: {e}")


def _opt_rebuild_fts():
    with st.spinner("Rebuilding FTS5 index..."):
        try:
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, str(ROOT / "cron" / "cron_rebuild_fts.py")],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "MEMORY_DB_PATH": str(dashboard.DB)},
            )
            if result.returncode == 0:
                st.toast("FTS5 index rebuilt", icon="\u2705")
            else:
                st.error(f"FTS rebuild failed: {result.stderr[:200]}")
        except Exception as e:
            st.error(f"Failed: {e}")


def _opt_rebuild_embeddings():
    with st.spinner("Rebuilding embeddings..."):
        try:
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, str(ROOT / "cron" / "cron_embedding_recompute.py")],
                capture_output=True, text=True, timeout=300,
                env={**os.environ, "MEMORY_DB_PATH": str(dashboard.DB)},
            )
            if result.returncode == 0:
                st.toast("Embeddings rebuilt", icon="\u2705")
            else:
                st.error(f"Embedding rebuild failed: {result.stderr[:200]}")
        except Exception as e:
            st.error(f"Failed: {e}")


def _opt_dedup_kg():
    with st.spinner("Deduplicating KG entities..."):
        try:
            _c = _api()
            if _c:
                res = _c.kg_dedup()
                merged = res.get("merged", 0) if isinstance(res, dict) else 0
            else:
                conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                # Find duplicate entities by name
                dupes = conn.execute(
                    "SELECT name, COUNT(*) cnt, GROUP_CONCAT(id) ids "
                    "FROM kg_entities GROUP BY LOWER(name) HAVING cnt > 1"
                ).fetchall()
                merged = 0
                for name, cnt, ids_str in dupes:
                    ids = [int(x) for x in ids_str.split(",")]
                    keep = ids[0]
                    for remove_id in ids[1:]:
                        conn.execute("UPDATE kg_edges SET source_id=? WHERE source_id=?", (keep, remove_id))
                        conn.execute("UPDATE kg_edges SET target_id=? WHERE target_id=?", (keep, remove_id))
                        conn.execute("DELETE FROM kg_entities WHERE id=?", (remove_id,))
                        merged += 1
                # Dedup edges
                conn.execute(
                    "DELETE FROM kg_edges WHERE id NOT IN ("
                    "SELECT MAX(id) FROM kg_edges GROUP BY source_id, target_id, relation)"
                )
                conn.commit()
                conn.close()
            st.toast(f"Merged {merged} duplicate entities", icon="\u2705")
        except Exception as e:
            st.error(f"Dedup failed: {e}")


def _opt_archive_stale():
    with st.spinner("Archiving stale memories..."):
        try:
            _c = _api()
            if _c:
                res = _c.archive_stale()
                archived = res.get("archived", 0) if isinstance(res, dict) else 0
            else:
                conn = sqlite3.connect(str(dashboard.DB), timeout=10)
                # Create archive table if not exists
                cols = conn.execute("PRAGMA table_info(memories)").fetchall()
                col_defs = ", ".join(f"{c[1]} {c[2]}" for c in cols)
                conn.execute(f"CREATE TABLE IF NOT EXISTS memory_archive ({col_defs})")
                # Find stale memories
                stale = conn.execute(
                    "SELECT id FROM memories "
                    "WHERE COALESCE(fitness_score, 0.5) < 0.3 "
                    "AND created_at < datetime('now', '-90 days')"
                ).fetchall()
                archived = 0
                for (mid,) in stale:
                    row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
                    if row:
                        col_names = [c[1] for c in cols]
                        placeholders = ",".join("?" for _ in col_names)
                        conn.execute(f"INSERT OR IGNORE INTO memory_archive ({','.join(col_names)}) VALUES ({placeholders})", row)
                        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                        archived += 1
                conn.commit()
                conn.close()
            st.toast(f"Archived {archived} stale memories", icon="\u2705")
        except Exception as e:
            st.error(f"Archive failed: {e}")


def _opt_backfill():
    with st.spinner("Running backfill..."):
        try:
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, str(ROOT / "backfill_all.py")],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, "MEMORY_DB_PATH": str(dashboard.DB)},
            )
            if result.returncode == 0:
                st.toast("Backfill complete", icon="\u2705")
            else:
                st.error(f"Backfill failed: {result.stderr[:200]}")
        except Exception as e:
            st.error(f"Failed: {e}")


def _run_full_optimization():
    steps = [
        ("Compact", _opt_compact),
        ("FTS5 Rebuild", _opt_rebuild_fts),
        ("Embedding Rebuild", _opt_rebuild_embeddings),
        ("KG Dedup", _opt_dedup_kg),
        ("Archive Stale", _opt_archive_stale),
        ("Backfill", _opt_backfill),
    ]
    progress = st.progress(0)
    status = st.empty()
    for i, (name, func) in enumerate(steps):
        status.text(f"Running: {name}...")
        progress.progress((i) / len(steps))
        try:
            func()
        except Exception as e:
            st.error(f"{name} failed: {e}")
    progress.progress(1.0)
    status.text("Optimization complete!")
    st.session_state["opt_done"] = True
    st.toast("Full optimization complete", icon="\u2705")
