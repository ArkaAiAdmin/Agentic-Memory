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

import logging
import os
import threading
from typing import Any, Optional

import torch

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "naver/splade-cocondenser-ensembledistil"

_splade_model = None
_splade_tokenizer = None
_splade_lock = threading.Lock()
_splade_load_attempted = False  # Prevents repeated failed load attempts


def _get_splade_model():
    """Lazy-load the SPLADE model.  Returns (model, tokenizer) or (None, None).

    Failure is cached: if loading fails once, subsequent calls return
    (None, None) without retrying.
    """
    global _splade_model, _splade_tokenizer, _splade_load_attempted
    if _splade_load_attempted:
        return _splade_model, _splade_tokenizer
    with _splade_lock:
        if _splade_load_attempted:
            return _splade_model, _splade_tokenizer
        _splade_load_attempted = True  # Mark as attempted before trying
        model_name = os.environ.get("MEMORY_SPLADE_MODEL", _DEFAULT_MODEL)
        try:
            from transformers import AutoModel, AutoTokenizer
            device = "mps" if torch.backends.mps.is_available() else "cpu"
            local_only = os.environ.get("HF_HUB_OFFLINE") == "1" or "PYTEST_CURRENT_TEST" in os.environ
            logger.info("Loading SPLADE model: %s on %s (local_only=%s)", model_name, device, local_only)
            tok = AutoTokenizer.from_pretrained(model_name, local_files_only=local_only)
            mdl = AutoModel.from_pretrained(model_name, local_files_only=local_only).to(device)
            mdl.eval()
            _splade_model = mdl
            _splade_tokenizer = tok
            logger.info("SPLADE model loaded (vocab=%d, device=%s)", tok.vocab_size, device)
            return _splade_model, _splade_tokenizer
        except Exception as e:
            logger.warning("Failed to load SPLADE model %s: %s", model_name, e)
            return None, None


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
        # SPLADE: use the CLS token's representation, apply activation
        # The model outputs logits over the vocabulary for each position.
        # We take the max across positions for each vocab dimension.
        logits = outputs.last_hidden_state[0]  # [seq_len, hidden_dim]

        # For SPLADE, we need to project to vocabulary space.
        # The model's MLM head does this, but we can also use the
        # hidden states directly with a learned projection.
        # For cocondenser-ensembledistil, the hidden dim matches vocab dim.
        if logits.shape[-1] == tokenizer.vocab_size:
            # Already in vocab space
            activated = _splade_activation(logits)
            # Max pooling across sequence positions
            sparse_vec = activated.max(dim=0).values
        else:
            # Need to project to vocab space via MLM head
            # Get the MLM head weights
            if hasattr(model, "cls") and hasattr(model.cls, "predictions"):
                mlm_head = model.cls.predictions
                if hasattr(mlm_head, "transform") and hasattr(mlm_head.transform, "dense"):
                    proj = mlm_head.transform.dense
                    activated = _splade_activation(proj(logits))
                    sparse_vec = activated.max(dim=0).values
                else:
                    # Fallback: use hidden states directly
                    activated = _splade_activation(logits)
                    sparse_vec = activated.max(dim=0).values
            else:
                activated = _splade_activation(logits)
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

        logits = outputs.last_hidden_state  # [batch, seq_len, hidden_dim]
        results = []

        for i in range(len(texts)):
            if logits.shape[-1] == tokenizer.vocab_size:
                activated = _splade_activation(logits[i])
                sparse_vec = activated.max(dim=0).values
            else:
                if hasattr(model, "cls") and hasattr(model.cls, "predictions"):
                    mlm_head = model.cls.predictions
                    if hasattr(mlm_head, "transform") and hasattr(mlm_head.transform, "dense"):
                        proj = mlm_head.transform.dense
                        activated = _splade_activation(proj(logits[i]))
                        sparse_vec = activated.max(dim=0).values
                    else:
                        activated = _splade_activation(logits[i])
                        sparse_vec = activated.max(dim=0).values
                else:
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
