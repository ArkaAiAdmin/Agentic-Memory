from __future__ import annotations
"""Per-operation dispatcher handlers for the memory_maintenance router.

Extracted from mcp_maintenance.py to keep that file focused on the
high-level @mcp.tool() definitions and the router itself. Each entry
in ``MAINTENANCE_HANDLERS`` is a lambda that maps the router's flat
kwargs to the underlying memory_* implementation.

Import cycle note: mcp_maintenance.py imports ``MAINTENANCE_HANDLERS``
from this module, and this module imports the underlying tool functions
from mcp_maintenance. To break the cycle, the tool imports are deferred
to ``_get_local_tools()`` / ``_get_domain_tools()`` which run on first
dispatch (not at module load). The ``MaintenanceOp`` enum is also
resolved lazily.
"""
import logging
logger = logging.getLogger(__name__)
from mcp_common import _bootstrap_path  # noqa: E402,F401

import json
import os
from pathlib import Path
from typing import Any, Callable, cast

from config import get_feature_flags
from mcp_common import _err, ErrorCode, classify_exception




# Lazy-loaded tool registries. Resolved on first dispatch.
_local_tools: dict | None = None
_domain_tools: dict | None = None
_maintenance_op_cls = None


def _get_local_tools() -> dict:
    """Lazy import of mcp_maintenance's @mcp.tool() functions.

    Resolved on first call to break the import cycle (mcp_maintenance
    imports this module, and we import mcp_maintenance).
    """
    global _local_tools
    if _local_tools is None:
        from mcp_rebuild import (
            memory_rebuild,
            memory_compact,
            memory_backfill_all,
        )
        from mcp_audit import (
            memory_audit,
            memory_audit_query,
            memory_check_integrity,
            memory_circuit_breaker_status,
            memory_temporal_contradictions,
            memory_temporal_query,
            memory_compliance_check,
        )
        from mcp_crdt import (
            memory_crdt_sync,
            memory_crdt_status,
        )
        from mcp_maintenance import (
            memory_heartbeat,
            memory_tier_stats,
            memory_run_tier_migration,
            memory_check_embedding_model,
            memory_incremental_update,
            memory_dedup,
            memory_session_admin_stats,
            memory_consolidate,
            memory_rewrite_links,
            memory_detect_contradictions,
            memory_arc_stats,
            memory_arc_reset,
            memory_extract_skills,
            memory_list_skills,
            memory_review_schedule,
            memory_pinned_decay_check,
            memory_compile_skill,
            memory_llm_unload,
            memory_resolve_contradiction,
            memory_list_federated_skills,
        )

        _local_tools = {
            "memory_heartbeat": memory_heartbeat,
            "memory_tier_stats": memory_tier_stats,
            "memory_run_tier_migration": memory_run_tier_migration,
            "memory_check_embedding_model": memory_check_embedding_model,
            "memory_incremental_update": memory_incremental_update,
            "memory_dedup": memory_dedup,
            "memory_session_admin_stats": memory_session_admin_stats,
            "memory_rebuild": memory_rebuild,
            "memory_audit": memory_audit,
            "memory_audit_query": memory_audit_query,
            "memory_consolidate": memory_consolidate,
            "memory_rewrite_links": memory_rewrite_links,
            "memory_detect_contradictions": memory_detect_contradictions,
            "memory_compact": memory_compact,
            "memory_arc_stats": memory_arc_stats,
            "memory_arc_reset": memory_arc_reset,
            "memory_extract_skills": memory_extract_skills,
            "memory_list_skills": memory_list_skills,
            "memory_review_schedule": memory_review_schedule,
            "memory_pinned_decay_check": memory_pinned_decay_check,
            "memory_check_integrity": memory_check_integrity,
            "memory_circuit_breaker_status": memory_circuit_breaker_status,
            "memory_temporal_contradictions": memory_temporal_contradictions,
            "memory_temporal_query": memory_temporal_query,
            "memory_compile_skill": memory_compile_skill,
            "memory_llm_unload": memory_llm_unload,
            "memory_backfill_all": memory_backfill_all,
            "memory_crdt_sync": memory_crdt_sync,
            "memory_crdt_status": memory_crdt_status,
            "memory_compliance_check": memory_compliance_check,
            "memory_resolve_contradiction": memory_resolve_contradiction,
            "memory_list_federated_skills": memory_list_federated_skills,
        }
    return _local_tools


