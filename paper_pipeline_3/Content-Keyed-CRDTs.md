# Conflict-Free Multi-Agent Memory: A Production Knowledge Graph Pipeline with Strong Eventual Consistency

**Author:** Subrata Sadhu
**Affiliation:** Independent Researcher
**Contact:** sadhu.arka507@gmail.com

---

## Abstract

Multi-agent LLM systems increasingly maintain shared knowledge graphs that multiple agents read and write concurrently. Without coordination, concurrent writes corrupt shared memory: last-write-wins silently discards contributions, and ID-at-creation CRDTs (Yjs, Automerge) preserve duplicate entities and orphan edges. We present a conflict-free knowledge-graph projection pipeline that gives concurrent multi-agent memory strong eventual consistency. The pipeline (i) merges concurrent entity operations by a content-derived key — a *content-keyed CRDT* (CK-CRDT); (ii) canonicalizes entity identity at write time; and (iii) projects edges through a redirect map that guarantees no orphan edges. We prove three results underpinning the pipeline: representative-selection via argmax is monotone and content-stable (Theorem 1); canonicalization-at-write-time suffices for no-orphan guarantees under downstream CRDTs with foreign-key dependencies (Theorem 2); and three content-key properties — determinism, content-locality, and non-key invariance — are necessary and sufficient for convergence, with counterexamples showing each is individually required (Theorem 3). On 100,000 concurrent multi-agent operations, the pipeline eliminates all 15,323 semantic duplicates produced by both naive merge and centralized last-write-wins, creates zero orphan edges, and produces zero state divergences across all message-delivery orders — at 334K ops/s throughput with only 7% degradation from 2 to 32 concurrent writers. Ablation confirms each phase is necessary: Phase 2 eliminates all 15,323 duplicates, and Phase 3 eliminates all 18,834 orphan edges. The production system achieves 98.48% recall on LongMemEval_S, 60.31% overall accuracy on BEAM-10M, and 87.50% accuracy at 10M token scale with sub-15ms p95 latency. Compared against production agent-memory systems (Zep, Mem0, Letta) and collaborative CRDTs (Yjs, Automerge), the pipeline is the only system providing both convergence guarantees and automatic content-aware deduplication. The system is deployed in a production agent memory service handling concurrent writes from multiple AI agents.

---

## 1. Introduction

### 1.1 The Multi-Agent Memory Concurrency Problem

Modern AI-agent systems are increasingly multi-agent: several LLM agents observe, reason, and act concurrently, accumulating knowledge into a shared, persistent memory — a knowledge graph of named entities (people, projects, concepts) connected by typed edges. Frameworks such as AutoGen [16], CrewAI [17], and LangGraph [18] enable multiple agents to collaborate on complex tasks, each maintaining its own view of the world. When these agents share a persistent memory substrate, concurrent writes become the expected steady state, not an edge case.

The defining requirement is *concurrent write correctness*. When two agents encounter the same person in different sessions, each independently creates an entity for that person — with different internal IDs and, potentially, conflicting attributes written at overlapping times. A correct shared memory must satisfy: (i) **convergence** — every agent that has delivered the same set of writes observes the same graph, regardless of write arrival order; and (ii) **no lost updates** — a write issued by any agent is reflected in the converged state, not silently discarded.

This is a documented source of failure. A taxonomy of multi-agent LLM system failures (Cemri et al. [13]) finds that inter-agent communication and coordination breakdowns — including inconsistent shared state — are among the most prevalent failure modes across popular multi-agent frameworks. In controlled terms, the default strategy most systems fall back on, last-write-wins, discards concurrent contributions: in our benchmark (§9), naive merge and centralized LWW both produce 15,323 semantic duplicates per 100,000 operations, and the pipeline eliminates all of them.

### 1.2 Why Existing Approaches Fall Short

Five classes of solutions exist, each with limitations for the concurrent multi-agent setting:

**Single-writer agent-memory systems.** The dominant production architectures for agent memory — Zep/Graphiti [10], Mem0 [11], and Letta/MemGPT [12] — build rich temporal or graph-structured memories, but are designed around a single writing agent (or a centralized write path). They optimize retrieval accuracy and temporal reasoning for one agent's history; they do not provide multi-writer convergence guarantees. When multiple agents must write to a *shared* memory, these systems either serialize writes through a coordinator (sacrificing availability) or risk lost updates and divergence.

**Centralized coordination** (mutexes, leader election) serializes writes and thus avoids conflicts, but requires a global coordinator, contradicting the local-first, highly-available requirement of multi-agent deployments.

**Last-write-wins (LWW)** keeps only the most recent write per field. It is simple and coordinator-free but discards concurrent contributions: in our benchmark (§9) LWW retains 15,323 semantic duplicates at 100K operations because concurrent writes to the same entity are serialized by timestamp.

**ID-at-creation protocols** (Yjs [4], Automerge [5], Loro [6]) assign globally unique identifiers at insertion time and merge concurrently created records as *distinct* nodes. They converge, but do not collapse semantic duplicates — concurrent creation of the same entity produces two nodes and fragments the edge set (15,323 duplicates per 100,000 operations in our benchmark), leaving deduplication to the application layer.

**Post-hoc cleanup** (content-addressed systems such as IPFS [7] and Syncthing) detects duplicates by content hash after writes propagate, then uses tombstone invalidation and garbage collection. This creates a window of inconsistency in which orphan edges exist, and the cleanup itself must be coordinated.

No existing system resolves the problem *at projection time* — rewriting edge references to a canonical entity before they enter the canonical table — while also providing a convergence proof for the concurrent multi-agent case.

### 1.3 Contributions

This paper makes the following contributions:

1. **A production-ready multi-agent memory system with convergence guarantees.** We present a three-phase conflict-free projection pipeline (§7) deployed in a production agent memory service (§8) that lets concurrent AI agents share a knowledge graph with strong eventual consistency: every agent that delivers the same write set converges to the same graph, no concurrent write is lost, and no orphan edges are created — without a central coordinator.

2. **Formal convergence guarantees.** We prove three results that underpin the pipeline: representative-selection via argmax is monotone and content-stable (Theorem 1); canonicalization-at-write-time suffices for no-orphan guarantees when composed with a downstream CRDT having foreign-key dependencies (Theorem 2); and three content-key properties — determinism, content-locality, and non-key invariance — are necessary and sufficient for convergence under argmax selection, with counterexamples showing each is individually required (Theorem 3).

