---
kind: external_dependency
name: Embedding model backend (model2vec)
slug: model2vec
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

model2vec provides the default embedding model pipeline when the optional `embeddings` extra is installed. It is pulled into the Docker image explicitly (`pip install model2vec`) and used as the fallback embedding backend when sentence-transformers is not available. Together with usearch it forms the semantic search channel of the 14-phase hybrid pipeline.