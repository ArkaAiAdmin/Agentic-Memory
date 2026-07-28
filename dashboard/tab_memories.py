#!/usr/bin/env python3
"""Memories tab — Browse, Search, Edit, Create, Bulk Actions."""
from __future__ import annotations

import logging

import pandas as pd
import plotly.express as px
import streamlit as st

import dashboard
from dashboard import DARK, get_conn, query, try_count

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def _try_count_api(table: str, where: str = "") -> int:
    """Count rows via api_client.query() with fallback to dashboard.try_count."""
    client = st.session_state.get("api_client")
    if client:
        sql = f"SELECT COUNT(*) as count FROM {table}"
        if where:
            sql += f" WHERE {where}"
        try:
            result = client.query(sql)
            rows = result.get("results", [])
            return rows[0]["count"] if rows else 0
        except Exception:
            pass
    return try_count(table, where) if where else try_count(table)


def _query_api(sql: str, params: list | None = None) -> pd.DataFrame | None:
    """Run a SELECT query via api_client with fallback to dashboard.query."""
    client = st.session_state.get("api_client")
    if client:
        try:
            result = client.query(sql, params=params or [])
            rows = result.get("results", [])
            if not rows:
                return None
            return pd.DataFrame(rows)
        except Exception:
            pass
    return query(sql, params=params or ())


def _get_conn_api():
    """Get a DB connection via fallback to dashboard.get_conn."""
    client = st.session_state.get("api_client")
    if client:
        return None  # signal: use API instead
    return get_conn()


def _render_memory_editor():
    """Render the inline memory editor for the selected memory."""
    sel_rows = st.session_state.get("mem_table", {}).get("selection", {}).get("rows", [])
    if not sel_rows:
        return

    sel_idx = sel_rows[0]
    m_df = st.session_state.get("mem_df")
    if m_df is None or sel_idx >= len(m_df):
        return

    sel_id = m_df.iloc[sel_idx]["id"]

    st.divider()
    st.markdown(f"### Edit: `{sel_id}`")

    client = st.session_state.get("api_client")

    # Load full content
    try:
        if client:
            mem = client.get_memory(sel_id)
            if not mem or "error" in mem:
                raise ValueError(mem.get("error", "not found"))
            row = mem
        else:
            full = query("SELECT * FROM memories WHERE id=?", (sel_id,))
            if full is None or full.empty:
                raise ValueError("Memory not found")
            row = full.iloc[0].to_dict()
    except Exception:
        full = query("SELECT * FROM memories WHERE id=?", (sel_id,))
        if full is None or full.empty:
            st.error("Could not load memory")
            return
        row = full.iloc[0]

    col1, col2 = st.columns([2, 1])

    with col1:
        new_content = st.text_area(
            "Content",
            value=str(row.get("content", "")),
            height=300,
            key="edit_content",
        )

    with col2:
        current_cat = str(row.get("category", "lessons") or "lessons")
        new_category = st.selectbox(
            "Category",
            ["lessons", "decisions", "projects", "sessions", "preferences"],
            index=["lessons", "decisions", "projects", "sessions", "preferences"].index(current_cat)
            if current_cat in ["lessons", "decisions", "projects", "sessions", "preferences"] else 0,
            key="edit_category",
        )

        new_importance = st.slider(
            "Importance",
            1, 5,
            int(row.get("importance", 3) or 3),
            key="edit_importance",
        )

        current_tier = str(row.get("tier", "warm") or "warm")
        new_tier = st.selectbox(
            "Tier",
            ["hot", "warm", "cold"],
            index=["hot", "warm", "cold"].index(current_tier) if current_tier in ["hot", "warm", "cold"] else 1,
            key="edit_tier",
        )

        new_pinned = st.checkbox(
            "Pinned",
            value=bool(row.get("pinned", False)),
            key="edit_pinned",
        )

    # Save / Delete buttons
    s1, s2, s3 = st.columns([1, 1, 4])

    with s1:
        if st.button("\U0001f4be Save Changes", type="primary", use_container_width=True):
            try:
                if not client:
                    st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
                else:
                    client.update_memory(
                        sel_id,
                        content=new_content,
                        category=new_category,
                        importance=new_importance,
                        pinned=new_pinned,
                        tier=new_tier,
                    )
                    st.toast(f"Memory {sel_id} updated", icon="\u2705")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to save: {e}")

    with s2:
        if st.button("\U0001f5d1\ufe0f Delete", use_container_width=True):
            st.session_state["confirm_delete"] = sel_id

    # Confirmation for delete
    if st.session_state.get("confirm_delete") == sel_id:
        st.warning(f"Permanently delete `{sel_id}`?")
        d1, d2, _ = st.columns([1, 1, 4])
        if d1.button("Yes, delete", type="primary", key="confirm_del_yes"):
            try:
                if not client:
                    st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
                else:
                    client.delete_memory(sel_id)
                st.session_state.pop("confirm_delete", None)
                st.toast(f"Memory {sel_id} deleted", icon="\u2705")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to delete: {e}")
        if d2.button("Cancel", key="confirm_del_no"):
            st.session_state.pop("confirm_delete", None)
            st.rerun()