def _get_domain_tools() -> dict:
    """Lazy import of domain module @mcp.tool() functions."""
    global _domain_tools
    if _domain_tools is None:
        from mcp_agent import (
            memory_agent_init,
            memory_agent_clear,
            memory_agent_list,
        )
        from mcp_sdk import memory_sdk_demo
        from mcp_sharing import (
            memory_auto_share,
            memory_share,
            memory_shared_list,
            memory_shared_import,
            memory_shared_stats,
        )
        from mcp_okf import memory_okf_export, memory_okf_import
        from mcp_memory import (
            memory_reinforce,
            memory_trash,
            memory_purge_expired,
            memory_auto_save_hook,
            memory_auto_save_status,
            memory_auto_save_daemon_metrics,
            memory_daily_digest,
            memory_purge_auto_saves,
            memory_restore,
            memory_supersede,
        )
        from mcp_summarization import (
            memory_summarize,
            memory_auto_summarize,
            memory_summarization_stats,
        )
        from mcp_quality import memory_quality_filter, memory_quality_stats
        from mcp_retention import (
            memory_adaptive_retention,
            memory_retention_stats,
        )
        from mcp_ctr_drift import (
            memory_record_ctr_feedback,
            memory_check_concept_drift,
            memory_list_drift_alarms,
        )
        from mcp_kg import (
            memory_facts_list,
            memory_facts_stats,
            memory_graph_stats,
            memory_graph_insights,
            memory_graph_evolution,
            memory_facts_search,
            memory_graph_search,
        )
        from mcp_kg_traversal import (
            memory_graph_shortest_path,
            memory_graph_traverse,
        )
        from mcp_profile import memory_profile_stats, memory_user_profile, memory_profile_access
        from mcp_multi_modal import memory_ingest
        from mcp_dashboard import memory_dashboard
        from mcp_metrics import memory_metrics_server
        from mcp_search import memory_semantic_search, memory_recall_stats
        from mcp_session import memory_thread_context, memory_list_threads, memory_resolve_thread
        from mcp_safety import memory_check_contradictions, memory_scan_injection, memory_strip_provenance

        _domain_tools = {
            "memory_agent_init": memory_agent_init,
            "memory_agent_clear": memory_agent_clear,
            "memory_agent_list": memory_agent_list,
            "memory_graph_shortest_path": memory_graph_shortest_path,
            "memory_graph_traverse": memory_graph_traverse,
            "memory_graph_insights": memory_graph_insights,
            "memory_graph_evolution": memory_graph_evolution,
            "memory_sdk_demo": memory_sdk_demo,
            "memory_auto_share": memory_auto_share,
            "memory_share": memory_share,
            "memory_shared_list": memory_shared_list,
            "memory_shared_import": memory_shared_import,
            "memory_shared_stats": memory_shared_stats,
            "memory_okf_export": memory_okf_export,
            "memory_okf_import": memory_okf_import,
            "memory_reinforce": memory_reinforce,
            "memory_trash": memory_trash,
            "memory_purge_expired": memory_purge_expired,
            "memory_auto_save_hook": memory_auto_save_hook,
            "memory_auto_save_status": memory_auto_save_status,
            "memory_auto_save_daemon_metrics": memory_auto_save_daemon_metrics,
            "memory_purge_auto_saves": memory_purge_auto_saves,
            "memory_daily_digest": memory_daily_digest,
            "memory_restore": memory_restore,
            "memory_supersede": memory_supersede,
            "memory_summarize": memory_summarize,
            "memory_auto_summarize": memory_auto_summarize,
            "memory_summarization_stats": memory_summarization_stats,
            "memory_quality_filter": memory_quality_filter,
            "memory_quality_stats": memory_quality_stats,
            "memory_adaptive_retention": memory_adaptive_retention,
            "memory_retention_stats": memory_retention_stats,
            "memory_record_ctr_feedback": memory_record_ctr_feedback,
            "memory_check_concept_drift": memory_check_concept_drift,
            "memory_list_drift_alarms": memory_list_drift_alarms,
            "memory_facts_list": memory_facts_list,
            "memory_facts_stats": memory_facts_stats,
            "memory_graph_stats": memory_graph_stats,
            "memory_profile_stats": memory_profile_stats,
            "memory_ingest": memory_ingest,
            "memory_dashboard": memory_dashboard,
            "memory_metrics_server": memory_metrics_server,
            "memory_semantic_search": memory_semantic_search,
            "memory_facts_search": memory_facts_search,
            "memory_graph_search": memory_graph_search,
            "memory_recall_stats": memory_recall_stats,
            "memory_thread_context": memory_thread_context,
            "memory_list_threads": memory_list_threads,
            "memory_resolve_thread": memory_resolve_thread,
            "memory_user_profile": memory_user_profile,
            "memory_check_contradictions": memory_check_contradictions,
            "memory_scan_injection": memory_scan_injection,
            "memory_strip_provenance": memory_strip_provenance,
            "memory_profile_access": memory_profile_access,
            # Phase 2 SSO verbs (admin-only).
            "memory_login_url": memory_login_url,
            "memory_callback": memory_callback,
            "memory_whoami": memory_whoami,
            "memory_rotate_key": memory_rotate_key,
            "memory_sso_sync_metadata": memory_sso_sync_metadata,
            "memory_sso_idp_list": memory_sso_idp_list,
            "memory_sso_idp_add": memory_sso_idp_add,
        }
    return _domain_tools


def _tools() -> dict:
    """Combined local + domain tools."""
    return {**_get_local_tools(), **_get_domain_tools()}


# The MAINTENANCE_HANDLERS dict is built lazily on first access to
# avoid the import cycle. The dict keys are MaintenanceOp enum values
# (also resolved lazily).
_MAINTENANCE_HANDLERS: dict | None = None


