#!/usr/bin/env python3
"""Knowledge tab — consolidated Knowledge Graph, Facts, and Embeddings sub-tabs."""
from __future__ import annotations

import difflib
import html
import logging
from collections import Counter
from datetime import datetime, timezone

import networkx as nx
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import dashboard
from dashboard import DARK, _blob_weight, get_conn
from dashboard.api_client import (
    _api,
    _query_api,
    _try_count_api,
    _table_exists_api,
)

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def render_knowledge():
    """Render the Knowledge tab with three sub-tabs: Knowledge Graph, Facts, Embeddings."""
    st.subheader("Knowledge")

    tab_kg, tab_facts, tab_emb = st.tabs(["Knowledge Graph", "Facts", "Embeddings"])

    with tab_kg:
        _render_knowledge_graph()

    with tab_facts:
        _render_facts()

    with tab_emb:
        _render_embeddings()


# ── Knowledge Graph ──────────────────────────────────────────────────────────


def _render_knowledge_graph():
    n_entities = _try_count_api("kg_entities")
    n_edges_total = _try_count_api("kg_edges")
    n_facts = _try_count_api("kg_facts") if _table_exists_api("kg_facts") else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entities", n_entities)
    c2.metric("Edges", n_edges_total)
    c3.metric("Facts", n_facts)
    density = f"{2 * n_edges_total / (n_entities * (n_entities - 1)):.3f}" if n_entities > 1 else "0"
    c4.metric("Density", density, help="edges / possible edges")

    st.divider()

    max_n = st.slider("Show top", 10, 500, 150, key="kg_n", help="Number of top entities to load")

    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 1.5, 1.5])
    with col_ctrl1:
        search_entity = st.text_input("\U0001f50d Find entity", placeholder="bash, tool, python\u2026", key="kg_search")
    with col_ctrl2:
        st.caption("Use search above \u2192 or select from menu below after graph loads")

    ent = _query_api(
        "SELECT id, name, entity_type, mentions FROM kg_entities "
        "ORDER BY mentions DESC LIMIT ?",
        [max_n],
    )
    if ent is None or ent.empty:
        st.info("No entities yet. Run KG backfill to populate.")
        return

    eid_list = [int(x) for x in ent["id"].values]
    name_map = dict(zip(ent["id"], ent["name"]))
    type_map = dict(zip(ent["id"], ent["entity_type"]))
    ment_map = dict(zip(ent["id"], ent["mentions"]))

    # ── Entity Type Distribution ──────────────────────────────────────────
    type_counts = ent["entity_type"].value_counts().reset_index()
    type_counts.columns = ["Type", "Count"]
    fig_types = px.bar(
        type_counts, x="Type", y="Count", color="Type",
        color_discrete_sequence=px.colors.qualitative.Set2,
        text_auto=True,
    )
    fig_types.update_layout(**DARK, height=220, margin=dict(t=30, b=10, l=10, r=10), showlegend=False)
    st.plotly_chart(fig_types, width="stretch")

    all_types = sorted(ent["entity_type"].unique())
    with st.popover(f"Filter types ({len(all_types)})", use_container_width=False):
        sel_types = []
        for t in all_types:
            if st.checkbox(t, value=True, key=f"kg_t_{t}"):
                sel_types.append(t)
        if not sel_types:
            sel_types = all_types

    # ── Entity Management ─────────────────────────────────────────────────
    _render_entity_management(ent, name_map, type_map, ment_map, all_types)

    # ── Edges ─────────────────────────────────────────────────────────────
    placeholders = ",".join("?" for _ in eid_list)
    edges_df = _query_api(
        f"SELECT source_id, target_id, relation, weight FROM kg_edges "
        f"WHERE source_id IN ({placeholders}) "
        f"AND target_id IN ({placeholders}) "
        f"ORDER BY weight DESC LIMIT 1000",
        eid_list + eid_list,
    )

    if edges_df is not None and not edges_df.empty:
        if "weight" in edges_df.columns:
            edges_df["weight"] = pd.to_numeric(edges_df["weight"], errors="coerce").fillna(1.0)
        col_e1, col_e2, col_e3 = st.columns(3)
        col_e1.metric("Visible Edges", len(edges_df))
        rel_counts = edges_df["relation"].value_counts()
        top_rel = rel_counts.index[0] if len(rel_counts) > 0 else "\u2014"
        col_e2.metric("Top Relation", top_rel)
        avg_w = edges_df["weight"].mean() if "weight" in edges_df.columns else 0
        col_e3.metric("Avg Weight", f"{avg_w:.2f}")

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

    # ── NetworkX Graph Visualization ──────────────────────────────────────
    G = nx.Graph()
    if edges_df is not None and not edges_df.empty:
        for _, r in edges_df.iterrows():
            G.add_edge(
                r["source_id"], r["target_id"],
                relation=r.get("relation", ""),
                weight=_blob_weight(r.get("weight", 1)),
            )

    filtered_nodes = [n for n in G.nodes() if type_map.get(n, "other") in sel_types]
    if not filtered_nodes:
        st.info("No entities match the type filter.")
        return

    G_sub = G.subgraph(filtered_nodes).copy()

    if "kg_layout" not in st.session_state or st.session_state.get("kg_layout_n") != len(filtered_nodes):
        with st.spinner("Laying out graph \u2026"):
            st.session_state["kg_layout"] = nx.spring_layout(G_sub, k=0.4, seed=42, iterations=50)
            st.session_state["kg_layout_n"] = len(filtered_nodes)
    pos = st.session_state["kg_layout"]

    focus_opts = [""] + sorted(set(
        name_map.get(n, str(n))
        for n in G_sub.nodes()
    ))
    focus_pick = st.selectbox("\U0001f4d1 Focus", focus_opts, key="kg_focus", placeholder="Focus on entity")

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

    neighbor_ids = set()
    if focus_id is not None and focus_id in G_sub:
        neighbor_ids = set(G_sub.neighbors(focus_id))
        neighbor_ids.add(focus_id)

    type_colors = {
        "tool": "#ef4444", "library": "#10b981", "project": "#3b82f6",
        "concept": "#f59e0b", "person": "#8b5cf6", "framework": "#ec4899",
        "language": "#06b6d4", "other": "#6b7280",
    }

    edge_traces = []
    for i, (u, v, d) in enumerate(G_sub.edges(data=True)):
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        w = d.get("weight", 1)
        w_scaled = w * 0.4 + 0.2
        is_focus = focus_id is not None and (u == focus_id or v == focus_id)

        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        dx, dy = x1 - x0, y1 - y0
        offset = 0.05 * (1 if i % 2 == 0 else -1)
        cx, cy = mx + dy * offset, my - dx * offset

        if is_focus:
            edge_color = "#8b5cf6"
            edge_width = w_scaled * 3.5
        else:
            intensity = min(1.0, w / 3.0)
            r_val = int(100 + intensity * 55)
            g_val = int(116 + intensity * 30)
            b_val = int(139 + intensity * 50)
            edge_color = f"rgba({r_val},{g_val},{b_val},{0.15 + intensity * 0.25})"
            edge_width = w_scaled * 1.5

        edge_traces.append(go.Scatter(
            x=(x0, cx, x1, None), y=(y0, cy, y1, None),
            mode="lines",
            line=dict(width=edge_width, color=edge_color, shape="spline"),
            hoverinfo="text",
            hovertext=f"<b>{name_map.get(u, u)}</b> <i>{d.get('relation', '')}</i> <b>{name_map.get(v, v)}</b><br>weight: {w:.2f}",
            showlegend=False,
        ))

    def _node_trace(nodes, size_mult, color_override=None, text_visible=False, opacity=0.9, glow=False):
        if not nodes:
            return None
        xs, ys, labels, types, ments = [], [], [], [], []
        for n in nodes:
            xs.append(pos[n][0])
            ys.append(pos[n][1])
            labels.append(name_map.get(n, str(n))[:20])
            types.append(type_map.get(n, "other"))
            ments.append(ment_map.get(n, 1))
        cols = [color_override or type_colors.get(t, "#6b7280") for t in types]
        sz = [min(36, 10 + m * 2.5) * size_mult for m in ments]

        marker_dict = dict(
            size=sz, color=cols,
            line=dict(
                width=3 if glow else (1.5 if text_visible else 0.5),
                color="#ffffff" if glow else ("#f0f2f6" if text_visible else "#0e1117"),
            ),
            opacity=opacity,
        )

        return go.Scatter(
            x=xs, y=ys,
            mode="markers+text" if text_visible else "markers",
            text=labels if text_visible else None,
            textposition="top center",
            textfont=dict(size=12 if text_visible else 10, color="#f0f2f6", family="Arial Black" if text_visible else "Arial"),
            marker=marker_dict,
            hovertext=[
                f"<b>{name_map.get(n, n)}</b><br>"
                f"<span style='color:{type_colors.get(type_map.get(n, ''), '#6b7280')}'>\u25cf</span> {type_map.get(n, '?')}<br>"
                f"Mentions: {ment_map.get(n, 0)}<br>"
                f"Connections: {G_sub.degree(n)}"
                for n in nodes
            ],
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
        traces.append(_node_trace(hl_other, 0.7, opacity=0.1 if focus_id else 0.35))
    if hl_nbr:
        traces.append(_node_trace(hl_nbr, 1.3, opacity=0.9))
    if hl_self:
        traces.append(_node_trace(hl_self, 2.0, color_override="#8b5cf6", text_visible=True, opacity=1.0, glow=True))

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=f"<b>{G_sub.number_of_nodes()} nodes</b> \u00b7 {G_sub.number_of_edges()} edges"
        + (f" \u00b7 focused: <b>{focus_name}</b>" if focus_name else ""),
        **DARK, showlegend=False, hovermode="closest",
        xaxis=dict(visible=False, showgrid=False, zeroline=False),
        yaxis=dict(visible=False, showgrid=False, zeroline=False),
        height=680,
        margin=dict(t=40, b=10, l=10, r=10),
    )
    st.plotly_chart(fig, width="stretch")

    # ── Focus Detail Panel ────────────────────────────────────────────────
    if focus_id is not None and focus_name:
        st.divider()
        col_d1, col_d2, col_d3 = st.columns([1, 1.5, 1.5])
        with col_d1:
            st.markdown(f"**{focus_name}**")
            st.caption(f"Type: `{type_map.get(focus_id, '?')}` | Mentions: {ment_map.get(focus_id, 0)} | Connections: {G_sub.degree(focus_id)}")
            ns = list(G_sub.neighbors(focus_id))
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
                st.html(
                    f"<div style='display:flex;align-items:center;gap:6px;font-size:0.78rem;padding:2px 0;'>"
                    f"<span style='color:{type_colors.get(type_map.get(n, ''), '#6b7280')};font-size:1.1rem;'>\u25cf</span>"
                    f"<span style='color:#d1d5db;'>{name_map.get(n, str(n))}</span>"
                    f"{rel_badge}"
                    f"<span style='color:#4b5563;font-size:0.65rem;'>w={w:.1f}</span>"
                    f"</div>",
                )
            if len(ns) > 15:
                st.caption(f"\u2026 and {len(ns) - 15} more")

        with col_d2:
            st.markdown("**Related Memories**")
            mems = _query_api(
                "SELECT id, substr(content,1,150) preview, category, "
                "COALESCE(fitness_score, 0.5) as fitness "
                "FROM memories WHERE content LIKE ? "
                "ORDER BY fitness DESC, created_at DESC LIMIT 8",
                [f"%{focus_name}%"],
            )
            if mems is not None and not mems.empty:
                for _, r in mems.iterrows():
                    cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b", "sessions": "#8b5cf6"}.get(r["category"], "#6b7280")
                    st.html(
                        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;padding:8px;margin:4px 0;'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                        f"<span style='background:{cat_color}22;color:{cat_color};padding:0.1rem 0.5rem;border-radius:999px;font-size:0.65rem;font-weight:600;'>{r['category']}</span>"
                        f"<span style='color:#4b5563;font-size:0.65rem;'>fitness: {r['fitness']:.2f}</span>"
                        f"</div>"
                        f"<div style='color:#9ca3af;font-size:0.72rem;margin-top:4px;'>{r['preview'][:120]}</div>"
                        f"</div>",
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

    with st.expander(f"All {len(ent)} entities", expanded=False):
        st.dataframe(
            ent[["name", "entity_type", "mentions"]].rename(columns={"name": "Name", "entity_type": "Type", "mentions": "Mentions"}),
            width="stretch", hide_index=True,
        )


def _render_entity_management(ent: pd.DataFrame, name_map: dict, type_map: dict, ment_map: dict, all_types: list):
    """Entity editing, prune, and merge suggestions."""
    st.divider()
    st.markdown("#### Entity Management")

    # ── Edit Entity Type ──────────────────────────────────────────────────
    with st.expander("\u270f\ufe0f Edit Entity Type", expanded=False):
        edit_opts = [f"{name_map.get(eid, str(eid))} [{type_map.get(eid, '?')}]" for eid in ent["id"].values[:100]]
        if not edit_opts:
            st.info("No entities to edit")
        else:
            edit_choice = st.selectbox("Select entity to edit", edit_opts, key="kg_edit_entity")
            if edit_choice:
                idx = edit_opts.index(edit_choice)
                eid = int(ent.iloc[idx]["id"])
                current_type = type_map.get(eid, "other")
                new_type = st.selectbox(
                    "New entity type",
                    all_types + ["other"],
                    index=(all_types + ["other"]).index(current_type) if current_type in (all_types + ["other"]) else len(all_types),
                    key="kg_new_type",
                )
                if st.button("\U0001f4be Update Type", type="primary", key="kg_update_type_btn"):
                    try:
                        client = _api()
                        if not client:
                            st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
                        else:
                            client.update_kg_entity(eid, entity_type=new_type)
                            st.toast(f"Updated {name_map.get(eid, str(eid))}: {current_type} \u2192 {new_type}", icon="\u2705")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Failed to update: {e}")

    # ── Prune Low-Mention Entities ────────────────────────────────────────
    with st.expander("\U0001f5d1\ufe0f Prune Low-Mention Entities", expanded=False):
        low_mention = ent[ent["mentions"] < 2]
        n_prunable = len(low_mention)
        if n_prunable == 0:
            st.info("No entities with mentions < 2")
        else:
            st.caption(f"**{n_prunable}** entities have mentions < 2:")
            st.dataframe(
                low_mention[["name", "entity_type", "mentions"]].rename(
                    columns={"name": "Name", "entity_type": "Type", "mentions": "Mentions"}
                ).head(20),
                width="stretch", hide_index=True,
            )
            if st.button("\U0001f5d1\ufe0f Prune All (mentions < 2)", type="primary", key="kg_prune_btn"):
                prunable_ids = [int(x) for x in low_mention["id"].values]
                if prunable_ids:
                    try:
                        client = _api()
                        if not client:
                            st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
                        else:
                            client.kg_prune(prunable_ids)
                            st.toast(f"Pruned {len(prunable_ids)} low-mention entities", icon="\u2705")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Prune failed: {e}")

    # ── Entity Merge Suggestions ──────────────────────────────────────────
    with st.expander("\U0001f504 Entity Merge Suggestions", expanded=False):
        st.caption("Find entities with similar names that may be duplicates")
        merge_threshold = st.slider("Similarity threshold", 0.3, 1.0, 0.7, 0.05, key="kg_merge_thresh")

        all_names = [(int(row["id"]), row["name"]) for _, row in ent.iterrows()]
        merge_pairs = []
        seen = set()
        for i, (eid_i, name_i) in enumerate(all_names):
            for j, (eid_j, name_j) in enumerate(all_names):
                if i >= j or (eid_i, eid_j) in seen:
                    continue
                ratio = difflib.SequenceMatcher(None, name_i.lower(), name_j.lower()).ratio()
                if ratio >= merge_threshold:
                    merge_pairs.append((eid_i, name_i, eid_j, name_j, round(ratio, 3)))
                    seen.add((eid_i, eid_j))

        if not merge_pairs:
            st.info("No similar entity pairs found at this threshold")
        else:
            merge_df = pd.DataFrame(merge_pairs, columns=["ID 1", "Name 1", "ID 2", "Name 2", "Similarity"])
            merge_df = merge_df.sort_values("Similarity", ascending=False).head(20)
            st.dataframe(merge_df, width="stretch", hide_index=True)

            for _, pair in merge_df.iterrows():
                e1, n1, e2, n2, sim = pair["ID 1"], pair["Name 1"], pair["ID 2"], pair["Name 2"], pair["Similarity"]
                mc1, mc2, mc3 = st.columns([3, 3, 2])
                with mc1:
                    st.html(
                        f"<div style='font-size:0.78rem;color:#d1d5db;'><b>{n1}</b> "
                        f"<span style='color:#6b7280;'>({type_map.get(e1, '?')}, {ment_map.get(e1, 0)} mentions)</span></div>"
                    )
                with mc2:
                    st.html(
                        f"<div style='font-size:0.78rem;color:#d1d5db;'><b>{n2}</b> "
                        f"<span style='color:#6b7280;'>({type_map.get(e2, '?')}, {ment_map.get(e2, 0)} mentions)</span></div>"
                    )
                with mc3:
                    if st.button("Merge \u2192", key=f"kg_merge_{e1}_{e2}"):
                        _merge_entities(int(e1), int(e2), n1, n2)


def _merge_entities(keep_id: int, remove_id: int, keep_name: str, remove_name: str):
    """Merge remove_id into keep_id: reassign edges, delete the removed entity."""
    try:
        client = _api()
        if not client:
            st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
            return
        client.kg_merge(keep_id, remove_id)
        st.toast(f"Merged '{remove_name}' \u2192 '{keep_name}'", icon="\u2705")
        st.rerun()
    except Exception as e:
        st.error(f"Merge failed: {e}")


# ── Facts ────────────────────────────────────────────────────────────────────


def _render_facts():
    if not _table_exists_api("kg_facts"):
        st.info("Table `kg_facts` not available \u2014 enable MEMORY_KNOWLEDGE_GRAPH=1")
        return

    n_facts = _try_count_api("kg_facts")
    n_locked = _try_count_api("kg_facts", "locked=1")
    try:
        _c = _api()
        if _c:
            res = _c.query("SELECT AVG(confidence) as avg_conf FROM kg_facts")
            avg_conf = res.get("results", [{}])[0].get("avg_conf") if res.get("results") else None
        else:
            avg_conf = get_conn().execute("SELECT AVG(confidence) FROM kg_facts").fetchone()[0]
    except Exception:
        avg_conf = None
    n_high_conf = _try_count_api("kg_facts", "confidence >= 0.7")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Facts", n_facts)
    c2.metric("Locked", n_locked)
    c3.metric("Avg Confidence", f"{avg_conf:.2f}" if avg_conf else "\u2014")
    c4.metric("High Confidence", n_high_conf)
    c5.metric("Open", n_facts - n_locked)

    st.divider()

    # ── Distribution Charts ───────────────────────────────────────────────
    col_conf, col_pred = st.columns([1, 1])

    with col_conf:
        conf_dist = _query_api(
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
            fig_conf.update_layout(**DARK, height=250, margin=dict(t=30, b=10, l=10, r=10), title="Confidence Distribution")
            st.plotly_chart(fig_conf, width="stretch")

    with col_pred:
        pred_dist = _query_api(
            "SELECT predicate, COUNT(*) cnt FROM kg_facts "
            "GROUP BY predicate ORDER BY cnt DESC LIMIT 10"
        )
        if pred_dist is not None and not pred_dist.empty:
            fig_pred = px.bar(
                pred_dist, x="cnt", y="predicate", orientation="h",
                color="cnt", color_continuous_scale="Viridis", text_auto=True,
            )
            fig_pred.update_layout(**DARK, height=250, margin=dict(t=30, b=10, l=10, r=10), showlegend=False, yaxis=dict(autorange="reversed"), xaxis_title="Count", title="Top Predicates")
            st.plotly_chart(fig_pred, width="stretch")

    st.divider()

    # ── Filter & Browse ───────────────────────────────────────────────────
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

    f_df = _query_api(
        f"SELECT id, subject, predicate, object, confidence, mention_count, "
        f"first_seen, last_seen, locked "
        f"FROM kg_facts WHERE {where_sql} ORDER BY confidence DESC, mention_count DESC LIMIT 200",
        f_params,
    )

    if f_df is not None and not f_df.empty:
        st.caption(f"**{len(f_df)}** facts matching filters")

        display_f = f_df[["subject", "predicate", "object", "confidence", "mention_count", "locked"]].copy()
        display_f["confidence"] = display_f["confidence"].apply(lambda x: f"{x:.2f}")
        display_f["locked"] = display_f["locked"].apply(lambda x: "\U0001f512" if x else "")
        display_f.columns = ["Subject", "Predicate", "Object", "Confidence", "Mentions", "Locked"]

        selected_f = st.dataframe(
            display_f, use_container_width=True, hide_index=True,
            column_config={
                "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=1, format="%.2f"),
                "Subject": st.column_config.TextColumn("Subject", width="medium"),
                "Predicate": st.column_config.TextColumn("Predicate", width="small"),
                "Object": st.column_config.TextColumn("Object", width="large"),
            },
            selection_mode="single-row",
            key="fact_table",
        )

        sel_fact_rows = st.session_state.get("fact_table", {}).get("selection", {}).get("rows", [])
        if sel_fact_rows:
            sel_idx = sel_fact_rows[0]
            sel_row = f_df.iloc[sel_idx]
            st.divider()

            conf_val = float(sel_row["confidence"])
            conf_color = "#10b981" if conf_val >= 0.7 else "#f59e0b" if conf_val >= 0.4 else "#ef4444"
            st.html(
                f"<div style='display:flex;align-items:center;gap:10px;'>"
                f"<span style='background:{conf_color};color:#fff;padding:0.2rem 0.7rem;border-radius:999px;font-size:0.8rem;font-weight:700;'>{conf_val:.2f}</span>"
                f"{'LOCKED' if sel_row.get('locked') else 'OPEN'}"
                f"</div>",
            )

            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;padding:1.2rem;margin:0.8rem 0;'>"
                f"<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap;'>"
                f"<span style='background:#3b82f622;color:#60a5fa;padding:0.3rem 0.8rem;border-radius:8px;font-weight:600;'>{html.escape(str(sel_row['subject']))}</span>"
                f"<span style='color:#6b7280;font-size:1.2rem;'>\u2192</span>"
                f"<span style='background:#8b5cf622;color:#a78bfa;padding:0.3rem 0.8rem;border-radius:8px;font-size:0.85rem;'>{html.escape(str(sel_row['predicate']))}</span>"
                f"<span style='color:#6b7280;font-size:1.2rem;'>\u2192</span>"
                f"<span style='background:#10b98122;color:#34d399;padding:0.3rem 0.8rem;border-radius:8px;font-weight:600;'>{html.escape(str(sel_row['object']))}</span>"
                f"</div>"
                f"</div>",
            )

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Confidence", f"{conf_val:.2f}")
            m2.metric("Mentions", sel_row["mention_count"])
            m3.metric("Locked", "Yes" if sel_row.get("locked") else "No")
            if pd.notna(sel_row.get("first_seen")):
                m4.metric("First Seen", datetime.fromtimestamp(sel_row['first_seen'], tz=timezone.utc).strftime('%Y-%m-%d'))

            mems = _query_api(
                "SELECT id, substr(content,1,150) preview, category FROM memories "
                "WHERE content LIKE ? ORDER BY created_at DESC LIMIT 5",
                [f"%{sel_row['subject']}%"],
            )
            if mems is not None and not mems.empty:
                st.markdown("**Related Memories**")
                for _, mr in mems.iterrows():
                    cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b"}.get(mr["category"], "#6b7280")
                    st.html(
                        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;padding:8px;margin:3px 0;'>"
                        f"<span style='background:{cat_color}22;color:{cat_color};padding:0.1rem 0.4rem;border-radius:999px;font-size:0.6rem;'>{mr['category']}</span> "
                        f"<span style='color:#9ca3af;font-size:0.72rem;'>{mr['preview'][:100]}</span>"
                        f"</div>",
                    )
    else:
        st.info("No facts match the filters")


# ── Embeddings ───────────────────────────────────────────────────────────────


@st.cache_data(ttl=3600)
def _compute_pca(embeddings_matrix, n_components):
    mc = embeddings_matrix - embeddings_matrix.mean(axis=0)
    _, S, Vt = np.linalg.svd(mc, full_matrices=False)
    p = mc @ Vt[:n_components].T
    var_explained = (S[:n_components] ** 2) / (S**2).sum() * 100
    return p, var_explained, Vt


def _render_embeddings():
    try:
        _c = _api()
        if _c:
            res = _c.query("SELECT COUNT(*) as c FROM memory_embeddings")
            n_emb = res.get("results", [{}])[0].get("c", 0) if res.get("results") else 0
        else:
            n_emb = get_conn().execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    except Exception:
        n_emb = 0
    if n_emb == 0:
        st.info("No embeddings")
        return

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        if _c:
            res2 = _c.query("SELECT DISTINCT m.category FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id WHERE m.category IS NOT NULL")
            cat_choices = sorted(r["category"] for r in res2.get("results", []) if r.get("category")) if res2.get("results") else []
        else:
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

    df = _query_api(
        "SELECT e.memory_id, e.embedding, e.dim, m.category, m.tier, m.fitness_score, SUBSTR(m.content, 1, 120) as preview "
        "FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id "
        f"WHERE 1=1 {cat_where} LIMIT ?",
        cat_params + [lim],
    )
    if df is None or df.empty:
        st.info("No embeddings match the filter")
        return

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
            logger.warning("Embedding decode failed: %s", e)

    if len(vecs) < 3:
        st.info(f"Need \u22653 vectors, got {len(vecs)}")
        return

    with st.spinner("Computing PCA ..."):
        mat = np.stack(vecs)
        n_pc = 3 if dim3d else 2
        p, var_explained, Vt = _compute_pca(mat, n_pc)

    search_q = st.text_input("\U0001f50d Filter by preview text", placeholder="e.g. 'database migration'", key="emb_search")
    search_hit_idx = None
    if search_q:
        previews_series = pd.Series(previews)
        mask = previews_series.str.contains(search_q, na=False, case=False)
        if mask.any():
            matched_indices = mask[mask].index.tolist()
            search_hit_idx = matched_indices[0]
            st.caption(f"\U0001f50d Matching memory: {mids[search_hit_idx]}"[:80])

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
        top5 = np.argsort(sims)[-6:-1][::-1]
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

    marker_sizes = [min(4 + f * 14, 28) for f in fits]

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
                    color=hl.index.to_series().map({i: cats[i] for i in range(len(cats))}).map(
                        {c: px.colors.qualitative.Set2[i % len(px.colors.qualitative.Set2)] for i, c in enumerate(sorted(set(cats)))}
                    ).fillna("#8b5cf6").tolist(),
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

    # ── Selected Memory Detail + Nearest Neighbors ────────────────────────
    if sel_for_lines is not None:
        mid = mids[sel_for_lines]
        st.markdown("---")
        col_info, col_nn = st.columns([1, 1])
        with col_info:
            st.markdown(f"**{mid}**")
            st.caption(f"Category: {cats[sel_for_lines]} | Fitness: {fits[sel_for_lines]:.3f}")
            preview_text = _query_api("SELECT content FROM memories WHERE id=?", [mid])
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

    # ── Category Concentration ────────────────────────────────────────────
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

    # ── PCA Dimension Weights ─────────────────────────────────────────────
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
