# CRDT Implementation Details

<cite>
**Referenced Files in This Document**
- [crdt/__init__.py](file://crdt/__init__.py)
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [save/crdt_helpers.py](file://save/crdt_helpers.py)
- [migrations/013_field_level_crdt.sql](file://migrations/013_field_level_crdt.sql)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)
- [migrations/051_field_crdt_tenant_id.sql](file://migrations/051_field_crdt_tenant_id.sql)
- [migrations/055_kg_crdt_tenant_id.sql](file://migrations/055_kg_crdt_tenant_id.sql)
- [migrations/065_kg_crdt_append_only.sql](file://migrations/065_kg_crdt_append_only.sql)
- [migrations/066_kg_crdt_tenant_id.sql](file://migrations/066_kg_crdt_tenant_id.sql)
- [migrations/073_kg_crdt_redirect_writes_to_append_tables.sql](file://migrations/073_kg_crdt_redirect_writes_to_append_tables.sql)
- [test/test_crdt_field.py](file://test/test_crdt_field.py)
- [test/test_crdt_merge.py](file://test/test_crdt_merge.py)
- [test/test_crdt_integration.py](file://test/test_crdt_integration.py)
- [test/test_crdt_sync.py](file://test/test_crdt_sync.py)
- [test/test_cron_crdt_sync.py](file://test/test_cron_crdt_sync.py)
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
This document explains the Conflict-Free Replicated Data Types (CRDT) implementation with a focus on content-keyed design, field-level operations, merge algorithms, and distributed consistency guarantees. It covers schema design, operation encoding/decoding, state reconciliation, synchronization workflows, and practical guidance for implementing custom CRDT types, handling network partitions, and debugging synchronization issues. Performance characteristics, memory usage patterns, and scalability limits are also addressed.

## Project Structure
The CRDT subsystem is organized into focused modules:
- crdt: Core CRDT primitives and merge logic
- kg: Knowledge graph integration with CRDT-backed entities
- cron: Scheduled synchronization tasks
- save: Helpers to integrate CRDT mutations into the save pipeline
- migrations: Database schema evolution for CRDT storage

```mermaid
graph TB
subgraph "CRDT Core"
A["crdt/__init__.py"]
B["crdt/crdt_field.py"]
C["crdt/crdt_merge.py"]
end
subgraph "Knowledge Graph Integration"
D["kg/kg_crdt.py"]
end
subgraph "Scheduling"
E["cron/cron_crdt_sync.py"]
end
subgraph "Save Pipeline"
F["save/crdt_helpers.py"]
end
subgraph "Schema"
G["migrations/013_field_level_crdt.sql"]
H["migrations/021_kg_crdt.sql"]
I["migrations/051_field_crdt_tenant_id.sql"]
J["migrations/055_kg_crdt_tenant_id.sql"]
K["migrations/065_kg_crdt_append_only.sql"]
L["migrations/066_kg_crdt_tenant_id.sql"]
M["migrations/073_kg_crdt_redirect_writes_to_append_tables.sql"]
end
A --> B
A --> C
D --> B
D --> C
E --> D
F --> B
F --> C
D --> G
D --> H
D --> I
D --> J
D --> K
D --> L
D --> M
```

**Diagram sources**
- [crdt/__init__.py](file://crdt/__init__.py)
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [save/crdt_helpers.py](file://save/crdt_helpers.py)
- [migrations/013_field_level_crdt.sql](file://migrations/013_field_level_crdt.sql)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)
- [migrations/051_field_crdt_tenant_id.sql](file://migrations/051_field_crdt_tenant_id.sql)
- [migrations/055_kg_crdt_tenant_id.sql](file://migrations/055_kg_crdt_tenant_id.sql)
- [migrations/065_kg_crdt_append_only.sql](file://migrations/065_kg_crdt_append_only.sql)
- [migrations/066_kg_crdt_tenant_id.sql](file://migrations/066_kg_crdt_tenant_id.sql)
- [migrations/073_kg_crdt_redirect_writes_to_append_tables.sql](file://migrations/073_kg_crdt_redirect_writes_to_append_tables.sql)

**Section sources**
- [crdt/__init__.py](file://crdt/__init__.py)
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [save/crdt_helpers.py](file://save/crdt_helpers.py)
- [migrations/013_field_level_crdt.sql](file://migrations/013_field_level_crdt.sql)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)
- [migrations/051_field_crdt_tenant_id.sql](file://migrations/051_field_crdt_tenant_id.sql)
- [migrations/055_kg_crdt_tenant_id.sql](file://migrations/055_kg_crdt_tenant_id.sql)
- [migrations/065_kg_crdt_append_only.sql](file://migrations/065_kg_crdt_append_only.sql)
- [migrations/066_kg_crdt_tenant_id.sql](file://migrations/066_kg_crdt_tenant_id.sql)
- [migrations/073_kg_crdt_redirect_writes_to_append_tables.sql](file://migrations/073_kg_crdt_redirect_writes_to_append_tables.sql)

## Core Components
- Content-keyed CRDT model: Entities are identified by stable content-derived keys rather than mutable IDs, enabling deterministic merging across replicas.
- Field-level CRDTs: Each field maintains its own conflict-free state, allowing independent updates and merges without global locks.
- Merge algorithm: Deterministic, commutative, and idempotent merge semantics ensure convergence regardless of update order or delivery duplicates.
- Vector clock synchronization: Logical timestamps capture causal relationships between updates, enabling efficient diff computation and consistent reconciliation.
- Distributed consistency guarantees: Strong eventual consistency is achieved through content-keying, field-level independence, and monotonic vector clocks.

Key responsibilities:
- crdt_field: Defines field-level CRDT structures and operations (e.g., append-only counters, last-writer-wins with vector clocks).
- crdt_merge: Implements merge functions that combine two states deterministically.
- kg_crdt: Integrates CRDT-backed knowledge graph entities with persistence and indexing.
- cron_crdt_sync: Schedules periodic synchronization jobs to exchange diffs and reconcile state.
- save/crdt_helpers: Bridges application writes to CRDT mutation pipelines.

**Section sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [save/crdt_helpers.py](file://save/crdt_helpers.py)

## Architecture Overview
The CRDT architecture separates concerns across core primitives, domain integration, scheduling, and persistence.

```mermaid
sequenceDiagram
participant App as "Application"
participant Save as "save/crdt_helpers.py"
participant Field as "crdt/crdt_field.py"
participant Merge as "crdt/crdt_merge.py"
participant KG as "kg/kg_crdt.py"
participant Cron as "cron/cron_crdt_sync.py"
participant DB as "Migrations (CRDT tables)"
App->>Save : "Write mutation"
Save->>Field : "Apply field-level CRDT op"
Field-->>Save : "Updated local state + vector clock"
Save->>KG : "Persist entity with content key"
KG->>DB : "Append-only records / snapshots"
Cron->>KG : "Fetch pending diffs"
Cron->>Cron : "Compute delta using vector clocks"
Cron->>Merge : "Merge remote state into local"
Merge-->>Cron : "Converged state"
Cron->>DB : "Persist merged state"
```

**Diagram sources**
- [save/crdt_helpers.py](file://save/crdt_helpers.py)
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [migrations/013_field_level_crdt.sql](file://migrations/013_field_level_crdt.sql)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)
- [migrations/065_kg_crdt_append_only.sql](file://migrations/065_kg_crdt_append_only.sql)
- [migrations/073_kg_crdt_redirect_writes_to_append_tables.sql](file://migrations/073_kg_crdt_redirect_writes_to_append_tables.sql)

## Detailed Component Analysis

### Content-Keyed Entity Model
Content-keyed entities use deterministic identifiers derived from their content or semantic fingerprint. This ensures that replicas can match and merge the same logical entity without coordination.

- Key derivation: Stable hashing over canonicalized fields.
- Identity resolution: Lookup by content key instead of mutable IDs.
- Convergence: Same content yields same identity; merges operate on identical keys across replicas.

```mermaid
flowchart TD
Start(["Entity Mutation"]) --> Canonicalize["Canonicalize fields"]
Canonicalize --> Hash["Derive content key"]
Hash --> Store["Store under content key"]
Store --> End(["Ready for sync"])
```

**Diagram sources**
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)

**Section sources**
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)

### Field-Level CRDT Operations
Field-level CRDTs enable fine-grained concurrency control and reduce merge overhead.

- Append-only lists: Commutative concatenation with deduplication via content keys.
- Counters: Monotonic increments with vector clock ordering.
- Last-writer-wins with causality: Timestamps include vector clocks to resolve conflicts deterministically.

```mermaid
classDiagram
class FieldCRDT {
+apply(op)
+merge(other)
+to_snapshot()
+vector_clock()
}
class AppendList {
+append(item)
+remove(item)
+merge(other)
}
class Counter {
+increment(delta)
+merge(other)
}
class LWWWithVC {
+set(value, vc)
+get()
+merge(other)
}
FieldCRDT <|-- AppendList
FieldCRDT <|-- Counter
FieldCRDT <|-- LWWWithVC
```

**Diagram sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)

**Section sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)

### Merge Algorithms and Conflict Resolution
The merge function combines two states deterministically, ensuring commutativity and idempotence.

- Pairwise merge: For each field, apply type-specific merge rules.
- Vector clock join: Combine clocks to reflect union of causal histories.
- Deduplication: Use content keys to avoid duplicate entries in append-only structures.

```mermaid
flowchart TD
Enter(["Merge(a, b)"]) --> JoinVC["Join vector clocks"]
JoinVC --> Fields{"For each field"}
Fields --> |AppendList| MergeAppend["Deduplicate and concatenate"]
Fields --> |Counter| MergeCount["Sum deltas"]
Fields --> |LWWWithVC| MergeLWW["Compare timestamps + VC"]
MergeAppend --> Result["Assemble merged state"]
MergeCount --> Result
MergeLWW --> Result
Result --> Exit(["Return merged state"])
```

**Diagram sources**
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [crdt/crdt_field.py](file://crdt/crdt_field.py)

**Section sources**
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [crdt/crdt_field.py](file://crdt/crdt_field.py)

### Vector Clock Synchronization
Vector clocks track causal relationships across replicas.

- Increment per write: Each replica increments its own component when writing.
- Diff computation: Compare clocks to identify missing updates.
- Reconciliation: Exchange only necessary deltas to minimize bandwidth.

```mermaid
sequenceDiagram
participant R1 as "Replica 1"
participant R2 as "Replica 2"
R1->>R1 : "Increment local VC component"
R1->>R2 : "Send diff (updates since last VC)"
R2->>R2 : "Join VC with R1's VC"
R2->>R2 : "Apply received updates"
R2->>R1 : "Acknowledge with updated VC"
```

**Diagram sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

**Section sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

### Schema Design and Persistence
CRDT data is persisted using append-only tables and tenant-scoped indexes.

- Field-level CRDT table: Stores per-field operations and snapshots.
- Knowledge graph CRDT table: Stores entity-level CRDT state keyed by content.
- Tenant isolation: Separate namespaces for multi-tenant environments.
- Append-only enforcement: Prevents in-place mutations to preserve history.

```mermaid
erDiagram
FIELD_CRDT {
uuid id PK
string entity_key
string field_name
jsonb op_payload
jsonb vector_clock
timestamp created_at
}
KG_CRDT {
uuid id PK
string content_key
jsonb state
jsonb vector_clock
timestamp created_at
}
FIELD_CRDT ||--o{ KG_CRDT : "references entity"
```

**Diagram sources**
- [migrations/013_field_level_crdt.sql](file://migrations/013_field_level_crdt.sql)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)
- [migrations/051_field_crdt_tenant_id.sql](file://migrations/051_field_crdt_tenant_id.sql)
- [migrations/055_kg_crdt_tenant_id.sql](file://migrations/055_kg_crdt_tenant_id.sql)
- [migrations/065_kg_crdt_append_only.sql](file://migrations/065_kg_crdt_append_only.sql)
- [migrations/066_kg_crdt_tenant_id.sql](file://migrations/066_kg_crdt_tenant_id.sql)
- [migrations/073_kg_crdt_redirect_writes_to_append_tables.sql](file://migrations/073_kg_crdt_redirect_writes_to_append_tables.sql)

**Section sources**
- [migrations/013_field_level_crdt.sql](file://migrations/013_field_level_crdt.sql)
- [migrations/021_kg_crdt.sql](file://migrations/021_kg_crdt.sql)
- [migrations/051_field_crdt_tenant_id.sql](file://migrations/051_field_crdt_tenant_id.sql)
- [migrations/055_kg_crdt_tenant_id.sql](file://migrations/055_kg_crdt_tenant_id.sql)
- [migrations/065_kg_crdt_append_only.sql](file://migrations/065_kg_crdt_append_only.sql)
- [migrations/066_kg_crdt_tenant_id.sql](file://migrations/066_kg_crdt_tenant_id.sql)
- [migrations/073_kg_crdt_redirect_writes_to_append_tables.sql](file://migrations/073_kg_crdt_redirect_writes_to_append_tables.sql)

### Operation Encoding/Decoding
Operations are serialized for transport and storage.

- Encoding: Convert field-level ops to JSON payloads with vector clocks.
- Decoding: Parse incoming payloads and validate structure before applying.
- Versioning: Include schema version to support evolution.

```mermaid
flowchart TD
EncodeStart(["Encode op"]) --> Serialize["Serialize payload + VC"]
Serialize --> Transport["Transmit to peer"]
DecodeStart(["Decode op"]) --> Validate["Validate schema version"]
Validate --> Apply["Apply to local CRDT"]
Apply --> Commit["Commit to append-only store"]
```

**Diagram sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

**Section sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

### State Reconciliation Process
Reconciliation merges remote state into local state while preserving causality.

- Fetch remote diffs based on vector clocks.
- Apply incremental updates to local CRDTs.
- Recompute snapshots if needed for compact reads.

```mermaid
sequenceDiagram
participant Local as "Local Replica"
participant Remote as "Remote Replica"
Local->>Remote : "Request diff since last VC"
Remote-->>Local : "Diff payload"
Local->>Local : "Apply diff to field CRDTs"
Local->>Local : "Join vector clocks"
Local->>Local : "Persist merged state"
```

**Diagram sources**
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)

**Section sources**
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)

### Practical Examples

#### Implementing Custom CRDT Types
- Define a new field CRDT class with apply, merge, and snapshot methods.
- Ensure commutativity and idempotence for all operations.
- Integrate with the merge dispatcher in the merge module.

```mermaid
classDiagram
class CustomCRDT {
+apply(op)
+merge(other)
+to_snapshot()
+vector_clock()
}
class MergeDispatcher {
+dispatch(field_type, left, right)
}
MergeDispatcher --> CustomCRDT : "uses"
```

**Diagram sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

**Section sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

#### Handling Network Partitions
- Buffer local writes during partition.
- On reconnection, compute minimal diffs using vector clocks.
- Resolve conflicts deterministically via merge rules.

```mermaid
flowchart TD
Partition["Network Partition Detected"] --> Buffer["Buffer local ops"]
Buffer --> Reconnect["Reconnect to peers"]
Reconnect --> Diff["Compute diffs via VC"]
Diff --> Merge["Merge remote updates"]
Merge --> Flush["Flush buffered ops"]
```

**Diagram sources**
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

**Section sources**
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)

#### Debugging Synchronization Issues
- Inspect vector clocks for causal gaps.
- Verify content keys for identity mismatches.
- Review append-only logs for duplicate or out-of-order ops.

```mermaid
flowchart TD
Start(["Sync Issue"]) --> CheckVC["Check vector clocks"]
CheckVC --> CheckKeys["Verify content keys"]
CheckKeys --> CheckLogs["Inspect append-only logs"]
CheckLogs --> Fix["Adjust merge rules or schema"]
```

**Diagram sources**
- [test/test_crdt_sync.py](file://test/test_crdt_sync.py)
- [test/test_cron_crdt_sync.py](file://test/test_cron_crdt_sync.py)

**Section sources**
- [test/test_crdt_sync.py](file://test/test_crdt_sync.py)
- [test/test_cron_crdt_sync.py](file://test/test_cron_crdt_sync.py)

## Dependency Analysis
The CRDT system exhibits clear separation between core primitives and domain integrations.

```mermaid
graph TB
Core["crdt/crdt_field.py"] --> Merge["crdt/crdt_merge.py"]
Core --> SaveHelpers["save/crdt_helpers.py"]
KG["kg/kg_crdt.py"] --> Core
KG --> Merge
Cron["cron/cron_crdt_sync.py"] --> KG
Cron --> Merge
```

**Diagram sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [save/crdt_helpers.py](file://save/crdt_helpers.py)

**Section sources**
- [crdt/crdt_field.py](file://crdt/crdt_field.py)
- [crdt/crdt_merge.py](file://crdt/crdt_merge.py)
- [kg/kg_crdt.py](file://kg/kg_crdt.py)
- [cron/cron_crdt_sync.py](file://cron/cron_crdt_sync.py)
- [save/crdt_helpers.py](file://save/crdt_helpers.py)

## Performance Considerations
- Memory usage: Prefer append-only logs with periodic compaction to bound memory growth.
- Merge complexity: Keep field-level CRDTs small; use content-keyed deduplication to reduce merge size.
- Sync bandwidth: Minimize diffs by leveraging vector clocks and incremental updates.
- Indexing: Maintain lightweight indexes on content keys and vector clocks for fast lookups.
- Scalability: Shard by tenant and entity key; parallelize merges across independent fields.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and resolutions:
- Duplicate entries in append-only lists: Ensure content-keyed deduplication is applied during merge.
- Stale reads after merge: Recompute snapshots or refresh read caches post-reconciliation.
- Non-convergence: Verify commutativity and idempotence of merge functions; check vector clock joins.
- Tenant leakage: Confirm tenant-scoped queries and indexes are enforced in all paths.

**Section sources**
- [test/test_crdt_field.py](file://test/test_crdt_field.py)
- [test/test_crdt_merge.py](file://test/test_crdt_merge.py)
- [test/test_crdt_integration.py](file://test/test_crdt_integration.py)

## Conclusion
The CRDT implementation leverages content-keyed identities, field-level independence, and vector clock synchronization to achieve strong eventual consistency across distributed replicas. The append-only persistence model and deterministic merge algorithms provide robustness against network partitions and concurrent writes. By following the guidelines for custom CRDT types, careful schema design, and targeted troubleshooting, teams can scale CRDT-backed systems effectively while maintaining correctness and performance.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices

### API and Workflow References
- Field-level CRDT tests: [test/test_crdt_field.py](file://test/test_crdt_field.py)
- Merge behavior tests: [test/test_crdt_merge.py](file://test/test_crdt_merge.py)
- Integration tests: [test/test_crdt_integration.py](file://test/test_crdt_integration.py)
- Sync workflow tests: [test/test_crdt_sync.py](file://test/test_crdt_sync.py)
- Cron sync tests: [test/test_cron_crdt_sync.py](file://test/test_cron_crdt_sync.py)

**Section sources**
- [test/test_crdt_field.py](file://test/test_crdt_field.py)
- [test/test_crdt_merge.py](file://test/test_crdt_merge.py)
- [test/test_crdt_integration.py](file://test/test_crdt_integration.py)
- [test/test_crdt_sync.py](file://test/test_crdt_sync.py)
- [test/test_cron_crdt_sync.py](file://test/test_cron_crdt_sync.py)