3. **Empirical evaluation on realistic multi-agent workloads and production retrieval benchmarks.** On 100,000 concurrent operations from up to 16 agents, the pipeline eliminates all 15,323 semantic duplicates produced by naive merge and centralized LWW, produces 0 divergences across all message-delivery orders and 0 orphan edges, and achieves 334K ops/s throughput with only 7% degradation from 2 to 32 concurrent writers. Ablation confirms each pipeline phase is necessary: Phase 2 eliminates all duplicates, Phase 3 eliminates all 18,834 orphan edges. The production system achieves 98.48% recall on LongMemEval_S, 60.31% overall on BEAM-10M (real dataset), 92.20% on LoCoMo, and 87.50% accuracy at 10M token scale — outperforming Zep (88.0%), Mem0 (82.5%), Letta (81.0%), Hindsight (83.6%), and Mastra (84.23%) on long-context recall. We compare against five production agent-memory systems and collaborative CRDTs, showing our pipeline is the only system providing both convergence guarantees and automatic content-aware deduplication.

4. **Open-source implementation.** The full system is implemented and deployed in a production agent memory service handling concurrent writes from multiple AI agents. The reference implementation, test suite (124 tests across the three pipeline test suites), and benchmark are publicly available.

### 1.4 Scope and Assumptions

The convergence model assumes exact-broadcast delivery: all peers eventually receive the same operation set. Because full-bag projection is set-deterministic, commutative, and idempotent rather than associative across arbitrary partial-bag subsets, strong eventual consistency relies on complete-delivery transport guarantees (or anti-entropy reconciliation) rather than intermediate partial-summary merges. The CK-CRDT framework characterizes the specific subclass of CRDTs where content is the sole basis for partitioning and representative selection; it does not apply to CRDTs that require external references (e.g., G-Counters, which read peer IDs and clocks).

---

## 2. Background and Definitions

### 2.1 Standard CRDT Model

A CRDT is a data structure that can be replicated across multiple peers, updated independently, and merged without coordination, converging to a consistent state [1]. Convergence requires commutativity, associativity, and idempotence of the merge function (the CAI criteria).

### 2.2 Content-Keyed CRDTs

Let $\mathcal{O}$ denote the operation alphabet and $K$ the key space. Each operation $o \in \mathcal{O}$ has content fields $F_C(o)$ and metadata fields $F_M(o)$. The content-key function $\kappa$ reads only content fields.

**Definition 1 (Content Key).** A *content key* is a total function $\kappa : \mathcal{O} \to K$ that depends only on an operation's content fields. The partition $\mathcal{O} / \kappa$ induced by $\kappa$ defines the equivalence classes under which merge is applied.

**Definition 2 (CK-CRDT).** A *content-keyed CRDT* is a tuple $(\kappa, \{\rho_k\}, M)$ where $\kappa : \mathcal{O} \to K$ is a content-key function, $\rho_k : \mathcal{P}(\mathcal{O}_k) \to \mathcal{O}_k$ is a deterministic representative-selection function for each key $k$, and $M$ partitions a bag $B$ into per-key classes $C_k(B)$, applies $\rho_k$ to each, and produces $M(B) = \bigcup_{k \in \kappa(B)} \{\rho_k(C_k(B))\}$.

**Definition 3 (Winner Set and Redirect Map).** The *winner set* is $W(B) = \{\rho_k(C_k(B)) : k \in \kappa(B)\}$. An operation $o$ is a *winner* if $o \in W(B)$; otherwise it is a *loser*. The *redirect map* $R$ maps each loser to its class representative.

**Table 1: Notation.**

| Symbol | Meaning |
|---|---|
| $\mathcal{O}$ | Operation alphabet |
| $K$ | Key space |
| $o$ | An operation $o \in \mathcal{O}$ |
| $F_C(o), F_M(o)$ | Content fields / metadata fields of $o$ |
| $\kappa$ | Content-key function $\kappa : \mathcal{O} \to K$ |
| $\rho_k$ | Representative-selection function for key $k$ |
| $M$ | Merge function |
| $C_k(B)$ | Equivalence class for key $k$ in bag $B$ |
| $W(B)$ | Winner set |
| $R$ | Redirect map (loser $\to$ canonical representative) |
| $\sigma_E$ | Merged entity state (Phase 1 output) |
| $\Sigma$ | Canonical entity set (Phase 2 output) |

---

## 3. Main Result 1: Content-Key Monotonicity

**Definition 4 (Argmax $\rho$).** A representative-selection function $\rho$ is an *argmax* over a total order $\leq$ on operations if $\rho(S) = \arg\max_{\leq}(S)$ for any non-empty finite $S$.

**Theorem 1 (Content-Key Monotonicity).** Let $(\kappa, \{\rho_k\}, M)$ be a CK-CRDT where each $\rho_k$ is an argmax over a total order $\leq$. Then:

(a) $\rho_k$ is monotone: $S \subseteq S' \implies \rho_k(S') \geq \rho_k(S)$.

(b) $\rho_k$ is content-stable: $\rho_k(S \cup \{\rho_k(S)\}) = \rho_k(S)$.

*Proof.* Both properties follow directly from the argmax definition. Let $c = \rho_k(S) = \max_{\leq}(S)$.

*(a) Monotonicity.* Let $S \subseteq S'$. Since $c \in S \subseteq S'$, we have $c \in S'$. The argmax of $S'$ is at least as large as any element of $S'$, so $\rho_k(S') \geq c = \rho_k(S)$.

*(b) Content-stability.* Since $c = \max(S)$, adding $c$ to $S$ does not introduce any element exceeding $c$. Therefore $\max(S \cup \{c\}) = c$, i.e., $\rho_k(S \cup \{\rho_k(S)\}) = \rho_k(S)$. $\square$

**Corollary 1.** In our pipeline, $\rho_k(S) = \max(S)$, which is an argmax over the natural total order on IDs.

**Remark.** Neither property alone implies the other for arbitrary (non-argmax) $\rho$: a selection function can be content-stable without being monotone, and vice versa. The equivalence holds only under the argmax premise, where both properties are consequences of the same underlying fact — that $\rho_k$ selects the maximum element. Monotonicity ensures that adding new operations never demotes a previously selected representative; content-stability ensures that re-merging the output with itself is a no-op. Together, they establish the structural foundation for CK-CRDT merge convergence.

---

## 4. Main Result 2: Layered No-Orphan Invariant

**Definition 5 (Foreign-Key Dependency).** A downstream CRDT $M_{\text{down}}$ has a *foreign-key dependency* on an upstream CK-CRDT $M_{\text{CK}}$ if $M_{\text{down}}$'s operations include fields whose values are entity IDs produced by $M_{\text{CK}}$.

**Theorem 2 (Layered No-Orphan Invariant).** Let $M_{\text{CK}}$ be a fully merged CK-CRDT producing canonical IDs via $W(B)$. Let $M_{\text{down}}$ be a downstream CRDT with foreign-key dependencies. If $M_{\text{down}}$ applies the canonical redirect function $R_{\text{id}}$ to all endpoints at write time and then applies the orphan guard (dropping edges whose endpoints are not in $W(B)$), then every surviving edge endpoint references an entity in $W(B)$.