def _get_handlers() -> dict:
    """Build the dispatch table on first call.

    Each entry is a lambda that unpacks the router's flat kwargs and
    calls the underlying memory_* function. Adding a 47th admin op
    is a one-line change here + one line in mcp_maintenance.py for
    the @mcp.tool() + one line in the MaintenanceOp enum.
    """
    global _MAINTENANCE_HANDLERS, _maintenance_op_cls
    if _MAINTENANCE_HANDLERS is None:
        from mcp_maintenance import MaintenanceOp

        _maintenance_op_cls = MaintenanceOp
        t = _tools()
        from session_manager import reconcile_audit as _reconcile_audit
        _MAINTENANCE_HANDLERS = {
            MaintenanceOp.HEARTBEAT: lambda *, dry_run=False, **_: t[
                "memory_heartbeat"
            ](dry_run=dry_run),
            MaintenanceOp.TIER_STATS: lambda **_: t["memory_tier_stats"](),
            MaintenanceOp.TIER_MIGRATION: lambda *, dry_run=False, **_: t[
                "memory_run_tier_migration"
            ](dry_run=dry_run),
            MaintenanceOp.EMBEDDING_MODEL_CHECK: lambda *, force=False, dry_run=False, **_: (
                t["memory_check_embedding_model"](force=force, dry_run=dry_run)
            ),
            MaintenanceOp.INCREMENTAL_UPDATE: lambda *, memory_id, new_content, old_state=None, **_: (
                t["memory_incremental_update"](
                    memory_id=memory_id, new_content=new_content, old_state=old_state
                )
            ),
            MaintenanceOp.DEDUP: lambda *, action="duplicates", threshold=None, **_: t[
                "memory_dedup"
            ](action=action, threshold=threshold),
            MaintenanceOp.REBUILD: lambda *, scope="active", **_: t["memory_rebuild"](
                scope=scope
            ),
            MaintenanceOp.AUDIT: lambda **_: t["memory_audit"](),
            MaintenanceOp.AUDIT_QUERY: lambda *, tool_name=None, since_ts=None, until_ts=None, only_errors=False, limit=50, offset=0, **_: (
                t["memory_audit_query"](
                    tool_name=tool_name,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    only_errors=only_errors,
                    limit=limit,
                    offset=offset,
                )
            ),
            MaintenanceOp.CONSOLIDATE: lambda **_: t["memory_consolidate"](),
            MaintenanceOp.REWRITE_LINKS: lambda **_: t["memory_rewrite_links"](),
            MaintenanceOp.DETECT_CONTRADICTIONS: lambda *, min_confidence="low", mode="both", semantic_threshold=0.65, **_: (
                t["memory_detect_contradictions"](
                    min_confidence=min_confidence,
                    mode=mode,
                    semantic_threshold=semantic_threshold,
                )
            ),
            MaintenanceOp.COMPACT: lambda *, dry_run=False, **_: t["memory_compact"](
                dry_run=dry_run
            ),
            MaintenanceOp.ARC_STATS: lambda **_: t["memory_arc_stats"](),
            MaintenanceOp.ARC_RESET: lambda **_: t["memory_arc_reset"](),
            MaintenanceOp.REVIEW_SCHEDULE: lambda **_: t["memory_review_schedule"](),
            MaintenanceOp.PINNED_DECAY: lambda *, dry_run=True, **_: t[
                "memory_pinned_decay_check"
            ](dry_run=dry_run),
            MaintenanceOp.CHECK_INTEGRITY: lambda *, deep=False, **_: t[
                "memory_check_integrity"
            ](deep=deep),
            MaintenanceOp.COMPILE_SKILL: lambda *, lesson_slug, skill_name, primary_triggers, secondary_triggers=None, **_: (
                t["memory_compile_skill"](
                    lesson_slug=lesson_slug,
                    skill_name=skill_name,
                    primary_triggers=primary_triggers or [],
                    secondary_triggers=secondary_triggers,
                )
            ),
            MaintenanceOp.BACKFILL_ALL: lambda *, backfill_mode="health", source="", **_: (
                t["memory_backfill_all"](mode=backfill_mode, source=source)
            ),
            MaintenanceOp.CRDT_SYNC: lambda *, agent_id, remote_notes_json, **_: t[
                "memory_crdt_sync"
            ](agent_id=agent_id, remote_notes_json=remote_notes_json),
            MaintenanceOp.CRDT_STATUS: lambda **_: t["memory_crdt_status"](),
            MaintenanceOp.OKF_EXPORT: lambda *, output_dir, include_deleted=False, overwrite=False, **_: (
                t["memory_okf_export"](
                    output_dir=output_dir,
                    include_deleted=include_deleted,
                    overwrite=overwrite,
                )
            ),
            MaintenanceOp.OKF_IMPORT: lambda *, input_dir, is_global=False, dry_run=False, overwrite=False, confirm=False, **_: (
                t["memory_okf_import"](
                    input_dir=input_dir,
                    is_global=is_global,
                    dry_run=dry_run,
                    overwrite=overwrite,
                    confirm=confirm,
                )
            ),
            MaintenanceOp.REINFORCE: lambda *, memory_ids, success, **_: t[
                "memory_reinforce"
            ](memory_ids=memory_ids, success=success),
            MaintenanceOp.TRASH: lambda *, include_expired=False, **_: t[
                "memory_trash"
            ](include_expired=include_expired),
            MaintenanceOp.PURGE_EXPIRED: lambda **_: t["memory_purge_expired"](),
            MaintenanceOp.AUTO_SAVE_HOOK: lambda *, auto_save_tool, auto_save_params_json, auto_save_result_preview, **_: (
                t["memory_auto_save_hook"](
                    tool=auto_save_tool,
                    params_json=auto_save_params_json,
                    result_preview=auto_save_result_preview,
                )
            ),
            MaintenanceOp.AUTO_SAVE_STATUS: lambda **_: t["memory_auto_save_status"](),
            MaintenanceOp.AUTO_SAVE_DAEMON_METRICS: lambda **_: t[
                "memory_auto_save_daemon_metrics"
            ](),
            MaintenanceOp.CIRCUIT_BREAKER_STATUS: lambda *, limit=20, since_ts=None, **_: (
                t["memory_circuit_breaker_status"](limit=limit, since_ts=since_ts)
            ),
            MaintenanceOp.TEMPORAL_CONTRADICTIONS: lambda *, since_ts=None, until_ts=None, reason=None, limit=50, offset=0, **_: (
                t["memory_temporal_contradictions"](
                    since_ts=since_ts,
                    until_ts=until_ts,
                    reason=reason,
                    limit=limit,
                    offset=offset,
                )
            ),
            MaintenanceOp.TEMPORAL_QUERY: lambda *, as_of=None, fact_id=None, since_ts=None, query="", limit=100, **_: (
                t["memory_temporal_query"](
                    as_of=as_of,
                    fact_id=fact_id,
                    since_ts=since_ts,
                    query=query or None,
                    limit=limit,
                )
            ),
            MaintenanceOp.COMPLIANCE_CHECK: lambda *, session_id="", **_: t[
                "memory_compliance_check"
            ](session_id=session_id),
            MaintenanceOp.GDPR_ERASE: lambda *, principal_id="", data_subject_sub="", tenant_id="default", confirm=False, **_: _op_gdpr_erase(
                principal_id=principal_id,
                data_subject_sub=data_subject_sub,
                tenant_id=tenant_id,
                confirm=confirm,
            ),
            MaintenanceOp.PURGE_AUTO_SAVES: lambda *, dry_run=False, **_: t[
                "memory_purge_auto_saves"
            ](dry_run=dry_run),
            MaintenanceOp.DAILY_DIGEST: lambda *, date="", **_: t[
                "memory_daily_digest"
            ](date=date),
            MaintenanceOp.SHARE: lambda *, share_note_id, share_agent_id, **_: t[
                "memory_share"
            ](note_id=share_note_id, share_agent_id=share_agent_id),
            MaintenanceOp.SHARED_LIST: lambda *, share_agent_id="", shared_category="", shared_limit=50, **_: (
                t["memory_shared_list"](
                    agent_id=share_agent_id,
                    category=shared_category,
                    limit=shared_limit,
                )
            ),
            MaintenanceOp.SHARED_IMPORT: lambda *, shared_id, target_agent_id, **_: t[
                "memory_shared_import"
            ](shared_id=shared_id, target_agent_id=target_agent_id),
            MaintenanceOp.SHARED_STATS: lambda **_: t["memory_shared_stats"](),
            MaintenanceOp.RECORD_CTR_FEEDBACK: lambda *, ctr_id, query_id, ctr_action="returned", ctr_source=None, **_: (
                t["memory_record_ctr_feedback"](
                    id=ctr_id,
                    query_id=query_id,
                    action=ctr_action,
                    source=ctr_source,
                )
            ),
            MaintenanceOp.CHECK_CONCEPT_DRIFT: lambda *, threshold=0.15, **_: t[
                "memory_check_concept_drift"
            ](threshold=threshold),
            MaintenanceOp.LIST_DRIFT_ALARMS: lambda *, acknowledged=None, alarm_level=None, limit=50, acknowledge_ids=None, acknowledged_by="operator", notes="", **_: (
                t["memory_list_drift_alarms"](
                    acknowledged=acknowledged,
                    alarm_level=alarm_level,
                    limit=limit,
                    acknowledge_ids=acknowledge_ids,
                    acknowledged_by=acknowledged_by,
                    notes=notes,
                )
            ),
            MaintenanceOp.QUALITY_FILTER: lambda *, query, quality_limit=50, **_: t[
                "memory_quality_filter"
            ](query=query, quality_limit=quality_limit),
            MaintenanceOp.QUALITY_STATS: lambda **_: t["memory_quality_stats"](),
            MaintenanceOp.SUMMARIZE: lambda *, note_id, **_: t["memory_summarize"](
                note_id=note_id
            ),
            MaintenanceOp.AUTO_SUMMARIZE: lambda *, min_length=500, dry_run=False, **_: (
                t["memory_auto_summarize"](min_length=min_length, dry_run=dry_run)
            ),
            MaintenanceOp.SUMMARIZATION_STATS: lambda **_: t[
                "memory_summarization_stats"
            ](),
            MaintenanceOp.FACTS_LIST: lambda *, facts_limit=20, facts_min_confidence=0.0, **_: (
                t["memory_facts_list"](
                    limit=facts_limit,
                    min_confidence=facts_min_confidence,
                )
            ),
            MaintenanceOp.FACTS_STATS: lambda **_: t["memory_facts_stats"](),
            MaintenanceOp.GRAPH_STATS: lambda **_: t["memory_graph_stats"](),
            MaintenanceOp.GRAPH_INSIGHTS: lambda *, sample_size=20, include_bridge=True, **_: t[
                "memory_graph_insights"
            ](sample_size=sample_size, include_bridge=include_bridge),
            MaintenanceOp.GRAPH_EVOLUTION: lambda *, since="24h", limit=5, **_: t[
                "memory_graph_evolution"
            ](since=since, limit=limit),
            MaintenanceOp.PROFILE_STATS: lambda **_: t["memory_profile_stats"](),
            MaintenanceOp.LLM_UNLOAD: lambda **_: t["memory_llm_unload"](),
            MaintenanceOp.ADAPTIVE_RETENTION: lambda *, dry_run=False, **_: t[
                "memory_adaptive_retention"
            ](dry_run=dry_run),
            MaintenanceOp.RETENTION_STATS: lambda **_: t["memory_retention_stats"](),
            MaintenanceOp.INGEST: lambda *, file_path=None, url=None, category="sessions", tags="", **_: (
                t["memory_ingest"](
                    file_path=file_path,
                    url=url,
                    category=category,
                    tags=tags,
                )
            ),
            MaintenanceOp.DASHBOARD: lambda *, action="status", port=8501, **_: t[
                "memory_dashboard"
            ](action=action, port=port if port != 9464 else 8501),
            MaintenanceOp.METRICS_SERVER: lambda *, action="status", port=9464, **_: t[
                "memory_metrics_server"
            ](action=action, port=port),
            MaintenanceOp.MEMORY_STATS: lambda **_: _op_memory_stats(),
            MaintenanceOp.SESSION_ADMIN_STATS: lambda *, type="all", **_: _op_session_admin_stats(
                type=type
            ),
            MaintenanceOp.LIST_ACTIVE_THREADS: lambda *, project_root="", status="", limit=20, **_: (
                _op_list_active_threads(
                    project_root=project_root, status=status, limit=limit
                )
            ),
            MaintenanceOp.RECOVER_SESSION: lambda *, session_id, **_: (
                _op_recover_session(session_id=session_id)
            ),
            MaintenanceOp.AGENT_INIT: lambda *, agent_id, display_name="", parent_agent="", namespace="", **_: t["memory_agent_init"](
                agent_id=agent_id, display_name=display_name, parent_agent=parent_agent, namespace=namespace,
            ),
            MaintenanceOp.AGENT_CLEAR: lambda **_: t["memory_agent_clear"](),
            MaintenanceOp.AGENT_LIST: lambda **_: t["memory_agent_list"](),
            MaintenanceOp.EXTRACT_SKILLS: lambda *, memory_id="", dry_run=False, **_: (
                _op_extract_skills(memory_id=memory_id, dry_run=dry_run)
            ),
            MaintenanceOp.LIST_SKILLS: lambda *, limit=50, **_: (
                _op_list_skills(limit=limit)
            ),
            MaintenanceOp.AUTO_SHARE: lambda *, agent_id="", min_importance=0, min_fitness=0.0, limit=0, dry_run=False, **_: t["memory_auto_share"](
                agent_id=agent_id, min_importance=min_importance, min_fitness=min_fitness, limit=limit, dry_run=dry_run,
            ),
            MaintenanceOp.GRAPH_SHORTEST_PATH: lambda *, source, target, max_depth=5, **_: t["memory_graph_shortest_path"](
                source=source, target=target, max_depth=max_depth,
            ),
            MaintenanceOp.GRAPH_TRAVERSE: lambda *, start, edge_patterns, **_: t["memory_graph_traverse"](
                start=start, edge_patterns=edge_patterns,
            ),
            MaintenanceOp.RECONCILE_AUDIT: lambda *, db_path=None, **_: (
                _reconcile_audit(db_path=Path(db_path) if db_path else None)
            ),
            MaintenanceOp.TRAIN_FORGET_MODEL: lambda **_: (
                __import__("cron.cron_train_forget_model", fromlist=["main"]).main()
            ),
            MaintenanceOp.SEMANTIC_SEARCH: lambda *, query, **_: t["memory_semantic_search"](query=query),
            MaintenanceOp.FACTS_SEARCH: lambda *, query, **_: t["memory_facts_search"](query=query),
            MaintenanceOp.GRAPH_SEARCH: lambda *, query, **_: t["memory_graph_search"](query=query),
            MaintenanceOp.RECALL_STATS: lambda *, action="status", query="", limit=50, event="", since_ts="", **_: (
                t["memory_recall_stats"](
                    action=action,
                    query=query,
                    limit=limit,
                    event=event,
                    since_ts=since_ts,
                ) if action == "context" else (
                    _op_recall_status() if action == "status" else _op_recall_trace(
                        limit=limit, event=event, since_ts=since_ts
                    )
                )
            ),
            MaintenanceOp.BACKGROUND_TASK_STATUS: lambda *, memory_id, **_: _op_background_task_status(
                memory_id=memory_id
            ),
            MaintenanceOp.THREAD_CONTEXT: lambda *, session_id, **_: t["memory_thread_context"](session_id=session_id),
            MaintenanceOp.LIST_THREADS: lambda *, session_id, **_: t["memory_list_threads"](session_id=session_id),
            MaintenanceOp.RESOLVE_THREAD: lambda *, session_id, thread_id, resolution, **_: t["memory_resolve_thread"](session_id=session_id, thread_id=thread_id, resolution=resolution),
            MaintenanceOp.USER_PROFILE: lambda **_: t["memory_user_profile"](),
            MaintenanceOp.CHECK_CONTRADICTIONS: lambda *, content, **_: t["memory_check_contradictions"](content=content),
            MaintenanceOp.SCAN_INJECTION: lambda *, content, **_: t["memory_scan_injection"](content=content),
            MaintenanceOp.PROFILE_ACCESS: lambda *, note_id, **_: t["memory_profile_access"](note_id=note_id),
            MaintenanceOp.FLAGS_STATUS: lambda **_: _op_flags_status(),
            MaintenanceOp.CONFIG_DRIFT: lambda *, persist=True, severity_floor="neutral",
                                           compare_to_last=True, format="json", **_: (
                _op_config_drift(persist=persist, severity_floor=severity_floor,
                                  compare_to_last=compare_to_last, format=format)
            ),
            MaintenanceOp.POLICY_HASH_STATUS: lambda *, peer_timeout_s=5.0, max_concurrent=4,
                                              cache_ttl_s=60.0, force_refresh=False,
                                              include_full_policy=False, since_ts=None, **_: (
                __import__("mcp_maintenance_policy_hash", fromlist=["policy_hash_status"])
                .policy_hash_status(
                    peer_timeout_s=peer_timeout_s,
                    max_concurrent=max_concurrent,
                    cache_ttl_s=cache_ttl_s,
                    force_refresh=force_refresh,
                    include_full_policy=include_full_policy,
                    since_ts=since_ts,
                )
            ),
            MaintenanceOp.PHASE_ERRORS: lambda *, since_ts=None, until_ts=None, limit=50, **_: _op_phase_errors(
                since_ts=since_ts, until_ts=until_ts, limit=limit
            ),
            MaintenanceOp.SEARCH_PHASE_STATS: lambda *, since_ts=None, until_ts=None, phase_name="", limit=200, **__: _op_search_phase_stats(
                since_ts=since_ts, until_ts=until_ts, phase_name=phase_name, limit=limit
            ),
            MaintenanceOp.RESOLVE_CONTRADICTION: lambda *, source_note_id="", target_note_id="", strategy="auto", **_: t[
                "memory_resolve_contradiction"
            ](source_note_id=source_note_id, target_note_id=target_note_id, strategy=strategy),
            MaintenanceOp.LIST_FEDERATED_SKILLS: lambda *, limit=50, agent_filter="", **_: t[
                "memory_list_federated_skills"
            ](limit=limit, agent_filter=agent_filter),
            MaintenanceOp.RESTORE: lambda *, note_id, **_: t[
                "memory_restore"
            ](note_id=note_id),
            MaintenanceOp.SUPERSEDE: lambda *, old_id, new_id, valid_to=None, **_: (
                t["memory_supersede"](
                    old_id=old_id,
                    new_id=new_id,
                    valid_to=valid_to,
                )
            ),
            MaintenanceOp.SDK_DEMO: lambda *, query="preferences", samples=3, **_: (
                t["memory_sdk_demo"](
                    query=query,
                    samples=samples,
                )
            ),
            MaintenanceOp.STRIP_PROVENANCE: lambda *, content, **_: t[
                "memory_strip_provenance"
            ](content=content),
        }
    return _MAINTENANCE_HANDLERS


