"""CHANGE 1: SPLADE load-failure cache must be retryable + have an offline fallback.

Verifies:
1. A transient load failure is NOT permanently cached: with MEMORY_SPLADE_RETRY
   (default on), the loader re-attempts on a later call, and encode_sparse does
   not return None forever due to a one-time failure.
2. When the model cannot be loaded, encode_sparse returns a NON-EMPTY deterministic
   fallback sparse vector (so the sparse/FTS-hybrid stage stays alive offline).
"""

from __future__ import annotations

import importlib

import pytest

import infra.splade_encoder as se


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset module-level load state + force retry + fallback on for tests."""
    monkeypatch.setattr(se, "_splade_load_attempted", False)
    monkeypatch.setattr(se, "_splade_failure_count", 0)
    monkeypatch.setattr(se, "_splade_model", None)
    monkeypatch.setattr(se, "_splade_tokenizer", None)
    monkeypatch.setenv("MEMORY_SPLADE_RETRY", "1")
    monkeypatch.setenv("MEMORY_SPLADE_FALLBACK", "1")
    monkeypatch.setenv("MEMORY_SPLADE_MAX_FAILURES", "2")
    yield
    # restore
    monkeypatch.setattr(se, "_splade_load_attempted", False)
    monkeypatch.setattr(se, "_splade_failure_count", 0)


def _force_load_failure(monkeypatch):
    """Make the transformers model load raise, simulating offline/transient failure."""

    class _Boom:
        def from_pretrained(self, *a, **k):
            raise RuntimeError("simulated offline load failure")

        def __getattr__(self, name):
            # AutoTokenizer / AutoModelForMaskedLM share the same failure path.
            return self.from_pretrained

    fake = _Boom()
    import transformers

    monkeypatch.setattr(transformers, "AutoModelForMaskedLM", fake)
    monkeypatch.setattr(transformers, "AutoTokenizer", fake)


def test_fallback_produces_nonempty_tokens(monkeypatch):
    """When the model cannot load, encode_sparse returns a non-empty fallback."""
    _force_load_failure(monkeypatch)

    vec = se.encode_sparse("alpha relates to beta")
    assert vec is not None, "sparse stage must not be dead when model unavailable"
    assert len(vec) > 0, "fallback must produce non-empty splade_tokens"
    # Each entry is a (vocab_id, weight) tuple with positive weight.
    assert all(isinstance(v, tuple) and len(v) == 2 and v[1] > 0 for v in vec)
    # Deterministic: same input -> same tokens.
    vec2 = se.encode_sparse("alpha relates to beta")
    assert vec2 == vec, "fallback encoder must be deterministic"


def test_load_failure_not_permanently_cached_with_retry(monkeypatch):
    """With MEMORY_SPLADE_RETRY on, a failure is retried, not permanently cached.

    We count how many times the loader actually attempts.  After the bounded
    failure budget is exhausted it resets the 'attempted' flag, so a later call
    re-attempts rather than returning the permanently-cached None.
    """
    attempts = {"n": 0}

    class _Boom:
        def from_pretrained(self, *a, **k):
            attempts["n"] += 1
            raise RuntimeError("simulated offline load failure")

        def __getattr__(self, name):
            return self.from_pretrained

    fake = _Boom()
    import transformers

    monkeypatch.setattr(transformers, "AutoModelForMaskedLM", fake)
    monkeypatch.setattr(transformers, "AutoTokenizer", fake)

    # First call: attempts once, fails -> fallback (non-empty).
    v1 = se.encode_sparse("hello world")
    assert v1 is not None and len(v1) > 0
    assert attempts["n"] >= 1

    # Drive the bounded retry budget to exhaustion (max_failures=2).
    se.encode_sparse("another text")
    assert attempts["n"] >= 2

    # Now a subsequent call must RE-ATTEMPT (not permanently cache the failure):
    # the budget resets, so the loader runs again.
    before = attempts["n"]
    se.encode_sparse("yet another text")
    assert attempts["n"] > before, (
        "load failure was permanently cached; retry did not re-attempt"
    )


def test_no_fallback_when_disabled(monkeypatch):
    """If MEMORY_SPLADE_FALLBACK=0, encode_sparse returns None on load failure."""
    monkeypatch.setenv("MEMORY_SPLADE_FALLBACK", "0")
    _force_load_failure(monkeypatch)

    assert se.encode_sparse("alpha beta") is None
