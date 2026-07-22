# A Framework for Content-Keyed CRDT Convergence — Companion Code & Verification Suite (Double-Blind Anonymized)

**Framework Paper:** *A Framework for Content-Keyed CRDT Convergence*  
**Systems Paper:** *Conflict-Free Knowledge-Graph Projection: Provably-Convergent Shared Memory for Concurrent Multi-Agent Systems*  
**Submission Status:** Double-Blind Peer Review  

---

## 1. Executive Overview

This package contains the complete, self-contained reference implementations, unit test suites, and adversarial convergence verification suites for both the **Theoretical Framework** and the **Systems Pipeline**.

- **Theoretical Framework (`ck_crdt.py`)**: Content-Keyed CRDT (CK-CRDT) formalization, Join-Semilattice restricted join math, and K1–K3 necessity verification.
- **Systems Pipeline (`crdt_projection.py`)**: Implementation of the 3-Phase Projection Pipeline (Entity Merge, Canonical Entity Resolution, Edge Redirection with Orphan Guard).
- **Proof Verification Suite (`test_adversarial.py`)**: 36 pytest tests verifying formal proof counterexamples (`TestK1Necessity`, `TestK2Necessity`, `TestK3Necessity`), monotonicity under argmax selection (Theorem 1), layered no-orphan invariants (Theorem 2), and tight information loss lower bounds (Lemmas 1–2).
- **Systems Test Suite (`test_pipeline.py`)**: 51 pytest unit tests for CRDT convergence, idempotence, edge redirection, and orphan freedom.

---

## 2. Quickstart & Verification Instructions

### Environment Setup

This suite depends only on standard Python (`>= 3.9`) and `pytest`.

```bash
python3 -m venv venv
source venv/bin/activate
pip install pytest
```

### Running the Formal Counterexample Verification Suite

To verify all K1–K3 necessity counterexamples and formal proofs:

```bash
pytest test_adversarial.py -v
```

**Expected Output:** `36 passed in < 3s`.

---

## 3. Package Layout

- `ck_crdt.py`          — Standalone CK-CRDT formalization and restricted join semilattice engine.
- `crdt_projection.py` — Standalone 3-Phase Projection Pipeline reference implementation.
- `test_adversarial.py` — Formal proof verification and necessity counterexamples (`TestK1Necessity`, `TestK2Necessity`, `TestK3Necessity`).
- `test_pipeline.py`    — Systems unit test suite.

---

## 4. Anonymization Disclosure

This artifact zip has been anonymized for double-blind review. Author names, institution affiliations, and external repository links have been removed in compliance with double-blind guidelines.
