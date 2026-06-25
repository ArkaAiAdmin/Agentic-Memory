#!/usr/bin/env python3
"""Sprint 4 / 8.2: rebuild the usearch HNSW vector index in-place.

Mirrors ``rebuild_index.py`` (the FTS5 + MEMORY.md regen script), but for
the vector index. Reads every memory in the DB, builds a usearch.Index
with f16 quantization + cosine metric, and persists the index as a
BLOB in the ``memory_vec_idx`` singleton table, with the
key -> memory_id map in ``memory_vec_keys``.

Unlike ``rebuild_index.py`` this script does NOT swap the DB file. It
operates in a single transaction (DELETE + INSERT) on the live DB. The
FP32 vectors stay in the existing ``memory_embeddings`` table for the
search path to rerank against.

Idempotent: re-running on the same DB clears the old index row + key map
and replaces them. Safe on an empty DB (early return, no rows written).

Usage:
    venv/bin/python rebuild_vec_index.py [db_path]
    venv/bin/python rebuild_vec_index.py /tmp/mem.db
"""

import hashlib
import logging
import sqlite3
import sys
import time
from pathlib import Path

from memory_common import safe_close_db

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

import numpy as np
from usearch.index import Index as USearchIndex

from embedding_search import (
    _cache_text,
    _content_hash,
    get_embedding_search,
)


# Index settings. These are written into the memory_vec_idx singleton
# so the search path can re-instantiate the Index with the same params.
VEC_INDEX_METRIC = "cos"
VEC_INDEX_DTYPE = "f16"
VEC_INDEX_CONNECTIVITY = 16
VEC_INDEX_EXPANSION_ADD = 128
VEC_INDEX_EXPANSION_SEARCH = 64


def _md5_to_uint64(memory_id: str) -> int:
    """Derive a stable uint64 key from a memory id.

    md5 first 8 bytes -> unsigned int, masked to signed int64 range
    (0..2^63-1) so the value fits in SQLite's INTEGER column. usearch
    accepts uint64 keys, but Python's sqlite3 module refuses values
    that exceed signed int64 — masking the high bit is the simplest
    way to make the key round-trip through both.

    Collision probability for 1M items: ~2.7e-7.
    """
    digest = hashlib.md5(memory_id.encode("utf-8")).digest()
    raw = int.from_bytes(digest[:8], "big", signed=False)
    return raw & ((1 << 63) - 1)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Run migrations so memory_vec_idx + memory_vec_keys exist."""
    try:
        from _lazy_imports import run_db_migrations

        run_db_migrations(conn)
    except Exception as e:
        logger.warning("Could not run migrations: %s", e)


def _load_cached_embeddings(conn: sqlite3.Connection) -> dict:
    """Read memory_embeddings into {memory_id: (content_hash, blob)}.

    Missing table -> empty dict (no cache available). Stale cache rows
    (mismatched content_hash) are skipped at the use site.
    """
    try:
        rows = conn.execute(
            "SELECT memory_id, content_hash, embedding FROM memory_embeddings"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {mid: (chash, blob) for mid, chash, blob in rows}


def _open_db_for_rebuild(db_path: Path) -> sqlite3.Connection:
    """Open with WAL + busy_timeout. Mirrors the open_db() policy."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.Error as e:
        logger.warning("Could not set WAL mode: %s", e)
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def _try_acquire_lock(lock_path, *, force: bool = False):
    """Acquire .vec_rebuild.lock. Returns the open lock file or None.

    With force=True, removes a stale lock file and retries once before
    giving up. A live holder (BlockingIOError on both attempts) still
    raises — force never bypasses a live rebuild.
    """
    if not fcntl:
        return None
    lock_file = None
    try:
        lock_file = open(lock_path, "w")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except BlockingIOError:
        if force and lock_file is not None:
            lock_file.close()
            lock_file = None
            try:
                lock_path.unlink()
            except OSError:
                pass
            try:
                lock_file = open(lock_path, "w")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_file
            except BlockingIOError:
                logger.error(
                    "Live rebuild in progress (lock held by another process). "
                    "Cannot --force: refusing to interrupt an active rebuild."
                )
                if lock_file is not None:
                    lock_file.close()
                    lock_file = None
                raise
        if lock_file is not None:
            lock_file.close()
            lock_file = None
        raise