def _render_bulk_actions(selected_ids: list[str] | None = None):
    """Render bulk action controls for selected memories."""
    if not selected_ids:
        return

    client = st.session_state.get("api_client")

    st.html(
        f"<div style='background:#1a1d23;border:1px solid #6366f1;border-radius:8px;"
        f"padding:8px 12px;margin:8px 0;display:flex;align-items:center;gap:10px;'>"
        f"<span style='color:#d1d5db;font-size:0.78rem;font-weight:600;'>"
        f"{len(selected_ids)} selected</span>"
        f"</div>",
    )

    b1, b2, b3, b4, b5 = st.columns(5)

    with b1:
        if st.button("\U0001f4cc Pin All", use_container_width=True):
            _bulk_update(selected_ids, pinned=True)

    with b2:
        if st.button("\U0001f4ce Unpin All", use_container_width=True):
            _bulk_update(selected_ids, pinned=False)

    with b3:
        new_tier = st.selectbox("Tier", ["hot", "warm", "cold"], key="bulk_tier", label_visibility="collapsed")
        if st.button("Set Tier", use_container_width=True):
            _bulk_update(selected_ids, tier=new_tier)

    with b4:
        new_cat = st.selectbox("Category", ["lessons", "decisions", "projects", "sessions", "preferences"], key="bulk_cat", label_visibility="collapsed")
        if st.button("Set Category", use_container_width=True):
            _bulk_update(selected_ids, category=new_cat)

    with b5:
        if st.button("\U0001f5d1\ufe0f Delete Selected", use_container_width=True):
            st.session_state["bulk_delete_ids"] = selected_ids

    # Confirmation for bulk delete
    if st.session_state.get("bulk_delete_ids"):
        st.warning(f"Permanently delete {len(st.session_state['bulk_delete_ids'])} memories?")
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("Yes, delete all", type="primary", key="bulk_del_confirm"):
            try:
                ids = st.session_state["bulk_delete_ids"]
                if not client:
                    st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
                else:
                    for mid in ids:
                        client.delete_memory(mid)
                st.toast(f"Deleted {len(ids)} memories", icon="\u2705")
                st.session_state.pop("bulk_delete_ids", None)
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")
        if c2.button("Cancel", key="bulk_del_cancel"):
            st.session_state.pop("bulk_delete_ids", None)
            st.rerun()


def _bulk_update(ids: list[str], **kwargs):
    """Bulk update memory fields using api_client."""
    client = st.session_state.get("api_client")
    try:
        if not client:
            st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
        else:
            for mid in ids:
                client.update_memory(mid, **kwargs)
        st.toast(f"Updated {len(ids)} memories", icon="\u2705")
        st.rerun()
    except Exception as e:
        st.error(f"Bulk update failed: {e}")


