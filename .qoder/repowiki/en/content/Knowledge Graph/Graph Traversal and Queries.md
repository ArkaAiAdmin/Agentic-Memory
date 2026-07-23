# Graph Traversal and Queries

<cite>
**Referenced Files in This Document**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [test_kg_traversal.py](file://tests/test_kg_traversal.py)
- [test_multi_hop_traversal.py](file://tests/test_multi_hop_traversal.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Dependency Analysis](#dependency-analysis)
7. [Performance Considerations](#performance-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Conclusion](#conclusion)
10. [Appendices](#appendices)

## Introduction
This document explains the graph traversal algorithms and query capabilities available for knowledge graphs, including path finding (BFS, DFS, shortest path), multi-hop queries, pattern matching, and analytical functions such as centrality measures, community detection, and general graph metrics. It also provides practical examples of complex graph queries, performance optimization techniques, indexing strategies for large graphs, and guidance on query language syntax, result formatting, and pagination for large result sets.

## Project Structure
The graph-related functionality is implemented across several modules:
- Traversal and pathfinding logic resides in a dedicated traversal module.
- Analytical functions (centrality, communities, metrics) are provided by analytics and community modules.
- Knowledge graph search and query interfaces are exposed via a search module and MCP tools.
- Database access and schema definitions support efficient storage and retrieval.

```mermaid
graph TB
subgraph "Graph Core"
T["Traversal Module<br/>kg_traversal.py"]
A["Analytics Module<br/>graph_analytics.py"]
C["Communities Module<br/>graph_communities.py"]
end
subgraph "Query Interface"
S["KG Search<br/>kg_search.py"]
M["MCP KG Traversal<br/>mcp_kg_traversal.py"]
end
subgraph "Storage"
DB["KG Database Access<br/>kg_db.py"]
SCHEMA["KG Schema<br/>kg_schema.py"]
end
T --> DB
A --> DB
C --> DB
S --> T
S --> A
S --> C
M --> S
S --> DB
DB --> SCHEMA
```

**Diagram sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)

**Section sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)

## Core Components
- Traversal and Pathfinding
  - Breadth-first search (BFS) for level-order exploration and shortest unweighted paths.
  - Depth-first search (DFS) for deep exploration and backtracking-based discovery.
  - Shortest path utilities that leverage BFS or weighted variants when applicable.
- Multi-Hop Queries
  - Composable traversal primitives enabling N-hop expansions with filters and constraints.
  - Pattern matching over sequences of edges and nodes to express structured queries.
- Analytical Functions
  - Centrality measures (e.g., degree, betweenness, closeness) for node importance analysis.
  - Community detection routines to identify cohesive subgraphs.
  - General graph metrics (connectivity, density, diameter approximations).
- Query Interface
  - High-level search API that composes traversal, filtering, ranking, and pagination.
  - MCP tooling for programmatic invocation from external systems.

**Section sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)

## Architecture Overview
The system separates concerns into traversal, analytics, and query layers, all backed by a consistent database interface. The search layer orchestrates traversal and analytics to answer user queries, while MCP exposes these capabilities to clients.

```mermaid
sequenceDiagram
participant Client as "Client"
participant MCP as "MCP KG Traversal"
participant Search as "KG Search"
participant Traverse as "Traversal Module"
participant Analytics as "Analytics Module"
participant DB as "KG Database"
Client->>MCP : "Invoke traversal/query"
MCP->>Search : "Parse request and build plan"
Search->>Traverse : "Execute traversal (BFS/DFS/shortest)"
Traverse->>DB : "Read edges/nodes"
DB-->>Traverse : "Graph slices"
Traverse-->>Search : "Paths and visited nodes"
Search->>Analytics : "Compute metrics (optional)"
Analytics->>DB : "Aggregate data"
DB-->>Analytics : "Aggregates"
Analytics-->>Search : "Scores/metrics"
Search-->>MCP : "Formatted results"
MCP-->>Client : "Response with pagination"
```

**Diagram sources**
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

## Detailed Component Analysis

### Traversal and Pathfinding
- Breadth-First Search (BFS)
  - Explores neighbors level-by-level; suitable for shortest unweighted paths and bounded-depth queries.
  - Supports pruning via filters and early termination upon reaching target nodes.
- Depth-First Search (DFS)
  - Follows one branch deeply before backtracking; useful for exhaustive exploration and cycle detection.
  - Can be constrained by depth limits and edge/node predicates.
- Shortest Path
  - Uses BFS for unweighted graphs; can integrate with weighted variants if edge weights are present.
  - Returns ordered paths with hop counts and optional intermediate metadata.

```mermaid
flowchart TD
Start(["Start Traversal"]) --> ChooseAlgo{"Algorithm?"}
ChooseAlgo --> |BFS| InitQueue["Initialize queue with start nodes"]
ChooseAlgo --> |DFS| InitStack["Initialize stack with start nodes"]
InitQueue --> LoopBFS["While queue not empty"]
InitStack --> LoopDFS["While stack not empty"]
LoopBFS --> ExpandBFS["Dequeue node u<br/>Expand neighbors v"]
LoopDFS --> ExpandDFS["Pop node u<br/>Expand neighbors v"]
ExpandBFS --> FilterBFS{"Filter passes?"}
ExpandDFS --> FilterDFS{"Filter passes?"}
FilterBFS --> |Yes| EnqBFS["Enqueue v if not visited"]
FilterBFS --> |No| SkipBFS["Skip v"]
FilterDFS --> |Yes| PushDFS["Push v if not visited"]
FilterDFS --> |No| SkipDFS["Skip v"]
EnqBFS --> CheckTarget{"Reached target?"}
PushDFS --> CheckTarget
CheckTarget --> |Yes| ReturnPath["Return path(s)"]
CheckTarget --> |No| ContinueBFS["Continue BFS"]
ContinueBFS --> LoopBFS
SkipBFS --> LoopBFS
SkipDFS --> LoopDFS
ReturnPath --> End(["End"])
```

**Diagram sources**
- [kg_traversal.py](file://kg/kg_traversal.py)

**Section sources**
- [kg_traversal.py](file://kg/kg_traversal.py)

### Multi-Hop Queries and Pattern Matching
- Multi-Hop Expansion
  - Compose multiple traversal steps with filters at each hop to constrain the search space.
  - Supports variable-length hops within configured bounds to avoid combinatorial explosion.
- Pattern Matching
  - Express patterns as sequences of node and edge constraints.
  - Matchers evaluate structural properties (labels, types, attributes) and temporal constraints where applicable.

```mermaid
sequenceDiagram
participant Q as "Query Builder"
participant P as "Pattern Matcher"
participant T as "Traversal Engine"
participant D as "Database"
Q->>P : "Define pattern (nodes/edges + filters)"
P->>T : "Compile to traversal plan"
T->>D : "Fetch initial candidates"
D-->>T : "Candidates"
loop For each hop
T->>D : "Follow edges with constraints"
D-->>T : "Next-hop nodes"
T->>P : "Apply predicate checks"
P-->>T : "Matched segments"
end
T-->>Q : "Complete paths"
Q-->>P : "Assemble result objects"
P-->>Q : "Structured matches"
```

**Diagram sources**
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [kg_traversal.py](file://kg/kg_traversal.py)

**Section sources**
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [kg_traversal.py](file://kg/kg_traversal.py)

### Analytical Functions
- Centrality Measures
  - Degree centrality: local connectivity strength.
  - Betweenness centrality: importance based on shortest-path usage.
  - Closeness centrality: proximity to all other nodes.
- Community Detection
  - Algorithms to partition the graph into cohesive clusters.
  - Useful for summarization and targeted analytics.
- Graph Metrics
  - Global statistics like density, average degree, connected components, and diameter approximations.

```mermaid
classDiagram
class Traversal {
+bfs(start_nodes, filters, max_depth)
+dfs(start_nodes, filters, max_depth)
+shortest_path(source, target, constraints)
}
class Analytics {
+degree_centrality(nodes)
+betweenness_centrality(nodes, edges)
+closeness_centrality(nodes, edges)
+graph_metrics()
}
class Communities {
+detect_communities(edges, resolution)
+community_stats(community_id)
}
class Search {
+multi_hop_query(pattern, filters, limit, offset)
+pattern_match(query_spec)
}
Traversal <.. Search : "used by"
Analytics <.. Search : "used by"
Communities <.. Search : "used by"
```

**Diagram sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)

**Section sources**
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)

### Query Language Syntax and Result Formatting
- Syntax Elements
  - Node and edge selectors with labels/types and attribute filters.
  - Hop specifications with min/max depth and directionality.
  - Aggregation and scoring options for ranking results.
- Result Format
  - Structured responses containing matched paths, scores, and metadata.
  - Consistent field naming for downstream processing.
- Pagination
  - Offset and limit parameters to paginate large result sets.
  - Cursor-based pagination for stable ordering across pages.

```mermaid
flowchart TD
Parse["Parse Query Spec"] --> Validate["Validate Constraints"]
Validate --> BuildPlan["Build Traversal Plan"]
BuildPlan --> Execute["Execute Traversal"]
Execute --> Score["Score & Rank Results"]
Score --> Paginate["Apply Pagination"]
Paginate --> Format["Format Response"]
Format --> Return["Return JSON-like Result"]
```

**Diagram sources**
- [kg_search.py](file://knowledge_graph/kg_search.py)

**Section sources**
- [kg_search.py](file://knowledge_graph/kg_search.py)

### Practical Examples
- Find all entities related to a concept within two hops, filtered by type and recency.
- Compute betweenness centrality for top-k nodes to identify influential connectors.
- Detect communities around a seed node set and summarize cluster characteristics.
- Pattern match a sequence of relationships to extract workflows or processes.

[No sources needed since this section provides conceptual examples]

## Dependency Analysis
The traversal and analytics modules depend on the database layer for reading graph structures. The search layer composes these modules to implement high-level queries. MCP wraps the search layer for external integration.

```mermaid
graph TB
T["kg_traversal.py"] --> DB["kg_db.py"]
A["graph_analytics.py"] --> DB
C["graph_communities.py"] --> DB
S["kg_search.py"] --> T
S --> A
S --> C
M["mcp_kg_traversal.py"] --> S
DB --> SCHEMA["kg_schema.py"]
```

**Diagram sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)

**Section sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)

## Performance Considerations
- Indexing Strategies
  - Ensure indexes on frequently filtered attributes (node labels, edge types, timestamps).
  - Use composite indexes for common filter combinations to reduce scan costs.
- Traversal Optimization
  - Apply early filters to prune branches aggressively.
  - Limit max depth and use bidirectional search for shortest paths when feasible.
- Analytics Efficiency
  - Cache centrality and community results incrementally; recompute only affected regions after updates.
  - Approximate metrics for very large graphs to balance accuracy and latency.
- Query Design
  - Prefer specific patterns and constraints to minimize candidate sets.
  - Use pagination and cursors to avoid loading entire result sets.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
- Common Issues
  - Excessive memory usage during deep traversals: reduce depth limits and add stronger filters.
  - Slow queries due to missing indexes: review filter predicates and add appropriate indexes.
  - Inconsistent results after writes: ensure transactions and snapshots are used consistently.
- Debugging Tools
  - Enable detailed logs for traversal plans and execution times.
  - Inspect intermediate results to validate pattern matching correctness.

**Section sources**
- [test_kg_traversal.py](file://tests/test_kg_traversal.py)
- [test_multi_hop_traversal.py](file://tests/test_multi_hop_traversal.py)

## Conclusion
The graph traversal and query subsystem provides robust primitives for path finding, multi-hop queries, and pattern matching, complemented by analytical functions for centrality, community detection, and global metrics. By combining careful query design, effective indexing, and incremental analytics, the system scales to large graphs while maintaining responsiveness. MCP exposure enables seamless integration with external applications.

## Appendices

### Appendix A: Query Language Reference
- Selectors
  - Nodes: label/type filters, attribute conditions, temporal windows.
  - Edges: relationship types, directionality, weight thresholds.
- Hops
  - Min/max depth, repeat constraints, branching factors.
- Filters and Scoring
  - Boolean expressions over attributes and computed features.
  - Ranking strategies (recency, relevance, centrality boost).
- Pagination
  - Offset/limit and cursor-based modes.

[No sources needed since this section provides conceptual reference]

### Appendix B: Example Workflows
- Workflow 1: Influence Analysis
  - Compute degree and betweenness centrality for top-k nodes.
  - Visualize communities around high-betweenness nodes.
- Workflow 2: Process Discovery
  - Define a pattern representing a workflow.
  - Match occurrences and aggregate frequencies.

[No sources needed since this section provides conceptual examples]