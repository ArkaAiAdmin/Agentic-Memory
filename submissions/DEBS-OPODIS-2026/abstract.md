# Submission Abstract — Paired DEBS/OPODIS 2026

## Title

**Content-Keyed CRDTs: A Framework for Conflict-Free Entity Deduplication in Distributed Knowledge Graphs**

## Authors

Subrata Sadhu (Independent Researcher)

## Abstract

Multi-agent systems that maintain local-first knowledge graphs face a synchronization problem with no clean solution in existing CRDT literature: when two agents independently create what is semantically the same entity — the same name, type, and description — they produce distinct records with separate edge sets. Naive merge preserves both records, fragmenting the graph. Existing CRDT designs (G-Set, OR-Set, LWW-Register) operate on opaque identifiers and cannot detect semantic overlap.

We present a two-part contribution addressing this gap. **Part I** defines *content-keyed CRDTs* (CK-CRDTs) — a CRDT subclass whose merge partitions operations by a content-derived key and selects one representative per class. We prove eight structural results: (1) representative-selection is monotone under argmax over a total order; (2) canonicalization-at-write-time suffices for no-orphan guarantees under downstream CRDTs; (3) merge discards exactly the within-class loser set; (4) convergence requires three content-key properties — determinism, content-locality, and non-key invariance; (5) composite keys inherit convergence; (6) deterministic approximate keys converge; (7) adaptive keys converge iff the migration graph is acyclic; (8) CK-CRDTs compose with delta-CRDTs under stratified delta computation. **Part II** instantiates the framework as a three-phase projection pipeline for knowledge graphs: Phase 1 merges entity operations via 2P-Set membership + LWW-Register per field; Phase 2 groups by inception fingerprint (SHA-256 of content fields), selects a winner, and emits a redirect map; Phase 3 applies the redirect to edge endpoints, ensuring the no-orphan invariant at write time.

We evaluate against 123 adversarial and property-based tests covering timestamp skew, Byzantine version vectors, 10,000-operation fingerprint collisions, Unicode normalization, and concurrent edge redirection. The pipeline degrades gracefully under adversarial load: a single-fingerprint group of 10,000 operations merges in <0.1s with no data corruption. The CK-CRDT framework classifies content-addressed storage, version control, deduplicating sync, and collaborative editors as instances, explaining the tradeoff between content-keying (dedup capability) and ID-at-creation (simplicity).

## Keywords

CRDT, content-keyed merge, entity deduplication, knowledge graph, multi-agent systems, local-first, distributed synchronization

## Submission Type

Full paper (paired Parts I + II, ~12 pages combined)

## Track

Distributed Data Management / Event-Based Systems

## Venue Notes

- **DEBS 2026:** Strong fit for the event-based multi-agent synchronization angle. The three-phase pipeline maps to DEBS's interest in event processing patterns. Emphasize the operational aspects (redirect map as audit trail, write-time canonicalization).
- **OPODIS 2026:** Strong fit for the formal CK-CRDT framework (Theorems 1–8). Emphasize the convergence proofs, the K1–K3 properties, and the classification of existing systems as CK-CRDT instances.

## Paper References

- Paper I: "A Framework for Content-Keyed CRDT Convergence" (paper_pipeline_2/)
- Paper II: "Conflict-Free Knowledge Graph Projection" (paper_pipeline/)
- Reference implementation: `crdt_projection.py`, `ck_crdt.py`
- Test suite: `test_pipeline.py` (83 tests), `test_adversarial.py` (66 tests)
- Production integration: `kg/kg_crdt.py` in agentic-memory
