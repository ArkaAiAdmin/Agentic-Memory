#!/usr/bin/env python3
"""Audit tab — Full audit log, performance metrics, error drill-down, export."""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from dashboard import DARK
from dashboard.api_client import _api

logger = logging.getLogger(__name__)


@st.cache_data(ttl=300)
def _cached_audit_query(where_sql, params_tuple):
    """Cache the audit log query — routes through the REST API when available."""
    client = _api()
    if client:
        try:
            # Extract hours from the WHERE clause for the REST API
            import re
            hours_match = re.search(r"ts >= strftime\('%s','now'\)-(\d+)", where_sql)
            hours = int(hours_match.group(1)) // 3600 if hours_match else 24
            tool_filter = ""
            tool_match = re.search(r"tool LIKE '\%(.+?)\%'", where_sql)
            if tool_match:
                tool_filter = tool_match.group(1)
            errors_only = "error IS NOT NULL" in where_sql
            logs = client.get_audit_logs(hours=hours, tool=tool_filter, errors_only=errors_only)
            if logs:
                return pd.DataFrame(logs)
        except Exception:
            pass
    # Fallback: direct DB read (read-only, for standalone/test runs)
    from dashboard import query
    return query(
        f"SELECT ts, tool, latency_ms, results_count, error, args "
        f"FROM memory_audit_log WHERE {where_sql} ORDER BY ts DESC LIMIT 2000",
        params_tuple,
    )


def _render_error_drilldown(df: pd.DataFrame):
    """Render error grouping and drill-down."""
    errs = df[df["has_err"]]
    if errs.empty:
        return

    st.markdown("#### Error Analysis")

    # Group errors by tool
    err_by_tool = errs.groupby("tool").agg(
        count=("error", "count"),
        latest=("ts_dt", "max"),
        sample_error=("error", "first"),
    ).reset_index().sort_values("count", ascending=False)

    err_cols = st.columns([2, 3])

    with err_cols[0]:
        fig_err = px.bar(
            err_by_tool, x="count", y="tool", orientation="h",
            color="count", color_continuous_scale="Reds", text_auto=True,
        )
        fig_err.update_layout(**DARK, height=max(200, len(err_by_tool) * 35),
                              margin=dict(t=30, b=10, l=10, r=10), showlegend=False, yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig_err, width="stretch")

    with err_cols[1]:
        for _, r in err_by_tool.iterrows():
            st.html(
                f"<div style='background:#1a1d23;border:1px solid #2d3139;border-radius:8px;"
                f"padding:8px 12px;margin:4px 0;'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center;'>"
                f"<span style='color:#d1d5db;font-size:0.78rem;font-weight:600;'>{r['tool']}</span>"
                f"<span style='color:#ef4444;font-size:0.65rem;'>{r['count']} errors</span>"
                f"</div>"
                f"<div style='color:#6b7280;font-size:0.7rem;margin-top:4px;'>"
                f"{str(r.get('sample_error', ''))[:120]}</div>"
                f"</div>",
            )


def _render_latency_analysis(df: pd.DataFrame):
    """Render latency distribution and trends."""
    st.markdown("#### Latency Distribution")

    col_hist, col_box = st.columns(2)

    with col_hist:
        fig_hist = px.histogram(
            df, x="latency_ms", nbins=40, color_discrete_sequence=["#6366f1"],
            title="Latency Distribution",
        )
        fig_hist.update_layout(**DARK, height=280, margin=dict(t=40, b=10, l=10, r=10),
                               bargap=0.1, xaxis_title="Latency (ms)", yaxis_title="Count")
        st.plotly_chart(fig_hist, width="stretch")

    with col_box:
        if "tool" in df.columns:
            top_tools = df["tool"].value_counts().head(8).index.tolist()
            box_df = df[df["tool"].isin(top_tools)]
            fig_box = px.box(
                box_df, x="tool", y="latency_ms", color="tool",
                title="Latency by Tool (top 8)",
            )
            fig_box.update_layout(**DARK, height=280, margin=dict(t=40, b=10, l=10, r=10),
                                  showlegend=False, xaxis_title=None, yaxis_title="Latency (ms)")
            st.plotly_chart(fig_box, width="stretch")


