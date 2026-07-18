from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

import dashboard


def render_sidebar():
    st.sidebar.html(
        """<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px;'>
            <div style='width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#8b5cf6,#6366f1);display:flex;align-items:center;justify-content:center;font-size:1.1rem;box-shadow:0 2px 8px rgba(139,92,246,0.3);'>\U0001f9e0</div>
            <div>
                <div style='color:#f0f2f6;font-weight:800;font-size:1.05rem;letter-spacing:-0.03em;line-height:1.1;'>Agentic Memory</div>
                <div style='color:#6b7280;font-size:0.55rem;letter-spacing:0.08em;text-transform:uppercase;font-weight:500;'>Local Agent Memory System</div>
            </div>
        </div>""",
    )

    # ── Agent selector ──────────────────────────────────────────────────
    # The install runs two agent processes (OpenCode + MIMOCODE), each with
    # its own physical DB. Surface a switcher so the dashboard can show
    # EITHER agent's full store instead of being hard-wired to one.
    _base = dashboard.resolve_db().parent
    _agents = {
        "OpenCode": _base / "memory.db",
        "MIMOCODE": _base / "memory-agent-b.db",
    }
    _valid = {k: v for k, v in _agents.items() if v.exists()}
    if _valid:
        if "agent_view" not in st.session_state:
            st.session_state["agent_view"] = "OpenCode" if "OpenCode" in _valid else next(iter(_valid))
        _choice = st.sidebar.selectbox(
            "Agent store",
            options=list(_valid.keys()),
            index=list(_valid.keys()).index(st.session_state["agent_view"]),
            key="agent_view_select",
        )
        # Switching the active agent store must invalidate the data caches
        # (try_count/table/query are keyed only on their SQL args, not on DB).
        if st.session_state.get("agent_view_db") != str(_valid[_choice]):
            st.cache_data.clear()
        st.session_state["agent_view"] = _choice
        st.session_state["agent_view_db"] = str(_valid[_choice])
        dashboard.DB = _valid[_choice]
        dashboard.MEM_DIR = dashboard.DB.parent

    st.sidebar.caption(
        f"`{dashboard.DB.parent.name}`  \u00b7 "
        f"{dashboard.DB.stat().st_size / 1024 / 1024:.0f} MB"
    )

    with st.sidebar:
        st.markdown("### System Overview")

        n_mem = dashboard.try_count("memories")
        n_ent = dashboard.try_count("kg_entities")
        n_edg = dashboard.try_count("kg_edges")
        n_audit = dashboard.try_count("memory_audit_log")
        n_pin = dashboard.try_count("memories", "pinned=1")
        n_facts = dashboard.try_count("kg_facts")

        c1, c2, c3 = st.columns(3)
        c1.metric("Memories", n_mem)
        c2.metric("Entities", n_ent)
        c3.metric("Facts", n_facts)

        c1.metric("Edges", n_edg)
        c2.metric("Pinned", n_pin)
        c3.metric("DB", f"{dashboard.DB.stat().st_size / 1024 / 1024:.0f} MB")

        st.divider()

        st.caption("Quick Actions")
        if st.button("\u21bb Refresh Now", key="sidebar_refresh", width="stretch"):
            st.rerun()
        st.html(
            f"<div style='display:flex;align-items:center;gap:6px;margin-top:4px;'>"
            f"<span style='width:6px;height:6px;border-radius:50%;background:#10b981;animation:pulse-dot 2s ease-in-out infinite;display:inline-block;'></span>"
            f"<span style='color:#6b7280;font-size:0.6rem;'>live \u00b7 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</span>"
            f"</div>",
        )
        st.html("""
        <style>
        @keyframes pulse-dot {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.5; transform: scale(0.8); }
        }
        </style>
        """)
