"""Central registry for lazy/deferred imports.

Function-body ``from X import Y`` is used throughout the codebase to
break circular import cycles (e.g., ``memory_common → config → memory_common``).
This module provides a single place to define *which module exports which name*,
so that callers write::

    from _lazy_imports import save_memory

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
    # read path
    "search_memories": ("search.orchestrator", "search_memories"),
    # config
    "get_config": ("config", "get_config"),
    "MemoryConfig": ("config", "MemoryConfig"),
    # database
    "open_db": ("memory_common", "open_db"),
    "safe_close_db": ("memory_common", "safe_close_db"),
    "connection_pool": ("memory_common", "connection_pool"),
    "run_db_migrations": ("memory_common", "run_db_migrations"),
    "get_memory_paths": ("memory_common", "get_memory_paths"),
    "find_project_root": ("memory_common", "find_project_root"),
    "GLOBAL_MEM_DIR": ("memory_common", "GLOBAL_MEM_DIR"),
    # embeddings
    "get_embedding_search": ("embedding_search", "get_embedding_search"),
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
    "FileLockError": ("file_lock", "FileLockError"),
    "acquire_flock_with_retry": ("file_lock", "acquire_flock_with_retry"),
    # saga
    "saga_save_memory": ("saga", "saga_save_memory"),
    # error codes
    "ErrorCode": ("infrastructure", "ErrorCode"),
    "ErrorCategory": ("infrastructure", "ErrorCategory"),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        mod, attr = _LAZY_IMPORTS[name]
        return getattr(importlib.import_module(mod), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _add_lazy_import(name: str, module: str, attr: str) -> None:
    """Register a new lazy import at runtime (for tests / extensibility)."""
    _LAZY_IMPORTS[name] = (module, attr)
