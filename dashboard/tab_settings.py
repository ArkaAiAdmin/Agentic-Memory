#!/usr/bin/env python3
"""Settings tab — Feature flags, system info, onboarding, export, config."""
from __future__ import annotations

import json
import logging
import platform
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
import tomllib

import dashboard
from dashboard import DARK, _get_schema_version, get_conn, query, try_count
from dashboard.api_client import _query_api, _try_count_api, _table_exists_api

logger = logging.getLogger(__name__)
ROOT = dashboard._REPO_ROOT


def _render_onboarding():
    """Render onboarding guidance for new users."""
    n_mem = _try_count_api("memories")
    if n_mem > 10:
        return  # Not a new user

    st.html(
        "<div class='onboard-card'>"
        "<h4>Welcome to Agentic Memory Dashboard</h4>"
        "<p>This dashboard helps you monitor and manage your AI agent's persistent memory system. "
        "Here's what you can do:</p>"
        "</div>",
    )

    cols = st.columns(3)

    with cols[0]:
        st.html(
            "<div class='onboard-card'>"
            "<h4>\U0001f4ca Dashboard</h4>"
            "<p>View system health, key metrics, and recent activity. "
            "Use Quick Actions to run common maintenance tasks.</p>"
            "</div>",
        )

    with cols[1]:
        st.html(
            "<div class='onboard-card'>"
            "<h4>\U0001f4dd Memories</h4>"
            "<p>Create, edit, and organize your memories. "
            "Use search to find specific notes. Pin important ones.</p>"
            "</div>",
        )

    with cols[2]:
        st.html(
            "<div class='onboard-card'>"
            "<h4>\U0001f9e0 Knowledge</h4>"
            "<p>Explore the knowledge graph, facts, and embeddings. "
            "See how your memories connect and cluster.</p>"
            "</div>",
        )