def _render_create_memory():
    """Render the new memory creation form."""
    with st.expander("\U0001f4dd Create New Memory", expanded=False):
        with st.form("create_memory", clear_on_submit=True):
            new_content = st.text_area("Content (Markdown)", height=200, placeholder="Write your memory note here...")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                new_cat = st.selectbox("Category", ["lessons", "decisions", "projects", "sessions", "preferences"])
            with c2:
                new_imp = st.slider("Importance", 1, 5, 3)
            with c3:
                new_tier = st.selectbox("Tier", ["warm", "hot", "cold"])
            with c4:
                new_pinned = st.checkbox("Pinned")
            new_tags = st.text_input("Tags (comma-separated)", placeholder="e.g. database, migration, postgres")

            submitted = st.form_submit_button("\U0001f4be Save Memory", type="primary")
            if submitted and new_content.strip():
                try:
                    tags_list = [t.strip() for t in new_tags.split(",") if t.strip()] if new_tags else []
                    client = st.session_state.get("api_client")
                    if not client:
                        st.error("Write requires the REST API (agent memory service) to be running. Local direct-write is disabled for security.")
                    else:
                        result = client.create_memory(
                            content=new_content,
                            category=new_cat,
                            importance=new_imp,
                            tier=new_tier,
                            tags=tags_list,
                            pinned=new_pinned,
                        )
                        mem_id = result.get("id", "?")
                    st.toast(f"Memory created: {mem_id}", icon="\u2705")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to create: {e}")


def _render_search():
    """Render the search interface with relevance feedback."""
    st.markdown("#### Search Memories")

    s_col1, s_col2, s_col3 = st.columns([3, 1, 1])
    with s_col1:
        q_text = st.text_input(
            "\U0001f50d Search",
            placeholder="e.g. 'database migration', 'search quality'",
            key="mem_search_q",
        )
    with s_col2:
        _client = st.session_state.get("api_client")
        try:
            if _client:
                _cats = _client.categories()
            else:
                _cats = [r[0] for r in get_conn().execute(
                    "SELECT DISTINCT category FROM memories WHERE category IS NOT NULL"
                ).fetchall() if r[0]]
        except Exception:
            _cats = []
        search_cat = st.selectbox(
            "Category",
            ["all"] + sorted(_cats),
            key="mem_search_cat",
        )
    with s_col3:
        search_mode = st.selectbox("Mode", ["hybrid", "fts", "semantic"], key="mem_search_mode")

    if q_text:
        import time as _time
        _t0 = _time.time()
        try:
            from search.orchestrator import search_memories
            _cat = search_cat if search_cat and search_cat != "all" else ""
            result = search_memories(
                db_path=dashboard.DB,
                query=q_text,
                limit=30,
                category=_cat,
                light=True,
                include_global=True,
            )
            _elapsed = (_time.time() - _t0) * 1000
            items = result.get("results", [])

            if items:
                c1, c2, c3 = st.columns(3)
                c1.metric("Results", len(items))
                c2.metric("Search Time", f"{_elapsed:.0f} ms")
                c3.metric("Mode", search_mode)

                for rank, r in enumerate(items):
                    cat_color = {"lessons": "#10b981", "decisions": "#3b82f6", "projects": "#f59e0b",
                                 "sessions": "#8b5cf6", "concepts": "#ec4899", "preferences": "#06b6d4"}.get(r.get("category", ""), "#6b7280")
                    fitness = r.get("fitness_score", 0.5)
                    score = r.get("score", 0)
                    pinned = "\U0001f4cc" if r.get("pinned") else ""

                    st.html(
                        f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:10px;"
                        f"padding:12px 16px;margin:6px 0;'>"
                        f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'>"
                        f"<div>"
                        f"<span style='background:{cat_color}22;color:{cat_color};padding:0.15rem 0.5rem;"
                        f"border-radius:999px;font-size:0.65rem;font-weight:600;'>{r.get('category', '')}</span>"
                        f" {pinned}"
                        f"</div>"
                        f"<div style='display:flex;gap:12px;'>"
                        f"<span style='color:#8b5cf6;font-size:0.65rem;font-weight:600;'>score: {score:.3f}</span>"
                        f"<span style='color:#4b5563;font-size:0.65rem;'>fitness: {fitness:.2f}</span>"
                        f"</div></div>"
                        f"<div style='color:#9ca3af;font-size:0.78rem;line-height:1.4;'>"
                        f"{(r.get('content') or '')[:250]}</div>"
                        f"</div>",
                    )

                    # CTR feedback buttons per result
                    fb1, fb2, _ = st.columns([1, 1, 6])
                    with fb1:
                        if st.button("\U0001f44d", key=f"fb_up_{rank}", help="Useful"):
                            try:
                                from search.feedback import record_ctr_feedback_db
                                record_ctr_feedback_db(str(dashboard.DB), id=r.get("id", ""), query_id=f"dash_{q_text}", action="clicked")
                                st.toast("Recorded feedback", icon="\u2705")
                            except Exception:
                                pass
                    with fb2:
                        if st.button("\U0001f44e", key=f"fb_down_{rank}", help="Not useful"):
                            try:
                                from search.feedback import record_ctr_feedback_db
                                record_ctr_feedback_db(str(dashboard.DB), id=r.get("id", ""), query_id=f"dash_{q_text}", action="dismissed")
                                st.toast("Recorded feedback", icon="\u2705")
                            except Exception:
                                pass
            else:
                st.info(f"No matches for '{q_text}'")
        except Exception as e:
            st.error(f"Search failed: {e}")


