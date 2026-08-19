"""Database manager for benchmark test instances with prebuilt DB caching."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Sequence

from .protocol import BenchmarkSession

logger = logging.getLogger(__name__)

BENCH_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = BENCH_ROOT / ".cache" / "dbs"


def _get_schema_version() -> int:
    try:
        from infra.migration_runner import SCHEMA_VERSION
        return int(SCHEMA_VERSION)
    except Exception:
        return 78


class BenchmarkDBManager:
    """Manages creation, population, and caching of SQLite databases for benchmarks."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._temp_dirs: list[Path] = []

    def __del__(self) -> None:
        self.cleanup_temp_dirs()

    def cleanup_temp_dirs(self) -> None:
        """Remove any temporary directories created during non-cached runs."""
        for d in self._temp_dirs:
            if d.exists():
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        self._temp_dirs.clear()

    def get_dataset_hash(self, sessions: Sequence[BenchmarkSession], tenant_id: str = "") -> str:
        """Compute MD5 hash over session IDs, contents, tenant_id, and SCHEMA_VERSION."""
        schema_v = _get_schema_version()
        hasher = hashlib.md5()
        hasher.update(f"v{schema_v}:{tenant_id}".encode("utf-8"))
        for s in sessions:
            hasher.update(s.session_id.encode("utf-8"))
            hasher.update(s.content.encode("utf-8"))
            hasher.update(s.timestamp.encode("utf-8"))
        return hasher.hexdigest()[:12]

    def get_or_create_db(
        self,
        suite_name: str,
        sessions: Sequence[BenchmarkSession],
        tenant_id: str = "benchmark",
        use_cache: bool = True,
        force_rebuild: bool = False,
    ) -> tuple[Path, float, bool]:
        """Get cached DB or populate a fresh one using fast batch indexing.

        Returns: (db_path, ingest_time_seconds, was_cached)
        """
        try:
            from eval._fixtures import bootstrap_temp_db_clean, populate_eval_memory_indexes_batch
        except ImportError:
            from _fixtures import bootstrap_temp_db_clean, populate_eval_memory_indexes_batch

        d_hash = self.get_dataset_hash(sessions, tenant_id=tenant_id)
        cached_db_path = self.cache_dir / f"{suite_name}_{d_hash}.db"

        if use_cache and cached_db_path.exists() and not force_rebuild:
            # Verify DB integrity and tenant match
            try:
                conn = sqlite3.connect(str(cached_db_path))
                count = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
                ).fetchone()[0]
                expected_count = len(set(s.session_id for s in sessions))
                if count >= expected_count and expected_count > 0:
                    return cached_db_path, 0.0, True
            except Exception as e:
                logger.warning("Cached DB invalid (%s), rebuilding...", e)
                try:
                    cached_db_path.unlink(missing_ok=True)
                except OSError:
                    pass

        # Target DB path
        if use_cache:
            target_path = cached_db_path
        else:
            tmp_d = Path(tempfile.mkdtemp(prefix=f"{suite_name}_"))
            self._temp_dirs.append(tmp_d)
            target_path = tmp_d / "memory.db"

        if target_path.exists():
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                pass

        bootstrap_temp_db_clean(target_path)

        t0 = time.time()
        conn = sqlite3.connect(str(target_path), timeout=60.0)
        batch_items = []

        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")

            for s in sessions:
                tags_json = json.dumps(s.tags)
                source_file = f"{suite_name}/{s.session_id}"
                conn.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, source_file, tags, created_at, updated_at,
                        observed_at, pinned, importance, category, tenant_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, ?, ?)""",
                    (s.session_id, s.content, source_file, tags_json,
                     s.timestamp, s.timestamp, s.timestamp, s.category, s.tenant_id or tenant_id),
                )
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO memories_fts (id, content) VALUES (?, ?)",
                        (s.session_id, s.content),
                    )
                except Exception as exc:
                    logger.debug("FTS insert failed for %s (non-fatal): %s", s.session_id, exc)

                batch_items.append((s.session_id, s.content, s.category, s.tags))

            conn.commit()

            # Fast batched multi-indexing pass (ColBERT, SPLADE, USearch, Chunks, KG)
            populate_eval_memory_indexes_batch(conn, batch_items, tenant_id=tenant_id)
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()

        if batch_items:
            try:
                from rebuild_vec_index import rebuild_vec_index

                stats = rebuild_vec_index(str(target_path))
                print(
                    f"Vector index built: {stats.get('n_indexed')} items "
                    f"({stats.get('serialized_bytes')} bytes) in {stats.get('elapsed_s', 0.0):.2f}s",
                    flush=True,
                )
            except Exception as exc:
                logger.warning("vec index build failed (non-fatal): %s", exc)

        ingest_time = time.time() - t0
        return target_path, round(ingest_time, 2), False

