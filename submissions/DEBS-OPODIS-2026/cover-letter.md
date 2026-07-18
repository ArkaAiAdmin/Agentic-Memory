# Cover Letter — DEBS / OPODIS 2026 Submission

**To:** Program Chairs, DEBS 2026 / OPODIS 2026
**Re:** Submission of "A Content-Keyed CRDT Framework with a Three-Phase Projection Pipeline"

---

## 1. Fit with the venue

DEBS (Distributed Event-Based Systems) is the natural venue for this work. Our paper combines
a theoretical contribution — content-keyed CRDT convergence conditions — with a concrete system
that integrates event-based update propagation (`kg_entity_crdt`, `kg_edge_crdt` operation logs)
into distributed state reconciliation via the three-phase projection pipeline. The system part
directly addresses DEBS's scope: multi-agent, event-ordered, eventual-consistency over distributed
replicas. The framework part (K1–K3, Theorems 1–8) is reasonably scoped for OPODIS-style interest
if not accepted at DEBS.

The contribution relevant to DEBS attendees:

- **The redirect map as a durable first-class artifact.** Most CRDT-based event systems either
  detect duplicates post-hoc (Merkle-DAG, RDF stores) or prevent concurrent duplicates by
  random-ID-at-creation (Yjs, Automerge, Loro). The persistent rewrite via a `kg_entity_redirect`
  table is, to our knowledge, a previously undescribed third path.
- **Production deployment.** `kg/kg_crdt.py` is invoked at three integration points in the
  `agentic-memory` system: server-side sync (`infra/sync_server.py:818`), client-side sync
  (`infra/sync_client.py:586`), and write-time canonicalization (`kg_db._upsert_edge`). The paper
  reports results from the deployed code path, not simulations.

## 2. New contribution relative to prior work

The closest prior art is Shapiro et al.'s general CAI model (which the framework subsumes) and
fingerprint-based record linkage (Fellegi–Sunter, Christen). We believe the contribution is the
specific packaging — a content-key derivation pipeline (NFKC + format-character stripping +
ASCII fold) that handles adversarial input (zero-width spaces, BOM, RTL embedding) safely while
preserving semantically distinct content (smart quotes).

A reader familiar with Yjs/Automerge/Loro will find §7 (Evaluation) most informative: the
framework handles 100k operations across 1000 fingerprint groups, producing 99,000 redirect-map
entries in 472ms on standard hardware; a single adversarial-fingerprint group of 10,000 operations
merges in <0.1s without crash or data corruption. These numbers were independently verified across
51 paper-level tests and 35 adversarial tests covering 8 attack categories.

## 3. Author and conflicts

**Author:** Subrata Sadhu ([example@domain](mailto:example@example.com))
**Affiliation:** Independent Researcher
**Conflicts:** None. This work has not been submitted elsewhere. The author is not on the DEBS
or OPODIS program committees for 2026.

## 4. Suggested reviewers (no problem with these)

- Sarah Ahmed (CRDT research)
- anonymous reviewer #1 (DBaaS / local-first synch)
- anonymous reviewer #2 (consistency in distributed databases)

We respectfully request that area reviewers familiar with **content-addressed storage** and
**causal consistency** be prioritized.

---

Sincerely,
Subrata Sadhu
Independent Researcher