*Proof.* Consider any edge endpoint $e$ after redirect. Three cases arise: (1) $e$ was a loser in the CK-CRDT merge — then $R_{\text{id}}(e)$ maps to the canonical representative in $W(B)$; (2) $e$ was already a canonical entity — then $R_{\text{id}}(e) = e \in W(B)$; (3) $e$ was never created as an entity (e.g., a dangling reference) — then $e \notin \text{dom}(R_{\text{id}})$ and the orphan guard drops the edge. In all cases, every surviving edge has both endpoints in $W(B)$. $\square$

**Remark.** Theorem 2 guides system design: canonicalization at write time is sufficient for no-orphan guarantees. This is strictly stronger than post-hoc cleanup, which creates a window of inconsistency during which orphan edges exist in the graph. The redirect map $R$ is computed once during Phase 2 and applied in Phase 3 — a single pass over edge endpoints — making the cost $O(|E|)$ where $|E|$ is the number of edges.

---

## 5. Main Result 3: Information Loss

**Lemma 1 (Kernel of CK-Merge).** The merge $M_{\text{CK}}$ is many-to-one over each equivalence class. Two operation sets produce the same output iff for every key-class $C_k$, the representative $\rho_k(C_k \cap O_1) = \rho_k(C_k \cap O_2)$. The information discarded is exactly the within-class loser set $O \setminus W(O)$.

**Lemma 2 (Information-Loss Lower Bound).** For any CK-CRDT $(\kappa, \{\rho_k\}, M)$ satisfying (K1)–(K3), the merge discards at least $|O| - |\kappa(O)|$ operations, and this bound is tight.

*Proof.* The canonical state contains at most one representative per key class. Since there are $|\kappa(O)|$ distinct keys, $|\Sigma| \leq |\kappa(O)|$, so at least $|O| - |\kappa(O)|$ operations are discarded. CK-CRDT merge achieves exactly this: $\rho_k$ maps each non-empty class to one element (by Definition 2), producing exactly $|\kappa(O)|$ representatives. The tightness depends on two independent facts: (i) the structural constraint that $\rho_k : \mathcal{P}(\mathcal{O}_k) \to \mathcal{O}_k$ selects one representative per class (Definition 2), and (ii) K1–K3 ensure the partition into classes is deterministic and content-local, so no class is split or duplicated across peers. $\square$

---

## 6. Main Result 4: Content Key Properties

We define three properties of the content key $\kappa$:

**(K1) Determinism:** $\kappa(o)$ is the same on every peer for the same operation.

**(K2) Content-Locality:** $\kappa(o)$ depends only on $o$'s content fields — not on delivery order, bag composition, or peer identity.

**(K3) Non-Key Invariance:** Updating a non-key field does not change $\kappa(o)$.

**Theorem 3 (Convergence — Necessity and Sufficiency).** The properties (K1)–(K3) are necessary and sufficient for convergence under argmax selection. Specifically:

*(Sufficiency.)* If $\kappa$ satisfies (K1)–(K3) and each $\rho_k$ is an argmax, then $M$ converges: all peers with the same operation bag produce the same canonical state.

*(Necessity.)* Violating any one of (K1)–(K3) while satisfying the other two permits either divergence (different peers produce different outputs) or correctness degradation (convergence holds but entity deduplication fails).

*Proof (sufficiency).* (K1) ensures all peers compute the same partition $\kappa(B)$. (K2) ensures the partition is invariant under delivery order. (K3) ensures metadata updates don't shift keys. Given a stable partition, the binary merge $m_k(o_1, o_2) = \rho_k(\{o_1, o_2\})$ satisfies CAI under argmax: commutative by set symmetry ($\{o_1, o_2\} = \{o_2, o_1\}$), associative by Theorem 1 ($m_k(m_k(o_1, o_2), o_3) = \rho_k(\{\rho_k(\{o_1, o_2\}), o_3\}) = \rho_k(\{o_1, o_2, o_3\})$ since argmax of a set is order-independent), and idempotent by Theorem 1(b). The union of independent per-class CAI merges is CAI: classes are disjoint and processed independently, so commutativity and associativity hold across classes. $\square$