# Module-level proxy: MAINTENANCE_HANDLERS["foo"] resolves through
# the lazy getter. mcp_maintenance.py does:
#   from mcp_maintenance_ops import MAINTENANCE_HANDLERS
#   handler = MAINTENANCE_HANDLERS[op_enum]
# which works with this proxy.

class _MaintenanceHandlersProxy:
    def __getitem__(self, key: Any) -> Callable[..., str]:
        return cast(Callable[..., str], _get_handlers()[key])

    def __iter__(self):
        return iter(_get_handlers())

    def __len__(self):
        return len(_get_handlers())

    def __contains__(self, key: object) -> bool:
        return key in _get_handlers()

    def keys(self):
        return _get_handlers().keys()

    def values(self):
        return _get_handlers().values()

    def items(self):
        return _get_handlers().items()


# ---------------------------------------------------------------------------
# Sprint 7: Session/thread admin helpers
import json as _json

# ---------------------------------------------------------------------------


def _op_memory_stats() -> str:
    try:
        db_path = os.environ.get("MEMORY_DB_PATH", "")
        db_size = 0
        note_count = 0
        if db_path:
            p = Path(db_path)
            if p.exists():
                db_size = p.stat().st_size
            try:
                from session_manager import SessionManager
                from infra.db import safe_close_db

                mgr = SessionManager(db_path=Path(db_path))
                conn = mgr._conn()
                try:
                    note_count = conn.execute(
                        "SELECT COUNT(*) FROM memories"
                    ).fetchone()[0]
                finally:
                    safe_close_db(conn)
            except Exception as e:
                logger.warning("Unhandled exception in _op_memory_stats: %s", e)
        queue_depth = 0
        try:
            from background.background_queue import init_task_queue, pending_count

            if db_path:
                from infra.db import open_db
                with open_db(Path(db_path)) as qconn:
                    init_task_queue(qconn)
                    queue_depth = pending_count(qconn)
        except Exception as e:
            logger.warning("Unhandled exception in _op_memory_stats: %s", e)
        cb_open = False
        for _mod in ("background.circuit_breaker", "circuit_breaker"):
            try:
                _mod_state = __import__(_mod, fromlist=["_auto_save_get_state"])
                _get_state_fn = _mod_state._auto_save_get_state
                cb_open = _get_state_fn().get("circuit_open", False)
                break
            except (ImportError, AttributeError):
                pass
        flags = {}
        try:
            flags = get_feature_flags()
        except Exception as e:
            logger.warning("Unhandled exception in _op_memory_stats: %s", e)
        return _json.dumps(
            {
                "db_path": "<configured>",
                "db_size_bytes": db_size,
                "note_count": note_count,
                "background_queue_depth": queue_depth,
                "circuit_breaker_open": cb_open,
                "feature_flags": flags,
            }
        )
    except Exception as e:
        logger.warning("Unhandled exception in _op_memory_stats: %s", e)
        return _err(classify_exception(e), str(e))


