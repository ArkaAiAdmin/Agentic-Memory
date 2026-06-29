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

import _bootstrap_path  # noqa: E402
import os
import sys
from pathlib import Path


from typing import Callable


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
            memory_duplicates,
            memory_merge_suggestions,
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
        )

        _local_tools = {
            "memory_heartbeat": memory_heartbeat,
            "memory_tier_stats": memory_tier_stats,
            "memory_run_tier_migration": memory_run_tier_migration,
            "memory_check_embedding_model": memory_check_embedding_model,
            "memory_incremental_update": memory_incremental_update,
            "memory_duplicates": memory_duplicates,
            "memory_merge_suggestions": memory_merge_suggestions,
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
        )
        from mcp_kg_traversal import (
            memory_graph_shortest_path,
            memory_graph_traverse,
        )
        from mcp_profile import memory_profile_stats
        from mcp_multi_modal import memory_ingest_file, memory_ingest_url
        from mcp_dashboard import memory_dashboard
        from mcp_metrics import memory_metrics_server

        _domain_tools = {
            "memory_agent_init": memory_agent_init,
            "memory_agent_clear": memory_agent_clear,
            "memory_agent_list": memory_agent_list,
            "memory_graph_shortest_path": memory_graph_shortest_path,
            "memory_graph_traverse": memory_graph_traverse,
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
            "memory_ingest_file": memory_ingest_file,
            "memory_ingest_url": memory_ingest_url,
            "memory_dashboard": memory_dashboard,
            "memory_metrics_server": memory_metrics_server,
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
        from cron.cron_train_forget_model import main as _train_forget_model
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
            MaintenanceOp.DUPLICATES: lambda *, threshold=0.85, **_: t[
                "memory_duplicates"
            ](threshold=threshold),
            MaintenanceOp.MERGE_SUGGESTIONS: lambda *, threshold=0.90, **_: t[
                "memory_merge_suggestions"
            ](threshold=threshold),
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
            MaintenanceOp.OKF_IMPORT: lambda *, input_dir, is_global=False, dry_run=False, overwrite=False, **_: (
                t["memory_okf_import"](
                    input_dir=input_dir,
                    is_global=is_global,
                    dry_run=dry_run,
                    overwrite=overwrite,
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
            MaintenanceOp.PROFILE_STATS: lambda **_: t["memory_profile_stats"](),
            MaintenanceOp.LLM_UNLOAD: lambda **_: t["memory_llm_unload"](),
            MaintenanceOp.ADAPTIVE_RETENTION: lambda *, dry_run=False, **_: t[
                "memory_adaptive_retention"
            ](dry_run=dry_run),
            MaintenanceOp.RETENTION_STATS: lambda **_: t["memory_retention_stats"](),
            MaintenanceOp.INGEST_FILE: lambda *, file_path, category="sessions", tags="", **_: (
                t["memory_ingest_file"](
                    file_path=file_path,
                    category=category,
                    tags=tags,
                )
            ),
            MaintenanceOp.INGEST_URL: lambda *, url, category="sessions", tags="", **_: (
                t["memory_ingest_url"](
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
            MaintenanceOp.SESSION_STATS: lambda **_: _op_session_stats(),
            MaintenanceOp.THREAD_STATS: lambda **_: _op_thread_stats(),
            MaintenanceOp.COMPACTION_STATS: lambda **_: _op_compaction_stats(),
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
                _train_forget_model()
            ),
        }
    return _MAINTENANCE_HANDLERS


# Module-level proxy: MAINTENANCE_HANDLERS["foo"] resolves through
# the lazy getter. mcp_maintenance.py does:
#   from mcp_maintenance_ops import MAINTENANCE_HANDLERS
#   handler = MAINTENANCE_HANDLERS[op_enum]
# which works with this proxy.
class _MaintenanceHandlersProxy:
    def __getitem__(self, key):
        return _get_handlers()[key]

    def __iter__(self):
        return iter(_get_handlers())

    def __len__(self):
        return len(_get_handlers())

    def __contains__(self, key):
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


def _op_session_stats() -> str:
    try:
        from pathlib import Path
        from session_manager import SessionManager
        from db import safe_close_db

        db_path = os.environ.get("MEMORY_DB_PATH")
        mgr = SessionManager(db_path=Path(db_path)) if db_path else SessionManager()
        conn = mgr._conn()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM sessions GROUP BY status"
            ).fetchall()
            total = sum(r[1] for r in rows)
            return _json.dumps(
                {
                    "total": total,
                    "by_status": {r[0]: r[1] for r in rows},
                }
            )
        finally:
            safe_close_db(conn)
    except Exception as e:
        return _json.dumps({"error": str(e)})


def _op_thread_stats() -> str:
    try:
        from pathlib import Path
        from session_manager import SessionManager
        from db import safe_close_db

        db_path = os.environ.get("MEMORY_DB_PATH")
        mgr = SessionManager(db_path=Path(db_path)) if db_path else SessionManager()
        conn = mgr._conn()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM decision_threads GROUP BY status"
            ).fetchall()
            return _json.dumps({"by_status": {r[0]: r[1] for r in rows}})
        finally:
            safe_close_db(conn)
    except Exception as e:
        return _json.dumps({"error": str(e)})


def _op_compaction_stats() -> str:
    try:
        from pathlib import Path
        from session_manager import SessionManager
        from db import safe_close_db

        db_path = os.environ.get("MEMORY_DB_PATH")
        mgr = SessionManager(db_path=Path(db_path)) if db_path else SessionManager()
        conn = mgr._conn()
        try:
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
            return _json.dumps(
                {
                    "total_compactions": total,
                    "avg_token_delta": round(avg_delta or 0, 1),
                    "zombie_sessions": zombies,
                }
            )
        finally:
            safe_close_db(conn)
    except Exception as e:
        return _json.dumps({"error": str(e)})


def _op_list_active_threads(
    project_root: str = "", status: str = "", limit: int = 20
) -> str:
    try:
        import json as _json
        from pathlib import Path
        from session_manager import SessionManager
        from db import safe_close_db

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
        return _json.dumps({"error": str(e)})


def _op_extract_skills(memory_id: str = "", dry_run: bool = False) -> str:
    """Wrapper for memory_extract_skills (needs conn injection)."""
    try:
        from db import open_db
        from pathlib import Path
        target_base = Path(os.environ.get("MEMORY_DB_PATH", ""))
        if not target_base.exists():
            from memory_common import get_memory_paths
            _, local_mem, _ = get_memory_paths()
            target_base = local_mem
        db_path = target_base / "memory.db" if target_base.suffix != ".db" else target_base
        with open_db(db_path, write=False) as conn:
            from mcp_maintenance import memory_extract_skills
            return memory_extract_skills(conn, memory_id=memory_id, dry_run=dry_run)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _op_list_skills(limit: int = 50) -> str:
    """Wrapper for memory_list_skills (needs conn injection)."""
    try:
        from db import open_db
        from pathlib import Path
        target_base = Path(os.environ.get("MEMORY_DB_PATH", ""))
        if not target_base.exists():
            from memory_common import get_memory_paths
            _, local_mem, _ = get_memory_paths()
            target_base = local_mem
        db_path = target_base / "memory.db" if target_base.suffix != ".db" else target_base
        with open_db(db_path, write=False) as conn:
            from mcp_maintenance import memory_list_skills
            return memory_list_skills(conn, limit=limit)
    except Exception as e:
        return json.dumps({"error": str(e)})


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
                from db import safe_close_db

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
        return _json.dumps({"error": str(e)})


MAINTENANCE_HANDLERS = _MaintenanceHandlersProxy()


__all__ = ["MAINTENANCE_HANDLERS"]
