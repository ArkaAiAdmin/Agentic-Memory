"""SPLADE-v3 learned sparse encoder for hybrid search.

Loads a SPLADE model (default: naver/splade-cocondenser-ensembledistil) and
produces sparse vectors where each dimension corresponds to a vocabulary
token and the value represents its importance.  This captures both lexical
matching and semantic expansion in a sparse representation.

Unlike dense embeddings (bge-base, ColBERT), SPLADE vectors are:
- Sparse: most entries are zero (~100-300 non-zero per doc)
- Interpretable: non-zero dimensions map to actual vocabulary tokens
- Effective for hybrid search: complement BM25 (exact match) and
  semantic search (meaning match)

The model is loaded lazily on first use and cached at module level.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
from typing import Any, Optional

# H2 fix: wrap torch import to handle CUDA/MPS init failures gracefully
try:
    import torch
except Exception as _torch_exc:
    import types
    torch = types.ModuleType("torch")  # placeholder; encode will fail with clear error
    logging.getLogger(__name__).warning("torch import failed (CUDA/MPS init?): %s", _torch_exc)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "naver/splade-cocondenser-ensembledistil"

# Deterministic fallback vocabulary size.  Matches the SPLADE model vocab so
# hashed terms occupy the same sparse space and can be merged/upgraded later.
_FALLBACK_VOCAB_SIZE = 30522

_splade_model = None
_splade_tokenizer = None
_splade_lock = threading.Lock()
# CHANGE 1: the load-failure cache is now *retryable*, not permanent.  We keep
# a failure count and a "load attempted" flag, but a transient/offline failure
# no longer permanently disables SPLADE for the whole process.  When
# MEMORY_SPLADE_RETRY=1 (the default in non-test runs), the attempted flag is
# reset so the next encode attempt retries the load — this recovers
# automatically once the model becomes reachable (e.g. HF hub back online, or
# the local cache populated by another process).
_splade_load_attempted = False
_splade_failure_count = 0
_splade_max_failures = int(os.environ.get("MEMORY_SPLADE_MAX_FAILURES", "3"))


def _splade_retry_enabled() -> bool:
    """Return True if load failures should be retried on the next call.

    Default on (recover automatically from transient/offline failures).
    Set MEMORY_SPLADE_RETRY=0 to restore the old permanent-cache behaviour.
    """
    val = os.environ.get("MEMORY_SPLADE_RETRY", "1")
    return val not in ("0", "false", "False", "no", "")


def _splade_fallback_enabled() -> bool:
    """Return True if the deterministic offline encoder may be used as a
    fallback when the model cannot be loaded.  Default on.  In tests/airgapped
    envs this keeps the sparse stage alive instead of returning None.
    """
    val = os.environ.get("MEMORY_SPLADE_FALLBACK", "1")
    return val not in ("0", "false", "False", "no", "")


def _get_splade_model():
    """Lazy-load the SPLADE model.  Returns (model, tokenizer) or (None, None).

    CHANGE 1: a load *failure* is no longer permanently cached.  We retry up to
    ``_splade_max_failures`` attempts (bounded), and when
    ``MEMORY_SPLADE_RETRY`` is enabled the "attempted" flag is cleared so the
    next encode call re-attempts the load — recovering automatically once the
    model becomes reachable.  Only after exhausting the bounded retry budget do
    we stop trying, and even then callers fall back to the deterministic
    offline encoder (see ``_fallback_encode``) so the sparse stage is never
    dead.

    C6 fix: model download happens OUTSIDE the lock to prevent system-wide
    deadlock when HF Hub is slow.  Only the brief assignment of loaded
    model/tokenizer to module globals happens inside the lock.
    """
    global _splade_model, _splade_tokenizer, _splade_load_attempted, _splade_failure_count
    # Fast path: already loaded — no lock needed.
    if _splade_load_attempted and _splade_model is not None:
        return _splade_model, _splade_tokenizer
    if _splade_load_attempted and not _splade_retry_enabled():
        return _splade_model, _splade_tokenizer
    if _splade_load_attempted and _splade_failure_count >= _splade_max_failures:
        if _splade_retry_enabled():
            _splade_failure_count = 0
            _splade_load_attempted = False
        return _splade_model, _splade_tokenizer

    # Download OUTSIDE the lock to avoid holding it during network I/O.
    model_name = os.environ.get("MEMORY_SPLADE_MODEL", _DEFAULT_MODEL)
    loaded_model = None
    loaded_tokenizer = None
    try:
        from transformers import AutoModelForMaskedLM, AutoTokenizer
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        local_only = os.environ.get("HF_HUB_OFFLINE") == "1" or "PYTEST_CURRENT_TEST" in os.environ
        logger.info("Loading SPLADE model: %s on %s (local_only=%s)", model_name, device, local_only)
        tok = AutoTokenizer.from_pretrained(model_name, local_files_only=local_only)
        mdl = AutoModelForMaskedLM.from_pretrained(model_name, local_files_only=local_only).to(device)
        mdl.eval()
        loaded_model = mdl
        loaded_tokenizer = tok
    except Exception as e:
        _splade_failure_count += 1
        logger.warning(
            "Failed to load SPLADE model %s (attempt %d/%d): %s",
            model_name, _splade_failure_count, _splade_max_failures, e,
        )
        if _splade_retry_enabled() and _splade_failure_count >= _splade_max_failures:
            _splade_load_attempted = False
        return None, None

    # Assign INSIDE the lock (brief — just pointer writes).
    with _splade_lock:
        _splade_model = loaded_model
        _splade_tokenizer = loaded_tokenizer
        _splade_load_attempted = True
        _splade_failure_count = 0
    logger.info("SPLADE model loaded (vocab=%d, device=%s)", loaded_tokenizer.vocab_size, device)
    return _splade_model, _splade_tokenizer


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fallback_tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _fallback_encode(text: str) -> list[tuple[int, float]]:
    """Deterministic, model-free sparse encoder.

    Hashes each term into the SPLADE vocabulary space and weights by
    term frequency (sublinear).  Produces a *non-empty* sparse vector whenever
    the input has any token, so the sparse/FTS-hybrid stage stays alive in
    offline, airgapped, or test environments where the learned model cannot
    be loaded.  Deterministic: same input → same tokens.
    """
    terms = _fallback_tokenize(text)
    if not terms:
        return []
    counts: dict[int, int] = {}
    for term in terms:
        h = hashlib.md5(term.encode("utf-8")).digest()
        vid = int.from_bytes(h[:4], "big") % _FALLBACK_VOCAB_SIZE
        counts[vid] = counts.get(vid, 0) + 1
    result = [
        (vid, round(1.0 + math.log1p(tf), 6))
        for vid, tf in counts.items()
    ]
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def _splade_activation(x: torch.Tensor) -> torch.Tensor:
    """SPLADE activation: ReLU(x) * log(1 + ReLU(x)).

    This produces sparse, non-negative values that represent token importance.
    """
    return torch.relu(x) * torch.log1p(torch.relu(x))


def encode_sparse(
    text: str, max_length: int = 256
) -> Optional[list[tuple[int, float]]]:
    """Encode text into a sparse vector.

    Returns a list of (vocab_id, weight) tuples for non-zero dimensions,
    or None if the model is unavailable.  Weights are SPLADE activation
    values (ReLU * log(1+ReLU)) that represent token importance.
    """
    model, tokenizer = _get_splade_model()
    if model is None or tokenizer is None:
        # CHANGE 1: degrade to the deterministic offline encoder instead of
        # permanently killing the sparse stage.  Falls back only when enabled.
        if _splade_fallback_enabled():
            return _fallback_encode(text)
        return None
    try:
        # Get device from model parameters
        device = next(model.parameters()).device
        inputs = tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=max_length,
            padding=False,
        ).to(device)
        with torch.no_grad():
            outputs = model(**inputs)
        # SPLADE: use the MLM logits directly!
        logits = outputs.logits[0]  # [seq_len, vocab_size]
        activated = _splade_activation(logits)
        # Max pooling across sequence positions
        sparse_vec = activated.max(dim=0).values

        # Extract non-zero entries
        non_zero = torch.nonzero(sparse_vec, as_tuple=False)
        result = []
        for idx in non_zero:
            vid = idx.item()
            weight = sparse_vec[vid].item()
            if weight > 0.01:  # Filter tiny weights
                result.append((vid, weight))

        # Sort by weight descending
        result.sort(key=lambda x: x[1], reverse=True)
        return result
    except Exception as e:
        logger.warning("SPLADE encode_sparse failed: %s", e)
        return None


def encode_sparse_batch(
    texts: list[str], max_length: int = 256
) -> Optional[list[list[tuple[int, float]]]]:
    """Encode a batch of texts into sparse vectors.

    Returns a list of sparse vectors, or None if the model is unavailable.
    More efficient than calling encode_sparse individually.
    """
    model, tokenizer = _get_splade_model()
    if model is None or tokenizer is None:
        # CHANGE 1: degrade to the deterministic offline encoder instead of
        # permanently killing the sparse stage.  Falls back only when enabled.
        if _splade_fallback_enabled():
            return [_fallback_encode(t) for t in texts]
        return None
    try:
        # Get device from model parameters
        device = next(model.parameters()).device
        inputs = tokenizer(
            texts,
            return_tensors="pt",
            truncation=True,
            max_length=max_length,
            padding=True,
        ).to(device)
        with torch.no_grad():
            outputs = model(**inputs)

        logits = outputs.logits  # [batch, seq_len, vocab_size]
        results = []

        for i in range(len(texts)):
            activated = _splade_activation(logits[i])
            sparse_vec = activated.max(dim=0).values

            non_zero = torch.nonzero(sparse_vec, as_tuple=False)
            sparse = []
            for idx in non_zero:
                vid = idx.item()
                weight = sparse_vec[vid].item()
                if weight > 0.01:
                    sparse.append((vid, weight))
            sparse.sort(key=lambda x: x[1], reverse=True)
            results.append(sparse)

        return results
    except Exception as e:
        logger.warning("SPLADE encode_sparse_batch failed: %s", e)
        return None


def is_available() -> bool:
    """Check if the SPLADE model can be loaded."""
    model, _ = _get_splade_model()
    return model is not None