def _op_list_active_threads(
    project_root: str = "", status: str = "", limit: int = 20
) -> str:
    try:
        import json as _json
        from pathlib import Path
        from session_manager import SessionManager
        from infra.db import safe_close_db

        db_path = os.environ.get("MEMORY_DB_PATH")
        mgr = SessionManager(db_path=Path(db_path)) if db_path else SessionManager()
        conn = mgr._conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT id, session_id, title, status, created_at, metadata "
                    "FROM decision_threads WHERE status=? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, session_id, title, status, created_at, metadata "
                    "FROM decision_threads ORDER BY created_at DESC"
                ).fetchall()
            threads = []
            for r in rows:
                metadata = _json.loads(r[5]) if r[5] else {}
                if project_root:
                    if project_root not in metadata.get("project_root", ""):
                        continue
                threads.append(
                    {
                        "id": r[0],
                        "title": r[2],
                        "status": r[3],
                        "session_id": r[1],
                        "created_at": r[4],
                    }
                )
            threads = threads[:limit]
            return _json.dumps({"threads": threads})
        finally:
            safe_close_db(conn)
    except Exception as e:
        logger.warning("Unhandled exception in _op_list_active_threads: %s", e)
        return _err(classify_exception(e), str(e))


def _op_extract_skills(memory_id: str = "", dry_run: bool = False) -> str:
    """Wrapper for memory_extract_skills (needs conn injection)."""
    try:
        from infra.db import open_db
        from pathlib import Path
        target_base = Path(os.environ.get("MEMORY_DB_PATH", ""))
        if not target_base.exists():
            from infra.memory_common import get_memory_paths
            _, local_mem, _ = get_memory_paths()
            target_base = local_mem
        db_path = target_base / "memory.db" if target_base.suffix != ".db" else target_base
        with open_db(db_path, write=False) as conn:
            from mcp_maintenance import memory_extract_skills
            return cast(str, memory_extract_skills(conn, memory_id=memory_id, dry_run=dry_run))
    except Exception as e:
        logger.warning("Unhandled exception in _op_extract_skills: %s", e)
        return _err(classify_exception(e), str(e))


def _op_list_skills(limit: int = 50) -> str:
    """Wrapper for memory_list_skills (needs conn injection)."""
    try:
        from infra.db import open_db
        from pathlib import Path
        target_base = Path(os.environ.get("MEMORY_DB_PATH", ""))
        if not target_base.exists():
            from infra.memory_common import get_memory_paths
            _, local_mem, _ = get_memory_paths()
            target_base = local_mem
        db_path = target_base / "memory.db" if target_base.suffix != ".db" else target_base
        with open_db(db_path, write=False) as conn:
            from mcp_maintenance import memory_list_skills
            return cast(str, memory_list_skills(conn, limit=limit))
    except Exception as e:
        logger.warning("Unhandled exception in _op_list_skills: %s", e)
        return _err(classify_exception(e), str(e))


