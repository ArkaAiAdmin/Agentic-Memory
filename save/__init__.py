"""Save pipeline subpackage.

Extracted from the original monolithic save_pipeline.py (2026-06-20)
as part of the god-module decomposition. The full save_pipeline.py
re-exports the symbols here for backward compatibility — existing
callers that do ``from save_pipeline import _crdt_agent_id`` keep
working unchanged.

Public API surface
------------------
``save_memory`` — the top-level save orchestrator (save_pipeline.py:709)
is intentionally defined **in the shim file** rather than a standalone
submodule. It is the entry point for every write path (``memory_save``,
the auto-save hook, manual calls). Splitting it into a submodule would
add indirection for a symbol that is already the canonical public API
surface.  It is re-exported here lazily via ``__getattr__`` so that
``from save import save_memory`` works for users who import the
subpackage directly.
"""

from save.crdt_helpers import (  # noqa: F401
    _crdt_agent_id,
    _is_crdt_enabled,
    _is_legacy_note_crdt_enabled,
    _crdt_bump_version,
)
from save.backlinks import (  # noqa: F401
    _auto_fts_backlinks,
    _auto_semantic_backlinks,
    _auto_backlink_multi_part,
)
from save.cleanup import (  # noqa: F401
    cleanup_memory_relations,
    remove_kg_relations_for_note,
    remove_backlinks_for_note,
)

__all__ = [
    "cleanup_memory_relations",
    "remove_kg_relations_for_note",
    "remove_backlinks_for_note",
    "save_memory",
    "save_memory_journal",
    "memory_supersede_db",
    "reinforce_memories_db",
    "_index_backlinks",
    "_index_chunks",
    "_index_embedding",
    "_index_kg",
    "_index_facts",
    "_index_adaptive_retention",
    "_enrich_context",
    "_recalculate_fitness_scores",
    "_run_post_save_hooks",
    "_enqueue_background_tasks",
]


_LAZY_SAVE_NAMES: dict[str, tuple[str, str]] = {
    "_index_backlinks": ("save.indexers", "_index_backlinks"),
    "_index_chunks": ("save.indexers", "_index_chunks"),
    "_index_embedding": ("save.indexers", "_index_embedding"),
    "_index_kg": ("save.indexers", "_index_kg"),
    "_index_facts": ("save.indexers", "_index_facts"),
    "_index_adaptive_retention": ("save.indexers", "_index_adaptive_retention"),
    "_enrich_context": ("save.post_save_hooks", "_enrich_context"),
    "_recalculate_fitness_scores": ("save.post_save_hooks", "_recalculate_fitness_scores"),
    "_run_post_save_hooks": ("save.post_save_hooks", "_run_post_save_hooks"),
    "_enqueue_background_tasks": ("save.post_save_hooks", "_enqueue_background_tasks"),
}


def __getattr__(name: str):
    if name in _LAZY_SAVE_NAMES:
        import importlib

        mod_name, attr_name = _LAZY_SAVE_NAMES[name]
        mod = importlib.import_module(mod_name)
        return getattr(mod, attr_name)
    if name == "save_memory":
        from infra._lazy_imports import save_memory

        return save_memory
    if name == "save_memory_journal":
        from save_pipeline import save_memory_journal

        return save_memory_journal
    if name == "memory_supersede_db":
        from save_pipeline import memory_supersede_db

        return memory_supersede_db
    if name == "reinforce_memories_db":
        from save_pipeline import reinforce_memories_db

        return reinforce_memories_db
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _reset_save_memory_cache() -> None:
    """Test helper: clear any cached reference to save_pipeline.save_memory.

    ``__getattr__`` (PEP 562) is not cached — it resolves on every
    attribute access — so this is a no-op today.  It exists so that
    tests that patch ``save_pipeline.save_memory`` and re-import via
    ``from save import save_memory`` have a canonical place to clear
    any future caching layer without knowing the internals.
    """
    pass
