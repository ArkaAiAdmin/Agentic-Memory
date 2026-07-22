# Graph Traversal and Querying

The **Graph Traversal and Querying** module provides path-finding, multi-hop sub-graph extraction, entity neighborhood expansion, and graph-guided search reranking.

## Architecture

The Knowledge Graph subsystem links factual entities (`subject -> predicate -> object`) extracted from saved memories into a semantic property graph:

```
[Memory A] ---> (mentions) ---> [Entity: Python] ---> (USES) ---> [Entity: SQLite]
                                     |
                                  (HAS_A)
                                     v
                           [Entity: WAL Mode]
```

## Key Components

| Component | Target File | Description |
| :--- | :--- | :--- |
| **MCP Traversal Surface** | [mcp_kg_traversal.py](file://mcp_kg_traversal.py) | Exposes `traverse_graph`, `find_path`, and `get_entity_neighborhood` |
| **Graph Traversal Core** | [kg/kg_traversal.py](file://kg/kg_traversal.py) | Performs BFS/DFS multi-hop graph expansion and edge weighting |
| **Search Phase Integrator** | [search/phases/kg_traversal.py](file://search/phases/kg_traversal.py) | Phase 6 search module enriching hybrid candidates with graph context |

## Key Capabilities

1. **Multi-Hop Path Finding**: Computes shortest or weighted paths between entities (up to max depth 4).
2. **Neighborhood Expansion**: Fetches all connected relations for a given target entity with configurable edge filters.
3. **Graph-Guided Reranking**: Boosts search candidate scores when candidate memories overlap with high-centrality graph nodes matching the query context.