def _op_recover_session(session_id: str) -> str:
    try:
        from pathlib import Path
        from session_manager import SessionManager

        db_path = os.environ.get("MEMORY_DB_PATH")
        mgr = SessionManager(db_path=Path(db_path)) if db_path else SessionManager()
        chain: list[dict] = []
        current = session_id
        visited = set()
        while current and current not in visited:
            visited.add(current)
            conn = mgr._conn()
            try:
                row = conn.execute(
                    "SELECT id, parent_session_id, summary_note_id, status, started_at "
                    "FROM sessions WHERE id=?",
                    (current,),
                ).fetchone()
            finally:
                from infra.db import safe_close_db

                safe_close_db(conn)
            if not row:
                break
            chain.append(
                {
                    "session_id": row[0],
                    "parent_session_id": row[1],
                    "summary_note_id": row[2],
                    "status": row[3],
                    "started_at": row[4],
                }
            )
            current = row[1] or ""
        return _json.dumps({"chain": chain})
    except Exception as e:
        logger.warning("Unhandled exception in _op_recover_session: %s", e)
        return _err(classify_exception(e), str(e))


def _op_flags_status() -> str:
    """Return all feature flags with their resolved values and sources."""
    try:
        from config import get_feature_flags

        return json.dumps(get_feature_flags(), indent=2)
    except Exception as e:
        logger.warning("Unhandled exception in _op_flags_status: %s", e)
        return _err(classify_exception(e), str(e))


def _op_config_drift(
    persist: bool = True,
    severity_floor: str = "neutral",
    compare_to_last: bool = True,
    format: str = "json",
) -> str:
    """Compute the drift report for the running process.

    Args:
        persist:            Write to memory/last_drift_snapshot.json. Default True.
        severity_floor:     Only return entries at or above this severity. Default "neutral".
        compare_to_last:    Include delta against the previous snapshot. Default True.
        format:             "json" only.

    Returns a JSON string with ``report`` and optionally ``delta``.
    """
    try:
        from infra.config_drift import (
            build_drift_report,
            persist_drift_report,
            load_last_drift_snapshot,
            diff_reports,
            DriftReport,
            DriftSeverity,
        )

        import json
        import logging
        logger = logging.getLogger(__name__)

        report = build_drift_report()
        if persist:
            persist_drift_report(report)
        prev = load_last_drift_snapshot() if compare_to_last else None
        delta_text = diff_reports(prev, report) if prev else []

        floor_order = {
            "neutral": 0, "operational": 1, "compliance": 2,
            "stability": 3, "integrity": 4,
        }
        floor_idx = floor_order.get(severity_floor, 0)
        severity_values = {
            "neutral": DriftSeverity.NEUTRAL,
            "operational": DriftSeverity.OPERATIONAL,
            "compliance": DriftSeverity.COMPLIANCE,
            "stability": DriftSeverity.STABILITY,
            "integrity": DriftSeverity.INTEGRITY,
        }
        filtered = [
            e for e in report.entries
            if floor_order.get(e.severity, 0) >= floor_idx
        ]

        # Build filtered drift_count_by_severity
        filtered_drift_count: dict[str, int] = {}
        for sev in severity_values.values():
            filtered_drift_count[sev.value] = sum(
                1 for e in filtered if e.severity == sev.value and e.has_drift()
            )

        filtered_report = DriftReport(
            schema_version=report.schema_version,
            generated_at=report.generated_at,
            host=report.host,
            agent_id=report.agent_id,
            entries=filtered,
            total_flags=report.total_flags,
            drift_count_by_severity=filtered_drift_count,
        )

        out = {"report": filtered_report.to_dict(), "delta": delta_text}
        return json.dumps(out, indent=2, default=_json_default)
    except Exception as e:
        logger.warning("Unhandled exception in _op_config_drift: %s", e)
        from mcp_common import _err, classify_exception
        return _err(classify_exception(e), str(e))


def _json_default(o):
    import dataclasses as _dc
    from enum import Enum
    if _dc.is_dataclass(o) and not isinstance(o, type):
        return _dc.asdict(o)
    if isinstance(o, Enum):
        return o.value
    return str(o)


def _op_policy_hash_status(
    *,
    peer_timeout_s: float = 5.0,
    max_concurrent: int = 4,
    cache_ttl_s: float = 60.0,
    force_refresh: bool = False,
    include_full_policy: bool = False,
    since_ts: float | None = None,
) -> str:
    try:
        import os
        import time
        import socket
        from infra.config_drift_policy import resolve_policy
        from infra.policy_hash_fetcher import fetch_all_peer_hashes
        from infra.policy_hash_cache import (
            load_peer_cache, persist_peer_cache, filter_stale_entries,
        )
        from infra.policy_hash_diff import dict_diff

        local_policy = resolve_policy()
        local_hash = local_policy.policy_hash()

        try:
            from sync import SyncManager
            sync_status = SyncManager().status()
            peers = sync_status.get("peers", [])
        except Exception:
            peers = []

        now = time.time()
        cache = ({} if force_refresh else load_peer_cache())

        if force_refresh:
            peers_to_query = peers
            cached_for_peers = {}
        else:
            fresh, stale = filter_stale_entries(cache, cache_ttl_s)
            peers_to_query = [
                p for p in peers
                if p.get("name") in stale or p.get("agent_id") in stale
            ]
            cached_for_peers = fresh

        sync_token = os.environ.get("MEMORY_SYNC_TOKEN", "")
        fresh_results = (
            fetch_all_peer_hashes(
                peers_to_query,
                timeout_s=peer_timeout_s,
                max_concurrent=max_concurrent,
                sync_token=sync_token,
            )
            if peers_to_query
            else {}
        )

        merged = {}
        for p in peers:
            name = p.get("name", p.get("agent_id", "?"))
            if name in fresh_results:
                status, latency, body = fresh_results[name]
                entry = {
                    "status": status,
                    "fetched_at": now,
                    "fetched_via": "live",
                    "latency_s": round(latency, 4),
                    "peer_url": p.get("url", ""),
                    "agent_id": p.get("agent_id", ""),
                    **(
                        {k: v for k, v in body.items() if k != "full_policy"}
                        if not include_full_policy
                        else body
                    ),
                }
            elif name in cached_for_peers:
                entry = dict(cached_for_peers[name], fetched_via="cache")
            else:
                entry = {
                    "status": "pending",
                    "peer_url": p.get("url", ""),
                    "agent_id": p.get("agent_id", ""),
                }
            merged[name] = entry

        persist_peer_cache(merged)

        aligned, divergent, unreachable, pending = [], [], [], []
        for name, entry in merged.items():
            peer_hash = entry.get("policy_hash", "")
            if entry.get("status") == "unreachable":
                unreachable.append(name)
            elif entry.get("status") == "pending":
                pending.append(name)
            elif peer_hash and peer_hash == local_hash:
                aligned.append(name)
            elif peer_hash:
                local_dict = local_policy.to_dict()
                peer_dict = {
                    k: entry.get(k)
                    for k in entry
                    if k not in ("fetched_at", "fetched_via", "latency_s", "status")
                }
                delta_keys = dict_diff(local_dict, peer_dict) if peer_dict else ["policy_hash"]
                divergent.append({
                    "name": name,
                    "peer_policy_hash": peer_hash,
                    "local_policy_hash": local_hash,
                    "delta_keys": delta_keys[:20],
                })

        out = {
            "schema_version": 1,
            "generated_at": now,
            "local": {
                "host": socket.gethostname(),
                "agent_id": getattr(local_policy, "agent_id", "") or "",
                "policy_hash": local_hash,
                "scope": local_policy.scope,
            },
            "peers": list(merged.values()),
            "summary": {
                "total_peers": len(merged),
                "aligned": len(aligned),
                "divergent": len(divergent),
                "unreachable": len(unreachable),
                "pending": len(pending),
            },
            "divergent_peers": divergent,
            "cache_ttl_s": cache_ttl_s,
        }
        return json.dumps(out, indent=2, default=str)
    except Exception as e:
        logger.warning("Unhandled exception in _op_policy_hash_status: %s", e)
        from mcp_common import _err, classify_exception
        return _err(classify_exception(e), str(e))


