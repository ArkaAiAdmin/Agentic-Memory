from __future__ import annotations

import os
from datetime import datetime, timezone

import streamlit as st

import dashboard
from dashboard import _run_health_checks, _compute_health_score
from dashboard.api_client import ApiClient, _try_count_api


# Agent identity → API base URL. OPENCODE happens to coincide with the rest-api
# port (9879) when the launchd managed rest-api is running; MIMOCODE routes via
# its sync daemon (9877) which proxies read-only query requests against its DB.
# Direct sqlite access is restricted to OPENCODE = dashboard.DB.
_AGENT_API_BASE = {
    "OpenCode": os.environ.get("MEMORY_API_BASE", "http://127.0.0.1:9879"),
    "MIMOCODE": os.environ.get("MEMORY_API_BASE_MIMO", "http://127.0.0.1:9877"),
}


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
    _base = dashboard.resolve_db().parent
    _agents = {
        "OpenCode": _base / "memory.db",
        "MIMOCODE": _base / "memory-agent-b.db",
    }
    _valid = {k: v for k, v in _agents.items() if v.exists()}
    if _Valid := _valid:
        if "agent_view" not in st.session_state:
            st.session_state["agent_view"] = "OpenCode" if "OpenCode" in _Valid else next(iter(_Valid))
        _choice = st.sidebar.selectbox(
            "Agent store",
            options=list(_Valid.keys()),
            index=list(_Valid.keys()).index(st.session_state["agent_view"]),
            key="agent_view_select",
            help=(
                "Switch API client only. Direct sqlite file access is restricted "
                "to OPENCODE — viewing MIMOCODE is read-only via the sync daemon "
                "(port 9877). Writes to MIMOCODE go through its own MCP."
            ),
        )
        if st.session_state.get("agent_view") != _choice:
            # Agent changed — rewire the API client to the right port.
            _token = os.environ.get("MEMORY_API_TOKEN", "")
            _base_url = _AGENT_API_BASE.get(_choice, os.environ.get("MEMORY_API_BASE", "http://127.0.0.1:9879"))
            st.session_state.api_client = ApiClient(base_url=_base_url, token=_token)
            st.cache_data.clear()
        st.session_state["agent_view"] = _choice
        st.session_state["agent_view_db"] = str(_Valid[_choice])
        # dashboard.DB stays bound to OPENCODE for the read-only fallback
        # path that resolution of OPENCODE local files requires. MIMOCODE
        # reads flow exclusively through the API client.
        dashboard.DB = _agents["OpenCode"]
        dashboard.MEM_DIR = dashboard.DB.parent
        st.session_state["agent_view_remote"] = _choice != "OpenCode"

    st.sidebar.caption(
        f"`{dashboard.DB.parent.name}`  \u00b7 "
        f"{dashboard.DB.stat().st_size / 1024 / 1024:.0f} MB"
    )

    with st.sidebar:
        st.markdown("### System Overview")

        n_mem = _try_count_api("memories")
        n_ent = _try_count_api("kg_entities")
        n_edg = _try_count_api("kg_edges")
        n_facts = _try_count_api("kg_facts")
        n_pin = _try_count_api("memories", "pinned=1")

        c1, c2, c3 = st.columns(3)
        c1.metric("Memories", n_mem)
        c2.metric("Entities", n_ent)
        c3.metric("Facts", n_facts)

        c1.metric("Edges", n_edg)
        c2.metric("Pinned", n_pin)
        c3.metric("DB", f"{dashboard.DB.stat().st_size / 1024 / 1024:.0f} MB")

        st.divider()

        # ── Quick health snapshot ────────────────────────────────────────
        st.markdown("### Health")
        checks = _run_health_checks()
        score, label = _compute_health_score(checks)
        color = "#10b981" if score >= 80 else "#f59e0b" if score >= 60 else "#ef4444"
        st.html(
            f"<div style='display:flex;align-items:center;gap:8px;padding:6px 0;'>"
            f"<span style='color:{color};font-size:1.3rem;font-weight:700;'>{score}</span>"
            f"<span style='color:#9ca3af;font-size:0.75rem;'>{label}</span>"
            f"</div>"
        )

        n_warn = sum(1 for c in checks if c["status"] == "warning")
        n_err = sum(1 for c in checks if c["status"] == "error")
        if n_err > 0:
            st.sidebar.error(f"{n_err} error(s) detected")
        elif n_warn > 0:
            st.sidebar.warning(f"{n_warn} warning(s)")
        else:
            st.sidebar.success("All systems nominal")

        st.divider()

        # ── Quick Actions ────────────────────────────────────────────────
        st.markdown("### Quick Actions")

        if st.button("\u21bb Refresh Now", key="sidebar_refresh", width="stretch"):
            st.rerun()

        # Phase 2: logout clears the JWT session cookie.
        if st.button("\U0001f511 Sign out", key="sidebar_logout", width="stretch"):
            client = st.session_state.get("api_client")
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            st.session_state.pop("authenticated", None)
            st.session_state.api_client = ApiClient(
                base_url=os.environ.get("MEMORY_API_BASE", "http://127.0.0.1:9878"),
                token=os.environ.get("MEMORY_API_TOKEN", ""),
            )
            st.rerun()

        # Live indicator
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

        # ── Keyboard shortcuts ──────────────────────────────────────────
        with st.expander("\u2328\ufe0f Shortcuts", expanded=False):
            st.html(
                "<div style='font-size:0.7rem;color:#9ca3af;line-height:1.8;'>"
                "<b>/</b> — Focus search &nbsp; <b>r</b> — Refresh<br>"
                "<b>1-6</b> — Switch tabs &nbsp; <b>e</b> — Edit memory<br>"
                "<b>?</b> — Show this help"
                "</div>"
            )