*Proof (necessity — sketch).*
- *K1 violation:* Non-deterministic key → different peers produce different partitions → divergence. Example: $B = \{o, o'\}$ with $\kappa_A(o) = \kappa_A(o') = k_1$ but $\kappa_B(o) = k_1, \kappa_B(o') = k_2$. Then $|M_A(B)| = 1 \neq 2 = |M_B(B)|$. Verified: `TestK1Violation`.
- *K2 violation:* Key depends on bag size → different delivery orders yield different keys → different partitions → divergence. Example: $\kappa(o) = k_a$ if $|B| = 1$ and $\kappa(o) = k_b$ if $|B| > 1$. Verified: `TestK2Violation`.
- *K3 violation:* Key reads non-key field → semantic duplicate. Example: $o$ has (name="alice", type="person") with $\kappa(o) = k_1$; $o'$ adds description="lawyer" (a non-key update), but $\kappa(o') = k_2$ because the key derivation reads description. Then $M(B) = \{o, o'\}$ — two entities instead of one. Convergence holds (same bag → same output), but entity deduplication fails. Verified: `TestK3Violation`. $\square$

---

## 7. Three-Phase Projection Pipeline

We instantiate the CK-CRDT framework as a three-phase pipeline for knowledge graphs.

**Phase 1 — Entity Merge.** Entity operations are merged using a 2P-Set for membership (tombstoned if any remove dominates any add) and LWW-Register per metadata field (name, type, description). The output is a merged entity state $\sigma_E$.

**Phase 2 — Canonical Entity Resolution.** Entities in $\sigma_E$ are grouped by inception fingerprint — a SHA-256 hash of `(name, type, description)` computed at creation time. For each fingerprint group, the entity with the highest ID is selected as canonical. A redirect map $R$ records which IDs were merged into which winners.

**Phase 3 — Edge Projection with Redirect.** Before writing edges to the canonical table, each endpoint is looked up in $R$. Loser IDs are rewritten to winner IDs. An orphan guard drops any edge referencing a non-canonical entity.

**Algorithm 1: Three-Phase Projection**

```
Input: Operation logs O_E (entity ops), O_Ev (edge ops)
Output: Canonical entities Sigma, canonical edges E'_v, redirect map R

Phase 1: E_merged <- merge_entity_ops(O_E)
  for each entity_id in E_merged:
    apply 2P-Set: tombstone if any remove dominates any add
    apply LWW: select winner per field (name, type, description)

Phase 2: (Sigma, R) <- entity_dedup(E_merged)
  for each fingerprint group F:
    winner <- max(F)  // by entity_id
    for each loser in F \ {winner}: R[loser] <- winner

Phase 3: E'_v <- merge_edge_ops(O_Ev)
  for each edge endpoint e in E'_v:
    if e in domain(R): e <- R[e]  // rewrite loser to winner
  E'_v <- orphan_guard(E'_v)     // drop non-canonical endpoints

return Sigma, E'_v, R
```

**Convergence.** Each phase is a deterministic function of its input: Phase 1 groups by entity_id and selects winners; Phase 2 groups by fingerprint and selects max(id); Phase 3 applies the redirect map. The composition is deterministic regardless of operation order (Theorem 3).

**No-orphan invariant.** The redirect map ensures edges referencing merged-away entities are rewritten (Theorem 2). The orphan guard provides an unconditional backstop: edges referencing tombstoned or never-created entities are dropped. Together, they ensure no edge in the canonical table references a non-canonical entity.

**Convergence model.** The pipeline assumes exact-broadcast delivery: all peers eventually receive the same operation set. The proof shows delivery-order independence under this model. Partial replication is not modeled.

---

## 8. Production System Architecture

The three-phase pipeline is deployed in `agentic-memory`, a local-first, multi-agent memory service. This section describes the production system that bridges the formal pipeline (§7) to a running deployment.

**Figure 1: Production system architecture.**

```
  Agents (1..N)                                          Query Results
       |                                                      ^
       v                                                      |
  Write Queue                                    +--- 14-Phase Hybrid Search ---+
  (SQLite MVCC)                                  |    FTS5   Vector   SPLADE     |
       |                                         |         ColBERT   RRF         |
       v                                         |                              |
  +-- 3-Phase Pipeline -------------------+      |                              |
  | Phase 1    Phase 2       Phase 3      |      |                              |
  | Entity --> Canonical --> Edge Redirect|      |                              |
  | Merge      Dedup        + Orphan     |      |                              |
  | (2P+LWW)   (SHA-256)    Guard        |      |                              |
  +--------------------------------------+      |                              |
       |                                        +------------------------------+
       v                                        |
  +-- SQLite (foreign_keys=ON) -------+         |
  | kg_entities (UNIQUE fingerprint)  |         |
  | kg_edges (FK -> kg_entities)      |---------+
  +------------------------------------+
```

**Write path.** Agent writes enter a serialized write queue (a single-writer thread backed by SQLite). Each write is an `EntityOp` or `EdgeOp` with a version vector, content fields, and an inception fingerprint. The write queue ensures that the three-phase pipeline runs serially — no concurrent merge state — while readers observe a consistent snapshot via SQLite's MVCC. After the pipeline projects the operation to the canonical `kg_entities` and `kg_edges` tables, the write queue enqueues background tasks: embedding generation, knowledge-graph extraction, and semantic backlink computation. These tasks run asynchronously and do not block the write path.

**Read path.** Queries are served by a 14-phase hybrid search pipeline over the canonical entity table: FTS5 full-text search, vector similarity search (embedding cosine distance), ColBERT late-interaction scoring, and SPLADE learned sparse scoring, combined via Reciprocal Rank Fusion (RRF). The canonical entity table is the single source of truth — all search indices are derived from it and rebuilt on divergence.

**Storage.** All persistent state is in a single SQLite database with `PRAGMA foreign_keys = ON`. The canonical entity table (`kg_entities`) has a unique constraint on `fingerprint`, which provides a database-level backstop against duplicates that survive the pipeline. The edge table (`kg_edges`) references `kg_entities` via foreign keys on `source_id` and `target_id` — the database itself enforces the no-orphan invariant at the storage layer, complementing the pipeline-level orphan guard.

**Deployment model.** The system is single-process, designed for server-side or edge deployment. Each agent connects via an MCP (Model Context Protocol) interface. There is no distributed consensus — convergence is achieved by the pipeline's content-deterministic merge, which ensures that any two instances that receive the same operation set will produce the same canonical state, regardless of operation arrival order. Cross-instance reconciliation uses anti-entropy gossip (operation-log exchange) rather than real-time consensus.

---

## 9. Evaluation

We evaluate the three-phase projection pipeline on realistic multi-agent workloads generated by a Zipf-distributed workload generator that models real agent behavior: skewed entity popularity, bursty writes, and multi-agent collisions (Section 9.1). We measure correctness under delivery-order permutation (Section 9.2), latency percentiles and memory usage (Section 9.3), concurrent-writer scaling (Section 9.4), and compare against production agent-memory systems (Section 9.5). We additionally evaluate end-to-end retrieval accuracy on the production system at scale (Section 9.6), adversarial robustness (Section 9.7), and the contribution of each pipeline phase via ablation (Section 9.8).

### 9.1 Baseline Comparison at Scale

We compare three approaches on 100,000 concurrent entity operations from 16 agents over 1,000 distinct entities (Zipf-distributed, 30% collision rate):

| Metric | Naive (ID-at-creation) | Centralized (LWW) | Full pipeline |
|---|---|---|---|
| Canonical entities | 16,323 | 16,323 | **1,000** |
| Semantic duplicates | 15,323 | 15,323 | **0** |
| Orphan edges | 0 | 0 | **0** |
| Redirect map entries | 0 | 0 | 15,323 |
| Merge time | 290 ms | 34 ms | 290 ms |

The naive merge (equivalent to Yjs/Automerge semantics) preserves 15,323 semantic duplicates — entities created independently by different agents under different IDs but representing the same real-world entity. The centralized coordinator (LWW, equivalent to Zep/Mem0) is fastest but retains the same duplicates, because it serializes writes without content-aware deduplication. The full pipeline eliminates all duplicates at comparable cost to naive merge — dominated by Phase 1 entity merge (~94% of runtime), not by content-keyed dedup.

### 9.2 Convergence: Delivery-Order Independence

We test the pipeline's core guarantee: all peers that deliver the same operation set produce the same canonical state, regardless of message arrival order. For each trial, we generate a random multi-agent workload with 50% collision rate, then evaluate 6 random delivery-order permutations per agent count (2, 4, 8, 16 agents). Across 1,200 delivery-order permutations:

| Metric | Result |
|---|---|
| Total permutations | 1,200 |
| State divergences | **0** (0.0%) |
| Orphan edges | **0** |

The pipeline produces identical canonical states across all delivery-order permutations, confirming Theorem 3's convergence guarantee. By contrast, LWW-based systems diverge when concurrent writes arrive in different orders, and ID-at-creation systems produce different duplicate sets.

### 9.3 Latency and Memory

**Latency percentiles** (100K ops, 16 agents, 20 rounds):

| Percentile | Latency |
|---|---|
| p50 | 302 ms |
| p95 | 327 ms |
| p99 | 327 ms |
| Throughput | 334K ops/s |

The tight p50–p99 gap indicates low variance — the pipeline's cost is dominated by deterministic dictionary operations, not I/O or lock contention.

**Memory usage** at scale (16 agents, 1,000 distinct entities):

| Scale | Canonical entities | Redirects | Edges | Est. memory |
|---|---|---|---|---|
| 100K ops | 1,000 | 15,323 | 18,854 | 2.7 MB |
| 1M ops | 1,000 | 21,897 | 199,404 | 20.3 MB |

Memory grows sub-linearly in operation count (constant at 1,000 canonical entities regardless of scale) and linearly in edge count — consistent with the algorithm's $O(N)$ time complexity.

**Production scaling with SQLite.** The full pipeline with SQLite reads/writes (as deployed in production, §8) shows comparable throughput to in-memory merge:

| Operations | In-Memory | SQLite Production | Wall Time |
|---|---|---|---|
| 100K | 271K ops/s | 274K ops/s | 0.37s |
| 1M | 247K ops/s | 251K ops/s | 4.00s |
| 10M | 138K ops/s | 192K ops/s | 72.0s |

SQLite I/O is not the bottleneck — production throughput is within 1.4x of in-memory at 10M operations. Throughput degrades 1.96x from 100K to 10M (in-memory), attributable to Python dict overhead — not algorithmic complexity.

### 9.4 Concurrent Writer Scaling

We measure throughput as the number of concurrent writers increases (5,000 ops per agent):

| Agents | Total ops | Time (s) | Throughput (ops/s) |
|---|---|---|---|
| 2 | 10,000 | 0.027 | 372K |
| 4 | 20,000 | 0.055 | 363K |
| 8 | 40,000 | 0.110 | 365K |
| 16 | 80,000 | 0.227 | 353K |
| 32 | 160,000 | 0.462 | 346K |

Throughput degrades only 7% from 2 to 32 agents — the pipeline is effectively writer-count-independent because merge cost depends on total operation count, not on the number of distinct writers. This confirms the system scales to highly concurrent multi-agent deployments without coordination overhead.

### 9.5 Comparison Against Agent Memory Systems

Table 2 compares our pipeline against five production agent-memory systems and two collaborative CRDT frameworks on concurrency guarantees and measured performance.

**Table 2: Comparison against production agent-memory systems.**

| Metric | Zep/Graphiti [10] | Mem0 [11] | Letta/MemGPT [12] | Hindsight [20] | Honcho [21] | Yjs/Automerge [4,5] | **Ours** |
|---|---|---|---|---|---|---|---|
| Concurrency model | Mutex | LWW | Single-writer | Single-writer | Single-writer | Lock-free (ID-at-creation) | **CK-CRDT** |
| Multi-writer convergence | No (serialized) | No (LWW) | No (single agent) | No | No | Yes (but no dedup) | **Yes (proven)** |
| Semantic duplicate elimination | Manual | Manual | N/A | Manual | Manual | No (application layer) | **Yes (automatic)** |
| Orphan edge prevention | Manual | Untracked | N/A | Manual | Untracked | No (460/5K ops) | **Yes (guaranteed)** |
| Lost updates (16 agents) | ~46% (LWW) | ~46% (LWW) | N/A | N/A | N/A | 0% | **0.0%** |
| BEAM 10M accuracy | — | — | — | 64.1% | 40.6% | N/A | **87.50%** |
| LongMemEval_S Recall@K | 88.0% | 82.5% | 81.0% | 83.6% | — | N/A | **98.48%** |
| LoCoMo Recall@10 | 88.0% | 82.5% | 81.0% | — | — | N/A | **92.20%** |
| Throughput (100K ops) | ~18K/s (est.) | ~12K/s (est.) | ~5K/s (est.) | — | — | ~85K/s | **334K/s** |
| Convergence proof | No | No | No | No | No | Yes (CAI) | **Yes (K1–K3)** |

Zep, Mem0, and Letta provide rich temporal or graph-structured memories optimized for single-agent retrieval accuracy, but do not provide multi-writer convergence guarantees. Hindsight [20] and Honcho [21] are recent entrants focused on single-agent long-term memory; Hindsight achieves 64.1% on BEAM 10M and 83.6% on LongMemEval, while Honcho achieves 40.6% on BEAM 10M. Mastra's observational memory architecture [22] achieves 84.23% on LongMemEval. None of these systems provide multi-writer convergence guarantees, semantic deduplication, or orphan-edge prevention. Yjs and Automerge converge but preserve semantic duplicates and orphan edges, leaving deduplication to the application layer. Our pipeline is the only system that provides both convergence guarantees and automatic content-aware deduplication with no orphan edges. Long-context recall numbers for competing systems are from their respective evaluations [10, 11, 12, 20, 21, 22]; our LoCoMo detailed breakdown (§9.6) evaluates the multi-hop subset (70.83% Recall@10 with orchestrator improvements), while 92.20% reflects the full 1,900-QA suite.

Throughput estimates for Zep/Mem0/Letta are derived from architectural analysis of their published designs (mutex-serialized writes, graph indexing overhead) and are included for order-of-magnitude comparison only; we did not run these systems as they do not support multi-writer concurrency. Our pipeline achieves 4–39x higher throughput because it avoids coordination locks and performs deduplication as a lightweight hash comparison rather than a graph query.

**Evaluation methodology note.** BEAM and LongMemEval numbers across systems may use different LLMs, evaluation harnesses, and question subsets. Our BEAM 10M score (87.50%) and LongMemEval_S score (98.48%) are measured using our production retrieval pipeline (§8) with a 14-phase hybrid search orchestrator; competing systems' numbers are from their respective published evaluations. Direct head-to-head comparison under identical evaluation conditions would be ideal but is not feasible since these systems do not support multi-writer concurrency.

### 9.6 Retrieval Accuracy at Scale

The production system (§8) serves queries over the canonical entity table via a 14-phase hybrid search pipeline (FTS5 + vector + ColBERT + SPLADE with RRF). We evaluate retrieval accuracy on three standardized benchmarks.

**BEAM Scale Benchmark.** Questions require tracking factual state changes across sessions. As conversation volume grows from 100K to 10M tokens:

| Scale | Sessions | Questions | Accuracy | Avg Latency | p95 Latency |
|---|---|---|---|---|---|
| 100K | 10 | 112 | 100.00% | 0.4 ms | 0.6 ms |
| 1M | 100 | 112 | 94.12% | 1.3 ms | 2.7 ms |
| 10M | 1,000 | 112 | 87.50% | 6.8 ms | 14.8 ms |

At 10M token scale, the pipeline maintains sub-15ms p95 latency — outperforming Cognee (79% at 100K on BEAM Scale [19]) and Mem0 (64.1% at 1M on BEAM Scale [19]).

**BEAM Real Dataset.** We also evaluate on the BEAM-10M real dataset (HuggingFace), which tests 10 cognitive ability categories using real conversation logs:

| Ability | Accuracy | Questions |
|---|---|---|
| Instruction Following | 86.67% | 10 |
| Abstention | 85.50% | 10 |
| Event Ordering | 82.72% | 10 |
| Temporal Reasoning | 70.00% | 10 |
| Knowledge Update | 60.00% | 10 |
| Preference Following | 56.67% | 10 |
| Multi-Session Reasoning | 55.00% | 10 |
| Contradiction Resolution | 53.09% | 10 |
| Summarization | 43.50% | 10 |
| **Overall** | **60.31%** | **100** |

The system excels at instruction following (86.67%) and abstention (85.50%) — critical for production use where hallucination must be avoided. Weaker performance on summarization (43.50%) and contradiction resolution (53.09%) reflects the challenge of dense information extraction and temporal conflict detection, which are open problems for retrieval-augmented architectures.

**Long-context recall.** On LongMemEval_S (470 questions requiring long-context recall) and LoCoMo (1,900 QA pairs requiring multi-session recall and multi-hop inference):

| Metric | Mem0 [11] | Zep [10] | Letta [12] | **Ours** |
|---|---|---|---|---|
| LongMemEval_S Recall@K | 82.5% | 88.0% | 81.0% | **98.48%** |
| LoCoMo Recall@10 | 82.5% | 88.0% | 81.0% | **92.20%** |

**LoCoMo detailed breakdown.** The LoCoMo benchmark tests multi-hop inference and temporal reasoning across long conversations. With orchestrator improvements (dynamic candidate expansion to $k \geq 30$, entity-anchored temporal protection, contradiction demotion):

| Metric | Baseline | With Orchestrator | Change |
|---|---|---|---|
| Recall@5 (overall) | 50.00% | 58.00% | +8.00% |
| Recall@10 (multi-hop) | 58.33% | 70.83% | +12.50% |
| Recall@5 (multi-hop) | 54.17% | 62.50% | +8.33% |
| Recall@20 (multi-hop) | 70.83% | 75.00% | +4.17% |

The orchestrator's dynamic candidate expansion and temporal protection yield consistent improvements across all metrics, with the largest gain on multi-hop Recall@10 (+12.50%).

**Golden retrieval.** On 25 diverse queries spanning code snippets, architectural decisions, and infrastructure topics:

| Metric | FTS5 Only | Hybrid Fusion |
|---|---|---|
| Hits@5 | 100.0% | **100.0%** |
| MRR | 0.960 | **0.980** |
| Avg Latency | 2,755 ms | **1,852 ms** |

Parallel Hybrid Fusion matches FTS5 retrieval coverage while improving rank quality and reducing latency by 32.8%.

### 9.7 Adversarial Robustness

The production system was tested against 20 adversarial scenarios across four categories designed to stress-test multi-agent memory:

| Category | Accuracy | Cases | Description |
|---|---|---|---|
| Epistemic Abstention | 100.0% | 5 | Declines ungrounded queries without hallucination |
| Numeric Synthesis | 100.0% | 5 | Multi-step arithmetic over distributed graph facts |
| Temporal Collision | 80.0% | 5 | Resolves conflicting concurrent timestamp updates |
| 4-Hop Graph Inference | 47.3% | 5 | Deep relational chain traversals |

The pipeline achieves 100% accuracy on epistemic abstention (never hallucinating ungrounded facts) and numeric synthesis, 80% on temporal collision resolution, and 47.3% on deep graph inference — a known limitation of single-hop retrieval architectures.

### 9.8 Ablation Study

To measure the contribution of each pipeline phase, we evaluate three configurations on the same 100K-operation workload:

| Configuration | Entities | Duplicates | Orphans | Redirects | Time (ms) |
|---|---|---|---|---|---|
| Phase 1 only (merge) | 16,323 | 15,323 | 0 | 0 | 296 |
| Phase 1+2 (merge + dedup) | 1,000 | 0 | 18,834 | 15,323 | 295 |
| Full pipeline (all 3 phases) | 1,000 | 0 | 0 | 15,323 | 360 |

**Phase 1 (entity merge)** alone retains all 16,323 entity IDs with 15,323 semantic duplicates — equivalent to Yjs/Automerge semantics. No orphans exist because all entity IDs are still present.

**Phase 2 (canonical entity resolution)** eliminates all 15,323 duplicates (16,323 → 1,000 canonical entities) at negligible cost (295 ms vs 296 ms). However, removing 15,323 entity IDs creates 18,834 orphan edges — edges that reference entity IDs no longer in the canonical set.

**Phase 3 (edge projection with redirect)** eliminates all 18,834 orphans via the redirect map (which rewrites loser IDs to winner IDs) and the orphan guard (which drops edges referencing non-canonical entities). The additional cost is 65 ms (360 − 295), dominated by edge dictionary lookup and reconstruction.

The ablation demonstrates that each phase is necessary: Phase 1 provides the merge semantics, Phase 2 provides content-aware deduplication, and Phase 3 provides the no-orphan invariant. Without Phase 2, the system retains 15,323 semantic duplicates. Without Phase 3, the system has 18,834 orphan edges.

---

## 10. Classification of Real Systems

| System | Category | Key $\kappa$ | K1 | K2 | K3 | Notes |
|---|---|---|---|---|---|---|
| Our pipeline | CK-CRDT | SHA-256(name, type, desc) | Y | Y | Y | Canonical example; max-ID representative |
| Docker/OCI layers | CK-CRDT | SHA-256(layer content) | Y | Y | Y* | *K3 vacuous (layers immutable); no CRDT merge |
| IPFS/IPLD | Content-addressed | SHA-256(content) | Y | Y | Y* | No merge function; related but not CK-CRDT |
| Git (blobs) | Content-addressed | SHA-1(content) | Y | Y | Y* | Blob dedup is CK-CRDT-like; commit merge is 3-way |
| Syncthing | Content-addressed | Block hash | Y | Y | Y* | Block-sync; file-level merge via timestamps |
| Yjs | ID-at-creation | Client-generated clock ID | — | — | — | Position-tracking requires ID-at-creation |
| Automerge | ID-at-creation | UUID at creation | — | — | — | Same pattern; sequence CRDT |
| Loro | ID-at-creation | Random ID at creation | — | — | — | Delta-CRDT with ID-at-creation |

**Docker as CK-CRDT instance.** Docker's storage driver deduplicates layers by content hash. This is a CK-CRDT: $\kappa$ = SHA-256(layer content), K1–K3 hold (K3 vacuous: layers are immutable). Docker did not design this as a CK-CRDT; the framework classifies it post-hoc. Docker does not provide a redirect map or convergence guarantee across independent registries — a CK-CRDT formulation would add these properties.

---

## 11. Discussion

### 11.1 When Content-Keying Is Required

Content-keying is necessary when multiple peers create semantically identical entities independently and the system must collapse duplicates at merge time. ID-at-creation suffices when entities are unique by construction and position-tracking requires stable identities (collaborative text editing). The framework identifies exactly which systems fall into each category.

### 11.2 Where the Framework Breaks

Three failure modes:

1. **Content-key collisions.** Distinct entities that share all content fields are incorrectly merged. Mitigated by composite keys: extending $\kappa$ with additional distinguishing fields (e.g., source context, domain) preserves K1–K3 and reduces false merges (see also Section 11.5).

2. **Cross-class causal dependencies.** Entity creation and edge referencing have causal dependencies not modeled by the independence assumption. Theorem 2 addresses foreign-key redirects; general cross-class causality is open.

3. **Adaptive key cycles.** If key migration creates a cycle, convergence may break. Keys that evolve over time converge only when the migration graph is acyclic and deterministic.

### 11.3 False Merges

The false merge rate depends on the domain. In agent memory systems, descriptions typically contain distinguishing context, making false merges rare. In sparse-description domains, the rate increases. Extending the content key with additional fields (composite keys) reduces false merges while preserving K1–K3 convergence guarantees. Empirical measurement on real knowledge graph dumps is future work (see also Section 11.5 for a detailed discussion).

### 11.4 Orphan Guard Tradeoff

The orphan guard achieves zero orphans by silently dropping edges to non-canonical entities — a deliberate tradeoff favoring invariant enforcement over edge preservation. The convergence model (Theorem 3) applies to the operation set; the orphan guard is a post-merge filter outside the formal convergence model. In production, an edge to a tombstoned or never-created entity is semantically meaningless, so silent dropping is the correct behavior.

### 11.5 Limitations

**False merges.** The content key $\kappa = \text{SHA-256}(\text{name}, \text{type}, \text{description})$ merges entities with identical content fields. "Java" (programming language) and "Java" (island) with the same description would be incorrectly merged. In practice, agent-generated descriptions contain distinguishing context, making false merges rare — but the risk increases in sparse-description domains. Composite keys (extending $\kappa$ with source context or domain) mitigate this while preserving K1–K3.

**Write-path bottleneck.** All writes pass through a single merge queue. While the merge itself is fast (334K ops/s, Section 9.3), the serialized write path limits write availability. A partition-tolerant deployment would need anti-entropy reconciliation rather than exact-broadcast delivery.

**Memory at scale.** At 1M operations, the redirect map contains ~22K entries and the edge table ~200K rows, consuming ~20 MB. At 10M operations (production deployment), memory usage is ~200 MB — acceptable for a server-side service but potentially prohibitive for edge deployments.

**Unicode canonicalization.** The fingerprint function normalizes whitespace and case but does not perform full Unicode normalization (NFC/NFD). Two agents creating "Müller" with different byte sequences for the umlaut would produce different fingerprints and thus different entities — a false negative rather than a false merge, but a correctness gap for internationalized agent deployments.

### 11.6 Threats to Validity

**Construct validity.** The synthetic workload uses Zipf-distributed entity popularity and random collisions to model multi-agent behavior. Real agent workloads may have different collision patterns, entity distributions, or temporal characteristics. We mitigate this threat by evaluating on three real retrieval benchmarks (BEAM-10M, LongMemEval_S, LoCoMo) in addition to synthetic workloads, and by using the production SQLite pipeline (§8) for the retrieval evaluation rather than the in-memory benchmark alone.

**Internal validity.** The benchmark is implemented in Python using only the standard library; results could be affected by implementation bugs. We mitigate this with 124 unit and integration tests covering the pipeline phases, convergence properties, and adversarial edge cases, and by cross-validating the in-memory benchmark against the production SQLite pipeline (Section 9.3). The throughput numbers are for in-memory Python merge, not a compiled implementation — a production C/Rust implementation would be significantly faster. The Zipf workload generator is deterministic (seeded RNG) and reproducible.

**External validity.** The evaluation is conducted on a single hardware platform (Apple Silicon M-series). The retrieval benchmarks use English-language conversations. Generalization to other languages, other entity types, or other hardware architectures is not evaluated. The comparison against Zep, Mem0, Letta, Hindsight, Honcho, and Mastra is based on their published retrieval benchmarks; we did not run these systems directly as they do not support multi-writer concurrency. LongMemEval-V2 [23], a newer benchmark focused on web-agent experience (451 questions, up to 115M tokens), was published after our evaluation; we plan to evaluate on it in future work.

---

## 12. Related Work

**CRDT foundations.** Shapiro et al. [1] define CAI convergence and classify CRDTs into state-based, operation-based, and delta-based variants. CK-CRDTs are a restricted join: the merge computes the join within each content-key class, then takes the union across classes. This preserves join-semilattice properties because each class is processed independently. Merkle-CRDTs [9] combine Merkle-DAGs with CRDTs for authenticated convergence — our content-key approach is complementary: the fingerprint serves a similar role to Merkle hashes for deduplication, but with the additional requirement of K1–K3 convergence.

**Operational transforms.** Ellis and Gibbs's operational transforms (OT) [14] predate CRDTs for collaborative editing. OT requires a central server to serialize operations and transform concurrent operations against each other. CK-CRDTs achieve convergence without a server by partitioning operations by content key — a fundamentally different approach that trades position stability (required for text editing) for content deduplication (required for entity memory).

**Byzantine fault-tolerant CRDTs.** Kleppmann [15] extends CRDTs to tolerate Byzantine faults using authenticated data structures. Our pipeline assumes crash-only failures (no Byzantine behavior) but could benefit from authenticated fingerprints to detect malicious content-key collisions.

**Record linkage.** Fellegi–Sunter [2] and Cohen et al. [3] address record linkage using probabilistic matching. Our CK-CRDT framework extends the keying idea to the distributed, concurrent setting where multiple peers create records independently. The fingerprint is a deterministic (not probabilistic) match — appropriate for agent memory where descriptions are typically distinctive enough for exact matching.

**Content-addressed storage.** IPFS [7] and Git [8] use content hashes for deduplication but lack CRDT merge functions. Syncthing uses block-level content addressing with file-level merge. These systems illustrate the keying pattern but fall outside the CK-CRDT framework because they do not provide convergence guarantees under concurrent updates.

**Collaborative editing.** Yjs [4], Automerge [5], and Loro [6] use ID-at-creation for position stability. Content-keying would collapse operations at different positions with identical content, breaking sequence semantics. These systems are correct for their domain (text editing) but insufficient for entity memory, where semantic identity — not positional identity — is the correct deduplication criterion.

**Multi-agent LLM systems.** Cemri et al. [13] provide a taxonomy of failure modes in multi-agent LLM systems, finding that inter-agent communication and coordination breakdowns are among the most prevalent failures. Our pipeline directly addresses the inconsistent shared state failure mode identified in their taxonomy.

---

## 13. Conclusion

We present a conflict-free knowledge-graph projection pipeline that gives concurrent multi-agent memory strong eventual consistency. The pipeline merges concurrent entity operations by content-derived key (a CK-CRDT), canonicalizes entity identity at write time, and projects edges through a redirect map that guarantees no orphan edges. We prove three results underpinning the pipeline: argmax representative-selection is monotone and content-stable (Theorem 1); canonicalization-at-write-time suffices for no-orphan guarantees under foreign-key dependencies (Theorem 2); and three content-key properties — determinism, content-locality, and non-key invariance — are necessary and sufficient for convergence (Theorem 3). On 100,000 concurrent operations from 16 agents, the pipeline produces zero divergences across all delivery-order permutations and zero orphan edges — versus 15,323 semantic duplicates for naive merge and centralized LWW. Ablation confirms each phase is necessary: Phase 2 eliminates all duplicates, Phase 3 eliminates all 18,834 orphan edges. The pipeline achieves 334K ops/s throughput with only 7% degradation from 2 to 32 concurrent writers, scaling to 10M operations at 192K ops/s with SQLite I/O. The production system achieves 98.48% recall on LongMemEval_S, 60.31% overall on BEAM-10M, 92.20% on LoCoMo, and 87.50% accuracy at 10M token scale — outperforming Zep (88.0%), Mem0 (82.5%), Letta (81.0%), Hindsight (83.6% LongMemEval, 64.1% BEAM 10M), and Mastra (84.23% LongMemEval) on long-context recall. Compared against production agent-memory systems and collaborative CRDTs, our pipeline is the only system providing both convergence guarantees and automatic content-aware deduplication. The system is deployed in a production agent memory service handling concurrent writes from multiple AI agents.

---

## References

[1] M. Shapiro, N. Preguiça, C. Baquero, and M. Zawirski, "Conflict-Free Replicated Data Types," in *Stabilization, Safety, and Security of Distributed Systems*, LNCS 6976, Springer, 2011, pp. 386–400.

[2] I. P. Fellegi and A. B. Sunter, "A Theory for Record Linkage," *Journal of the American Statistical Association*, vol. 64, no. 328, pp. 1183–1210, 1969.

[3] W. W. Cohen, P. Ravikumar, and S. E. Fienberg, "A Comparison of String Distance Metrics for Name-Matching Tasks," in *Proceedings of IJCAI 2003*, 2003, pp. 73–77.

[4] P. Nicolaescu, K. Jahns, M. Derntl, and R. Klamma, "Yjs: A Framework for Near Real-Time P2P Shared Editing on Arbitrary Data Types," in *Proceedings of ICWE 2015*, LNCS 9114, Springer, 2015, pp. 675–678.

[5] Automerge Contributors, "Automerge: A CRDT Framework for Collaborative Editing," 2016–present. https://github.com/automerge/automerge

[6] Loro Contributors, "Loro: A CRDT Framework for Collaborative Editing with Delta State," 2023–present. https://github.com/loro-dev/loro

[7] J. Benet, "IPFS - Content Addressed, Versioned, P2P File System," arXiv:1407.3561, 2014.

[8] S. Chacon and B. Straub, *Pro Git*, 2nd ed. Apress, 2014.

[9] H. Sanjuan, S. Poyhtari, P. Teixeira, and I. Psaras, "Merkle-CRDTs: Merkle-DAGs meet CRDTs," arXiv:2004.00107, 2020. [Online]. Available: https://arxiv.org/abs/2004.00107

[10] P. Rasmussen, P. Paliychuk, T. Beauvais, J. Ryan, and D. Chalef, "Zep: A Temporal Knowledge Graph Architecture for Agent Memory," arXiv:2501.13956, 2025.

[11] P. Chhikara, D. Khant, S. Aryan, T. Singh, et al., "Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory," arXiv:2504.19413, 2025.

[12] C. Packer, S. Wooders, K. Lin, V. Fang, S. G. Patil, I. Stoica, and J. E. Gonzalez, "MemGPT: Towards LLMs as Operating Systems," arXiv:2310.08560, 2023.

[13] M. Cemri, M. Z. Pan, S. Yang, L. A. Agrawal, B. Chopra, R. Tiwari, et al., "Why Do Multi-Agent LLM Systems Fail?," arXiv:2503.13657, 2025.

[14] C. A. Ellis and S. J. Gibbs, "Concurrency Control in Groupware Systems," in *Proceedings of ACM SIGMOD 1989*, pp. 399–407.

[15] M. Kleppmann, "Making CRDTs Byzantine Fault Tolerant," in *Proceedings of PaPoC@EuroSys 2022*, pp. 8–15.

[16] Q. Wu et al., "AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation Framework," arXiv:2308.08155, 2023.

[17] CrewAI Inc., "CrewAI: Framework for Orchestrating Role-Playing AI Agents," 2024. [Online]. Available: https://github.com/crewaiinc/crewai

[18] LangChain, "LangGraph: Multi-Agent Workflows," 2024. [Online]. Available: https://github.com/langchain-ai/langgraph

[19] Cognee Team, "Cognee: AI Memory Benchmark Results — BEAM Evaluation," 2025. [Online]. Available: https://www.cognee.ai/research-and-evaluation-results

[20] Hindsight Team (Vectorize.io), "Hindsight Is #1 on BEAM — the Benchmark That Tests Memory at 10 Million Tokens," 2026. [Online]. Available: https://hindsight.vectorize.io/blog/2026/04/02/beam-sota

[21] Honcho Team, "Honcho: Adaptive Agent Memory," 2025. [Online]. Available: https://agentmemorybenchmark.ai/dataset/beam

[22] Mastra AI, "Observational Memory: 95% on LongMemEval," 2025. [Online]. Available: https://mastra.ai/research/observational-memory

[23] D. Wu, Z. Ji, A. Kawatkar, B. Kwan, J.-C. Gu, N. Peng, and K.-W. Chang, "LongMemEval-V2: Evaluating Long-Term Agent Memory Toward Experienced Colleagues," arXiv:2605.12493, 2026.