def _op_phase_errors(since_ts: float | None = None, until_ts: float | None = None, limit: int = 50) -> str:
    """Return per-phase error counts from the error counter."""
    try:
        from infra.error_counter import get_counts

        return json.dumps(
            get_counts(since_ts=since_ts, until_ts=until_ts, limit=limit), indent=2
        )
    except Exception as e:
        logger.warning("Unhandled exception in _op_phase_errors: %s", e)
        return _err(classify_exception(e), str(e))


def _op_search_phase_stats(
    since_ts: float | None = None,
    until_ts: float | None = None,
    phase_name: str = "",
    limit: int = 200,
) -> str:
    """Return per-phase latency stats from the search_phase_stats table.

    Aggregates latency_ms by phase_name, returning min/avg/max/p95 and
    sample count. Optional time-window filter via since_ts/until_ts.
    """
    try:
        from infra.infrastructure import resolve_active_memory_dir
        import sqlite3

        db_path = resolve_active_memory_dir() / "memory.db"
        if not db_path.exists():
            return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            where: list[str] = []
            params: list[object] = []
            if since_ts is not None:
                where.append("created_at >= ?")
                params.append(since_ts)
            if until_ts is not None:
                where.append("created_at <= ?")
                params.append(until_ts)
            if phase_name:
                where.append("phase_name = ?")
                params.append(phase_name)
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            rows = conn.execute(
                f"SELECT phase_name, COUNT(*), MIN(latency_ms), AVG(latency_ms), "
                f"MAX(latency_ms) FROM search_phase_stats {where_sql} "
                f"GROUP BY phase_name ORDER BY phase_name",
                params,
            ).fetchall()
            p95_rows = conn.execute(
                f"SELECT phase_name, latency_ms FROM search_phase_stats {where_sql}",
                params,
            ).fetchall()
            p95_map: dict[str, list[float]] = {}
            for pname, lat in p95_rows:
                p95_map.setdefault(pname, []).append(lat)
            results = []
            for pname, count, mn, avg, mx in rows:
                vals = sorted(p95_map.get(pname, []))
                p95 = vals[int(len(vals) * 0.95)] if vals else avg
                results.append({
                    "phase": pname,
                    "count": count,
                    "min_ms": round(mn, 2),
                    "avg_ms": round(avg, 2),
                    "p95_ms": round(p95, 2),
                    "max_ms": round(mx, 2),
                })
            return json.dumps(
                {"phases": results, "count": len(results)}, indent=2
            )
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Unhandled exception in _op_search_phase_stats: %s", e)
        return _err(classify_exception(e), str(e))


