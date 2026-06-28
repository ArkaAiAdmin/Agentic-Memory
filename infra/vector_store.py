"""Vector store abstraction for the agentic-memory system.

This module provides a single ``VectorStore`` interface that abstracts
the underlying ANN library (usearch, faiss, hnswlib, etc.). The default
implementation uses usearch; tests can swap in ``NumpyVectorStore`` for
in-memory operation.

Why an abstraction (Phase 5.1):
  * usearch is fast but has heavy native deps. Tests / CI environments
    sometimes can't install it. The abstraction lets us run with a
    pure-Python fallback without changing call sites.
  * Future: we may want to swap usearch for faiss, milvus, or qdrant
    for distributed deployment. The interface is the contract.
  * The vec index code in embedding_search.py and rebuild_vec_index.py
    currently imports ``usearch.index.Index`` directly. This module
    centralizes that import behind a stable API.

Interface (the minimum a VectorStore must support):
  * ``add(key, vector)`` — insert or update a vector
  * ``remove(key)`` — delete a vector
  * ``search(query_vector, k)`` — return top-k (key, distance) pairs
  * ``save(blob)`` / ``load(blob)`` — serialize / deserialize the index
  * ``__len__()`` — number of indexed vectors
  * ``ndim`` / ``metric`` — properties
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    """A single search result: the persisted key, the distance to the
    query, and the rank (0 = best).
    """

    key: int
    distance: float
    rank: int


@runtime_checkable
class VectorStore(Protocol):
    """Protocol every vector store must satisfy.

    Concrete implementations: USearchVectorStore, NumpyVectorStore.
    """

    ndim: int
    metric: str

    def __len__(self) -> int: ...
    def add(self, key: int, vector: list[float] | object) -> None: ...
    def remove(self, key: int) -> None: ...
    def search(self, query_vector: list[float] | object, k: int) -> list[SearchHit]: ...
    def save(self) -> bytes: ...
    @classmethod
    def load(cls, blob: bytes, ndim: int, metric: str) -> "VectorStore": ...


# ---------------------------------------------------------------------------
# Pure-Python fallback (NumpyVectorStore)
# ---------------------------------------------------------------------------


class NumpyVectorStore:
    """Brute-force cosine-similarity search backed by numpy.

    Use this for:
      * Tests where pulling in usearch's native deps is too heavy
      * Small corpora (≤ 10K vectors) where brute force is fast enough
      * CI environments where usearch's manylinux wheels are unavailable

    Public API matches the VectorStore protocol.
    """

    def __init__(self, ndim: int, metric: str = "cos") -> None:
        if metric not in ("cos", "l2sq", "ip"):
            raise ValueError(f"unsupported metric: {metric!r}")
        self.ndim = ndim
        self.metric = metric
        self._keys: list[int] = []
        self._vectors: list[list[float]] = []

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: int, vector) -> None:
        vec_list = _to_list(vector, self.ndim)
        # If key already exists, update in place; else append.
        try:
            idx = self._keys.index(key)
            self._vectors[idx] = vec_list
        except ValueError:
            self._keys.append(key)
            self._vectors.append(vec_list)

    def remove(self, key: int) -> None:
        try:
            idx = self._keys.index(key)
            self._keys.pop(idx)
            self._vectors.pop(idx)
        except ValueError:
            # Silent: same contract as usearch (no error on missing key).
            pass

    def search(self, query_vector, k: int) -> list[SearchHit]:
        if k <= 0 or not self._keys:
            return []
        q = _to_list(query_vector, self.ndim)
        scores: list[tuple[float, int]] = []
        for key, vec in zip(self._keys, self._vectors):
            if self.metric == "cos":
                d = _cosine_distance(q, vec)
            elif self.metric == "l2sq":
                d = _l2sq_distance(q, vec)
            else:  # "ip" — inner product; smaller is "more similar" via -ip
                d = -_inner_product(q, vec)
            scores.append((d, key))
        scores.sort()
        return [
            SearchHit(key=key, distance=d, rank=i)
            for i, (d, key) in enumerate(scores[:k])
        ]

    def save(self) -> bytes:
        import json

        return json.dumps({"keys": self._keys, "vectors": self._vectors}).encode()

    @classmethod
    def load(cls, blob: bytes, ndim: int, metric: str) -> "NumpyVectorStore":
        import json

        data = json.loads(blob.decode())
        store = cls(ndim=ndim, metric=metric)
        store._keys = list(data["keys"])
        store._vectors = list(data["vectors"])
        return store


# ---------------------------------------------------------------------------
# usearch implementation (the default for production)
# ---------------------------------------------------------------------------


class USearchVectorStore:
    """usearch-backed vector store (the default).

    Wraps the usearch index. Public API matches the VectorStore
    protocol. Falls back to a no-op ``add`` if usearch isn't installed
    so tests can run without the native dep, but ``search`` will raise
    a clear error in that case.
    """

    def __init__(self, ndim: int, metric: str = "cos") -> None:
        if metric not in ("cos", "l2sq", "ip"):
            raise ValueError(f"unsupported metric: {metric!r}")
        self.ndim = ndim
        self.metric = metric
        self._idx = _build_usearch_index(ndim, metric)

    def __len__(self) -> int:
        try:
            return int(self._idx.size)  # type: ignore[union-attr]
        except AttributeError:
            return 0

    def add(self, key: int, vector) -> None:
        if self._idx is None:
            return  # no-op when usearch unavailable
        self._idx.add(key, _to_list(vector, self.ndim))  # type: ignore[union-attr,arg-type]

    def remove(self, key: int) -> None:
        if self._idx is None:
            return
        try:
            self._idx.remove(key)  # type: ignore[union-attr]
        except Exception as e:  # usearch raises KeyError on missing
            LOG.debug("usearch remove key=%s: %s", key, e)

    def search(self, query_vector, k: int) -> list[SearchHit]:
        if self._idx is None:
            raise RuntimeError(
                "usearch not installed; install with `pip install usearch` "
                "or use NumpyVectorStore for tests"
            )
        if k <= 0 or len(self) == 0:
            return []
        matches = self._idx.search(  # type: ignore[union-attr,arg-type]
            _to_list(query_vector, self.ndim), k
        )
        keys = [int(k) for k in matches.keys.tolist()]
        dists = [float(d) for d in matches.distances.tolist()]
        return [
            SearchHit(key=k, distance=d, rank=i)
            for i, (k, d) in enumerate(zip(keys, dists))
        ]

    def save(self) -> bytes:
        if self._idx is None:
            raise RuntimeError("usearch not installed")
        buf = io.BytesIO()
        self._idx.save(buf)  # type: ignore[union-attr]
        return buf.getvalue()

    @classmethod
    def load(cls, blob: bytes, ndim: int, metric: str) -> "USearchVectorStore":
        store = cls(ndim=ndim, metric=metric)
        if store._idx is None:
            raise RuntimeError("usearch not installed")
        buf = io.BytesIO(blob)
        store._idx.load(buf)  # type: ignore[union-attr]
        return store


def _build_usearch_index(ndim: int, metric: str):
    """Build a usearch index, or return None if usearch isn't installed.

    Returning None lets the wrapper class no-op on add/remove and
    raise a clear error on search — useful in test environments
    where usearch is unavailable.
    """
    try:
        from usearch.index import Index
    except ImportError:
        LOG.warning(
            "usearch not installed; USearchVectorStore will be a no-op. "
            "Install with `pip install usearch` for production use."
        )
        return None
    return Index(ndim=ndim, metric=metric, dtype="f32")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_vector_store(
    backend: str = "usearch",
    ndim: int = 256,
    metric: str = "cos",
) -> VectorStore:
    """Factory for vector stores.

    Args:
        backend: ``"usearch"`` (production), ``"numpy"`` (fallback / tests).
        ndim: Embedding dimension. 256 is the default for model2vec.
        metric: ``"cos"`` (cosine), ``"l2sq"`` (squared L2), or ``"ip"`` (inner product).

    Returns:
        A VectorStore instance. Concrete type depends on backend.

    Raises:
        ValueError: if backend is unknown.
    """
    if backend == "usearch":
        return USearchVectorStore(ndim=ndim, metric=metric)
    if backend == "numpy":
        return NumpyVectorStore(ndim=ndim, metric=metric)
    raise ValueError(
        f"unknown vector store backend: {backend!r}. "
        f"Valid: 'usearch' (default), 'numpy' (fallback)"
    )


def from_blob(backend: str, blob: bytes, ndim: int, metric: str = "cos") -> VectorStore:
    """Deserialize a vector store from a BLOB.

    Convenience wrapper around ``USearchVectorStore.load`` /
    ``NumpyVectorStore.load`` that picks the right class from
    ``backend``.
    """
    if backend == "usearch":
        return USearchVectorStore.load(blob, ndim=ndim, metric=metric)
    if backend == "numpy":
        return NumpyVectorStore.load(blob, ndim=ndim, metric=metric)
    raise ValueError(f"unknown backend: {backend!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_list(vector, ndim: int) -> list[float]:
    """Coerce a list, tuple, or numpy array to a flat list of floats.

    usearch wants a Python list (or its own ndarray). Numpy is fine
    for cosine math but we normalize to list here to keep the
    wrappers thin and dependency-light.
    """
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    if len(vector) != ndim:
        raise ValueError(f"vector length {len(vector)} != ndim {ndim}")
    return [float(x) for x in vector]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine_similarity. Range [0, 2] for unit vectors, [0, 1] typical."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 1.0
    sim = dot / (na * nb)
    # Clamp to [-1, 1] to absorb floating-point noise.
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


def _l2sq_distance(a: list[float], b: list[float]) -> float:
    """Squared L2 distance. Smaller = closer."""
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _inner_product(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


__all__ = [
    "VectorStore",
    "SearchHit",
    "NumpyVectorStore",
    "USearchVectorStore",
    "get_vector_store",
    "from_blob",
]
