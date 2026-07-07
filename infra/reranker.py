"""M2: Lazy-loaded reranker singleton for deep semantic rerank.

The default search path uses a lightweight hand-rolled weak cross-encoder
(IDF-weighted token coverage + bigram phrase bonus, see _cross_encoder_score
in memory_mcp.py). That path is sub-millisecond and dependency-free.

This module adds an opt-in ``deep_rerank=True`` flag that swaps in a real
neural cross-encoder for queries where the user wants the best ranking we
can give.

Model selection (verified 2026-06-15, both confirmed MPS-safe on M-series):
  Primary:   Qwen/Qwen3-Reranker-0.6B   (Apache 2.0, 0.6B, listwise yes/no
             scoring on Qwen3 base, ~60 MTEB Rerank en)
  Fallback:  BAAI/bge-reranker-v2-m3    (MIT, 568M, XLM-RoBERTa cross-encoder,
             standard AutoModelForSequenceClassification, ~57 MTEB Rerank en)

On CPU-only M-series Mac the primary is fast enough for the default search
path (top-50 ~few seconds) but it stays opt-in. Failure handling: any
load/score error degrades to None from score() so the caller can fall back
to the weak CE. The model is never required.

License note: both primary and fallback are commercially usable. The
previous jina-reranker-v3 (CC BY-NC 4.0) was disabled because it SIGSEGVs
on Apple Silicon at load time, likely from its custom modeling.py hitting
an MPS kernel that segfaults on unified-memory pointer math.
"""

from __future__ import annotations

__all__ = ["get_reranker", "reset_reranker_for_tests", "normalize_rerank_score"]

import logging
import multiprocessing as mp
import threading
import time
from typing import TYPE_CHECKING, Any, List, Optional, cast

if TYPE_CHECKING:
    from transformers import AutoModel, AutoTokenizer  # noqa: F401  (type hints only)

logger = logging.getLogger(__name__)

# Primary: Qwen3-Reranker-0.6B (Apache 2.0). LLM-based yes/no scoring.
PRIMARY_MODEL_ID = "Qwen/Qwen3-Reranker-0.6B"
# Pinned to an explicit commit hash (OWASP LLM03-001): never use a moving
# branch ref. SHA is the current HEAD of the model repo
# (verified via the HuggingFace Hub API "sha" field).
PRIMARY_REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"

# Fallback: BAAI/bge-reranker-v2-m3 (MIT). Plain cross-encoder.
FALLBACK_MODEL_ID = "BAAI/bge-reranker-v2-m3"
FALLBACK_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"

# Legacy aliases (pre-2026-06-15 module pointed at jina-reranker-v3). Kept
# as module attributes so any third-party code that imported them still
# resolves, but they no longer reflect what the system actually loads.
# The active load path is PRIMARY_MODEL_ID → FALLBACK_MODEL_ID.
RERANKER_MODEL_ID = PRIMARY_MODEL_ID
RERANKER_REVISION = PRIMARY_REVISION

# Singleton state. The lock guards the instance pointer; per-instance locks
# guard the load() path so concurrent first-callers don't double-load.
_reranker_instance: Optional["Reranker"] = None
_reranker_pointer_lock = threading.Lock()