def _op_recall_trace(
    limit: int = 50,
    event: str = "",
    since_ts: str = "",
) -> str:
    """Return recent recall trace JSONL entries.

    Reads ``memory/recall_trace.jsonl`` from the active memory directory
    and returns the last `limit` matching events.

    Args:
        limit: Max entries to return (default 50, max 500).
        event: Filter by event name (e.g. ``recall_complete``, ``recall_truncated``).
               Empty = no filter.
        since_ts: ISO datetime filter — only entries at or after this timestamp.
                  Empty = no filter.

    Returns:
        JSON array of trace entry dicts, newest last.
    """
    try:
        from infra.infrastructure import resolve_active_memory_dir

        trace_path = resolve_active_memory_dir() / "recall_trace.jsonl"
        if not trace_path.exists():
            return json.dumps({"entries": [], "count": 0, "path": str(trace_path)})

        limit = max(1, min(int(limit), 500))
        entries: list[dict] = []
        with open(trace_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event and obj.get("event") != event:
                    continue
                if since_ts and obj.get("ts", "") < since_ts:
                    continue
                entries.append(obj)

        entries = entries[-limit:]
        return json.dumps(
            {"entries": entries, "count": len(entries), "path": str(trace_path)},
            indent=2,
        )
    except Exception as e:
        logger.warning("Unhandled exception in _op_recall_trace: %s", e)
        return _err(classify_exception(e), str(e))


def _op_recall_status() -> str:
    """Return session_recap recall policy configuration and tier metadata.

    Returns:
        JSON with:
          config: effective recall tuning values (from MemoryConfig or defaults)
          tiers: list of tier descriptors (name, description, max_items, source)
    """
    try:
        from recall.recall import _get_recall_cfg

        cfg = _get_recall_cfg()
        tiers = [
            {
                "name": "tier1_hot_curated",
                "description": "Pinned or high-importance (>=4) notes created in the last 7 days",
                "max_items": 5,
                "section_header": "## Key Context",
                "source": "memories table (pinned=1 OR importance>=4)",
            },
            {
                "name": "tier2_semantic_search",
                "description": "search_memories(query, light=True) for project-relevant content",
                "max_items": 5,
                "section_header": "## Relevant to this session",
                "source": "search_memories (light=True)",
                "requires_query": True,
            },
            {
                "name": "tier3_kg_facts",
                "description": "Known facts from the knowledge graph for the current namespace",
                "max_items": 3,
                "section_header": "## Known Facts",
                "source": "kg_facts table",
            },
            {
                "name": "tier4_recent_sessions",
                "description": "Recent non-auto session notes (last 3 days), fallback only",
                "max_items": 3,
                "section_header": "## Recent Activity",
                "source": "memories table (sessions/* excluding auto-*)",
                "fallback_trigger": "activates only when tiers 1-3 return fewer than 5 total items",
            },
        ]
        return json.dumps(
            {
                "config": {
                    "max_tokens": int(cfg.get("max_tokens", 800)),
                    "tier1_hot_days": int(cfg.get("tier1_hot_days", 7)),
                    "tier_fallback_threshold": int(cfg.get("tier_fallback_threshold", 5)),
                },
                "tiers": tiers,
            },
            indent=2,
        )
    except Exception as e:
        logger.warning("Unhandled exception in _op_recall_status: %s", e)
        return _err(classify_exception(e), str(e))


def _op_session_admin_stats(type: str = "all") -> str:
    try:
        from pathlib import Path
        from session_manager import SessionManager
        from infra.db import safe_close_db

        db_path = os.environ.get("MEMORY_DB_PATH")
        mgr = SessionManager(db_path=Path(db_path)) if db_path else SessionManager()
        conn = mgr._conn()
        try:
            result = {}
            if type in ("session", "all"):
                rows = conn.execute(
                    "SELECT status, COUNT(*) FROM sessions GROUP BY status"
                ).fetchall()
                total = sum(r[1] for r in rows)
                result["sessions"] = {
                    "total": total,
                    "by_status": {r[0]: r[1] for r in rows},
                }
            if type in ("thread", "all"):
                rows = conn.execute(
                    "SELECT status, COUNT(*) FROM decision_threads GROUP BY status"
                ).fetchall()
                result["decision_threads"] = {"by_status": {r[0]: r[1] for r in rows}}
            if type in ("compaction", "all"):
                total = conn.execute(
                    "SELECT COUNT(*) FROM session_compaction_log"
                ).fetchone()[0]
                avg_delta = conn.execute(
                    "SELECT AVG(COALESCE(tokens_before,0) - COALESCE(tokens_after,0)) "
                    "FROM session_compaction_log"
                ).fetchone()[0]
                zombies = conn.execute(
                    "SELECT COUNT(*) FROM sessions s "
                    "WHERE s.status='active' AND s.started_at < datetime('now', '-24 hours') "
                    "AND NOT EXISTS (SELECT 1 FROM session_compaction_log c WHERE c.session_id=s.id)"
                ).fetchone()[0]
                result["compactions"] = {
                    "total_compactions": total,
                    "avg_token_delta": round(avg_delta or 0, 1),
                    "zombie_sessions": zombies,
                }

            if not result:
                return _err(ErrorCode.INVALID_PARAMS, f"Unknown type '{type}'. Valid: all, session, thread, compaction")

            return _json.dumps(result)
        finally:
            safe_close_db(conn)
    except Exception as e:
        logger.warning("Unhandled exception in _op_session_admin_stats: %s", e)
        return _err(classify_exception(e), str(e))


def _op_background_task_status(memory_id: str) -> str:
    """Query the status of deferred background tasks for a given memory_id."""
    try:
        from pathlib import Path
        from infra.db import open_db

        db_path_env = os.environ.get("MEMORY_DB_PATH")
        db_path = Path(db_path_env) if db_path_env else Path.home() / ".config" / "agentic-memory" / "memory.db"
        if not db_path.exists():
            return json.dumps({"status": "not_found", "message": f"Database not found at {db_path}"})

        with open_db(db_path) as conn:
            rows = conn.execute(
                "SELECT status, started_at, completed_at, error, attempts, task_type, payload "
                "FROM task_queue WHERE payload LIKE ?",
                (f"%{memory_id}%",)
            ).fetchall()

            if not rows:
                return json.dumps({"status": "not_found", "message": "No background tasks found for memory_id."})

            status_summary = "completed"
            last_attempt = None
            max_attempts = 0
            errors = []

            any_pending = False
            any_failed = False

            for row in rows:
                status, started, completed, err, attempts, task_type, payload_str = row
                try:
                    payload = json.loads(payload_str)
                except Exception as e:
                    logger.warning("Unhandled exception in _op_background_task_status: %s", e)
                    payload = {}
                if payload.get("memory_id") != memory_id:
                    continue

                max_attempts = max(max_attempts, attempts)
                if status in ("pending", "processing"):
                    any_pending = True
                elif status == "failed":
                    any_failed = True
                    if err:
                        errors.append(f"{task_type}: {err}")

                ts = completed or started
                if ts and (not last_attempt or ts > last_attempt):
                    last_attempt = ts

            if any_pending:
                status_summary = "pending"
            elif any_failed:
                status_summary = "failed"
            elif not max_attempts:
                return json.dumps({"status": "not_found", "message": "No background tasks found for memory_id."})

            res = {
                "status": status_summary,
                "last_attempt": last_attempt,
                "retries": max(0, max_attempts - 1),
            }
            if errors:
                res["error"] = "; ".join(errors)
            return json.dumps(res)
    except Exception as e:
        logger.warning("Unhandled exception in _op_background_task_status: %s", e)
        return _err(classify_exception(e), str(e))


def _op_gdpr_erase(
    principal_id: str = "",
    data_subject_sub: str = "",
    tenant_id: str = "default",
    confirm: bool = False,
) -> str:
    """Cascading GDPR Right-to-Be-Forgotten erase for a data subject.

    Gated by confirm=True (destructive operation) and RBAC role
    ``compliance:gdpr-erase``.
    """
    if not data_subject_sub:
        return _err(ErrorCode.INVALID_PARAMS, "data_subject_sub is required")
    if not confirm:
        return _err(ErrorCode.INVALID_PARAMS, "GDPR erase is destructive and requires confirm=True")
    try:
        from infra.authorizer import mcp_authorize, log_authorization_decision
        from agent_context import get_agent

        ctx = get_agent()
        resolved_principal = principal_id or getattr(ctx, "principal_id", None)

        from pathlib import Path
        from infra.db import open_db

        db_path_env = os.environ.get("MEMORY_DB_PATH")
        db_path: Path | None = None
        if db_path_env:
            db_path = Path(db_path_env)
        else:
            from mcp_common import _resolve_memory_dir
            memory_dir = _resolve_memory_dir()
            if memory_dir:
                candidate = Path(memory_dir) / "memory.db"
                if candidate.exists():
                    db_path = candidate

        if not db_path or not db_path.exists():
            return _err(ErrorCode.DB_ERROR, "Memory database not found")

        allowed = mcp_authorize(
            principal_id=resolved_principal,
            action="compliance",
            resource="gdpr-erase",
            db_path=str(db_path),
        )
        if not allowed:
            log_authorization_decision(
                principal_id=resolved_principal,
                action="compliance",
                resource="gdpr-erase",
                allowed=False,
                db_path=str(db_path),
            )
            return _err(
                ErrorCode.AUTHORIZATION_DENIED,
                f"Not authorized for compliance:gdpr-erase. "
                f"Principal '{resolved_principal or 'anonymous'}' lacks the required role.",
            )

        with open_db(db_path) as conn:
            from infra.gdpr import gdpr_erase
            result = gdpr_erase(
                conn=conn,
                principal_id=resolved_principal or "anonymous",
                data_subject_sub=data_subject_sub,
                tenant_id=tenant_id,
            )
        return json.dumps(result, default=str)
    except Exception as e:
        logger.warning("Unhandled exception in GDPR erase: %s", e)
        return _err(classify_exception(e), str(e))


MAINTENANCE_HANDLERS = _MaintenanceHandlersProxy()


__all__ = ["MAINTENANCE_HANDLERS"]
