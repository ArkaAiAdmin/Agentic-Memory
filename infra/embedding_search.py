#!/usr/bin/env python3
"""Semantic search using model2vec embeddings.

Note: Requires model2vec + numpy installed in the agentic-memory venv.
Run with: ~/.config/agentic-memory/venv/bin/python embedding_search.py <query>
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING, cast

from infra.memory_config import install_root

if TYPE_CHECKING:
    import numpy as np

# Vector-search result cache. Same LRU+TTL pattern as knowledge_graph.
# 20 entries / 30s TTL balances hit rate with staleness: a freshly-
# inserted memory is reflected within 30s, and the cache can't grow
# unbounded under a long agentic loop.
import threading as _threading
import time as _time
from collections import OrderedDict as _OrderedDict

logger = logging.getLogger(__name__)

_VEC_CACHE_MAX = 20
_VEC_CACHE_TTL_S = 30.0
try:
    from config import get_config as _get_vec_cache_cfg

    _VEC_CACHE_MAX = int(getattr(_get_vec_cache_cfg(), "vec_cache_max", 20))
    _VEC_CACHE_TTL_S = float(getattr(_get_vec_cache_cfg(), "vec_cache_ttl_s", 30.0))
except Exception:
    logger.warning("Failed to read vec cache config")
    pass
_vec_cache: _OrderedDict = _OrderedDict()
_vec_cache_lock = _threading.Lock()


def _vec_cache_get(key: tuple) -> Any | None:
    now = _time.monotonic()
    with _vec_cache_lock:
        entry = _vec_cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if _VEC_CACHE_TTL_S > 0 and (now - ts) > _VEC_CACHE_TTL_S:
            _vec_cache.pop(key, None)
            return None
        _vec_cache.move_to_end(key)
        return value


def _vec_cache_put(key: tuple, value) -> None:
    if _VEC_CACHE_TTL_S <= 0:
        return
    with _vec_cache_lock:
        _vec_cache[key] = (_time.monotonic(), value)
        _vec_cache.move_to_end(key)
        while len(_vec_cache) > _VEC_CACHE_MAX:
            _vec_cache.popitem(last=False)


def clear_vec_cache() -> None:
    """Clear the vector search cache. Useful for tests."""
    with _vec_cache_lock:
        _vec_cache.clear()


__all__ = [
    "MODEL_ID",
    "MODEL_REVISION",
    "UNINDEXED_SAFETY_NET_LIMIT",
    "EmbeddingSearch",
    "get_embedding_search",
]

# H3 fix: this venv check was producing stdout output on import, which
# corrupts the MCP-server's first response. Promote to a logger.warning
# so the message is still visible (when logs are configured) but does not
# pollute the agent's stdout.
venv_python = install_root() / "venv" / "bin" / "python"
if (
    sys.executable != str(venv_python)
    and not (
        Path(sys.executable).parents[1] / ".config" / "agentic-memory" / "venv"
    ).exists()
):
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "For semantic search, run with the venv python: %s %s <query>",
        venv_python,
        __file__,
    )


# Pinned model identity.
# StaticModel.from_pretrained() does not accept a `revision` kwarg, so we
# resolve the pinned snapshot ourselves via huggingface_hub.snapshot_download
# and feed the resulting local path to from_pretrained. This guarantees the
# same weights across machines and reruns, so embedding scores are stable.
MODEL_ID = "minishlab/potion-base-8M"
MODEL_REVISION = "bf8b056651a2c21b8d2565580b8569da283cab23"
# To refresh the pin run:
#   ~/.config/agentic-memory/venv/bin/python -c "from huggingface_hub import HfApi; print(HfApi().model_info('minishlab/potion-base-8M').sha)"


# Cap on the unindexed-memory safety-net LEFT JOIN inside the indexed
# search path. The unindexed set is "memories added since the last
# rebuild_vec_index run"; on a healthy workflow that should be small
# (a few hundred). The cap keeps the SQL bounded at O(LIMIT) instead
# of O(N) so end-to-end search latency stays flat on 100K+ corpora.
# If a user has truly added more than this many memories without a
# rebuild, `rebuild_vec_index` is the right answer and runs in seconds.
UNINDEXED_SAFETY_NET_LIMIT = 1000


# ---------------------------------------------------------------------------
# Cache key helper
# ---------------------------------------------------------------------------


def _cache_text(content: str) -> str:
    """The exact 500-char NFKC-normalized text we embed + hash for cache keys.

    Centralized so the rebuild_index.py batch writer and the live search
    path can never drift apart: if either side changes the truncation or
    normalization, both will produce identical content_hashes, so the
    cache stays valid across rebuilds and live updates.
    """
    if not content:
        return ""
    return unicodedata.normalize("NFKC", content[:500])


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_tags(tags_json: str) -> list:
    """Parse a JSON tags string into a list. Returns [] on failure."""
    if not tags_json:
        return []
    try:
        result = json.loads(tags_json)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Contextual Retrieval (Anthropic 2024-09)
# ---------------------------------------------------------------------------
# Opt-in via MEMORY_CONTEXTUAL_RETRIEVAL=1. When enabled, a short context
# prefix (category + tags + source_file) is prepended to the content before
# embedding, so the vector captures "what kind of note is this?" alongside
# the raw text. At search time the same prefix logic applies to the query.

# _CONTEXTUAL_ENABLED is dynamically resolved via __getattr__


def _build_context_prefix(
    category: str = "", tags: Optional[list] = None, source_file: str = ""
) -> str:
    """Build a short context string to prepend before embedding.

    Returns a string like "[lessons | python, testing] " or "" if no
    useful context can be derived. Kept short (< 80 chars) so it doesn't
    dominate the 500-char embedding budget.
    """
    parts = []
    if category:
        parts.append(category)
    if tags:
        tag_str = ", ".join(tags[:5])
        parts.append(tag_str)
    if not parts and source_file:
        # Extract the top-level folder from the source path
        sf = source_file.replace("\\", "/")
        top = sf.split("/")[0] if "/" in sf else ""
        if top:
            parts.append(top)
    if not parts:
        return ""
    return "[" + " | ".join(parts) + "] "


def _embed_text_with_context(
    content: str, category: str = "", tags: Optional[list] = None, source_file: str = ""
) -> str:
    """Return the text to embed, with optional context prefix.

    When MEMORY_CONTEXTUAL_RETRIEVAL=1, prepends a context string.
    Otherwise returns the raw content (NFKC-normalized, truncated to 500).
    """
    import sys

    _self = sys.modules[__name__]
    base = unicodedata.normalize("NFKC", content)[:500]
    if not _self._CONTEXTUAL_ENABLED:
        return base
    prefix = _build_context_prefix(category, tags, source_file)
    if not prefix:
        return base
    # Reserve space: prefix + base must fit in 500 chars.
    max_base = 500 - len(prefix)
    if max_base <= 0:
        return base
    return prefix + base[:max_base]


class EmbeddingSearch:
    model: Any
    np: Any

    def __init__(self):
        self.model = None
        self.np = None
        self.is_transformer = False
        # Per-process cache of loaded usearch Indexes. Keyed by db_path;
        # value is (Index, meta_dict). The cache is invalidated when the
        # singleton row's built_at or blob length changes (i.e. a rebuild
        # happened) or when the persisted dim no longer matches the
        # current model. Test isolation: tests that need a clean cache
        # can call self.clear_vec_index_cache().
        self._vec_index_cache: dict = {}
        # Query embedding cache (LRU 128) — opt-in via MEMORY_QUERY_CACHE=1
        # Maps query_text -> embedding_vector (numpy array).
        self._query_cache: OrderedDict = OrderedDict()
        self._QUERY_CACHE_MAX = 128
        from infra._lazy_imports import get_config

        self._QUERY_CACHE_ENABLED = get_config().query_cache
        self._chunk_index_cache: dict = {}
        self._CHUNK_SEARCH_ENABLED = os.environ.get("MEMORY_CHUNK_SEARCH", "1") not in ("0", "false", "no")
        # Lazy model load: spawn background thread so __init__ never blocks
        # the caller (prevents MCP server hangs on cold start / network stalls).
        self._model_loaded = False
        self._model_load_failed = False
        import threading
        _load_thread = threading.Thread(target=self._load_model, daemon=True)
        _load_thread.start()

    def _embed_query(self, query: str) -> Any:
        """Get query embedding with optional LRU cache."""
        if self.model is None or getattr(self, "np", None) is None:
            return None
        if self._QUERY_CACHE_ENABLED:
            if query in self._query_cache:
                self._query_cache.move_to_end(query)
                return self._query_cache[query]
        # Cache miss or disabled — encode and optionally cache.
        query_vec = self.model.encode([query])
        if query_vec.ndim == 1:
            query_vec = query_vec.reshape(1, -1)
        query_vec = query_vec[0]  # shape (dim,)
        if self._QUERY_CACHE_ENABLED:
            self._query_cache[query] = query_vec
            self._query_cache.move_to_end(query)
            if len(self._query_cache) > self._QUERY_CACHE_MAX:
                self._query_cache.popitem(last=False)
        return query_vec

    def _load_model(self) -> None:
        try:
            import numpy as np

            self.np = np

            # Resolve model ID and revision from environment (opt-in)
            model_id = os.environ.get("MEMORY_EMBEDDING_MODEL_ID", MODEL_ID)
            model_revision = os.environ.get(
                "MEMORY_EMBEDDING_MODEL_REVISION", MODEL_REVISION
            )

            # Check if this is the default Model2Vec model (potion) or model2vec requested
            if "potion" in model_id or "model2vec" in model_id:
                from model2vec import StaticModel
                from huggingface_hub import snapshot_download

                # Only use the pinned revision for the default model
                local_path = snapshot_download(
                    repo_id=model_id,
                    revision=model_revision if model_id == MODEL_ID else None,
                )
                self.model = StaticModel.from_pretrained(local_path)
                self.is_transformer = False
            else:
                # Load via sentence-transformers or pure transformers/torch
                try:
                    from sentence_transformers import SentenceTransformer

                    self.model = SentenceTransformer(model_id)
                    # Set dim attribute
                    self.model.dim = self.model.get_sentence_embedding_dimension()
                    self.is_transformer = True
                except ImportError:
                    # Fallback to pure transformers/torch
                    import torch
                    from transformers import AutoTokenizer, AutoModel

                    class TransformerModelWrapper:
                        def __init__(self, model_name):
                            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
                            self.model = AutoModel.from_pretrained(model_name)
                            self.dim = self.model.config.hidden_size

                        def encode(self, texts) -> torch.Tensor:
                            # Mean Pooling implementation
                            inputs = self.tokenizer(
                                texts,
                                padding=True,
                                truncation=True,
                                return_tensors="pt",
                            )
                            with torch.no_grad():
                                outputs = self.model(**inputs)
                            attention_mask = inputs["attention_mask"]
                            token_embeddings = outputs[0]
                            input_mask_expanded = (
                                attention_mask.unsqueeze(-1)
                                .expand(token_embeddings.size())
                                .float()
                            )
                            sum_embeddings = torch.sum(
                                token_embeddings * input_mask_expanded, 1
                            )
                            sum_mask = torch.clamp(input_mask_expanded.sum(1), min=1e-9)
                            embeddings = sum_embeddings / sum_mask
                            # L2 Normalize
                            embeddings = torch.nn.functional.normalize(
                                embeddings, p=2, dim=1
                            )
                            return embeddings.cpu().numpy()  # type: ignore[return-value]

                    self.model = TransformerModelWrapper(model_id)
                    self.is_transformer = True
            self._model_loaded = True
        except Exception as e:
            logger.error("Failed to load embedding model: %s", e)
            self._model_load_failed = True
            self.model = None

    def wait_for_model(self, timeout_s: float = 60.0) -> bool:
        """Block until the model finishes loading or loading is declared failed.

        Returns True if the model is ready, False if loading failed or timed
        out.  Safe to call from tests and CLI entry points that require the
        model before proceeding.
        """
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._model_loaded:
                return True
            if self._model_load_failed:
                return False
            time.sleep(0.05)
        return self._model_loaded

    def encode(self, texts) -> np.ndarray | None:
        if self.model is None or getattr(self, "np", None) is None:
            return None
        if not texts:
            # model2vec raises on empty input; return an empty (0, dim) array
            # so callers (and tests) can rely on a stable shape contract.
            return self.np.empty((0, self.model.dim), dtype=self.np.float32)
        if getattr(self, "is_transformer", False) and hasattr(
            self.model, "get_sentence_embedding_dimension"
        ):
            vectors = self.model.encode(texts, normalize_embeddings=True)
        else:
            vectors = self.model.encode(texts)
        # L2-norm check: only validate in debug mode to avoid performance overhead
        import os as _os

        if _os.environ.get("AGENTIC_MEMORY_DEBUG_NORMS"):
            norms = self.np.linalg.norm(vectors, axis=1)
            if not self.np.allclose(norms, 1.0, atol=0.01):
                worst = int(self.np.argmax(self.np.abs(norms - 1.0)))
                raise ValueError(
                    f"Embedding L2 norm assertion failed: vector[{worst}] has "
                    f"norm={float(norms[worst]):.6f}, expected ~1.0 (atol=0.01). "
                    f"All norms={norms.tolist()}"
                )
        return vectors

    # ------------------------------------------------------------------
    # Cache write helpers — used by rebuild_index.py and _update_memory_
    # index_incremental in memory_mcp.py. Best-effort: a failed cache
    # write MUST NOT abort the calling operation; the search path will
    # just recompute the embedding on the next read.
    # ------------------------------------------------------------------

    def index_embedding(
        self,
        db,
        memory_id: str,
        content: str,
        category: str = "",
        tags: Optional[list] = None,
        source_file: str = "",
    ) -> None:
        """Compute and persist the embedding for a single memory row.

        No-op if the model isn't loaded. Best-effort — wraps the INSERT
        in try/except so callers (rebuild, save, incremental update) can
        call it without needing their own error handling.

        When MEMORY_CONTEXTUAL_RETRIEVAL=1, prepends a context prefix
        (category, tags, source_file) before embedding so the vector
        captures semantic context alongside raw text.
        """
        if self.model is None:
            return
        try:
            text = _embed_text_with_context(content, category, tags, source_file)
            chash = _content_hash(text)
            # P1-1 fix (2026-06-22): the previous version only checked
            # content_hash, so an upgrade of the embedding model would
            # not trigger re-embed of unchanged content.  The row stays
            # with the old (different) model_revision and old dim, but
            # the search code would use it as if it were the new
            # model.  Now we require BOTH the content_hash AND the
            # model_revision to match — model upgrades trigger a
            # re-embed of all existing rows on the next save.
            existing = db.execute(
                "SELECT 1 FROM memory_embeddings "
                "WHERE content_hash = ? AND model_revision = ?",
                (chash, MODEL_REVISION),
            ).fetchone()
            if existing:
                return
            vec = self.model.encode([text])[0]
            db.execute(
                "INSERT OR REPLACE INTO memory_embeddings "
                "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    chash,
                    vec.tobytes(),
                    MODEL_REVISION,
                    int(self.model.dim),
                    time.time(),
                ),
            )
        except Exception as e:
            logger.warning("index_embedding failed for %s: %s", memory_id, e)

    def index_embeddings_batch(self, db, items: list, stale_days: int = 30) -> int:
        """Batch-index a list of (memory_id, content) tuples.

        Returns the number of rows actually written (skips model2vec-
        unavailable case and DB write failures). Best-effort.
        """
        if self.model is None or not items:
            return 0
        try:
            import sys

            _self = sys.modules[__name__]
            # Look up metadata for contextual retrieval if enabled. Bound
            # the scan to rows that need re-embedding (no embedding or
            # stale by `stale_days`) so we don't full-scan 4k+ rows on
            # every batch.
            meta_map = {}
            if _self._CONTEXTUAL_ENABLED:
                try:
                    _cutoff = time.time() - (stale_days * 86400)
                    mid = "unknown"
                    for mid, content, tags, source_file in db.execute(
                        "SELECT m.id, m.content, m.tags, m.source_file "
                        "FROM memories m LEFT JOIN memory_embeddings e "
                        "  ON e.memory_id = m.id "
                        "WHERE m.deleted_at IS NULL "
                        "  AND (e.memory_id IS NULL OR e.updated_at < ?)",
                        (_cutoff,),
                    ).fetchall():
                        meta_map[mid] = (tags, source_file)
                except Exception:
                    logger.warning(
                        "Failed to load metadata for memory %s during encode", mid
                    )
                    pass

            texts = []
            for mid, content in items:
                if _self._CONTEXTUAL_ENABLED and mid in meta_map:
                    tags_json, source_file = meta_map[mid]
                    tags = _parse_tags(tags_json) if tags_json else []
                    category = mid.split("/")[0] if "/" in mid else ""
                    texts.append(
                        _embed_text_with_context(content, category, tags, source_file)
                    )
                else:
                    texts.append(_cache_text(content))

            chashes = [_content_hash(t) for t in texts]
            vecs = self.model.encode(texts)
            rows = [
                (
                    mid,
                    ch,
                    vec.tobytes(),
                    MODEL_REVISION,
                    int(self.model.dim),
                    time.time(),
                )
                for (mid, _content), ch, vec in zip(items, chashes, vecs)
            ]
            db.executemany(
                "INSERT OR REPLACE INTO memory_embeddings "
                "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            return len(rows)
        except Exception as e:
            logger.warning("index_embeddings_batch failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Cache-aware search
    # ------------------------------------------------------------------

    def clear_vec_index_cache(self) -> None:
        """Drop the in-process usearch index cache.

        Test helper. Production code does not need this — the cache
        self-invalidates when the singleton row's built_at changes.
        """
        self._vec_index_cache.clear()

    # ------------------------------------------------------------------
    # Vector index (usearch) integration — Sprint 4 / P2 #8
    # ------------------------------------------------------------------

    def _load_vec_index(
        self, db_path, db
    ) -> tuple[Any, dict | None] | tuple[None, None]:
        """Try to load the persisted usearch index for this DB.

        Returns (Index, meta_dict) on success, (None, None) if the
        index is missing, corrupt, dimension-mismatched, or any error
        occurs. The caller MUST treat None as "fall back to full scan".

        The loaded Index + meta are cached in self._vec_index_cache so
        subsequent searches on the same DB don't re-parse the BLOB
        (10K vectors is ~5MB; parsing that per call would erase the
        ANN speedup).

        Cache invalidation: when the singleton row's `built_at` or the
        BLOB length changes, we know a rebuild happened and we reload.
        """
        cache_key = str(db_path)
        try:
            row = db.execute(
                "SELECT n_vectors, dim, metric, quantization, connectivity, "
                "       expansion_add, expansion_search, built_at, length(index_blob) "
                "FROM memory_vec_idx WHERE id=1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None, None  # table missing
        if row is None:
            return None, None  # no index built yet
        (
            n_vectors,
            dim,
            metric,
            qdtype,
            connectivity,
            exp_add,
            exp_s,
            built_at,
            blob_len,
        ) = row
        meta = {
            "n_vectors": n_vectors,
            "dim": dim,
            "metric": metric,
            "quantization": qdtype,
            "connectivity": connectivity,
            "expansion_add": exp_add,
            "expansion_search": exp_s,
            "built_at": built_at,
            "blob_len": blob_len,
        }

        # Cache hit? Only reuse if both the timestamp AND the BLOB
        # length are stable. (Two rebuilds within the same second can
        # produce the same built_at; the BLOB length is the tiebreaker.)
        cached = self._vec_index_cache.get(cache_key)
        if (
            cached is not None
            and cached[1]["built_at"] == built_at
            and cached[1]["blob_len"] == blob_len
        ):
            return cached  # type: ignore[no-any-return]

        # Dim mismatch between persisted index and current model —
        # refuse to use the index. Caller will fall back to full scan.
        if self.model is not None and dim != int(self.model.dim):
            return None, None

        # Load the BLOB.
        try:
            blob = db.execute(
                "SELECT index_blob FROM memory_vec_idx WHERE id=1"
            ).fetchone()[0]
            from usearch.index import Index as USearchIndex

            idx = USearchIndex(
                ndim=dim,
                metric=metric,
                dtype=qdtype,
                connectivity=connectivity,
                expansion_add=exp_add,
                expansion_search=exp_s,
            )
            idx.load(blob)
        except Exception as e:
            # Corrupt BLOB, dimension mismatch with what's actually
            # in the bytes, missing usearch, whatever. Caller falls
            # back to full scan; we DO NOT cache the failure so the
            # next call gets a fresh attempt.
            logger.warning("usearch index load failed for %s: %s", db_path, e)
            return None, None

        self._vec_index_cache[cache_key] = (idx, meta)
        return idx, meta

    def _search_via_index(self, idx, meta, db, query, limit) -> str | list[dict] | None:
        """ANN top-K + FP32 rerank. Returns list[dict] like _search_full_scan,
        or None if the indexed path should fall back (any error inside).

        Two-stage:
          1. ANN: usearch top-200 (or fewer for tiny DBs) by cosine.
          2. Rerank: load FP32 vectors from memory_embeddings for the
             candidates and re-sort by exact cosine. Falls back to
             re-encoding for cache misses / stale rows.

        New memories added since the last index rebuild are still
        discoverable: a LEFT JOIN against memory_vec_keys surfaces them,
        and they are reranked alongside the ANN candidates.
        """
        if self.model is None or self.np is None:
            return None
        n_vectors = meta["n_vectors"]
        if n_vectors == 0:
            return []

        # Adaptive k: cap at the smaller of {plan: 200, n_vectors}.
        # On a 4-row DB we still want the 4 to come back; on a 1M-row
        # DB the candidate set is bounded.
        ann_k = min(200, n_vectors)

        query_vec = self._embed_query(query)

        try:
            matches = idx.search(query_vec, ann_k)
        except Exception as e:
            logger.warning("usearch search failed: %s", e)
            return None

        candidate_keys = [int(k) for k in matches.keys.tolist()]

        # Pull key -> memory_id for the ANN candidates. Unindexed
        # memories (added since the last rebuild) are handled below.
        key_to_mid: dict = {}
        if candidate_keys:
            placeholders = ",".join("?" for _ in candidate_keys)
            try:
                rows = db.execute(
                    f"SELECT key, memory_id FROM memory_vec_keys "
                    f"WHERE key IN ({placeholders})",
                    candidate_keys,
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            key_to_mid = {int(k): mid for k, mid in rows}

        # Surface unindexed memories too. After a `memory_save` that
        # hasn't been followed by a rebuild, the new row exists in
        # `memories` but not in `memory_vec_keys` — without this merge
        # step it would be invisible to search.
        #
        # Anti-join via NOT EXISTS uses the UNIQUE INDEX on
        # memory_vec_keys.memory_id, so SQLite only walks the
        # unindexed side (typically near-empty). The earlier LEFT JOIN
        # version scanned the full memories table — O(N) per search.
        # Capped at UNINDEXED_SAFETY_NET_LIMIT; covers the
        # "added a few hundred memories since last rebuild" case.
        # If a user has truly added more, `rebuild_vec_index` is
        # the right answer (1.9s on 10K, 13s on 100K).
        try:
            unindexed = db.execute(
                "SELECT m.id FROM memories m "
                "WHERE m.deleted_at IS NULL "
                "AND NOT EXISTS "
                "  (SELECT 1 FROM memory_vec_keys k WHERE k.memory_id = m.id) "
                f"LIMIT {UNINDEXED_SAFETY_NET_LIMIT}"
            ).fetchall()
        except sqlite3.OperationalError:
            unindexed = []
        unindexed_ids = [r[0] for r in unindexed]

        # Combine: ANN candidates (in order) + unindexed (appended).
        # Duplicates are fine — the rerank is keyed by memory_id and
        # will just use whichever entry it sees first.
        candidate_mids: list = []
        seen: set = set()
        for k in candidate_keys:
            mid = key_to_mid.get(k)
            if mid and mid not in seen:
                seen.add(mid)
                candidate_mids.append(mid)
        for mid in unindexed_ids:
            if mid not in seen:
                seen.add(mid)
                candidate_mids.append(mid)

        if not candidate_mids:
            return []

        # Fetch memory rows for the candidate set.
        placeholders = ",".join("?" for _ in candidate_mids)
        try:
            mem_rows = db.execute(
                f"SELECT id, content, source_file, tags FROM memories "
                f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                candidate_mids,
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        mem_by_id = {r[0]: r for r in mem_rows}

        # Fetch FP32 cache rows for the candidates. Cache miss /
        # stale rows get re-encoded in the rerank loop below.
        cached: dict = {}
        try:
            for mid, chash, emb_blob, rev in db.execute(
                f"SELECT memory_id, content_hash, embedding, model_revision "
                f"FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                candidate_mids,
            ).fetchall():
                cached[mid] = (chash, emb_blob, rev)
        except sqlite3.OperationalError:
            # memory_embeddings table missing — rerank will re-encode
            # everything. Slower, but correct.
            cached = {}

        # Rerank: compute exact cosine for each candidate. Write back
        # any re-encodes we had to do (best-effort).
        # Two-phase: (1) collect per-candidate (vec, row, mid, chash) so
        # the cache-miss re-encodes can be batched in a single model
        # call, and (2) one np.dot over the stacked (n, dim) matrix
        # produces all similarities at once. This is the 8.6 perf gate
        # path — the per-candidate dot in the naive version added
        # ~3-5ms on 200 candidates.
        dim = meta["dim"]
        items: list = []  # (mid, row, vec, chash, was_cache_hit)
        to_save: list = []
        text_to_encode: list = []
        text_to_indices: list = []
        for mid in candidate_mids:
            row = mem_by_id.get(mid)
            if row is None:
                continue
            content = row[1]
            text = _cache_text(content)
            chash = _content_hash(text)
            entry = cached.get(mid)
            vec = None
            if entry is not None and entry[0] == chash and entry[2] == MODEL_REVISION:
                try:
                    v = self.np.frombuffer(entry[1], dtype=self.np.float32)
                    if v.size == dim:
                        vec = v
                except Exception:
                    logger.warning("Failed to decode cached vector for memory %s", mid)
                    vec = None
            if vec is None:
                # Queue for batch re-encode.
                text_to_encode.append(text)
                text_to_indices.append(len(items))
            items.append((mid, row, vec, chash))

        if text_to_encode:
            try:
                fresh = self.model.encode(text_to_encode)
            except Exception as e:
                fresh = None
                logger.warning("batch re-encode failed: %s", e)
            if fresh is not None:
                for k, idx_in_items in enumerate(text_to_indices):
                    mid, row, _old_vec, chash = items[idx_in_items]
                    items[idx_in_items] = (mid, row, fresh[k], chash)
                    to_save.append((mid, chash, fresh[k], dim))

        # Single matrix multiply for all candidates.
        valid = [
            (mid, row, vec, chash) for mid, row, vec, chash in items if vec is not None
        ]
        if not valid:
            return []
        vec_matrix = self.np.stack([v for _mid, _row, v, _c in valid])
        sims = vec_matrix @ query_vec  # (n,) — one matmul

        scored = list(zip(sims.tolist(), [row for _mid, row, _v, _c in valid]))

        if to_save:
            try:
                now = time.time()
                with db:
                    db.executemany(
                        "INSERT OR REPLACE INTO memory_embeddings "
                        "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (mid, chash, vec.tobytes(), MODEL_REVISION, dim, now)
                            for mid, chash, vec, dim in to_save
                        ],
                    )
            except Exception as e:
                logger.warning("failed to write embeddings back: %s", e)

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, row in scored[:limit]:
            results.append(
                {
                    "id": row[0],
                    "source": row[2],
                    "tags": json.loads(row[3]) if row[3] else [],
                    "score": sim,
                    "preview": row[1][:200] if row[1] else "",
                }
            )
        return results

    def _search_full_scan(self, db, query, limit) -> str | list[dict]:
        """The pre-Sprint-4 search path: encode every memory and dot-product.

        Used as the fallback whenever the index is absent, corrupt, or
        the indexed path returns None. Kept verbatim from the original
        search() implementation so behavior is bit-for-bit identical.
        """
        if self.model is None or self.np is None:
            return "Embedding model not loaded."
        # Get all memories (the candidate set).
        rows = db.execute(
            "SELECT id, content, source_file, tags FROM memories WHERE deleted_at IS NULL"
        ).fetchall()
        if not rows:
            return "No memories found."

        # LEFT-JOIN the embedding cache. Rows missing from the cache or
        # stale (content_hash or model_revision mismatch) get re-encoded
        # on the fly and best-effort written back.
        cached = {}
        try:
            for mid, chash, emb_blob, rev in db.execute(
                "SELECT memory_id, content_hash, embedding, model_revision "
                "FROM memory_embeddings"
            ).fetchall():
                cached[mid] = (chash, emb_blob, rev)
        except sqlite3.OperationalError:
            # memory_embeddings table missing — open_db didn't run or
            # migration hasn't been applied. Treat as empty cache.
            cached = {}

        vectors = []
        to_save = []
        for row in rows:
            mid = row[0]
            content = row[1]
            text = _cache_text(content)
            chash = _content_hash(text)
            entry = cached.get(mid)
            if entry is not None and entry[0] == chash and entry[2] == MODEL_REVISION:
                # Cache hit: load bytes straight into a 1-D float32 vector.
                vec = self.np.frombuffer(entry[1], dtype=self.np.float32)
                if vec.size != self.model.dim:
                    # Stale row from a different model dim — fall through
                    # to re-encode and overwrite below.
                    vec = self.model.encode([text])[0]
                    to_save.append((mid, chash, vec, int(self.model.dim)))
            else:
                # Cache miss / stale model: encode fresh.
                vec = self.model.encode([text])[0]
                to_save.append((mid, chash, vec, int(self.model.dim)))
            vectors.append(vec)

        # Single matrix-multiply. We handle the n=1 case explicitly
        # because np.stack of one vector gives shape (1, dim), and the
        # search hot path must not choke on small DBs.
        content_vecs = (
            self.np.stack(vectors)
            if vectors
            else self.np.empty((0, self.model.dim), dtype=self.np.float32)
        )

        # Best-effort save-back. Wrapped in a single transaction so it's
        # fast; failures here are non-fatal because we already have the
        # in-memory vectors for this call.
        if to_save:
            try:
                now = time.time()
                try:
                    db.execute("PRAGMA busy_timeout = 50;")
                except Exception:
                    pass
                try:
                    with db:
                        db.executemany(
                            "INSERT OR REPLACE INTO memory_embeddings "
                            "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                (mid, chash, vec.tobytes(), MODEL_REVISION, dim, now)
                                for mid, chash, vec, dim in to_save
                            ],
                        )
                finally:
                    try:
                        db.execute("PRAGMA busy_timeout = 30000;")
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("failed to write embeddings back: %s", e)

        # Encode the query and guard the ndim so a 1-row DB doesn't
        # collapse similarities into a scalar.
        query_vec = self._embed_query(query)

        if content_vecs.shape[0] == 0:
            return []

        similarities = self.np.dot(content_vecs, query_vec.T).squeeze()
        if similarities.ndim == 0:
            similarities = self.np.array([float(similarities)])

        # Sort by similarity.
        top_indices = self.np.argsort(similarities)[::-1][:limit]

        results = []
        for idx in top_indices:
            row = rows[idx]
            results.append(
                {
                    "id": row[0],
                    "source": row[2],
                    "tags": json.loads(row[3]) if row[3] else [],
                    "score": float(similarities[idx]),
                    "preview": row[1][:200] if row[1] else "",
                }
            )
        return results

    def search_by_vector(
        self, query_vec, db_path, limit=5, db=None
    ) -> list[dict] | str:
        """Search by vector directly (no text encoding needed).

        Uses the usearch index (with ANN + FP32 rerank) when available.
        Falls back to a full scan if the index is missing or corrupt.

        Args:
            query_vec: numpy float32 array of shape (dim,).
            db_path: Path to memory.db.
            limit: Number of results.
            db: Optional pre-existing DB connection. If None, opens one.

        Returns list of dicts with id, source, tags, score, preview.
        Falls back to ``search()`` with a dummy query if the model is not
        loaded, since the text-based path's full scan handles raw encoding.
        """
        from infra._lazy_imports import connection_pool

        own_db = False
        if db is None:
            db = connection_pool.get(str(db_path), timeout=30.0)
            db.execute("PRAGMA busy_timeout = 30000;")
            db.execute("PRAGMA journal_mode = WAL;")
            own_db = True

        try:
            idx, meta = self._load_vec_index(db_path, db)
            if idx is not None and meta is not None and meta.get("n_vectors", 0) > 0:
                try:
                    ann_k = min(200, meta["n_vectors"])
                    matches = idx.search(query_vec, ann_k)
                    candidate_keys = [int(k) for k in matches.keys.tolist()]
                except Exception:
                    logger.warning("Failed to search vec index")
                    candidate_keys = []

                key_to_mid = {}
                if candidate_keys:
                    placeholders = ",".join("?" for _ in candidate_keys)
                    try:
                        rows = db.execute(
                            f"SELECT key, memory_id FROM memory_vec_keys "
                            f"WHERE key IN ({placeholders})",
                            candidate_keys,
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                    key_to_mid = {int(k): mid for k, mid in rows}

                candidate_mids = []
                seen = set()
                for k in candidate_keys:
                    mid = key_to_mid.get(k)
                    if mid and mid not in seen:
                        seen.add(mid)
                        candidate_mids.append(mid)

                if candidate_mids:
                    placeholders = ",".join("?" for _ in candidate_mids)
                    mem_rows = db.execute(
                        f"SELECT id, content, source_file, tags FROM memories "
                        f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                        candidate_mids,
                    ).fetchall()
                    mem_by_id = {r[0]: r for r in mem_rows}

                    cached = {}
                    try:
                        for mid, chash, emb_blob, rev in db.execute(
                            f"SELECT memory_id, content_hash, embedding, model_revision "
                            f"FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                            candidate_mids,
                        ).fetchall():
                            cached[mid] = (chash, emb_blob, rev)
                    except sqlite3.OperationalError:
                        cached = {}

                    dim = meta["dim"]
                    valid = []
                    for mid in candidate_mids:
                        row = mem_by_id.get(mid)
                        if row is None:
                            continue
                        entry = cached.get(mid)
                        if entry is not None:
                            try:
                                v = self.np.frombuffer(entry[1], dtype=self.np.float32)
                                if v.size == dim:
                                    valid.append((row, v))
                            except Exception:
                                logger.warning(
                                    "Failed to decode cached vector for rerank"
                                )
                                pass

                    if valid:
                        vec_matrix = self.np.stack([v for _row, v in valid])
                        sims = vec_matrix @ query_vec
                        valid = [(sims[i], row) for i, (row, _) in enumerate(valid)]
                        valid.sort(key=lambda x: x[0], reverse=True)
                        results = []
                        for sim, row in valid[:limit]:
                            results.append(
                                {
                                    "id": row[0],
                                    "source": row[2],
                                    "tags": json.loads(row[3]) if row[3] else [],
                                    "score": sim,
                                    "preview": row[1][:200] if row[1] else "",
                                }
                            )
                        return results
            return self._search_full_scan_memories(db, query_vec, limit)
        finally:
            if own_db:
                from infra._lazy_imports import safe_close_db

                safe_close_db(db)

    def _search_full_scan_memories(self, db, query_vec, limit) -> list[dict]:
        """Fallback: scan all memory_embeddings and compute cosine similarity."""
        try:
            rows = db.execute(
                "SELECT memory_id, embedding FROM memory_embeddings LIMIT 500"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if not rows:
            return []
        norm_q = self.np.linalg.norm(query_vec)
        if norm_q < 1e-8:
            return []
        scored = []
        for mid, blob in rows:
            try:
                vec = self.np.frombuffer(blob, dtype=self.np.float32)
                sim = float(
                    self.np.dot(query_vec, vec)
                    / (norm_q * max(self.np.linalg.norm(vec), 1e-8))
                )
                scored.append((sim, mid))
            except Exception:
                logger.warning("Failed to compute similarity for memory %s", mid)
                continue
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, mid in scored[:limit]:
            results.append({"id": mid, "score": sim, "preview": ""})
        return results

    def search(self, query, db_path, limit=5) -> str | list[dict]:
        if self.model is None:
            return "Embedding search unavailable. Install model2vec: pip install model2vec numpy"

        from infra.memory_common import connection_pool

        # TTL cache for vector search results. The full-scan path is
        # O(N) over all embeddings; caching helps when an agent issues
        # the same query many times in a loop. TTL is short (30s) so
        # newly-added memories are reflected quickly.

        cache_key = (str(db_path), query, limit)
        cached = _vec_cache_get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        db = connection_pool.get(str(db_path), timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000;")
        db.execute("PRAGMA journal_mode = WAL;")

        try:
            # Try the indexed path first. Any failure inside
            # _search_via_index returns None, which we treat as
            # "fall back to full scan". The full-scan path is the
            # pre-Sprint-4 implementation, bit-for-bit identical.
            idx, meta = self._load_vec_index(db_path, db)
            if idx is not None and meta is not None and meta.get("n_vectors", 0) > 0:
                try:
                    indexed_result = self._search_via_index(idx, meta, db, query, limit)
                    if indexed_result is not None:
                        # P0 fix #4: record each non-empty result as a
                        # recent hit on the T1/T2 side of ARC, so the
                        # ghost-list pressure signal reflects real
                        # workload.
                        self._arc_track_hits(indexed_result, db_path)
                        _vec_cache_put(cache_key, indexed_result)
                        return indexed_result
                    # None here = the indexed path errored; fall through.
                except Exception as e:
                    logger.warning(
                        "indexed search failed, falling back to full scan: %s", e
                    )
            result = self._search_full_scan(db, query, limit)
            if isinstance(result, list):
                self._arc_track_hits(result, db_path)
            _vec_cache_put(cache_key, result)
            return result
        finally:
            from infra.memory_common import safe_close_db

            safe_close_db(db)

    def _arc_track_hits(self, results: str | list, db_path) -> None:
        """P0 fix #4: forward embedding-search hits to the ARC cache.

        Each non-empty ``memory_id`` is a successful retrieval, which
        on the ARC side corresponds to a hit on the T1/T2 live lists.
        If that memory is in the ghost list (``arc_ghosts``), we flip
        its ``would_have_been_hit`` flag so the next
        ``compute_eviction_pressure`` call sees a higher hit rate and
        pressures the tier migrator to be less aggressive.

        Best-effort: any failure is swallowed because telemetry must
        never break the search hot path.
        """
        if not results:
            return
        try:
            from infra.db import _local_state
            if getattr(_local_state, "in_save_pipeline", False):
                return

            from infra.arc_cache import ARCCache

            cache = ARCCache(db_path)
            try:
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    mid = r.get("id")
                    if not mid:
                        continue
                    cache.record_recent(mid)
                    # Promote the ghost hit if this memory_id was ever
                    # evicted. Idempotent: a no-op when not a ghost.
                    try:
                        cache.record_hit(mid)
                    except Exception:
                        pass
            finally:
                cache.close()
        except Exception as e:
            logger.warning("ARC hit-tracking failed: %s", e)


    def index_chunk_embeddings_batch(self, db, chunks: list[dict]) -> int:
        if self.model is None or not chunks:
            return 0
        try:
            texts = [_chunk_cache_text(c["content"]) for c in chunks]
            chashes = [_chunk_content_hash(t) for t in texts]
            vecs = self.model.encode(texts)
            now = time.time()
            rows = [
                (
                    c.get("chunk_id"),
                    c["parent_id"],
                    ch,
                    vec.tobytes(),
                    MODEL_REVISION,
                    int(self.model.dim),
                    now,
                )
                for c, ch, vec in zip(chunks, chashes, vecs)
            ]
            db.executemany(
                "INSERT OR REPLACE INTO memory_chunk_embeddings "
                "(chunk_id, parent_id, content_hash, embedding, model_revision, dim, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        c.get("chunk_id"),
                        c["parent_id"],
                        ch,
                        vec.tobytes(),
                        MODEL_REVISION,
                        int(self.model.dim),
                        now,
                    )
                    for c, ch, vec in zip(chunks, chashes, vecs)
                ],
            )
            return len(rows)
        except Exception as e:
            logger.warning("index_chunk_embeddings_batch failed: %s", e)
            return 0

    def _load_chunk_vec_index(self, db_path, db) -> tuple[Any, dict | None] | tuple[None, None]:
        cache_key = str(db_path)
        try:
            row = db.execute(
                "SELECT n_vectors, dim, metric, quantization, connectivity, "
                "       expansion_add, expansion_search, built_at, length(index_blob) "
                "FROM memory_chunk_vec_idx WHERE id=1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None, None
        if row is None:
            return None, None
        (n_vectors, dim, metric, qdtype, connectivity, exp_add, exp_s, built_at, blob_len) = row
        meta = {
            "n_vectors": n_vectors, "dim": dim, "metric": metric,
            "quantization": qdtype, "connectivity": connectivity,
            "expansion_add": exp_add, "expansion_search": exp_s,
            "built_at": built_at, "blob_len": blob_len,
        }
        cached = self._chunk_index_cache.get(cache_key)
        if cached is not None and cached[1]["built_at"] == built_at and cached[1]["blob_len"] == blob_len:
            return cast(tuple[Any, dict[Any, Any] | None], cached)
        if self.model is not None and dim != int(self.model.dim):
            return None, None
        try:
            blob = db.execute("SELECT index_blob FROM memory_chunk_vec_idx WHERE id=1").fetchone()[0]
            from usearch.index import Index as USearchIndex
            idx = USearchIndex(
                ndim=dim, metric=metric, dtype=qdtype,
                connectivity=connectivity, expansion_add=exp_add, expansion_search=exp_s,
            )
            idx.load(blob)
        except Exception as e:
            logger.warning("chunk usearch index load failed for %s: %s", db_path, e)
            return None, None
        self._chunk_index_cache[cache_key] = (idx, meta)
        return idx, meta

    def _search_chunks_via_index(self, idx, meta, db, query, limit) -> list[dict] | None:
        if self.model is None or self.np is None:
            return None
        n_vectors = meta["n_vectors"]
        if n_vectors == 0:
            return []
        ann_k = min(200, n_vectors)
        query_vec = self._embed_query(query)
        if query_vec is None:
            return None
        try:
            matches = idx.search(query_vec, ann_k)
        except Exception as e:
            logger.warning("chunk usearch search failed: %s", e)
            return None
        candidate_keys = [int(k) for k in matches.keys.tolist()]
        if not candidate_keys:
            return []
        placeholders = ",".join("?" for _ in candidate_keys)
        try:
            rows = db.execute(
                f"SELECT key, chunk_id, parent_id FROM memory_chunk_vec_keys "
                f"WHERE key IN ({placeholders})",
                candidate_keys,
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        key_map = {int(r[0]): {"chunk_id": r[1], "parent_id": r[2]} for r in rows}
        mid_scores: dict = {}
        for k in candidate_keys:
            entry = key_map.get(k)
            if entry is None:
                continue
            parent_id = entry["parent_id"]
            idx_in_matches = candidate_keys.index(k)
            score = float(matches.distances[idx_in_matches])
            if parent_id not in mid_scores or score > mid_scores[parent_id]["score"]:
                mid_scores[parent_id] = {"parent_id": parent_id, "score": score, "chunk_id": entry["chunk_id"]}
        ranked = sorted(mid_scores.values(), key=lambda x: x["score"], reverse=True)[:limit]
        return ranked

    def _search_chunks_full_scan(self, db, query, limit) -> list[dict]:
        if self.model is None or self.np is None:
            return []
        try:
            rows = db.execute(
                "SELECT mc.chunk_id, mc.parent_id, mc.embedding FROM memory_chunk_embeddings mc "
                "JOIN memory_chunks ck ON ck.id = mc.chunk_id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if not rows:
            return []
        query_vec = self._embed_query(query)
        if query_vec is None:
            return []
        dim_val = int(self.model.dim)
        mid_scores: dict = {}
        for chunk_id, parent_id, emb_blob in rows:
            try:
                vec = self.np.frombuffer(emb_blob, dtype=self.np.float32)
                if vec.size != dim_val:
                    continue
                score = float(self.np.dot(vec, query_vec))
                if parent_id not in mid_scores or score > mid_scores[parent_id]["score"]:
                    mid_scores[parent_id] = {"parent_id": parent_id, "score": score, "chunk_id": chunk_id}
            except Exception:
                continue
        ranked = sorted(mid_scores.values(), key=lambda x: x["score"], reverse=True)[:limit]
        return ranked

    def search_chunks(self, db, query, limit=5, db_path=None) -> list[dict]:
        from infra.memory_common import connection_pool as _cp
        _path = Path(db_path) if db_path else None
        if _path is None:
            try:
                from infra.memory_config import get_memory_paths
                _, local_mem, _ = get_memory_paths()
                _path = local_mem / "memory.db"
            except Exception:
                _path = Path("memory/memory.db")
        _db = db
        own_db = False
        if _db is None:
            _db = _cp.get(str(_path), timeout=30.0)
            _db.execute("PRAGMA busy_timeout = 30000;")
            _db.execute("PRAGMA journal_mode = WAL;")
            own_db = True
        try:
            if not self._CHUNK_SEARCH_ENABLED:
                return self._search_chunks_full_scan(_db, query, limit)
            idx, meta = self._load_chunk_vec_index(_path, _db)
            if idx is not None and meta is not None and meta.get("n_vectors", 0) > 0:
                result = self._search_chunks_via_index(idx, meta, _db, query, limit)
                if result is not None:
                    return result
            return self._search_chunks_full_scan(_db, query, limit)
        finally:
            if own_db:
                from infra.memory_common import safe_close_db
                safe_close_db(_db)


# ---------------------------------------------------------------------------
# Chunk-level helpers (module-level)
# ---------------------------------------------------------------------------

_CHUNK_MAX_SIZE = 500
_CHUNK_OVERLAP = 50


def chunk_memory(content: str, max_chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """Split content into paragraph-level chunks for multi-vector retrieval."""
    if not content:
        return []
    max_cs = max(100, max_chunk_size)
    ov = max(0, min(overlap, max_cs // 5))
    paras = [p for p in content.split("\n\n") if p.strip()]
    chunks: list[dict] = []
    buf = ""
    b_start = 0
    cidx = 0
    cursor = 0
    for para in paras:
        p_start = cursor
        p_end = cursor + len(para)
        candidate = buf + ("\n\n" if buf else "") + para
        if buf and len(candidate) > max_cs and len(buf) >= max_cs // 4:
            end = b_start + len(buf)
            chunks.append(
                {"content": buf, "chunk_idx": cidx, "start_offset": b_start, "end_offset": end}
            )
            cidx += 1
            if ov > 0 and len(buf) > ov:
                b_start = end - ov
                buf = buf[-ov:] + "\n\n"
            else:
                b_start = p_start
                buf = ""
        buf += ("\n\n" if buf else "") + para
        cursor = p_end + 2
    if buf:
        end = b_start + len(buf)
        chunks.append(
            {"content": buf, "chunk_idx": cidx, "start_offset": b_start, "end_offset": end}
        )
    if not chunks and content:
        chunks.append(
            {"content": content, "chunk_idx": 0, "start_offset": 0, "end_offset": len(content)}
        )
    return chunks


def _chunk_cache_text(content: str) -> str:
    if not content:
        return ""
    return unicodedata.normalize("NFKC", content[:500])


def _chunk_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
# Loading StaticModel takes ~700ms cold, so we lazily cache one instance per
# process and hand it out to every caller. The double-checked locking
# pattern below is safe under concurrent first-callers and cheap on the
# hot path (a single None-check on subsequent calls).
_es_singleton: Optional[EmbeddingSearch] = None
_es_singleton_lock = threading.Lock()


def get_embedding_search() -> EmbeddingSearch:
    """Return the process-wide EmbeddingSearch singleton.

    The first call loads the model (~700ms cold, <50ms warm). Every
    subsequent call is a near-free None-check on the module global.

    Thread-safe: concurrent first-callers are serialized by a Lock and
    all observe the same instance.
    """
    global _es_singleton
    if _es_singleton is None:
        with _es_singleton_lock:
            if _es_singleton is None:
                _es_singleton = EmbeddingSearch()
    return _es_singleton


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: embedding_search.py <query> [limit]")
        sys.exit(1)

    query = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    db_env = os.environ.get("MEMORY_DB_PATH")
    if db_env:
        db_path = Path(db_env)
    else:
        from infra.memory_config import get_memory_paths

        _, local_mem, _ = get_memory_paths()
        db_path = local_mem / "memory.db"

    searcher = EmbeddingSearch()
    results = searcher.search(query, db_path, limit)

    if isinstance(results, str):
        print(results)
    else:
        print(f"\nSemantic search results for: '{query}' (Top {len(results)})")
        print("=" * 80)
        for i, r in enumerate(results, 1):
            print(f"[{i}] {r['id']}  (Score: {r['score']:.4f})")
            print(f"    Source: memory/{r['source']}")
            print(f"    Tags: {', '.join(r['tags'])}")
            print("-" * 80)


from infra.memory_common import make_lazy_getattr

__getattr__ = make_lazy_getattr({"_CONTEXTUAL_ENABLED": "contextual_retrieval"})