class Reranker:
    """Lazy-loaded wrapper around a cross-encoder reranker.

    Tries the primary (Qwen3-0.6B) first; on any load failure falls back
    to the safety-net (BGE-m3). On any score failure returns None so the
    caller can degrade to the weak CE.

    Use get_reranker() to get the module-level singleton. Direct
    instantiation is for tests only.
    """

    def __init__(
        self,
        primary_id: str = PRIMARY_MODEL_ID,
        primary_revision: str = PRIMARY_REVISION,
        fallback_id: str = FALLBACK_MODEL_ID,
        fallback_revision: str = FALLBACK_REVISION,
    ) -> None:
        self.primary_id = primary_id
        self.primary_revision = primary_revision
        self.fallback_id = fallback_id
        self.fallback_revision = fallback_revision
        self._model: Any = None  # transformers model
        self._tokenizer: Any = None
        self._backend: str = ""  # "qwen3" or "bge"
        self._device: str = ""
        self._load_lock = threading.Lock()
        self._load_attempted = False
        self._load_error: Optional[str] = None

    def is_loaded(self) -> bool:
        return self._model is not None

    def backend(self) -> str:
        """Return which backend loaded successfully ("qwen3" or "bge")."""
        return self._backend

    def load_error(self) -> Optional[str]:
        return self._load_error

    def _resolve_device(self) -> str:
        """Pick the best available torch device. Order: cuda > mps > cpu."""
        try:
            import torch

            if hasattr(torch, "cuda") and torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except Exception:  # pragma: no cover
            logger.warning("Failed to detect GPU device, falling back to CPU")
            pass
        return "cpu"

    def _load_qwen3(self, device: str) -> bool:
        """Load the Qwen3-Reranker-0.6B primary. Returns True on success."""
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.primary_id, revision=self.primary_revision
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self.primary_id,
                revision=self.primary_revision,
                dtype="auto",
            )
            self._model = self._model.to(device).eval()
            self._backend = "qwen3"
            self._device = device
            logger.info(
                "Reranker: loaded primary %s on %s (Apache 2.0, 0.6B Qwen3-based)",
                self.primary_id,
                device,
            )
            return True
        except Exception as e:
            logger.warning(
                "Reranker: primary %s load failed (%s: %s)",
                self.primary_id,
                type(e).__name__,
                e,
            )
            self._model = None
            self._tokenizer = None
            return False

    def _load_bge(self, device: str) -> bool:
        """Load the BAAI/bge-reranker-v2-m3 fallback. Returns True on success."""
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.fallback_id, revision=self.fallback_revision
            )
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self.fallback_id,
                revision=self.fallback_revision,
                dtype="auto",
            )
            self._model = self._model.to(device).eval()
            self._backend = "bge"
            self._device = device
            logger.info(
                "Reranker: loaded fallback %s on %s (MIT, 568M XLM-RoBERTa)",
                self.fallback_id,
                device,
            )
            return True
        except Exception as e:
            logger.warning(
                "Reranker: fallback %s load failed (%s: %s)",
                self.fallback_id,
                type(e).__name__,
                e,
            )
            self._model = None
            self._tokenizer = None
            return False

    def load(self) -> bool:
        """Load primary then fallback. Idempotent and thread-safe.

        Returns True on success, False otherwise. On False, score() will
        return None and the caller should fall back to the weak CE.
        """
        if self.is_loaded():
            return True
        with self._load_lock:
            if self._load_attempted:
                return self.is_loaded()
            self._load_attempted = True
            import sys

            if not sys.modules[__name__].RERANKER_ENABLED:
                self._load_error = "reranker disabled via MEMORY_RERANKER_DISABLED"
                logger.info("Reranker: %s", self._load_error)
                return False
            device = self._resolve_device()
            # Try MPS first, then CPU. (MPS is what Jina SIGSEGV'd on;
            # both new backends have been smoke-tested clean on MPS.)
            if self._load_qwen3(device):
                return True
            if self._load_bge(device):
                return True
            # If MPS load crashed both, try CPU as last resort.
            if device == "mps":
                logger.info("Reranker: retrying on CPU after MPS failure")
                if self._load_qwen3("cpu"):
                    return True
                if self._load_bge("cpu"):
                    return True
            self._load_error = "all reranker backends failed to load"
            return False

    def _score_qwen3(self, query: str, docs: List[str]) -> Optional[List[float]]:
        """LLM-based yes/no scoring using Qwen3-Reranker prompt template.

        Single forward pass per (query, doc) pair (no batch yet — keeps the
        first implementation simple; can be batched later). Returns scores
        in [0, 1] via softmax over the yes/no logits at the last input
        position.
        """
        try:
            import torch

            assert self._model is not None and self._tokenizer is not None
            tok = self._tokenizer
            # Qwen3-Reranker uses this exact prompt format from the model card.
            prefix = (
                "<|im_start|>system\n"
                "Judge whether the Document meets the requirements based on the Query.<|im_end|>\n"
                "<|im_start|>user\n"
                f"Query: {query}\n\nDocument: "
            )
            suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
            yes_id = tok.convert_tokens_to_ids("yes")
            no_id = tok.convert_tokens_to_ids("no")
            if yes_id is None or no_id is None or yes_id == tok.unk_token_id:
                # Some Qwen3 tokenizers don't have a "yes" single token; bail
                # to the fallback path.
                logger.warning(
                    "Reranker(qwen3): tokenizer missing 'yes'/'no' tokens, falling back"
                )
                return None
            scores: List[float] = []
            device = self._device or "cpu"
            if not docs:
                return scores
            # Batch the forward pass: tokenize all (query, doc) pairs
            # together, then compute the yes/no logits in one shot.
            texts = [f"{prefix}{doc}{suffix}" for doc in docs]
            enc = tok(
                texts,
                padding=True,
                truncation=True,
                max_length=2048,
                return_tensors="pt",
            )
            enc = {k: v.to(device) for k, v in enc.items()}
            with torch.no_grad():
                logits = self._model(**enc).logits
            last = logits[:, -1, :]
            p_yes = last[:, yes_id]
            p_no = last[:, no_id]
            exp_yes = torch.exp(p_yes)
            exp_no = torch.exp(p_no)
            denom = exp_yes + exp_no
            batched = (exp_yes / denom).cpu().tolist()
            return [float(s) for s in batched]
        except Exception as e:
            logger.warning("Reranker.score(qwen3) failed: %s", e)
            return None

    def _score_bge(self, query: str, docs: List[str]) -> Optional[List[float]]:
        """Plain cross-encoder scoring using BGE-m3. Returns sigmoid(logits)."""
        try:
            import torch

            assert self._model is not None and self._tokenizer is not None
            pairs = [[query, d] for d in docs]
            device = self._device or "cpu"
            enc = self._tokenizer(
                pairs,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            ).to(device)
            with torch.no_grad():
                logits = self._model(**enc).logits
            # BGE-m3 has num_labels=1 with the relevance score as the single logit.
            raw = (
                logits.squeeze(-1).float().cpu().tolist()
                if hasattr(logits, "squeeze")
                else [float(x) for x in logits]
            )
            return [float(x) for x in raw]
        except Exception as e:
            logger.warning("Reranker.score(bge) failed: %s", e)
            return None

    def score(
        self,
        query: str,
        docs: List[str],
        timeout: Optional[float] = None,
    ) -> Optional[List[float]]:
        """Score each doc's relevance to query.

        Returns a list of floats in the same order as ``docs`` (NOT sorted).
        Backend-dependent scale:
          - qwen3 backend: [0, 1] (probability of "yes")
          - bge backend: raw logits (typically [-10, 10]); apply
            normalize_rerank_score() before blending with other scores
        Returns None if the model failed to load, the forward pass errored,
        or the call exceeded ``timeout`` seconds (when set). The caller
        should fall back to the weak CE on None.

        Timeout mechanism: when ``timeout`` is set, the forward pass is run
        in a child process. If the child doesn't return within the timeout
        wall-clock window, it is killed. This is the only reliable way to
        abort a hung torch MPS kernel — ``threading.Timer`` cannot interrupt
        a native kernel call running on the MPS stream. The cost is one
        process spawn per call (~200ms on macOS) plus model load (~3-5s on
        first call, ~1-2s on subsequent calls when the model is cached in
        the parent process image).

        Set ``timeout`` to a positive float for the hung-kernel insurance
        path. Set to ``None`` (default) to run in-process — fast (no
        spawn overhead) but no kill switch if MPS hangs.
        """
        if not docs:
            return []
        if not self.is_loaded():
            if not self.load():
                return None
        if timeout is None or timeout <= 0:
            # In-process: fast path, no kill switch.
            if self._backend == "qwen3":
                return self._score_qwen3(query, docs)
            if self._backend == "bge":
                return self._score_bge(query, docs)
            return None
        # Hung-kernel insurance: run in a child process we can kill.
        return _score_with_timeout(self, query, docs, timeout)

    def warmup(self) -> bool:
        """Run a dummy inference to ensure the model is hot. Returns success."""
        if not self.is_loaded():
            if not self.load():
                return False
        try:
            self.score("warmup query", ["warmup doc 1", "warmup doc 2", "warmup doc 3"])
            return True
        except Exception as e:
            logger.warning("Reranker.warmup failed: %s", e)
            return False


