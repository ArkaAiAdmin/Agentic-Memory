# Knowledge Graph Visualization

<cite>
**Referenced Files in This Document**
- [kg.py](file://agentic_memory/kg.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_crdt.py](file://kg/kg_crdt.py)
- [kg_dedup.py](file://kg/kg_dedup.py)
- [kg_backfill.py](file://backfill/kg_backfills.py)
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [test_kg_traversal.py](file://tests/test_kg_traversal.py)
- [test_multi_hop_traversal.py](file://tests/test_multi_hop_traversal.py)
- [test_session_clustering_enhancement.py](file://tests/test_session_clustering_enhancement.py)
- [test_kg_entity_filter.py](file://tests/test_kg_entity_filter.py)
- [test_kg_validation.py](file://tests/test_kg_validation.py)
- [test_kg_analytics_off_save_path.py](file://tests/test_kg_analytics_off_save_path.py)
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
This document describes the knowledge graph visualization interface and its interactive exploration features. It explains how to browse entities, filter nodes, highlight relationships, traverse paths, cluster concepts, detect communities, query the graph, export data, and analyze topology. It also covers analytics such as centrality measures, density analysis, and temporal evolution tracking, along with performance optimization strategies for large graphs, zoom controls, and export formats.

## Project Structure
The knowledge graph visualization is implemented across several modules:
- Dashboard UI integration for browsing and interacting with the graph
- MCP endpoints exposing traversal and analytics operations
- Core graph algorithms for traversal, community detection, and analytics
- Persistence and schema layers for storing graph data
- Background jobs for backfilling and periodic analytics computation

```mermaid
graph TB
subgraph "Dashboard"
DK["tab_knowledge.py"]
end
subgraph "MCP Endpoints"
MKG["mcp_kg.py"]
MKGT["mcp_kg_traversal.py"]
end
subgraph "Graph Core"
KG["kg.py"]
KGT["kg_traversal.py"]
GA["graph_analytics.py"]
GC["graph_communities.py"]
TR["temporal_resolver.py"]
CRDT["kg_crdt.py"]
DEDUP["kg_dedup.py"]
end
subgraph "Persistence & Schema"
KDB["kg_db.py"]
KSS["kg_schema.py"]
KEX["kg_extract.py"]
end
subgraph "Background Jobs"
CBA["cron_kg_analytics.py"]
KB["cron_kg_backfill.py"]
KBM["cron_kg_backfill_monitor.py"]
BKB["kg_backfills.py"]
end
DK --> MKG
DK --> MKGT
MKG --> KG
MKGT --> KGT
KGT --> KDB
GA --> KDB
GC --> KDB
TR --> KDB
CRDT --> KDB
DEDUP --> KDB
CBA --> GA
KB --> KDB
KBM --> KB
BKB --> KDB
```

**Diagram sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg.py](file://agentic_memory/kg.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_crdt.py](file://kg/kg_crdt.py)
- [kg_dedup.py](file://kg/kg_dedup.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [kg_backfills.py](file://backfill/kg_backfills.py)

**Section sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg.py](file://agentic_memory/kg.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_crdt.py](file://kg/kg_crdt.py)
- [kg_dedup.py](file://kg/kg_dedup.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [kg_backfills.py](file://backfill/kg_backfills.py)

## Core Components
- Interactive graph browser (Dashboard): Provides entity browsing, node filtering, relationship highlighting, path traversal, clustering views, and community detection overlays.
- MCP endpoints: Expose programmatic access to graph queries, traversal, and analytics results for automation and integrations.
- Traversal engine: Implements breadth-first and multi-hop traversal with filters and constraints.
- Analytics engine: Computes centrality measures, density metrics, and supports temporal evolution tracking via snapshots or time-bounded queries.
- Community detection: Identifies clusters and communities using graph partitioning algorithms.
- Temporal resolution: Supports time-aware queries and evolution tracking over graph snapshots.
- Persistence layer: Stores nodes, edges, metadata, and computed analytics; integrates with schema definitions and extraction pipelines.
- Background jobs: Backfill historical graph data and periodically recompute analytics.

**Section sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [kg_backfills.py](file://backfill/kg_backfills.py)

## Architecture Overview
The visualization architecture separates concerns between UI, API, algorithms, and storage:
- The dashboard tab renders the graph and handles user interactions (filtering, highlighting, traversal).
- MCP endpoints translate UI actions into server-side operations (queries, traversal, analytics).
- The traversal and analytics engines operate on the persisted graph model.
- Background jobs maintain data freshness and precompute expensive metrics.

```mermaid
sequenceDiagram
participant UI as "Dashboard UI"
participant MCP as "MCP Endpoints"
participant TRV as "Traversal Engine"
participant ANA as "Analytics Engine"
participant COMM as "Community Detection"
participant DB as "KG Database"
UI->>MCP : "Request filtered nodes/edges"
MCP->>DB : "Query by filters"
DB-->>MCP : "Filtered subgraph"
MCP-->>UI : "Renderable graph data"
UI->>MCP : "Traverse from seed(s)"
MCP->>TRV : "Start traversal with constraints"
TRV->>DB : "Fetch neighbors iteratively"
DB-->>TRV : "Neighbor sets"
TRV-->>MCP : "Path set"
MCP-->>UI : "Highlighted paths"
UI->>MCP : "Compute analytics"
MCP->>ANA : "Centrality/density"
ANA->>DB : "Read graph stats"
DB-->>ANA : "Counts, degrees"
ANA-->>MCP : "Metrics"
MCP-->>UI : "Analytics overlay"
UI->>MCP : "Detect communities"
MCP->>COMM : "Run partitioning"
COMM->>DB : "Read components"
DB-->>COMM : "Subgraphs"
COMM-->>MCP : "Cluster assignments"
MCP-->>UI : "Community coloring"
```

**Diagram sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

## Detailed Component Analysis

### Interactive Graph Exploration
- Node filtering: Filter by entity type, label patterns, attributes, and temporal windows.
- Relationship highlighting: Highlight edges based on relation types, weights, or traversal context.
- Path traversal: Start from seeds, expand by hops, apply constraints, and visualize shortest or all paths within limits.
- Entity browsing: Paginated listing, search, and drill-down into node details and incident edges.
- Concept clustering: Group semantically related nodes using extracted concepts or embeddings.
- Community detection: Visualize detected communities with distinct colors and cluster summaries.

```mermaid
flowchart TD
Start(["User Action"]) --> Filter["Apply Filters<br/>type, label, attributes, time window"]
Filter --> Query["Build Query"]
Query --> Result{"Results Found?"}
Result --> |No| Empty["Show empty state"]
Result --> |Yes| Render["Render Nodes/Edges"]
Render --> Interact["Interactions:<br/>Highlight, Expand, Traverse"]
Interact --> Traverse["Traverse from Seed(s)<br/>with hop limit and constraints"]
Traverse --> Paths["Compute Paths"]
Paths --> Overlay["Overlay Results<br/>paths, clusters, communities"]
Overlay --> Export["Export Data"]
Export --> End(["Done"])
Empty --> End
```

**Diagram sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_communities.py](file://kg/graph_communities.py)

**Section sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_communities.py](file://kg/graph_communities.py)

### Traversal Engine
- Breadth-first expansion with configurable depth and branching constraints.
- Multi-hop traversal supporting complex filters and early termination conditions.
- Path aggregation and deduplication to avoid redundant computations.
- Integration with temporal resolution for time-bounded traversals.

```mermaid
classDiagram
class TraversalEngine {
+expand(seed_nodes, max_hops, filters)
+collect_paths()
+apply_constraints()
-fetch_neighbors(node_id)
-deduplicate_paths(paths)
}
class TemporalResolver {
+as_of(timestamp)
+time_window(start, end)
}
class KGDatabase {
+get_neighbors(node_id, filters)
+count_edges()
+list_entities(filters)
}
TraversalEngine --> KGDatabase : "reads neighbors"
TraversalEngine --> TemporalResolver : "applies time bounds"
```

**Diagram sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

**Section sources**
- [kg_traversal.py](file://kg/kg_traversal.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [test_kg_traversal.py](file://tests/test_kg_traversal.py)
- [test_multi_hop_traversal.py](file://tests/test_multi_hop_traversal.py)

### Analytics Engine
- Centrality measures: Degree, betweenness, closeness, eigenvector-like approximations where applicable.
- Density analysis: Local and global density metrics, component sizes, and edge-to-node ratios.
- Temporal evolution: Track changes across snapshots or time windows to observe growth and decay.
- Precomputation: Cron job computes and caches metrics for faster UI rendering.

```mermaid
sequenceDiagram
participant UI as "Dashboard UI"
participant MCP as "MCP Endpoints"
participant ANA as "Analytics Engine"
participant DB as "KG Database"
UI->>MCP : "Request centrality/density"
MCP->>ANA : "Compute metrics"
ANA->>DB : "Read degree counts, edges"
DB-->>ANA : "Stats"
ANA-->>MCP : "Centrality scores, density"
MCP-->>UI : "Analytics overlay"
Note over ANA,DB : "Cron job periodically recomputes metrics"
```

**Diagram sources**
- [graph_analytics.py](file://kg/graph_analytics.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

**Section sources**
- [graph_analytics.py](file://kg/graph_analytics.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [test_kg_analytics_off_save_path.py](file://tests/test_kg_analytics_off_save_path.py)

### Community Detection
- Partitioning algorithm identifies cohesive subgraphs.
- Assigns community IDs to nodes for visual grouping.
- Integrates with clustering views and concept-based grouping.

```mermaid
flowchart TD
Input["Load Subgraph"] --> Detect["Run Community Detection"]
Detect --> Assign["Assign Community IDs"]
Assign --> Visualize["Color Nodes by Community"]
Visualize --> Summarize["Generate Cluster Summaries"]
Summarize --> Output["Return Communities"]
```

**Diagram sources**
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

**Section sources**
- [graph_communities.py](file://kg/graph_communities.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [test_session_clustering_enhancement.py](file://tests/test_session_clustering_enhancement.py)

### Temporal Evolution Tracking
- Time-bounded queries allow viewing the graph at specific timestamps or within intervals.
- Snapshots enable comparison across time points.
- Temporal resolver coordinates time windows for traversal and analytics.

```mermaid
sequenceDiagram
participant UI as "Dashboard UI"
participant MCP as "MCP Endpoints"
participant TR as "Temporal Resolver"
participant DB as "KG Database"
UI->>MCP : "View graph at timestamp"
MCP->>TR : "Resolve time window"
TR->>DB : "Query nodes/edges within window"
DB-->>TR : "Time-filtered data"
TR-->>MCP : "Resolved snapshot"
MCP-->>UI : "Render temporal view"
```

**Diagram sources**
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

**Section sources**
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

### Data Model and Schema
- Entities and relations are stored with typed labels and attributes.
- Schema defines constraints and indexes for efficient querying.
- Extraction pipeline populates the graph from structured/unstructured inputs.

```mermaid
erDiagram
ENTITY {
string id PK
string type
map attributes
timestamp created_at
timestamp updated_at
}
RELATION {
string id PK
string source_id FK
string target_id FK
string type
map attributes
timestamp observed_at
}
ENTITY ||--o{ RELATION : "source"
ENTITY ||--o{ RELATION : "target"
```

**Diagram sources**
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

**Section sources**
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

### MCP Endpoints for Graph Operations
- Programmatic access to traversal, filtering, and analytics.
- Standardized request/response contracts for client applications.
- Supports exporting results in multiple formats.

```mermaid
sequenceDiagram
participant Client as "Client App"
participant MCP as "MCP Endpoints"
participant TRV as "Traversal Engine"
participant ANA as "Analytics Engine"
participant DB as "KG Database"
Client->>MCP : "POST /kg/traverse"
MCP->>TRV : "Execute traversal"
TRV->>DB : "Fetch neighbors"
DB-->>TRV : "Neighbors"
TRV-->>MCP : "Paths"
MCP-->>Client : "JSON paths"
Client->>MCP : "POST /kg/analytics"
MCP->>ANA : "Compute metrics"
ANA->>DB : "Read stats"
DB-->>ANA : "Counts"
ANA-->>MCP : "Metrics"
MCP-->>Client : "JSON analytics"
```

**Diagram sources**
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

**Section sources**
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)

### Background Jobs and Backfills
- Backfill jobs reconstruct historical graph data and ensure consistency.
- Monitor jobs track progress and handle failures.
- Periodic analytics jobs keep metrics up to date.

```mermaid
flowchart TD
Schedule["Cron Scheduler"] --> Backfill["Backfill Job"]
Backfill --> Extract["Extract Entities/Relations"]
Extract --> Persist["Persist to DB"]
Persist --> Validate["Validate Integrity"]
Validate --> Done(["Backfill Complete"])
Schedule --> Monitor["Monitor Job"]
Monitor --> Status["Report Progress"]
Status --> Done
```

**Diagram sources**
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [kg_backfills.py](file://backfill/kg_backfills.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)

**Section sources**
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [kg_backfills.py](file://backfill/kg_backfills.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)

## Dependency Analysis
Key dependencies and coupling:
- Dashboard depends on MCP endpoints for data and operations.
- MCP endpoints depend on traversal and analytics engines.
- Engines depend on the database layer and temporal resolver.
- Background jobs depend on persistence and extraction utilities.
- Deduplication and CRDT layers ensure consistency and conflict-free updates.

```mermaid
graph TB
UI["Dashboard UI"] --> MCP["MCP Endpoints"]
MCP --> TRV["Traversal Engine"]
MCP --> ANA["Analytics Engine"]
MCP --> COMM["Community Detection"]
TRV --> DB["KG Database"]
ANA --> DB
COMM --> DB
TRV --> TR["Temporal Resolver"]
DB --> SCHEMA["Schema"]
DB --> EXTRACT["Extraction"]
DB --> CRDT["CRDT Layer"]
DB --> DEDUP["Dedup Layer"]
BG["Background Jobs"] --> DB
BG --> EXTRACT
```

**Diagram sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [kg_crdt.py](file://kg/kg_crdt.py)
- [kg_dedup.py](file://kg/kg_dedup.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [kg_backfills.py](file://backfill/kg_backfills.py)

**Section sources**
- [tab_knowledge.py](file://dashboard/tab_knowledge.py)
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_schema.py](file://knowledge_graph/kg_schema.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)
- [kg_crdt.py](file://kg/kg_crdt.py)
- [kg_dedup.py](file://kg/kg_dedup.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [cron_kg_backfill.py](file://cron/cron_kg_backfill.py)
- [cron_kg_backfill_monitor.py](file://cron/cron_kg_backfill_monitor.py)
- [kg_backfills.py](file://backfill/kg_backfills.py)

## Performance Considerations
- Large graph handling:
  - Use pagination and progressive loading for nodes and edges.
  - Apply strict filters (type, label, time window) to reduce result sets.
  - Limit traversal depth and branch factors; prefer targeted seeds.
- Indexing and queries:
  - Ensure indexes exist on frequently filtered fields (entity type, labels, timestamps).
  - Prefer precomputed analytics via cron jobs to avoid heavy on-demand calculations.
- Rendering and UX:
  - Implement zoom controls and viewport culling to render only visible regions.
  - Batch updates and debounce user interactions to prevent UI jank.
- Export formats:
  - Provide JSON, CSV, and GraphML exports for interoperability.
  - Allow selective export (filtered subgraphs) to manage file size.
- Concurrency and caching:
  - Cache frequent queries and analytics results.
  - Use background workers for long-running operations.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
- Validation errors:
  - Check entity and relation schema constraints when imports fail.
  - Review deduplication logs for conflicts and merges.
- Traversal issues:
  - Verify seed nodes exist and have expected neighbor sets.
  - Inspect hop limits and constraint filters that may prune paths.
- Analytics discrepancies:
  - Confirm cron jobs ran successfully and metrics were updated.
  - Compare on-demand vs cached metrics to identify staleness.
- Temporal queries:
  - Ensure timestamps are present and consistent across nodes and edges.
  - Validate time window boundaries and timezone handling.

**Section sources**
- [test_kg_validation.py](file://tests/test_kg_validation.py)
- [kg_dedup.py](file://kg/kg_dedup.py)
- [kg_traversal.py](file://kg/kg_traversal.py)
- [cron_kg_analytics.py](file://cron/cron_kg_analytics.py)
- [temporal_resolver.py](file://kg/temporal_resolver.py)

## Conclusion
The knowledge graph visualization interface combines a responsive dashboard, robust MCP endpoints, and powerful backend engines for traversal, analytics, and community detection. With careful filtering, indexing, and background computation, it scales to large graphs while providing rich interactive exploration, including path traversal, clustering, and temporal evolution tracking. Export capabilities and performance optimizations ensure usability across diverse workflows.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices

### Querying the Graph
- Use MCP endpoints to submit filtered queries, specify time windows, and request traversal results.
- Combine filters for entity types, labels, and attributes to narrow scope.
- Request analytics overlays for centrality and density insights.

**Section sources**
- [mcp_kg.py](file://mcp_kg.py)
- [mcp_kg_traversal.py](file://mcp_kg_traversal.py)
- [kg_search.py](file://knowledge_graph/kg_search.py)

### Exporting Graph Data
- Supported formats include JSON, CSV, and GraphML.
- Selective export allows downloading filtered subgraphs.
- Background export tasks can be used for large datasets.

**Section sources**
- [kg_db.py](file://knowledge_graph/kg_db.py)
- [kg_extract.py](file://knowledge_graph/kg_extract.py)

### Analyzing Graph Topology
- Centrality measures highlight influential nodes.
- Density analysis reveals connectivity patterns.
- Community detection surfaces cohesive clusters.

**Section sources**
- [graph_analytics.py](file://kg/graph_analytics.py)
- [graph_communities.py](file://kg/graph_communities.py)