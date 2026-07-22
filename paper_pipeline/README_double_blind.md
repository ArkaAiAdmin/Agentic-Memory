# Conflict-Free Knowledge Graph Projection — Companion Code & Verification Suite (Double-Blind Anonymized)

**Paper Submission:** *Conflict-Free Knowledge-Graph Projection: Provably-Convergent Shared Memory for Concurrent Multi-Agent Systems*  
**Framework Paper:** *A Framework for Content-Keyed CRDT Convergence*  
**Submission Status:** Double-Blind Peer Review  

---

## 1. Executive Overview

This package contains the complete, self-contained reference implementations, unit test suites, and adversarial convergence verification suites for both the **Systems Pipeline** and the **Theoretical Framework**.

- **Systems Pipeline (`crdt_projection.py`)**: Implementation of the 3-Phase Projection Pipeline (Entity Merge, Canonical Entity Resolution, Edge Redirection with Orphan Guard).
- **Theoretical Framework (`ck_crdt.py`)**: Content-Keyed CRDT (CK-CRDT) formalization, Join-Semilattice restricted join math, and K1–K3 necessity verification.
- **Systems Test Suite (`test_pipeline.py`)**: 51 pytest unit tests for CRDT convergence, idempotence, edge redirection, and orphan freedom.
- **Adversarial Test Suite (`test_adversarial.py`)**: 37 pytest tests verifying convergence under out-of-order delivery, network latency, vector clock overflow, and K1–K3 necessity counterexamples (`TestK1Necessity`, `TestK2Necessity`, `TestK3Necessity`).

---

## 2. Quickstart & Verification Instructions

### Environment Setup

This suite depends only on standard Python (`>= 3.9`) and `pytest`.

```bash
python3 -m venv venv
source venv/bin/activate
pip install pytest
```

### Running the Full Test & Counterexample Verification Suite

To verify all convergence, orphan-freedom, and formal proof counterexamples:

```bash
pytest test_pipeline.py test_adversarial.py -v
```

**Expected Output:** `88 passed in < 1s`.

To run the scaling and memory profile benchmark:

```bash
python3 benchmark.py
```

---

## 3. Package Layout

- `crdt_projection.py` — Standalone 3-Phase Projection Pipeline reference implementation.
- `ck_crdt.py`          — Standalone CK-CRDT formalization and restricted join semilattice engine.
- `test_pipeline.py`    — Unit test suite (Phase 1, Phase 2, Phase 3 convergence).
- `test_adversarial.py` — Adversarial test suite, including formal counterexample verification (`TestK1Necessity`, `TestK2Necessity`, `TestK3Necessity`).
- `benchmark.py`       — Scale benchmark (up to 10M operations).

---

## 4. Anonymization Disclosure

This artifact zip has been anonymized for double-blind review. Author names, institution affiliations, and external repository links have been removed in compliance with double-blind guidelines.
