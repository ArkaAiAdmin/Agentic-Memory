"""Knowledge graph subpackage.

Contains contradiction detection, CRDT replication, entity dedup,
and graph traversal.  Root-level shims re-export everything here
for backward compatibility.
"""

from kg.graph_analytics import (  # noqa: F401
    compute_pagerank,
    update_graph_analytics,
    compute_betweenness,
    update_betweenness,
)
from kg.graph_communities import (  # noqa: F401
    connected_components,
    louvain_communities,
    compute_communities,
    write_community_ids,
)
from kg.contradiction_detector import (  # noqa: F401
    detect_contradictions,
    detect_contradictions_semantic,
    detect_contradictions_all,
    split_segments,
    split_sentences,
    significant_words,
    classify_operation,
)
from kg.kg_crdt import (  # noqa: F401
    EntityOp,
    EdgeOp,
    merge_entity_ops,
    merge_edge_ops,
    apply_entity_crdt_to_db,
    apply_edge_crdt_to_db,
    compute_entity_crdt_state,
    compute_edge_crdt_state,
    ensure_kg_crdt_schema,
)
from kg.kg_dedup import (  # noqa: F401
    dedup_entities,
    compute_semantic_merge_candidates,
    merge_entities,
    main,
)
from kg.kg_traversal import (  # noqa: F401
    find_shortest_path,
    find_neighbors,
    traverse_graph,
)
from kg.temporal_resolver import (  # noqa: F401
    get_temporal_facts,
    resolve_temporal_contradiction,
)

__all__ = [
    # graph_analytics
    "compute_pagerank",
    "update_graph_analytics",
    "compute_betweenness",
    "update_betweenness",
    # graph_communities
    "connected_components",
    "louvain_communities",
    "compute_communities",
    "write_community_ids",
    # contradiction_detector
    "detect_contradictions",
    "detect_contradictions_semantic",
    "detect_contradictions_all",
    "split_segments",
    "split_sentences",
    "significant_words",
    "classify_operation",
    # kg_crdt
    "EntityOp",
    "EdgeOp",
    "merge_entity_ops",
    "merge_edge_ops",
    "apply_entity_crdt_to_db",
    "apply_edge_crdt_to_db",
    "compute_entity_crdt_state",
    "compute_edge_crdt_state",
    "ensure_kg_crdt_schema",
    # kg_dedup
    "dedup_entities",
    "compute_semantic_merge_candidates",
    "merge_entities",
    "main",
    # kg_traversal
    "find_shortest_path",
    "find_neighbors",
    "traverse_graph",
    # temporal_resolver
    "get_temporal_facts",
    "resolve_temporal_contradiction",
]
