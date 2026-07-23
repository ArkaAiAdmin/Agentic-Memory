---
kind: external_dependency
name: ONNX Runtime inference engine
slug: onnxruntime
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

ONNX Runtime is a core dependency (not optional) used for running reranking / ColBERT-style models at query time. It is loaded from the ONNX graph produced by the training pipeline and invoked during the rerank phase of the search orchestrator. This is the CPU-bound path that dominates latency at scale.