def _render_system_info():
    """Render system information panel."""
    st.markdown("#### System Info")

    db_path = str(dashboard.DB) if dashboard.DB else "N/A"
    mem_dir = str(dashboard.MEM_DIR) if dashboard.MEM_DIR else "N/A"

    mem_dir_size = 0
    if dashboard.MEM_DIR and dashboard.MEM_DIR.exists():
        try:
            mem_dir_size = sum(f.stat().st_size for f in dashboard.MEM_DIR.rglob("*") if f.is_file()) / (1024 * 1024)
        except Exception:
            pass

    sys_info = {
        "Python": platform.python_version(),
        "Platform": f"{platform.system()} {platform.release()}",
        "Architecture": platform.machine(),
        "DB Path": db_path,
        "Memory Dir": mem_dir,
        "Schema": _get_schema_version(),
        "DB Size": f"{dashboard.DB.stat().st_size / 1024 / 1024:.1f} MB" if dashboard.DB and dashboard.DB.exists() else "N/A",
        "Total Size": f"{mem_dir_size:.1f} MB",
        "Last Updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    for k, v in sys_info.items():
        st.html(
            f"<div style='display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1f2937;'>"
            f"<span style='color:#8b8fa3;font-size:0.78rem;'>{k}</span>"
            f"<span style='color:#d1d5db;font-size:0.78rem;font-weight:600;'>{v}</span>"
            f"</div>",
        )


def _render_feature_flags():
    """Render feature flags as working toggle switches."""
    st.markdown("#### Feature Flags")

    try:
        toml_path = ROOT / "memory.toml"
        toml_text = toml_path.read_text()
        raw = tomllib.loads(toml_text)
        feat_section = raw.get("features", {})

        if not feat_section:
            st.info("No feature flags configured in memory.toml")
            return

        changed = False
        new_lines = toml_text.split("\n")

        for flag_name, val in feat_section.items():
            val_str = str(val).lower()

            # Only show toggle for boolean flags
            if val_str in ("true", "false", "yes", "no"):
                current = val_str in ("true", "yes")
                new_val = st.toggle(
                    flag_name,
                    value=current,
                    key=f"flag_{flag_name}",
                    help=f"memory.toml: [features].{flag_name}",
                )

                if new_val != current:
                    # Update the TOML file
                    for i, line in enumerate(new_lines):
                        stripped = line.strip()
                        if stripped.startswith(f"{flag_name} =") or stripped.startswith(f"{flag_name}="):
                            old_val = "true" if current else "false"
                            new_val_str = "true" if new_val else "false"
                            new_lines[i] = line.replace(f"= {old_val}", f"= {new_val_str}").replace(f"={old_val}", f"={new_val_str}")
                            changed = True
                            break
            else:
                # Non-boolean flags (strings, ints) — show as read-only
                st.html(
                    f"<div style='display:flex;justify-content:space-between;align-items:center;"
                    f"padding:8px 0;border-bottom:1px solid #1f2937;'>"
                    f"<span style='color:#d1d5db;font-size:0.78rem;'>{flag_name}</span>"
                    f"<span style='color:#60a5fa;font-size:0.78rem;font-weight:600;'>{val}</span>"
                    f"</div>",
                )

        if changed:
            toml_path.write_text("\n".join(new_lines))
            st.toast("Feature flags updated. Restart worker for changes to take effect.", icon="\u2705")
            st.rerun()

    except Exception as e:
        st.info(f"Feature flags unavailable: {e}")


def _render_export_section():
    """Render data export functionality."""
    st.markdown("#### Export Data")

    cols = st.columns(3)

    with cols[0]:
        if st.button("\U0001f4e5 Export Memories (JSON)", use_container_width=True):
            try:
                df = _query_api("SELECT * FROM memories ORDER BY created_at DESC")
                if df is not None and not df.empty:
                    json_data = df.to_json(orient="records", date_format="iso", indent=2)
                    st.download_button(
                        "Download JSON",
                        json_data,
                        file_name=f"memories_{datetime.now().strftime('%Y%m%d')}.json",
                        mime="application/json",
                        key="dl_mem_json",
                    )
                else:
                    st.info("No memories to export")
            except Exception as e:
                st.error(f"Export failed: {e}")

    with cols[1]:
        if st.button("\U0001f4e5 Export Memories (CSV)", use_container_width=True):
            try:
                df = _query_api("SELECT id, content, category, importance, tier, pinned, fitness_score, created_at FROM memories ORDER BY created_at DESC")
                if df is not None and not df.empty:
                    csv_data = df.to_csv(index=False)
                    st.download_button(
                        "Download CSV",
                        csv_data,
                        file_name=f"memories_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_mem_csv",
                    )
                else:
                    st.info("No memories to export")
            except Exception as e:
                st.error(f"Export failed: {e}")

    with cols[2]:
        if st.button("\U0001f4e5 Export Audit Log (CSV)", use_container_width=True):
            try:
                df = _query_api("SELECT * FROM memory_audit_log ORDER BY ts DESC LIMIT 5000")
                if df is not None and not df.empty:
                    csv_data = df.to_csv(index=False)
                    st.download_button(
                        "Download CSV",
                        csv_data,
                        file_name=f"audit_log_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        key="dl_audit_csv",
                    )
                else:
                    st.info("No audit entries to export")
            except Exception as e:
                st.error(f"Export failed: {e}")

    # Knowledge graph export
    st.divider()
    st.markdown("#### Export Knowledge Graph")

    kg_cols = st.columns(2)

    with kg_cols[0]:
        if st.button("\U0001f4e5 Export KG as JSON", use_container_width=True):
            try:
                entities = _query_api("SELECT * FROM kg_entities")
                edges = _query_api("SELECT * FROM kg_edges") if _table_exists_api("kg_edges") else None
                facts = _query_api("SELECT * FROM kg_facts") if _table_exists_api("kg_facts") else None

                kg_data = {
                    "entities": entities.to_dict("records") if entities is not None else [],
                    "edges": edges.to_dict("records") if edges is not None else [],
                    "facts": facts.to_dict("records") if facts is not None else [],
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                }
                json_data = json.dumps(kg_data, indent=2, default=str)
                st.download_button(
                    "Download KG JSON",
                    json_data,
                    file_name=f"kg_export_{datetime.now().strftime('%Y%m%d')}.json",
                    mime="application/json",
                    key="dl_kg_json",
                )
            except Exception as e:
                st.error(f"Export failed: {e}")

    with kg_cols[1]:
        if st.button("\U0001f4e5 Export as GraphML", use_container_width=True):
            try:
                import networkx as nx
                entities = _query_api("SELECT id, name, entity_type, mentions FROM kg_entities")
                edges = _query_api("SELECT source_id, target_id, relation, weight FROM kg_edges") if _table_exists_api("kg_edges") else None

                if entities is not None and not entities.empty:
                    G = nx.Graph()
                    for _, r in entities.iterrows():
                        G.add_node(r["name"], type=r["entity_type"], mentions=r["mentions"])
                    if edges is not None and not edges.empty:
                        name_map = dict(zip(entities["id"], entities["name"]))
                        for _, r in edges.iterrows():
                            src = name_map.get(r["source_id"])
                            tgt = name_map.get(r["target_id"])
                            if src and tgt:
                                G.add_edge(src, tgt, relation=r.get("relation", ""), weight=r.get("weight", 1))

                    graphml_data = nx.generate_graphml(G)
                    st.download_button(
                        "Download GraphML",
                        graphml_data,
                        file_name=f"kg_{datetime.now().strftime('%Y%m%d')}.graphml",
                        mime="application/xml",
                        key="dl_graphml",
                    )
                else:
                    st.info("No entities to export")
            except Exception as e:
                st.error(f"Export failed: {e}")


def _render_config_viewer():
    """Render the memory.toml config viewer."""
    st.markdown("#### Configuration")

    try:
        toml_path = ROOT / "memory.toml"
        config_text = toml_path.read_text()
        with st.expander("memory.toml (full config)", expanded=False):
            st.code(config_text, language="toml")
    except Exception as e:
        st.info(f"Config unavailable: {e}")


def render_settings():
    """Main settings tab — onboarding, system info, flags, export, config."""
    if "settings_rendered" not in st.session_state:
        st.session_state["settings_rendered"] = False

    st.subheader("Settings")

    _render_onboarding()

    col1, col2 = st.columns(2)

    with col1:
        _render_system_info()
        st.divider()
        _render_config_viewer()

    with col2:
        _render_feature_flags()
        st.divider()
        _render_export_section()
