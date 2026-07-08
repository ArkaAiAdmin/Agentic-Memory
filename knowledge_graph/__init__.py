"""knowledge_graph subpackage — decomposed from knowledge_graph.py.

Re-exports all public symbols from the submodules so both
``from knowledge_graph import graph_search`` and the original
``from knowledge_graph import graph_search`` keep working.
"""

import logging
logger = logging.getLogger(__name__)

from knowledge_graph.kg_db import (  # noqa: F401
    _upsert_edge,
    _upsert_entity,
    get_active_edges_for_entity,
    index_kg_for_memory,
    invalidate_edge,
)
from knowledge_graph.kg_extract import (
    clear_extraction_cache,
    extract_entities,
    extract_relations,
)
from knowledge_graph.kg_schema import ensure_kg_schema
from knowledge_graph.kg_search import (  # noqa: F401
    _row_to_edge_dict,
    _row_to_entity_dict,
    _temporal_edge_clause,
    clear_graph_cache,
    graph_search,
    graph_search_db,
    graph_stats,
    graph_stats_db,
    index_kg_for_memory_db,
)

__all__ = [
    "KG_ENABLED",
    "ensure_kg_schema",
    "extract_entities",
    "extract_relations",
    "index_kg_for_memory",
    "graph_search",
    "graph_stats",
    "graph_search_db",
    "graph_stats_db",
    "index_kg_for_memory_db",
    "clear_extraction_cache",
    "clear_graph_cache",
    "invalidate_edge",
    "get_active_edges_for_entity",
]

_KG_ENABLED_CACHE: bool | None = None


def __getattr__(name: str):
    if name == "KG_ENABLED":
        global _KG_ENABLED_CACHE
        if _KG_ENABLED_CACHE is not None:
            return _KG_ENABLED_CACHE
        try:
            from config import get_config

            _KG_ENABLED_CACHE = bool(getattr(get_config(), "knowledge_graph", True))
        except Exception as e:
            logger.warning("__getattr__ failed: %s", e)
            _KG_ENABLED_CACHE = True
        return _KG_ENABLED_CACHE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _clear_kg_enabled_cache() -> None:
    global _KG_ENABLED_CACHE
    _KG_ENABLED_CACHE = None
