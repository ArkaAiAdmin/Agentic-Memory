from __future__ import annotations
"""Central registry for lazy/deferred imports.

Function-body ``from X import Y`` is used throughout the codebase to
break circular import cycles (e.g., ``memory_common → config → memory_common``).
This module provides a single place to define *which module exports which name*,
so that callers write::

    from infra._lazy_imports import save_memory

instead of::

    from save_pipeline import save_memory

Benefits:
1. If the implementation module moves, update one mapping here.
2. ``grep``-ing for ``save_memory`` now finds all callers via one import path.
3. New developers see the canonical name-to-module mapping in one place.

Usage MUST still be inside function bodies to preserve the deferred-load
behaviour — module-level ``from _lazy_imports import X`` would trigger
``importlib.import_module`` at import time, re-introducing the cycle.
"""

import importlib

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # write path
    "SaveRequest": ("save_pipeline", "SaveRequest"),
    "save_memory": ("save_pipeline", "save_memory"),
    "save_memory_journal": ("save_pipeline", "save_memory_journal"),
    "save_memory_auto": ("save_pipeline", "save_memory_auto"),
    # read path
    "search_memories": ("search.orchestrator", "search_memories"),
    # config
    "get_config": ("infra.config", "get_config"),
    "MemoryConfig": ("infra.config", "MemoryConfig"),
    # database
    "open_db": ("infra.memory_common", "open_db"),
    "safe_close_db": ("infra.memory_common", "safe_close_db"),
    "connection_pool": ("infra.memory_common", "connection_pool"),
    "run_db_migrations": ("infra.memory_common", "run_db_migrations"),
    "get_memory_paths": ("infra.memory_common", "get_memory_paths"),
    "find_project_root": ("infra.memory_common", "find_project_root"),
    "GLOBAL_MEM_DIR": ("infra.memory_common", "GLOBAL_MEM_DIR"),
    # embeddings
    "get_embedding_search": ("infra.embedding_search", "get_embedding_search"),
    # injection safety
    "scan_for_injection": ("memory_injection", "scan_for_injection"),
    # agent context
    "init_agent": ("agent_context", "init_agent"),
    "get_agent": ("agent_context", "get_agent"),
    "clear_agent": ("agent_context", "clear_agent"),
    "list_agents": ("agent_context", "list_agents"),
    "agent_save": ("agent_context", "agent_save"),
    "agent_search": ("agent_context", "agent_search"),
    "scope_note_id": ("agent_context", "scope_note_id"),
    "agent_filter_clause": ("agent_context", "agent_filter_clause"),
    # file lock
    "FileLockError": ("infra.file_lock", "FileLockError"),
    "acquire_flock_with_retry": ("infra.file_lock", "acquire_flock_with_retry"),
    # saga
    "saga_save_memory": ("infra.saga", "saga_save_memory"),
    # error codes
    "ErrorCode": ("infra.infrastructure", "ErrorCode"),
    "ErrorCategory": ("infra.infrastructure", "ErrorCategory"),
    # backfill
    "backfill_health_check": ("backfill.orchestrator", "health_check"),
    # knowledge graph traversal
    "find_shortest_path": ("kg.kg_traversal", "find_shortest_path"),
    "find_neighbors": ("kg.kg_traversal", "find_neighbors"),
    "traverse_graph": ("kg.kg_traversal", "traverse_graph"),
    # crdt
    "crdt_save": ("crdt.crdt_merge", "crdt_save"),
    "crdt_sync_all": ("crdt.crdt_merge", "crdt_sync_all"),
    # cache
    "clear_vec_cache": ("infra.cache", "clear_vec_cache"),
    # adaptive retention
    "compute_adaptive_halflife": ("background.adaptive_retention", "compute_adaptive_halflife"),
    "record_access": ("background.adaptive_retention", "record_access"),
    # facts
    "extract_facts": ("fact.fact_extract", "extract_facts"),
    # db
    "_resolve_mmap_size": ("infra.db", "_resolve_mmap_size"),
    "_local_state": ("infra.db", "_local_state"),
    # search memory
    "search_memories_impl": ("recall.search_memory", "search_memories_impl"),
    # migration
    "SCHEMA_VERSION": ("infra.migration_runner", "SCHEMA_VERSION"),
    # reranker
    "get_reranker": ("infra.reranker", "get_reranker"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        mod, attr = _LAZY_IMPORTS[name]
        return getattr(importlib.import_module(mod), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _add_lazy_import(name: str, module: str, attr: str) -> None:
    """Register a new lazy import at runtime (for tests / extensibility)."""
    _LAZY_IMPORTS[name] = (module, attr)