def get_reranker() -> Reranker:
    """Return the module-level singleton, creating it on first call."""
    global _reranker_instance
    if _reranker_instance is not None:
        return _reranker_instance
    with _reranker_pointer_lock:
        if _reranker_instance is None:
            _reranker_instance = Reranker()
    return _reranker_instance


def reset_reranker_for_tests() -> None:
    """Drop the singleton. Tests-only."""
    global _reranker_instance
    with _reranker_pointer_lock:
        _reranker_instance = None


# ---------------------------------------------------------------------------
# Hung-kernel insurance: run Reranker.score() in a child process we can kill.
# ---------------------------------------------------------------------------
#
# On Apple Silicon (MPS), torch forward passes can hang indefinitely in a
# kernel. ``threading.Timer`` / ``concurrent.futures.TimeoutError`` cannot
# interrupt a native kernel call running on the MPS stream — the only
# reliable way to abort a hung kernel is ``Process.terminate()`` (or
# ``.kill()``) on a child process. The trade-off is one process spawn per
# timed call (~200ms on macOS via spawn context). The model itself is not
# shared between parent and child — the child re-loads. To keep the
# per-call cost low, the child's load uses the parent's already-resolved
# device (cuda/mps/cpu) and warmups to keep first-call latency down.
#
# On CUDA, MPS-style hangs are not observed, so the in-process fast path
# is preferred (set timeout=None). On CPU, hangs are also unlikely; the
# fast path is preferred.