def _render_export(df: pd.DataFrame):
    """Render export functionality."""
    st.markdown("#### Export")

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("\U0001f4e5 Export CSV", use_container_width=True):
            csv_data = df.to_csv(index=False)
            st.download_button(
                "Download CSV",
                csv_data,
                file_name=f"audit_log_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )

    with col2:
        if st.button("\U0001f4e5 Export JSON", use_container_width=True):
            json_data = df.to_json(orient="records", date_format="iso", indent=2)
            st.download_button(
                "Download JSON",
                json_data,
                file_name=f"audit_log_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
                mime="application/json",
            )

    with col3:
        # Time range summary
        if not df.empty and "ts_dt" in df.columns:
            min_dt = df["ts_dt"].min()
            max_dt = df["ts_dt"].max()
            st.caption(f"Range: {min_dt.strftime('%Y-%m-%d %H:%M')} \u2192 {max_dt.strftime('%Y-%m-%d %H:%M')}")


def render_audit():
    """Main audit tab — full audit log, performance, errors, export."""
    if "audit_rendered" not in st.session_state:
        st.session_state["audit_rendered"] = False

    st.subheader("Audit Log & API Performance")

    # ── Time range filter ────────────────────────────────────────────────
    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
    with f_col1:
        hours = st.selectbox("Time range", [24, 72, 168, 720], index=0, key="audit_hours",
                             format_func=lambda x: f"Last {x}h" if x < 168 else f"Last {x // 24}d")
    with f_col2:
        tool_filter = st.text_input("Filter tool", placeholder="e.g. memory_save", key="audit_tool_filter")
    with f_col3:
        error_only = st.checkbox("Errors only", key="audit_errors_only")
    with f_col4:
        min_latency = st.number_input("Min latency (ms)", value=0, key="audit_min_lat")

    # Build query
    where_parts = [f"ts >= strftime('%s','now')-{hours * 3600}"]
    params = []
    if tool_filter:
        where_parts.append("tool LIKE ?")
        params.append(f"%{tool_filter}%")
    if error_only:
        where_parts.append("error IS NOT NULL")
    if min_latency > 0:
        where_parts.append("latency_ms >= ?")
        params.append(min_latency)

    where_sql = " AND ".join(where_parts)

    df = _cached_audit_query(where_sql, tuple(params))

    if df is None or df.empty:
        st.info("No audit log entries match the filters.")
        return

    # ── Core metrics ─────────────────────────────────────────────────────
    df["ts_dt"] = pd.to_datetime(df["ts"], unit="s", errors="coerce")
    df["has_err"] = df["error"].notna()
    df["day"] = df["ts_dt"].dt.date

    p50 = df["latency_ms"].quantile(0.5)
    p95 = df["latency_ms"].quantile(0.95)
    p99 = df["latency_ms"].quantile(0.99)
    err_count = df["has_err"].sum()
    n_tools = df["tool"].nunique()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Calls", len(df))
    c2.metric("Avg Latency", f"{df['latency_ms'].mean():.0f} ms")
    c3.metric("p50 / p95", f"{p50:.0f} / {p95:.0f} ms")
    c4.metric("p99", f"{p99:.0f} ms")
    c5.metric("Error Rate", f"{df['has_err'].mean() * 100:.1f}%")
    c6.metric("Unique Tools", n_tools)

    _render_export(df)

    st.divider()

    # ── Tool performance ─────────────────────────────────────────────────
    col_calls, col_trend = st.columns([1, 2])

    with col_calls:
        st.markdown("#### Calls by Tool")
        tc = df["tool"].value_counts().reset_index()
        tc.columns = ["Tool", "Calls"]
        fig_calls = px.bar(tc, x="Calls", y="Tool", orientation="h",
                           color="Calls", color_continuous_scale="Viridis", text_auto=True)
        fig_calls.update_layout(**DARK, height=max(250, len(tc) * 30), margin=dict(t=30, b=10, l=10, r=10),
                                showlegend=False, yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig_calls, width="stretch")

    with col_trend:
        st.markdown("#### Latency Trend")
        daily = df.groupby("day").agg(
            avg_lat=("latency_ms", "mean"),
            p95_lat=("latency_ms", lambda x: x.quantile(0.95)),
            calls=("latency_ms", "count"),
            errors=("has_err", "sum"),
        ).reset_index()
        daily["error_rate"] = (daily["errors"] / daily["calls"] * 100).round(1)

        fig_trend = go.Figure()
        fig_trend.add_trace(go.Scatter(
            x=daily["day"], y=daily["avg_lat"],
            name="Avg Latency", mode="lines+markers",
            line=dict(color="#6366f1", width=2),
        ))
        fig_trend.add_trace(go.Scatter(
            x=daily["day"], y=daily["p95_lat"],
            name="p95 Latency", mode="lines+markers",
            line=dict(color="#f59e0b", width=2, dash="dot"),
        ))
        fig_trend.add_trace(go.Bar(
            x=daily["day"], y=daily["errors"],
            name="Errors", marker_color="#ef4444", opacity=0.5, yaxis="y2",
        ))
        fig_trend.update_layout(
            **DARK, height=300,
            margin=dict(t=30, b=10, l=10, r=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, font=dict(size=9)),
            yaxis=dict(title="Latency (ms)"),
            yaxis2=dict(title="Errors", overlaying="y", side="right", showgrid=False),
        )
        st.plotly_chart(fig_trend, width="stretch")

    st.divider()

    # ── Tool Performance Table ───────────────────────────────────────────
    st.markdown("#### Tool Performance Detail")
    tool_perf = df.groupby("tool").agg(
        calls=("latency_ms", "count"),
        avg_lat=("latency_ms", "mean"),
        p50_lat=("latency_ms", lambda x: x.quantile(0.5)),
        p95_lat=("latency_ms", lambda x: x.quantile(0.95)),
        max_lat=("latency_ms", "max"),
        errors=("has_err", "sum"),
    ).reset_index()
    tool_perf["error_rate"] = (tool_perf["errors"] / tool_perf["calls"] * 100).round(1)
    tool_perf = tool_perf.sort_values("calls", ascending=False)

    display_perf = tool_perf[["tool", "calls", "avg_lat", "p50_lat", "p95_lat", "max_lat", "error_rate"]].copy()
    display_perf.columns = ["Tool", "Calls", "Avg (ms)", "p50 (ms)", "p95 (ms)", "Max (ms)", "Error %"]
    for col in ["Avg (ms)", "p50 (ms)", "p95 (ms)", "Max (ms)"]:
        display_perf[col] = display_perf[col].round(0)

    st.dataframe(display_perf, width="stretch", hide_index=True, column_config={
        "Error %": st.column_config.ProgressColumn("Error %", min_value=0, max_value=100, format="%.1f%%"),
    })

    # ── Latency Analysis ─────────────────────────────────────────────────
    _render_latency_analysis(df)

    st.divider()

    # ── Error Analysis ───────────────────────────────────────────────────
    _render_error_drilldown(df)

    # ── Drill-in by tool ─────────────────────────────────────────────────
    with st.expander("Drill in by tool", expanded=False):
        drill_tool = st.selectbox("Select tool", ["all"] + sorted(tool_perf["tool"].tolist()), key="audit_drill_tool")
        drill_df = df if drill_tool == "all" else df[df["tool"] == drill_tool]
        st.dataframe(
            drill_df[["ts_dt", "tool", "latency_ms", "results_count", "error"]].head(200).copy(),
            width="stretch", hide_index=True,
            column_config={
                "ts_dt": st.column_config.DatetimeColumn("Time"),
                "latency_ms": "Latency (ms)",
                "results_count": "Results",
                "error": st.column_config.TextColumn("Error", width="large"),
            },
        )
