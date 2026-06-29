"""Tool registry: defines tool visibility tiers for the MCP server.

This list is the source of truth for what the agent sees in its
tool surface. The mcp_*.py modules define **85** actual @mcp.tool
functions (15 CORE + 70 ADMIN); the tool_registry must stay in
lockstep with that definition. Run `scripts/tool_drift_check.py`
to detect any drift (exits non-zero on mismatch).

CORE_TOOLS (15): always exposed. High-value, day-to-day memory
operations that the agent needs without extra context-budget cost.

ADMIN_TOOLS (70): grouped under `memory_maintenance` so the agent
sees a single router tool. The agent calls them via
`memory_maintenance(operation="...")`. This keeps the visible tool
count low (~20 instead of 80), which reduces model context overhead
and makes the tool surface scannable.
"""

# Core tools: always exposed to agents via MCP.
CORE_TOOLS = [
    "memory_save",
    "memory_search",
    "memory_delete",
    "memory_restore",
    "memory_rebuild",
    "memory_session_start",
    "memory_supersede",
]

# Admin tools: routed through memory_maintenance. Kept in sync with
# the @mcp.tool() definitions in mcp_tools.py. Run
# `scripts/tool_drift_check.py` after adding/removing a tool.
ADMIN_TOOLS = [
    "memory_adaptive_retention",
    "memory_arc_stats",
    "memory_audit",
    "memory_audit_query",
    "memory_auto_save_hook",
    "memory_auto_save_status",
    "memory_auto_save_daemon_metrics",
    "memory_auto_summarize",
    "memory_backfill_all",
    "memory_check_concept_drift",  # 2026-06-15: added to admin (was unfiltered)
    "memory_check_integrity",
    "memory_compact",
    "memory_consolidate",
    "memory_compile_skill",
    "memory_daily_digest",
    "memory_detect_contradictions",
    "memory_facts_list",
    "memory_facts_stats",
    "memory_graph_stats",
    "memory_pinned_decay_check",
    "memory_profile_stats",
    "memory_purge_expired",
    "memory_purge_auto_saves",
    "memory_quality_filter",
    "memory_quality_stats",
    "memory_record_ctr_feedback",  # 2026-06-15: added to admin (was unfiltered)
    "memory_list_drift_alarms",  # 2026-06-22: per-memory drift alarm list + ack (v15)
    "memory_reinforce",
    "memory_retention_stats",
    "memory_review_schedule",
    "memory_rewrite_links",
    "memory_share",
    "memory_shared_import",
    "memory_shared_list",
    "memory_shared_stats",
    "memory_strip_provenance",
    "memory_summarize",
    "memory_summarization_stats",
    "memory_trash",
    "memory_maintenance",  # grouped router; the agent calls this instead
    # of the 30+ admin tools individually
    "memory_crdt_sync",  # 2026-06-17: multi-agent CRDT sync
    "memory_crdt_status",  # 2026-06-17: multi-agent sync status
    "memory_okf_export",  # 2026-06-17: Open Knowledge Format export
    "memory_okf_import",  # 2026-06-17: Open Knowledge Format import
    # 2026-06-18: H4 fix — 5 tools leaked through the filter
    "memory_heartbeat",
    "memory_tier_stats",
    "memory_run_tier_migration",
    "memory_check_embedding_model",
    "memory_incremental_update",
    "memory_duplicates",
    "memory_merge_suggestions",
    "memory_llm_unload",
    "memory_ingest_file",
    "memory_ingest_url",
    "memory_dashboard",
    "memory_metrics_server",
    # 2026-06-22: 8 tools were exposed via @mcp.tool() but not curated
    # in this registry. Now listed to match the actual surface.
    "memory_agent_init",  # mcp_agent.py — initialize agent memory scope
    "memory_agent_clear",  # mcp_agent.py — clear agent memory scope
    "memory_agent_list",  # mcp_agent.py — list agent memory scopes
    "memory_arc_reset",  # mcp_maintenance.py — reset ARC ghost lists + stats
    "memory_extract_skills",  # mcp_maintenance.py — refresh memory_skills cache
    "memory_list_skills",  # mcp_maintenance.py — list cached skills
    "memory_sdk_demo",  # mcp_sdk.py — SDK demo / quickstart
    "memory_auto_share",  # mcp_sharing.py — auto-publish opt-in memories
    # 2026-06-25: 5 tools were exposed via @mcp.tool() but not in registry (drift fix)
    "memory_graph_shortest_path",  # mcp_memory_server.py — KG shortest path
    "memory_graph_traverse",  # mcp_memory_server.py — KG edge traversal
    "memory_circuit_breaker_status",  # mcp_maintenance.py — CB open/close history
    "memory_temporal_contradictions",  # mcp_maintenance.py — fact supersession events
    "memory_temporal_query",  # mcp_maintenance.py — time-aware KG queries
    "memory_compliance_check",  # mcp_audit.py — AGENTS.md rule compliance audit
    # Demoted core tools
    "memory_semantic_search",
    "memory_facts_search",
    "memory_graph_search",
    "memory_recall_context",
    "memory_thread_context",
    "memory_list_threads",
    "memory_resolve_thread",
    "memory_user_profile",
    "memory_check_contradictions",
    "memory_scan_injection",
    "memory_profile_access",
]