def render_memories():
    """Main memories tab — browse, search, edit, create, bulk actions."""
    st.subheader("Memory Management")

    # ── Create Memory ────────────────────────────────────────────────────
    _render_create_memory()

    st.divider()

    # ── Search Interface ─────────────────────────────────────────────────
    _render_search()

    st.divider()

    # ── Browse & Filter ──────────────────────────────────────────────────
    st.markdown("#### Browse All Memories")

    client = st.session_state.get("api_client")

    # Stats row
    n_total = _try_count_api("memories")
    n_pinned = _try_count_api("memories", "pinned=1")
    try:
        if client:
            cats = client.categories()
            n_cats = len(cats)
        else:
            n_cats = len([r[0] for r in get_conn().execute(
                "SELECT DISTINCT category FROM memories WHERE category IS NOT NULL"
            ).fetchall() if r[0]])
    except Exception:
        n_cats = 0
    try:
        if client:
            r = client.query("SELECT AVG(fitness_score) as avg_fit FROM memories WHERE fitness_score IS NOT NULL")
            rows = r.get("results", [])
            avg_fit = rows[0]["avg_fit"] if rows else None
        else:
            avg_fit = get_conn().execute(
                "SELECT AVG(fitness_score) FROM memories WHERE fitness_score IS NOT NULL"
            ).fetchone()[0]
    except Exception:
        avg_fit = None
    n_hot = _try_count_api("memories", "tier='hot'")
    n_warm = _try_count_api("memories", "tier='warm'")
    n_cold = _try_count_api("memories", "tier='cold'")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total", n_total)
    c2.metric("Pinned", n_pinned)
    c3.metric("Categories", n_cats)
    c4.metric("Avg Fitness", f"{avg_fit:.2f}" if avg_fit else "\u2014")
    c5.metric("Hot / Warm / Cold", f"{n_hot} / {n_warm} / {n_cold}")

    # Charts
    col_cat, col_tier = st.columns([2, 1])
    with col_cat:
        cat_df = _query_api(
            "SELECT COALESCE(category, 'uncategorized') as cat, COUNT(*) as cnt "
            "FROM memories GROUP BY cat ORDER BY cnt DESC"
        )
        if cat_df is not None and not cat_df.empty:
            fig_cat = px.bar(cat_df, x="cat", y="cnt", color="cnt", color_continuous_scale="Viridis", text_auto=True)
            fig_cat.update_layout(**DARK, height=220, margin=dict(t=30, b=10, l=10, r=10), showlegend=False, xaxis_title=None, yaxis_title="Count")
            st.plotly_chart(fig_cat, width="stretch")

    with col_tier:
        tier_df = _query_api(
            "SELECT COALESCE(tier, 'unassigned') as tier, COUNT(*) as cnt "
            "FROM memories GROUP BY tier ORDER BY cnt DESC"
        )
        if tier_df is not None and not tier_df.empty:
            cmap = {"hot": "#ef4444", "warm": "#f59e0b", "cold": "#3b82f6", "unassigned": "#4b5563"}
            fig_tier = px.pie(tier_df, names="tier", values="cnt", color="tier", color_discrete_map=cmap)
            fig_tier.update_layout(**DARK, height=200, margin=dict(t=30, b=10, l=10, r=10))
            st.plotly_chart(fig_tier, width="stretch")

    # Fitness distribution
    fit_df = _query_api("SELECT fitness_score FROM memories WHERE fitness_score IS NOT NULL")
    if fit_df is not None and not fit_df.empty:
        fig_fit = px.histogram(fit_df, x="fitness_score", nbins=30, color_discrete_sequence=["#6366f1"])
        fig_fit.update_layout(**DARK, height=200, margin=dict(t=30, b=10, l=10, r=10), bargap=0.1, xaxis_title="Fitness Score", yaxis_title="Count")
        st.plotly_chart(fig_fit, width="stretch")

    st.divider()

    # ── Filters ──────────────────────────────────────────────────────────
    f_col1, f_col2, f_col3 = st.columns([2, 1, 1])
    with f_col1:
        m_search = st.text_input("\U0001f50d Filter memories", placeholder="content LIKE ...", key="mem_browse_search")
    with f_col2:
        m_min_fit = st.slider("Min fitness", 0.0, 1.0, 0.0, 0.05, key="mem_browse_fit")
    with f_col3:
        try:
            if client:
                cat_options = ["all"] + sorted(client.categories())
            else:
                cat_options = ["all"] + sorted([r[0] for r in get_conn().execute(
                    "SELECT DISTINCT category FROM memories WHERE category IS NOT NULL"
                ).fetchall() if r[0]])
        except Exception:
            cat_options = ["all"]
        m_cat_filter = st.selectbox("Category", cat_options, key="mem_browse_cat")

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
    m_df = _query_api(
        f"SELECT id, substr(content,1,250) as preview, category, created_at, pinned, "
        f"COALESCE(fitness_score, 0.5) as fitness, COALESCE(tier, 'unassigned') as tier, "
        f"COALESCE(importance, 3) as importance "
        f"FROM memories WHERE {where_sql} ORDER BY created_at DESC LIMIT 200",
        params,
    )

    if m_df is not None and not m_df.empty:
        st.session_state["mem_df"] = m_df
        st.caption(f"**{len(m_df)}** memories matching filters")

        display_df = m_df[["id", "category", "fitness", "tier", "importance", "pinned"]].copy()
        display_df["preview"] = m_df["preview"].str[:80]
        display_df["created"] = pd.to_datetime(m_df["created_at"], errors="coerce").dt.strftime("%Y-%m-%d")
        display_df.columns = ["ID", "Category", "Fitness", "Tier", "Importance", "Pinned", "Preview", "Created"]

        # Single-select for editing
        st.markdown("**Click a row to edit:**")
        selected = st.dataframe(
            display_df, use_container_width=True, hide_index=True,
            column_config={
                "Fitness": st.column_config.ProgressColumn("Fitness", min_value=0, max_value=1, format="%.2f"),
                "Preview": st.column_config.TextColumn("Preview", width="large"),
            },
            selection_mode="single-row",
            key="mem_table",
        )

        # Editor for selected row
        _render_memory_editor()

        st.divider()

        # Multi-select for bulk actions using data_editor with checkboxes
        st.markdown("**Select multiple for bulk actions:**")
        bulk_display = display_df[["ID", "Category", "Tier"]].head(50).copy()
        bulk_display["Select"] = False
        edited_bulk = st.data_editor(
            bulk_display, use_container_width=True, hide_index=True,
            column_config={
                "Select": st.column_config.CheckboxColumn("Select"),
            },
            key="mem_table_bulk",
        )

        # Get selected IDs from the data_editor
        bulk_selected = edited_bulk[edited_bulk["Select"] == True]["ID"].tolist() if "Select" in edited_bulk.columns else []
        _render_bulk_actions(bulk_selected)

    else:
        st.info("No memories match the filters")