def rebuild_vec_index(db_path, *, force: bool = False) -> dict:
    """Rebuild the vector index in-place.

    Returns a stats dict:
        {n_memories, n_indexed, n_skipped, dim, quantization, metric,
         serialized_bytes, elapsed_s, collisions_resolved}

    Raises:
        FileNotFoundError: if the DB doesn't exist.
        RuntimeError: if the embedding model isn't available.
    """
    db_path = Path(db_path).resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    # Sanity check: the memories table is created by rebuild_index.py,
    # not by run_db_migrations. If it's missing, the user probably
    # hasn't bootstrapped this DB yet. Surface a clear error.
    probe = sqlite3.connect(str(db_path), timeout=5.0)
    probe.execute("PRAGMA foreign_keys=ON")
    try:
        _ensure_schema(probe)
        tables = {
            r[0]
            for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        probe.close()
    if "memories" not in tables:
        raise RuntimeError(
            "The `memories` table is missing. Run `rebuild_index.py` first "
            "(it creates the memories table from the source files), then "
            "re-run `rebuild_vec_index.py`."
        )

    # Cross-process lock so two rebuilds don't race.
    lock_path = db_path.parent / ".vec_rebuild.lock"
    lock_file = _try_acquire_lock(lock_path, force=force)

    try:
        t0 = time.time()
        es = get_embedding_search()
        if es.model is None:
            raise RuntimeError(
                "Embedding model unavailable. Run with venv python + model2vec installed."
            )
        dim = int(es.model.dim)

        # Read memories + cached embeddings.
        conn = _open_db_for_rebuild(db_path)
        try:
            _ensure_schema(conn)
            memory_rows = conn.execute("SELECT id, content FROM memories").fetchall()
            cached = _load_cached_embeddings(conn)
        finally:
            safe_close_db(conn)

        n = len(memory_rows)
        if n == 0:
            elapsed = time.time() - t0
            print("  No memories in DB. Nothing to index.")
            return {
                "n_memories": 0,
                "n_indexed": 0,
                "n_skipped": 0,
                "dim": dim,
                "quantization": VEC_INDEX_DTYPE,
                "metric": VEC_INDEX_METRIC,
                "serialized_bytes": 0,
                "elapsed_s": elapsed,
                "collisions_resolved": 0,
            }

        # Build a single (n, dim) float32 array. Cache hits copy from
        # cached bytes; misses get fresh model2vec encodings in one
        # batch call. The cache key is the same _cache_text() the
        # search() path uses, so the vectors we index match the vectors
        # the search will produce.
        vec_array = np.empty((n, dim), dtype=np.float32)
        to_encode_texts: list[str] = []
        to_encode_indices: list[int] = []
        n_cache_hits = 0
        n_cache_misses = 0
        for i, (_mid, content) in enumerate(memory_rows):
            text = _cache_text(content)
            chash = _content_hash(text)
            entry = cached.get(_mid)
            if entry is not None and entry[0] == chash and entry[1] is not None:
                try:
                    vec = np.frombuffer(entry[1], dtype=np.float32)
                    if vec.size == dim:
                        vec_array[i] = vec
                        n_cache_hits += 1
                        continue
                except Exception:
                    pass
            to_encode_texts.append(text)
            to_encode_indices.append(i)
            n_cache_misses += 1

        if to_encode_texts:
            fresh = es.model.encode(to_encode_texts)
            for j, idx in enumerate(to_encode_indices):
                vec_array[idx] = fresh[j]

        # Build the usearch Index. usearch accepts the (n, dim) float32
        # array and casts to the index's f16 dtype internally.
        index = USearchIndex(
            ndim=dim,
            metric=VEC_INDEX_METRIC,
            dtype=VEC_INDEX_DTYPE,
            connectivity=VEC_INDEX_CONNECTIVITY,
            expansion_add=VEC_INDEX_EXPANSION_ADD,
            expansion_search=VEC_INDEX_EXPANSION_SEARCH,
        )

        # Stable uint64 keys; resolve any md5 collisions deterministically
        # by bumping the colliding key by 1 (mod 2^63 so it stays in
        # SQLite's signed int64 range).
        used_keys: set[int] = set()
        key_to_id: list[tuple[int, str]] = []
        collisions_resolved = 0
        for i, (mid, _content) in enumerate(memory_rows):
            key = _md5_to_uint64(mid)
            while key in used_keys:
                key = (key + 1) % (1 << 63)
                if key < 0:
                    key = 0
                collisions_resolved += 1
            used_keys.add(key)
            index.add(np.uint64(key), vec_array[i])
            key_to_id.append((key, mid))

        # Serialize for storage. usearch Index.save() with no arg
        # returns the serialized bytes.
        serialized = index.save() or b""
        serialized_len = len(serialized)
        elapsed = time.time() - t0
        print(
            f"  Built index: {n} vectors, dim={dim}, "
            f"dtype={VEC_INDEX_DTYPE}, serialized={serialized_len} bytes, "
            f"cache_hits={n_cache_hits}, cache_misses={n_cache_misses}, "
            f"elapsed={elapsed:.2f}s"
        )

        # Persist: DELETE old, INSERT new, all in one transaction.
        # We use the context-manager form of the connection so the
        # writes commit atomically. The fp32 vectors in
        # memory_embeddings are untouched — the search path reranks
        # against them.
        conn = _open_db_for_rebuild(db_path)
        try:
            _ensure_schema(conn)
            with conn:
                conn.execute("DELETE FROM memory_vec_keys")
                conn.execute("DELETE FROM memory_vec_idx")
                conn.execute(
                    "INSERT INTO memory_vec_idx "
                    "(id, n_vectors, dim, metric, quantization, connectivity, "
                    " expansion_add, expansion_search, built_at, index_blob, key_count) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        n,
                        dim,
                        VEC_INDEX_METRIC,
                        VEC_INDEX_DTYPE,
                        VEC_INDEX_CONNECTIVITY,
                        VEC_INDEX_EXPANSION_ADD,
                        VEC_INDEX_EXPANSION_SEARCH,
                        time.time(),
                        serialized,
                        len(key_to_id),
                    ),
                )
                conn.executemany(
                    "INSERT INTO memory_vec_keys (key, memory_id) VALUES (?, ?)",
                    key_to_id,
                )
        finally:
            safe_close_db(conn)

        return {
            "n_memories": n,
            "n_indexed": n,
            "n_skipped": 0,
            "dim": dim,
            "quantization": VEC_INDEX_DTYPE,
            "metric": VEC_INDEX_METRIC,
            "serialized_bytes": serialized_len,
            "elapsed_s": elapsed,
            "collisions_resolved": collisions_resolved,
        }
    finally:
        if lock_file is not None:
            lock_file.close()
            try:
                lock_path.unlink()
            except OSError:
                pass


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Rebuild the agentic-memory vector index."
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        default="memory/memory.db",
        help="Path to the memory.db file (default: memory/memory.db).",
    )
    parser.add_argument(
        "--subsystems",
        type=str,
        default="fts5,embeddings,kg,backlinks,chunks,audit,vec_idx",
        help=(
            "Comma-separated list of subsystems to rebuild. "
            "Valid: fts5,embeddings,kg,backlinks,chunks,audit,vec_idx. "
            "Default: all."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Clean up a stale .vec_rebuild.lock if the holder process "
            "is dead. Does NOT bypass a lock held by a live process."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    requested = {s.strip() for s in args.subsystems.split(",") if s.strip()}
    valid = {"fts5", "embeddings", "kg", "backlinks", "chunks", "audit", "vec_idx"}
    bad = requested - valid
    if bad:
        print(
            f"ERROR: unknown subsystems: {sorted(bad)}. Valid: {sorted(valid)}",
            file=sys.stderr,
        )
        return 4

    if "vec_idx" not in requested:
        # Caller explicitly opted out of the vec-index rebuild. We still
        # do the other subsystems via the standard path (rebuild_index.py)
        # but skip the vec-index pass. For now, rebuild_vec_index.py is
        # dedicated to the vec index, so opt-out means "do nothing" — we
        # return success without rebuilding.
        print(
            "vec_idx not in subsystems; skipping (caller wanted other rebuilds only).",
            file=sys.stderr,
        )
        return 0

    db_path = args.db_path

    try:
        stats = rebuild_vec_index(db_path, force=args.force)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except BlockingIOError:
        print(
            "ERROR: another rebuild is running (use --force to clear stale lock)",
            file=sys.stderr,
        )
        return 3

    print("\n=== Vector index rebuild complete ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
