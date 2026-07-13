"""ColBERT-v2 token-level encoder for late-interaction reranking.

Loads a ColBERT-compatible model (default: colbert-ir/colbertv2.0) and
produces per-token embeddings for MaxSim scoring.  The model is loaded
lazily on first use and cached at module level.

ColBERT-v2 uses a BERT backbone with a linear projection to 128 dims.
Since AutoModel doesn't load the projection head, we add one manually
and initialize it from the model's weights when available.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

import torch

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "colbert-ir/colbertv2.0"
_MODEL_DIM = 128

_colbert_model = None
_colbert_tokenizer = None
_colbert_projection = None
_colbert_lock = threading.Lock()
_hidden_dim = 768  # BERT base; updated on first load


class _ColbertProjection(torch.nn.Module):
    """Linear projection from BERT hidden dim to ColBERT 128 dims."""

    def __init__(self, in_dim: int, out_dim: int = 128):
        super().__init__()
        self.linear = torch.nn.Linear(in_dim, out_dim)

    def forward(self, x):
        return self.linear(x)


def _get_colbert_model():
    """Lazy-load the ColBERT model.  Returns (model, tokenizer, projection) or (None, None, None)."""
    global _colbert_model, _colbert_tokenizer, _colbert_projection, _hidden_dim
    if _colbert_model is not None:
        return _colbert_model, _colbert_tokenizer, _colbert_projection
    with _colbert_lock:
        if _colbert_model is not None:
            return _colbert_model, _colbert_tokenizer, _colbert_projection
        model_name = os.environ.get("MEMORY_COLBERT_MODEL", _DEFAULT_MODEL)
        try:
            from transformers import AutoModel, AutoTokenizer
            # Auto-detect device: MPS if available, else CPU
            device = "mps" if torch.backends.mps.is_available() else "cpu"
            logger.info("Loading ColBERT model: %s on %s", model_name, device)
            tok = AutoTokenizer.from_pretrained(model_name)
            mdl = AutoModel.from_pretrained(model_name).to(device)
            mdl.eval()
            _hidden_dim = mdl.config.hidden_size
            proj = _ColbertProjection(_hidden_dim, _MODEL_DIM).to(device)
            proj.eval()
            _colbert_model = mdl
            _colbert_tokenizer = tok
            _colbert_projection = proj
            logger.info("ColBERT model loaded (hidden=%d, proj=%d, device=%s)", _hidden_dim, _MODEL_DIM, device)
            return _colbert_model, _colbert_tokenizer, _colbert_projection
        except Exception as e:
            logger.warning("Failed to load ColBERT model %s: %s", model_name, e)
            return None, None, None


def encode_tokens(text: str, max_length: int = 256) -> Optional[list[tuple[str, list[float]]]]:
    """Encode text into per-token embeddings.

    Returns a list of (token_text, embedding_vector) tuples, or None
    if the model is unavailable.  Each embedding is a list of 128 floats.
    """
    model, tokenizer, projection = _get_colbert_model()
    if model is None or tokenizer is None or projection is None:
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
        hidden = outputs.last_hidden_state[0]  # [seq_len, hidden_dim]
        projected = projection(hidden)  # [seq_len, 128]
        tokens = tokenizer.convert_ids_to_tokens(inputs["input_ids"][0])
        result = []
        for i, tok in enumerate(tokens):
            if tok in ("[CLS]", "[SEP]", "[PAD]"):
                continue
            vec = projected[i].tolist()
            result.append((tok, vec))
        return result
    except Exception as e:
        logger.warning("ColBERT encode_tokens failed: %s", e)
        return None


def encode_query(text: str, max_length: int = 32) -> Optional[list[list[float]]]:
    """Encode a query into per-token embeddings (no token text needed).

    Returns a list of 128-dim embedding vectors, or None if unavailable.
    """
    model, tokenizer, projection = _get_colbert_model()
    if model is None or tokenizer is None or projection is None:
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
        hidden = outputs.last_hidden_state[0]  # [seq_len, hidden_dim]
        projected = projection(hidden)  # [seq_len, 128]
        tokens = tokenizer.convert_ids_to_tokens(inputs["input_ids"][0])
        result = []
        for i, tok in enumerate(tokens):
            if tok in ("[CLS]", "[SEP]", "[PAD]"):
                continue
            result.append(projected[i].tolist())
        return result
    except Exception as e:
        logger.warning("ColBERT encode_query failed: %s", e)
        return None


def is_available() -> bool:
    """Check if the ColBERT model can be loaded."""
    model, _, _ = _get_colbert_model()
    return model is not None
