"""Streamlit login page for the Agentic Memory dashboard (Phase 2).

The dashboard is an operator console. When the REST API enforces auth (no
static MEMORY_API_TOKEN configured, or the deployment requires a session),
the operator logs in with an API token; the token is exchanged for a JWT
session cookie that the ApiClient keeps for the session. The raw token is
never persisted in the browser session state.
"""

from __future__ import annotations

import os

import streamlit as st

from dashboard.api_client import ApiClient


def render_login(base_url: str) -> None:
    """Render the login form and perform the token -> cookie exchange."""
    st.set_page_config(page_title="Agentic Memory — Sign in", page_icon="\U0001f9e0")
    st.title("Agentic Memory")
    st.caption("Operator console — sign in to continue")

    with st.form("login_form"):
        token = st.text_input(
            "API token",
            type="password",
            help="Your MEMORY_API_TOKEN or a [api.principals] mapped token.",
        )
        submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:
        if not token:
            st.error("Enter your API token to continue.")
            return
        client = ApiClient(base_url=base_url)
        try:
            client.login(token)
        except Exception as exc:  # network / 403 / 503
            st.error(f"Login failed: {exc}")
            return
        if not client.authenticated:
            st.error("Login failed: no session returned.")
            return
        st.session_state.api_client = client
        st.session_state.authenticated = True
        st.rerun()


def requires_login() -> bool:
    """Return True if the dashboard must gate behind login.

    Login is required when no static API token is configured for the
    ApiClient (i.e. the deployment relies on the session-cookie flow).
    Operators who set MEMORY_API_TOKEN get the legacy bearer behaviour and
    skip the login page.
    """
    return not bool(os.environ.get("MEMORY_API_TOKEN", "").strip())
