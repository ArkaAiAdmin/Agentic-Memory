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
import sys
import threading
from typing import Optional

# H2 fix: wrap torch import to handle CUDA/MPS init failures gracefully
try:
    import torch
except Exception as _torch_exc:
    import types
    torch = types.ModuleType("torch")  # placeholder; encode will fail with clear error
    logging.getLogger(__name__).warning("torch import failed (CUDA/MPS init?): %s", _torch_exc)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "colbert-ir/colbertv2.0"
_MODEL_DIM = 128

_colbert_model = None
_colbert_tokenizer = None
_colbert_projection = None
_colbert_lock = threading.Lock()
_colbert_load_attempted = False  # Prevents repeated failed load attempts
_hidden_dim = 768  # BERT base; updated on first load


class _ColbertProjection(torch.nn.Module):
    """Linear projection from BERT hidden dim to ColBERT 128 dims."""

    def __init__(self, in_dim: int, out_dim: int = 128):
        super().__init__()
        self.linear = torch.nn.Linear(in_dim, out_dim, bias=False)

    def forward(self, x):
        return self.linear(x)


def _get_colbert_model():
    """Lazy-load the ColBERT model.  Returns (model, tokenizer, projection) or (None, None, None).

    Failure is cached: if loading fails once, subsequent calls return
    (None, None, None) without retrying.

    C6 fix: model download happens OUTSIDE the lock to prevent system-wide
    deadlock when HF Hub is slow.  Only the brief assignment of loaded
    model/tokenizer/projection to module globals happens inside the lock.
    """
    global _colbert_model, _colbert_tokenizer, _colbert_projection, _colbert_load_attempted, _hidden_dim
    if _colbert_load_attempted:
        return _colbert_model, _colbert_tokenizer, _colbert_projection

    is_testing = (
        "pytest" in sys.modules
        or "unittest" in sys.modules
        or "PYTEST_CURRENT_TEST" in os.environ
    )
    if is_testing and os.environ.get("MEMORY_TEST_COLBERT") != "1":
        _colbert_load_attempted = True
        return None, None, None

    # Download OUTSIDE the lock to avoid holding it during network I/O.
    model_name = os.environ.get("MEMORY_COLBERT_MODEL", _DEFAULT_MODEL)
    loaded_model = None
    loaded_tokenizer = None
    loaded_projection = None
    try:
        from transformers import AutoModel, AutoTokenizer
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        local_only = os.environ.get("HF_HUB_OFFLINE") == "1" or "PYTEST_CURRENT_TEST" in os.environ
        logger.info("Loading ColBERT model: %s on %s (local_only=%s)", model_name, device, local_only)
        tok = AutoTokenizer.from_pretrained(model_name, local_files_only=local_only)
        mdl = AutoModel.from_pretrained(model_name, local_files_only=local_only).to(device)
        mdl.eval()
        hidden_dim = mdl.config.hidden_size
        proj = _ColbertProjection(hidden_dim, _MODEL_DIM).to(device)
        _weights_loaded = False
        try:
            from huggingface_hub import hf_hub_download
            weights_path = hf_hub_download(
                repo_id=model_name,
                filename="pytorch_model.bin",
                local_files_only=local_only,
            )
            state_dict = torch.load(weights_path, map_location="cpu")
            if "linear.weight" in state_dict:
                proj.linear.weight.data.copy_(state_dict["linear.weight"])
                _weights_loaded = True
                logger.info("Loaded ColBERT projection weights from checkpoint")
            else:
                logger.warning("linear.weight not found in ColBERT checkpoint")
        except Exception as _pe:
            logger.warning(
                "Failed to load ColBERT projection weights from checkpoint: %s. "
                "ColBERT reranking will be disabled (random weights would corrupt "
                "rankings). Set MEMORY_COLBERT_MODEL to a local path or ensure "
                "HF Hub access.",
                _pe,
            )
        if not _weights_loaded:
            proj = None
        else:
            proj.eval()
        loaded_model = mdl
        loaded_tokenizer = tok
        loaded_projection = proj
    except Exception as e:
        logger.warning("Failed to load ColBERT model %s: %s", model_name, e)
        with _colbert_lock:
            _colbert_load_attempted = True
        return None, None, None

    # Assign INSIDE the lock (brief — just pointer writes).
    with _colbert_lock:
        _colbert_model = loaded_model
        _colbert_tokenizer = loaded_tokenizer
        _colbert_projection = loaded_projection
        _colbert_load_attempted = True
        _hidden_dim = hidden_dim
    logger.info("ColBERT model loaded (hidden=%d, proj=%d, device=%s)", hidden_dim, _MODEL_DIM, device)
    return _colbert_model, _colbert_tokenizer, _colbert_projection


def encode_tokens(text: str, max_length: int = 256) -> Optional[list[tuple[str, list[float]]]]:
    """Encode text into per-token embeddings.

    Returns a list of (token_text, embedding_vector) tuples, or None
    if the model is unavailable.  Each embedding is a list of 128 floats.
    """
    model, tokenizer, projection = _get_colbert_model()
    if model is None or tokenizer is None or projection is None:
        return None
    try:
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
        hidden = outputs.last_hidden_state[0]
        projected = projection(hidden)
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


def encode_tokens_batch(texts: list[str], max_length: int = 256) -> list[Optional[list[tuple[str, list[float]]]]]:
    """Encode multiple texts into per-token embeddings in a single forward pass.

    Returns a list of results (one per input text), each a list of
    (token_text, embedding_vector) tuples or None if encoding failed.
    """
    model, tokenizer, projection = _get_colbert_model()
    if model is None or tokenizer is None or projection is None:
        return [None] * len(texts)
    try:
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
        hidden = outputs.last_hidden_state  # [batch, seq_len, hidden_dim]
        projected = projection(hidden)  # [batch, seq_len, 128]
        attn_mask = inputs["attention_mask"]  # [batch, seq_len]
        results: list[list[tuple[str, list[float]]] | None] = []
        for b in range(len(texts)):
            seq_len = attn_mask[b].sum().item()
            tokens = tokenizer.convert_ids_to_tokens(inputs["input_ids"][b][:seq_len])
            batch_vecs = projected[b, :seq_len].tolist()
            result = []
            for i, tok in enumerate(tokens):
                if tok in ("[CLS]", "[SEP]", "[PAD]"):
                    continue
                result.append((tok, batch_vecs[i]))
            results.append(result)
        return results
    except Exception as e:
        logger.warning("ColBERT encode_tokens_batch failed: %s", e)
        return [None] * len(texts)


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
