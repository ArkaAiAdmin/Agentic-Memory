---
kind: external_dependency
name: In-process ANN vector index (usearch)
slug: usearch
category: external_dependency
category_hints:
    - framework_behavior
scope:
    - '**'
---

usearch is the embedded vector store used for semantic retrieval alongside FTS5 BM25. It lives in-process per Python worker (no external service), which is the single-node scaling bottleneck identified in the audit. Vector index rebuilds go through `backfill/index_backfills.py::_backfill_vec_index_raw`. Because it is in-process, it cannot be shared across workers or scaled horizontally without sharding the corpus.