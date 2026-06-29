"""
MCP tool registry — thin re-export layer.

Each domain module registers its tools on the shared ``mcp`` instance
via ``@mcp.tool()`` at import time.  This module re-exports every tool
and helper so that ``from mcp_tools import memory_search`` works.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

# ---- domain modules (import = register via @mcp.tool()) ----



from mcp_search import (  # noqa: F401
    search_memories,
    memory_search,
    memory_semantic_search,
    memory_recall_context,
    memory_session_start,
)
from mcp_kg import (  # noqa: F401
    memory_graph_search,
    memory_graph_stats,
    memory_facts_search,
    memory_facts_list,
    memory_facts_stats,
)
from mcp_kg_traversal import (  # noqa: F401
    memory_graph_shortest_path,
    memory_graph_traverse,
)
from mcp_rebuild import (  # noqa: F401
    memory_rebuild,
    memory_compact,
    memory_backfill_all,
)
from mcp_audit import (  # noqa: F401
    memory_audit,
    memory_audit_query,
    memory_check_integrity,
)
from mcp_crdt import (  # noqa: F401
    memory_crdt_sync,
    memory_crdt_status,
)
from mcp_session import (  # noqa: F401
    memory_thread_context,
    memory_list_threads,
    memory_resolve_thread,
)
from mcp_maintenance import (  # noqa: F401
    memory_heartbeat,
    memory_tier_stats,
    memory_run_tier_migration,
    memory_check_embedding_model,
    memory_incremental_update,
    memory_duplicates,
    memory_merge_suggestions,
    memory_consolidate,
    memory_rewrite_links,
    memory_detect_contradictions,
    memory_arc_stats,
    memory_review_schedule,
    memory_pinned_decay_check,
    memory_compile_skill,
    memory_maintenance,
)
from mcp_memory import (  # noqa: F401
    memory_save,
    memory_supersede,
    memory_auto_save_hook,
    memory_daily_digest,
    memory_auto_save_status,
    memory_reinforce,
    memory_delete,
    memory_restore,
    memory_trash,
    memory_purge_expired,
    memory_purge_auto_saves,
)
from mcp_safety import (  # noqa: F401
    memory_scan_injection,
    memory_strip_provenance,
    memory_check_contradictions,
)
from mcp_quality import (  # noqa: F401
    memory_quality_filter,
    memory_quality_stats,
)
from mcp_summarization import (  # noqa: F401
    memory_summarize,
    memory_auto_summarize,
    memory_summarization_stats,
)
from mcp_profile import (  # noqa: F401
    memory_profile_access,
    memory_user_profile,
    memory_profile_stats,
)
from mcp_retention import (  # noqa: F401
    memory_adaptive_retention,
    memory_retention_stats,
)
from mcp_sharing import (  # noqa: F401
    memory_share,
    memory_shared_list,
    memory_shared_import,
    memory_shared_stats,
    memory_auto_share,
)
from mcp_ctr_drift import (  # noqa: F401
    memory_record_ctr_feedback,
    memory_check_concept_drift,
    memory_list_drift_alarms,
)
from mcp_okf import (  # noqa: F401
    memory_okf_export,
    memory_okf_import,
)
from mcp_maintenance import (  # noqa: F401
    memory_llm_unload,
)
from mcp_multi_modal import (  # noqa: F401
    memory_ingest_file,
    memory_ingest_url,
)
from mcp_dashboard import (  # noqa: F401
    memory_dashboard,
)
from mcp_metrics import (  # noqa: F401
    memory_metrics_server,
)
from mcp_sdk import (  # noqa: F401
    memory_sdk_demo,
)

# ---- helpers ----
from mcp_agent import (  # noqa: F401, E402
    memory_agent_init,
    memory_agent_clear,
    memory_agent_list,
)
from mcp_common import _run_subprocess_output, recompile_skills_catalog  # noqa: F401

# ---- canonical tool inventory ----
__all__ = [
    # Search
    "search_memories",
    "memory_search",
    "memory_semantic_search",
    "memory_recall_context",
    "memory_session_start",
    # CRUD & lifecycle
    "memory_save",
    "memory_supersede",
    "memory_auto_save_hook",
    "memory_daily_digest",
    "memory_auto_save_status",
    "memory_reinforce",
    "memory_delete",
    "memory_restore",
    "memory_trash",
    "memory_purge_expired",
    "memory_purge_auto_saves",
    # Knowledge graph & facts
    "memory_graph_search",
    "memory_graph_stats",
    "memory_facts_search",
    "memory_facts_list",
    "memory_facts_stats",
    "memory_graph_shortest_path",
    "memory_graph_traverse",
    # Self-directed tiering
    "memory_heartbeat",
    "memory_tier_stats",
    "memory_run_tier_migration",
    "memory_check_embedding_model",
    "memory_incremental_update",
    "memory_duplicates",
    "memory_merge_suggestions",
    # Maintenance & system
    "memory_rebuild",
    "memory_compile_skill",
    "memory_audit",
    "memory_audit_query",
    "memory_consolidate",
    "memory_rewrite_links",
    "memory_detect_contradictions",
    "memory_compact",
    "memory_arc_stats",
    "memory_review_schedule",
    "memory_pinned_decay_check",
    "memory_check_integrity",
    "memory_backfill_all",
    "memory_maintenance",
    # Safety & security
    "memory_scan_injection",
    "memory_strip_provenance",
    "memory_check_contradictions",
    # Quality
    "memory_quality_filter",
    "memory_quality_stats",
    # Summarization
    "memory_summarize",
    "memory_auto_summarize",
    "memory_summarization_stats",
    # User profile
    "memory_profile_access",
    "memory_user_profile",
    "memory_profile_stats",
    # Adaptive retention
    "memory_adaptive_retention",
    "memory_retention_stats",
    # Multi-agent CRDT sync
    "memory_crdt_sync",
    # Multi-agent sharing
    "memory_share",
    "memory_shared_list",
    "memory_shared_import",
    "memory_shared_stats",
    "memory_auto_share",
    # CTR feedback & concept drift
    "memory_record_ctr_feedback",
    "memory_check_concept_drift",
    "memory_list_drift_alarms",  # 2026-06-22: per-memory alarm list + ack workflow (v15)
    # OKF export/import
    "memory_okf_export",
    "memory_okf_import",
    # LLM extraction
    "memory_llm_unload",
    # Multi-modal ingestion
    "memory_ingest_file",
    "memory_ingest_url",
    # Dashboard
    "memory_dashboard",
    # Metrics server
    "memory_metrics_server",
    # SDK demo
    "memory_sdk_demo",
    # Helpers
    "_run_subprocess_output",
    "recompile_skills_catalog",
]
