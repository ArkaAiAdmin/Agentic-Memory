# Search Pipeline Overview

<cite>
**Referenced Files in This Document**
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/config.py](file://search/config.py)
- [search/query_parser.py](file://search/query_parser.py)
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py)
- [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py)
- [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)
- [search/rerankers.py](file://search/rerankers.py)
- [search/scoring.py](file://search/scoring.py)
- [search/state.py](file://search/state.py)
- [infra/fts.py](file://infra/fts.py)
- [infra/vector_store.py](file://infra/vector_store.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)
- [recall/search_memory.py](file://recall/search_memory.py)
- [docs/concepts/search-pipeline.md](file://docs/concepts/search-pipeline.md)
- [docs/how-to/debug-search.md](file://docs/how-to/debug-search.md)
- [eval/run_eval_main_pipeline.py](file://eval/run_eval_main_pipeline.py)
- [eval/hybrid_strategy.py](file://eval/hybrid_strategy.py)
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
This document explains the multi-phase search pipeline architecture, focusing on the hybrid approach that combines BM25 keyword matching with vector similarity search. It covers how phases are orchestrated, how results are fused and ranked, how queries are parsed, and how to configure and tune performance. Practical examples demonstrate query construction, phase customization, and result interpretation. The guide also includes optimization strategies, caching approaches, and debugging techniques for diagnosing pipeline issues.

## Project Structure
The search subsystem is organized around a central orchestrator that composes multiple retrieval phases (BM25, vector, SPLADE, Colbert, knowledge graph facts, session memory, temporal filters, skill lookup). Each phase returns candidate items with scores; an aggregation and reranking stage fuses these candidates into a final ordered list. Supporting modules provide configuration parsing, query parsing, scoring utilities, and state management.

```mermaid
graph TB
subgraph "Search Core"
Orchestrator["Orchestrator<br/>composes phases"]
Config["Config<br/>phase options"]
QueryParser["Query Parser<br/>tokenize, expand"]
State["State<br/>per-request context"]
Rerankers["Rerankers<br/>cross-encoder, LTR"]
Scoring["Scoring<br/>fusion weights"]
end
subgraph "Phases"
BM25["BM25 Phase"]
Vector["Vector Phase"]
SPLADE["SPLADE Phase"]
Colbert["Colbert Phase"]
KGFacts["KG Facts Phase"]
SessionMem["Session Memory Phase"]
Temporal["Temporal Phase"]
SkillLookup["Skill Lookup Phase"]
end
subgraph "Infra"
FTS["FTS Index"]
VecStore["Vector Store"]
KGSearch["KG Search"]
end
Orchestrator --> BM25
Orchestrator --> Vector
Orchestrator --> SPLADE
Orchestrator --> Colbert
Orchestrator --> KGFacts
Orchestrator --> SessionMem
Orchestrator --> Temporal
Orchestrator --> SkillLookup
BM25 --> FTS
Vector --> VecStore
SPLADE --> FTS
Colbert --> VecStore
KGFacts --> KGSearch
SessionMem --> FTS
Temporal --> FTS
SkillLookup --> FTS
Orchestrator --> Rerankers
Orchestrator --> Scoring
Orchestrator --> Config
Orchestrator --> QueryParser
Orchestrator --> State
```

**Diagram sources**
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py)
- [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py)
- [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)
- [infra/fts.py](file://infra/fts.py)
- [infra/vector_store.py](file://infra/vector_store.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

**Section sources**
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/config.py](file://search/config.py)
- [search/query_parser.py](file://search/query_parser.py)
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py)
- [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py)
- [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)
- [search/rerankers.py](file://search/rerankers.py)
- [search/scoring.py](file://search/scoring.py)
- [search/state.py](file://search/state.py)
- [infra/fts.py](file://infra/fts.py)
- [infra/vector_store.py](file://infra/vector_store.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

## Core Components
- Orchestrator: Builds the execution plan from configuration, runs phases, aggregates results, applies fusion and reranking, and returns the final ranking.
- Phases: Independent retrieval components that each produce scored candidates. Examples include BM25, vector similarity, SPLADE, Colbert, KG facts, session memory, temporal filtering, and skill lookup.
- Query Parser: Normalizes and expands user input into structured tokens and constraints consumed by phases.
- Fusion and Ranking: Combines per-phase scores using configurable weights and optional cross-encoder or learning-to-rank models.
- Configuration: Defines which phases run, their parameters, and fusion strategy.
- State: Holds per-request metadata such as tenant scope, time bounds, and cache keys.

Key responsibilities and interactions are implemented across the following files:
- Orchestration and composition: [search/orchestrator.py](file://search/orchestrator.py)
- Phase definitions: [search/phases/*.py](file://search/phases/bm25_phase.py), [search/phases/vector_phase.py](file://search/phases/vector_phase.py), [search/phases/splade_phase.py](file://search/phases/splade_phase.py), [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py), [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py), [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py), [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py), [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)
- Query parsing: [search/query_parser.py](file://search/query_parser.py)
- Fusion and reranking: [search/rerankers.py](file://search/rerankers.py), [search/scoring.py](file://search/scoring.py)
- Request state: [search/state.py](file://search/state.py)
- Infrastructure backends: [infra/fts.py](file://infra/fts.py), [infra/vector_store.py](file://infra/vector_store.py), [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

**Section sources**
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py)
- [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py)
- [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)
- [search/query_parser.py](file://search/query_parser.py)
- [search/rerankers.py](file://search/rerankers.py)
- [search/scoring.py](file://search/scoring.py)
- [search/state.py](file://search/state.py)
- [infra/fts.py](file://infra/fts.py)
- [infra/vector_store.py](file://infra/vector_store.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

## Architecture Overview
The pipeline follows a modular, composable design:
- Input: A natural language query plus optional filters (time window, tenant, tags).
- Parsing: Tokens, synonyms, and constraints are extracted.
- Parallel Retrieval: Multiple phases execute concurrently where possible, leveraging FTS and vector indexes.
- Aggregation: Candidate IDs are unified; per-phase scores are normalized and combined.
- Reranking: Optional cross-encoder or LTR model refines order.
- Output: Ranked results with metadata and provenance (which phases contributed).

```mermaid
sequenceDiagram
participant Client as "Client"
participant API as "API Layer"
participant Orchestrator as "Orchestrator"
participant Parser as "Query Parser"
participant BM25 as "BM25 Phase"
participant Vector as "Vector Phase"
participant SPLADE as "SPLADE Phase"
participant Colbert as "Colbert Phase"
participant Fusion as "Fusion/Reranker"
participant Result as "Ranked Results"
Client->>API : "Submit query + filters"
API->>Orchestrator : "Build request"
Orchestrator->>Parser : "Parse and expand"
Parser-->>Orchestrator : "Tokens, constraints"
par "Parallel retrieval"
Orchestrator->>BM25 : "Run BM25"
Orchestrator->>Vector : "Run vector search"
Orchestrator->>SPLADE : "Run SPLADE"
Orchestrator->>Colbert : "Run Colbert"
end
BM25-->>Orchestrator : "Candidates + scores"
Vector-->>Orchestrator : "Candidates + scores"
SPLADE-->>Orchestrator : "Candidates + scores"
Colbert-->>Orchestrator : "Candidates + scores"
Orchestrator->>Fusion : "Normalize and fuse"
Fusion-->>Orchestrator : "Refined scores"
Orchestrator-->>API : "Final ranked list"
API-->>Client : "Results with provenance"
```

**Diagram sources**
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/query_parser.py](file://search/query_parser.py)
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/rerankers.py](file://search/rerankers.py)
- [search/scoring.py](file://search/scoring.py)

## Detailed Component Analysis

### Hybrid Search Strategy: BM25 + Vector Similarity
- BM25 provides precise lexical recall for exact terms and phrases.
- Vector similarity captures semantic proximity and paraphrases.
- Fusion typically uses weighted sum or reciprocal rank fusion (RRF) to balance both signals.
- SPLADE can enhance BM25 by expanding terms via learned sparse representations.
- Colbert offers late-interaction dense retrieval for fine-grained relevance.

```mermaid
flowchart TD
Start(["Start"]) --> Parse["Parse query into tokens and constraints"]
Parse --> RunBM25["Run BM25 over FTS index"]
Parse --> RunVector["Run vector similarity over embeddings"]
Parse --> RunSPLADE["Run SPLADE expansion + FTS"]
Parse --> RunColbert["Run Colbert late-interaction"]
RunBM25 --> Normalize["Normalize per-phase scores"]
RunVector --> Normalize
RunSPLADE --> Normalize
RunColbert --> Normalize
Normalize --> Fuse["Fuse scores (weights or RRF)"]
Fuse --> OptionalRerank{"Optional reranker?"}
OptionalRerank --> |Yes| CrossEncoder["Cross-encoder / LTR refinement"]
OptionalRerank --> |No| Keep["Keep fused scores"]
CrossEncoder --> Finalize["Finalize ranking"]
Keep --> Finalize
Finalize --> End(["Return ranked results"])
```

**Diagram sources**
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/scoring.py](file://search/scoring.py)
- [search/rerankers.py](file://search/rerankers.py)

**Section sources**
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/scoring.py](file://search/scoring.py)
- [search/rerankers.py](file://search/rerankers.py)

### Orchestration and Phase Composition
- The orchestrator reads configuration to determine active phases, their parameters, and fusion strategy.
- It constructs a dependency graph and executes independent phases concurrently.
- It normalizes scores across phases and applies fusion rules before optional reranking.
- It attaches provenance metadata indicating which phases contributed to each result.

```mermaid
classDiagram
class Orchestrator {
+execute(query, config)
+compose_phases(config)
+normalize_scores(candidates)
+fuse_and_rerank(fused)
}
class Phase {
<<interface>>
+run(parsed_query, options) Candidates
}
class BM25Phase
class VectorPhase
class SPLADEPhase
class ColbertPhase
class KGFactsPhase
class SessionMemoryPhase
class TemporalPhase
class SkillLookupPhase
Orchestrator --> Phase : "invokes"
Phase <|-- BM25Phase
Phase <|-- VectorPhase
Phase <|-- SPLADEPhase
Phase <|-- ColbertPhase
Phase <|-- KGFactsPhase
Phase <|-- SessionMemoryPhase
Phase <|-- TemporalPhase
Phase <|-- SkillLookupPhase
```

**Diagram sources**
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py)
- [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py)
- [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)

**Section sources**
- [search/orchestrator.py](file://search/orchestrator.py)

### Query Parsing and Constraints
- Tokenization and normalization: lowercasing, stemming/lemmatization, punctuation handling.
- Synonym and query expansion: leveraging SPLADE or dictionary-based expansions.
- Constraint extraction: time windows, tenant scoping, tags, and entity filters.
- Output: Structured representation consumed by all phases.

```mermaid
flowchart TD
Q["Raw Query"] --> Norm["Normalize text"]
Norm --> Tokens["Extract tokens"]
Tokens --> Expand["Expand (synonyms/SPLADE)"]
Norm --> ExtractConstraints["Extract filters (time, tenant, tags)"]
Expand --> Parsed["Parsed Query"]
ExtractConstraints --> Parsed
Parsed --> Phases["Phases consume Parsed Query"]
```

**Diagram sources**
- [search/query_parser.py](file://search/query_parser.py)

**Section sources**
- [search/query_parser.py](file://search/query_parser.py)

### Fusion Strategies and Ranking Algorithms
- Score normalization: min-max or z-score per phase to make scores comparable.
- Fusion methods:
  - Weighted sum: configurable weights per phase.
  - Reciprocal Rank Fusion (RRF): robust to score calibration differences.
- Reranking:
  - Cross-encoder models for pairwise or listwise refinement.
  - Learning-to-rank features combining lexical, semantic, and temporal signals.

```mermaid
flowchart TD
Cands["Per-phase candidates"] --> Norm["Normalize scores"]
Norm --> Weights["Apply fusion weights"]
Weights --> RRF["Optional RRF"]
RRF --> CE["Optional cross-encoder rerank"]
CE --> Final["Final ranking"]
```

**Diagram sources**
- [search/scoring.py](file://search/scoring.py)
- [search/rerankers.py](file://search/rerankers.py)

**Section sources**
- [search/scoring.py](file://search/scoring.py)
- [search/rerankers.py](file://search/rerankers.py)

### Knowledge Graph and Session-Aware Retrieval
- KG facts phase retrieves relevant entities and relations to boost contextual recall.
- Session memory phase prioritizes recent or salient memories within the current session.
- Temporal phase enforces time-window constraints and decay priors.
- Skill lookup phase matches skills/tools to improve task-oriented retrieval.

```mermaid
graph LR
Parsed["Parsed Query"] --> KGFacts["KG Facts Phase"]
Parsed --> SessionMem["Session Memory Phase"]
Parsed --> Temporal["Temporal Phase"]
Parsed --> SkillLookup["Skill Lookup Phase"]
KGFacts --> Fusion["Fusion"]
SessionMem --> Fusion
Temporal --> Fusion
SkillLookup --> Fusion
```

**Diagram sources**
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py)
- [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py)
- [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

**Section sources**
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [search/phases/session_memory_phase.py](file://search/phases/session_memory_phase.py)
- [search/phases/temporal_phase.py](file://search/phases/temporal_phase.py)
- [search/phases/skill_lookup_phase.py](file://search/phases/skill_lookup_phase.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

### Practical Examples

#### Constructing a Hybrid Search Query
- Use natural language with explicit filters when needed (e.g., time range, tags).
- Enable BM25 and vector phases by default; add SPLADE for term expansion if recall is low.
- Optionally enable Colbert for high-precision late interaction on top candidates.

Example references:
- Conceptual overview and usage patterns: [docs/concepts/search-pipeline.md](file://docs/concepts/search-pipeline.md)
- Evaluation harness showing main pipeline invocation: [eval/run_eval_main_pipeline.py](file://eval/run_eval_main_pipeline.py)
- Hybrid strategy tuning example: [eval/hybrid_strategy.py](file://eval/hybrid_strategy.py)

#### Customizing Phases
- Adjust phase weights in configuration to emphasize BM25 vs vector depending on domain vocabulary density.
- Toggle SPLADE for synonym-heavy domains; disable Colbert for latency-sensitive paths.
- Enable KG facts and session memory for conversational or tool-assisted workflows.

Configuration entry points:
- [search/config.py](file://search/config.py)
- [search/orchestrator.py](file://search/orchestrator.py)

#### Interpreting Results
- Provenance metadata indicates which phases contributed to each result.
- High BM25 scores imply strong lexical match; high vector scores imply semantic relevance.
- Reranked results reflect deeper semantic alignment when rerankers are enabled.

Relevant implementation:
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/rerankers.py](file://search/rerankers.py)

**Section sources**
- [docs/concepts/search-pipeline.md](file://docs/concepts/search-pipeline.md)
- [eval/run_eval_main_pipeline.py](file://eval/run_eval_main_pipeline.py)
- [eval/hybrid_strategy.py](file://eval/hybrid_strategy.py)
- [search/config.py](file://search/config.py)
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/rerankers.py](file://search/rerankers.py)

## Dependency Analysis
The search pipeline depends on infrastructure services for indexing and storage:
- Full-text search via FTS backend.
- Vector similarity via vector store.
- Knowledge graph search for fact-based retrieval.

```mermaid
graph TB
Orchestrator["Orchestrator"] --> BM25["BM25 Phase"]
Orchestrator --> Vector["Vector Phase"]
Orchestrator --> SPLADE["SPLADE Phase"]
Orchestrator --> Colbert["Colbert Phase"]
BM25 --> FTS["FTS Backend"]
SPLADE --> FTS
Colbert --> VecStore["Vector Store"]
Vector --> VecStore
KGFacts["KG Facts Phase"] --> KGSearch["KG Search"]
```

**Diagram sources**
- [search/orchestrator.py](file://search/orchestrator.py)
- [search/phases/bm25_phase.py](file://search/phases/bm25_phase.py)
- [search/phases/splade_phase.py](file://search/phases/splade_phase.py)
- [search/phases/colbert_phase.py](file://search/phases/colbert_phase.py)
- [search/phases/vector_phase.py](file://search/phases/vector_phase.py)
- [search/phases/kg_facts_phase.py](file://search/phases/kg_facts_phase.py)
- [infra/fts.py](file://infra/fts.py)
- [infra/vector_store.py](file://infra/vector_store.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

**Section sources**
- [infra/fts.py](file://infra/fts.py)
- [infra/vector_store.py](file://infra/vector_store.py)
- [knowledge_graph/kg_search.py](file://knowledge_graph/kg_search.py)

## Performance Considerations
- Phase selection: Disable expensive phases (Colbert, rerankers) for low-latency paths; enable selectively for precision-critical queries.
- Batch operations: Prefer batched vector searches and FTS queries to reduce overhead.
- Caching:
  - Cache parsed queries and SPLADE expansions.
  - Cache frequent vector embeddings and shortlist candidates.
  - Use query fingerprinting keyed by tenant and filters.
- Index tuning:
  - Ensure FTS tokenization aligns with domain terminology.
  - Maintain embedding freshness and dimensionality appropriate for workload.
- Concurrency:
  - Execute independent phases in parallel.
  - Apply timeouts and circuit breakers for slow backends.
- Fusion stability:
  - Normalize scores consistently to avoid dominance by one phase.
  - Use RRF when score distributions vary widely across phases.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and diagnostics:
- No results returned:
  - Verify FTS index health and coverage.
  - Check query parser output for over-constrained filters.
  - Inspect phase logs for empty candidate sets.
- Poor precision:
  - Increase BM25 weight or enable SPLADE for better lexical recall.
  - Enable rerankers for semantic refinement.
- High latency:
  - Disable Colbert or rerankers temporarily.
  - Reduce candidate set sizes and limit reranking depth.
- Inconsistent rankings:
  - Confirm score normalization and fusion weights.
  - Validate deterministic behavior under same inputs.

Useful resources:
- Debugging guide: [docs/how-to/debug-search.md](file://docs/how-to/debug-search.md)
- Integration tests for search pipeline behavior: [recall/search_memory.py](file://recall/search_memory.py)

**Section sources**
- [docs/how-to/debug-search.md](file://docs/how-to/debug-search.md)
- [recall/search_memory.py](file://recall/search_memory.py)

## Conclusion
The multi-phase search pipeline delivers robust hybrid retrieval by combining BM25, vector similarity, SPLADE, and Colbert, augmented by KG facts, session memory, temporal constraints, and skill lookup. The orchestrator composes these phases, normalizes and fuses scores, and optionally reranks results. With careful configuration, caching, and monitoring, the system balances recall, precision, and latency across diverse use cases.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices

### Configuration Options Summary
- Phase toggles and parameters: [search/config.py](file://search/config.py)
- Fusion weights and reranker settings: [search/scoring.py](file://search/scoring.py), [search/rerankers.py](file://search/rerankers.py)
- Per-request state and scoping: [search/state.py](file://search/state.py)

**Section sources**
- [search/config.py](file://search/config.py)
- [search/scoring.py](file://search/scoring.py)
- [search/rerankers.py](file://search/rerankers.py)
- [search/state.py](file://search/state.py)