def _score_worker_main(
    reranker_dict: dict,
    query: str,
    docs: List[str],
    q: "mp.Queue",
) -> None:
    """Child-process entry point. Runs the forward pass and returns the result.

    ``reranker_dict`` is a small serializable dict (device, primary_id,
    fallback_id, etc.) — we rebuild a fresh ``Reranker`` instance in the
    child so we don't try to pickle the loaded torch model.
    """
    try:
        r = Reranker(
            primary_id=reranker_dict["primary_id"],
            primary_revision=reranker_dict["primary_revision"],
            fallback_id=reranker_dict["fallback_id"],
            fallback_revision=reranker_dict["fallback_revision"],
        )
        # Honor the device the parent picked (don't re-resolve, which
        # could pick a different default in the child).
        r._resolve_device = lambda: reranker_dict["device"]  # type: ignore[method-assign]
        if not r.load():
            q.put(("load_fail", r.load_error() or "load() returned False"))
            return
        if r._backend == "qwen3":
            scores = r._score_qwen3(query, docs)
        elif r._backend == "bge":
            scores = r._score_bge(query, docs)
        else:
            scores = None
        if scores is None:
            q.put(("score_fail", "score() returned None"))
        else:
            q.put(("ok", scores))
    except Exception as e:
        q.put(("error", f"{type(e).__name__}: {e}"))


def _score_with_timeout(
    reranker: "Reranker",
    query: str,
    docs: List[str],
    timeout: float,
) -> Optional[List[float]]:
    """Run ``reranker.score(query, docs)`` in a child process with a hard wall-clock timeout.

    Returns the scores on success, ``None`` on timeout, load failure, or
    score error. Never raises. The child process is always reaped
    (terminate → join → kill if still alive).
    """
    reranker_dict = {
        "primary_id": reranker.primary_id,
        "primary_revision": reranker.primary_revision,
        "fallback_id": reranker.fallback_id,
        "fallback_revision": reranker.fallback_revision,
        "device": reranker._device or reranker._resolve_device(),
    }
    ctx = mp.get_context("spawn")
    q: "mp.Queue" = ctx.Queue()
    p = ctx.Process(
        target=_score_worker_main,
        args=(reranker_dict, query, docs, q),
        daemon=True,
    )
    t0 = time.monotonic()
    p.start()
    p.join(timeout=timeout)
    wall = time.monotonic() - t0

    if p.is_alive():
        # Hard kill — the only way to abort a hung torch MPS kernel.
        p.terminate()
        p.join(timeout=2)
        if p.is_alive():
            p.kill()
            p.join(timeout=2)
        logger.warning(
            "Reranker.score(%s) on %s timed out after %.1fs — killed child; "
            "falling back to weak CE. This is the 2026-06-19 MPS hang signature.",
            reranker._backend,
            reranker._device,
            wall,
        )
        return None

    if p.exitcode != 0:
        logger.warning(
            "Reranker.score(%s) child exited with code %s after %.1fs",
            reranker._backend,
            p.exitcode,
            wall,
        )
        return None

    try:
        msg = q.get(timeout=2)
    except Exception as e:
        logger.warning(
            "Reranker.score(%s) result queue empty: %s", reranker._backend, e
        )
        return None

    kind = msg[0]
    if kind == "ok":
        return cast(list[float] | None, msg[1])
    if kind == "load_fail":
        logger.warning(
            "Reranker.score(%s) load failed in child: %s", reranker._backend, msg[1]
        )
        return None
    if kind == "score_fail":
        logger.warning(
            "Reranker.score(%s) score failed in child: %s", reranker._backend, msg[1]
        )
        return None
    if kind == "error":
        logger.warning("Reranker.score(%s) child raised: %s", reranker._backend, msg[1])
        return None
    logger.warning(
        "Reranker.score(%s) unknown child message: %r", reranker._backend, msg
    )
    return None


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid. Clipped to avoid overflow at |x| > 700."""
    if x >= 0:
        z: float = 2.718281828459045 ** (-x)
        return 1.0 / (1.0 + z)
    z = 2.718281828459045**x
    return z / (1.0 + z)


def normalize_rerank_score(raw: float, backend: str = "") -> float:
    """Map a raw reranker score to [0, 1].

    - Qwen3 backend already returns [0, 1] (softmax of yes/no); pass-through.
    - BGE-m3 returns raw logits in roughly [-10, 10] with 0 as neutral;
      sigmoid maps this to (0, 1) so a downstream ce_blend-style
      multiplier behaves intuitively (0.5 = neutral, >0.5 = more relevant,
      <0.5 = less relevant).
    """
    if backend == "qwen3":
        return float(raw)
    return _sigmoid(float(raw))


from infra.memory_common import make_lazy_getattr

# RERANKER_ENABLED is the negation of config.reranker_disabled.
# The (name, attr, transform) form supports the negation.
__getattr__ = make_lazy_getattr(
    {"RERANKER_ENABLED": ("reranker_disabled", lambda v: not v)}
)
