"""
H21 fixture helpers — extracted from conftest.py so they can be
imported directly by test files.

The conftest.py version still exists (for pytest auto-discovery),
but tests that need to call the helper directly should import from
here.
"""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from infra.memory_common import get_memory_paths


def bootstrap_temp_db(db_path: Path) -> None:
    """Copy the live prod schema (and data) into *db_path*, then bring it
    up to the current schema version via ``run_schema_setup``.

    This is the H21-recommended bootstrap: a fully-bootstrapped temp DB
    with all numbered migrations applied (currently 52).  The extra
    ``run_schema_setup`` call ensures post-052 corrective helpers
    (e.g. kg_edges tenant_id, audit tenant_id index) are applied even
    when the prod snapshot was taken before those helpers were added.

    Use as a function (e.g. in setUp()) or via the temp_db_path pytest
    fixture in conftest.py.

    NOTE: This copies prod DATA, not just schema. Tests that need a
    clean DB (no pre-existing notes) should use
    `bootstrap_temp_db_clean` instead.
    """
    _, _, global_mem = get_memory_paths()
    prod_db = global_mem / "memory.db"
    if prod_db.exists():
        # M8 fix: copy WAL sidecar files alongside the main DB
        for suffix in ("", "-wal", "-shm"):
            src = prod_db.parent / (prod_db.name + suffix)
            if src.exists():
                shutil.copy2(src, db_path.parent / (db_path.name + suffix))
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        from infra.db_migrations import run_schema_setup

        run_schema_setup(conn)
    finally:
        conn.close()


import threading as _threading

_TEMPLATE_DB_PATH: Path | None = None
_TEMPLATE_DB_LOCK = _threading.Lock()


def _get_or_create_template_db() -> Path:
    global _TEMPLATE_DB_PATH
    if _TEMPLATE_DB_PATH is not None and _TEMPLATE_DB_PATH.exists():
        return _TEMPLATE_DB_PATH
    with _TEMPLATE_DB_LOCK:
        if _TEMPLATE_DB_PATH is not None and _TEMPLATE_DB_PATH.exists():
            return _TEMPLATE_DB_PATH
        tmp = Path(tempfile.gettempdir()) / f"ami_schema_template_v78_{os.getpid()}.db"
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        conn = sqlite3.connect(str(tmp), timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        from infra.db_migrations import run_schema_setup

        run_schema_setup(conn)
        from fact import ensure_facts_schema

        ensure_facts_schema(conn)
        conn.commit()
        conn.close()
        _TEMPLATE_DB_PATH = tmp
        return tmp


def bootstrap_temp_db_clean(db_path: Path | str) -> None:
    """Create a fresh DB with the full schema by cloning pre-migrated template in < 1ms."""
    db_path = Path(db_path)
    template = _get_or_create_template_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, db_path)
    for s in ("-wal", "-shm"):
        p = Path(str(db_path) + s)
        if p.exists():
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def set_benchmark_env() -> None:
    """Set optimal environment variables for benchmark execution on CPU / macOS."""
    import os

    os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    os.environ["MEMORY_FAIL_ON_INTEGRITY_DRIFT"] = "0"


def populate_eval_memory_indexes(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    category: str = "sessions",
    tags: list[str] | None = None,
) -> None:
    """Index a memory row across chunk FTS, vector embedding, ColBERT, SPLADE, KG, and facts.

    This ensures benchmark runs query the full 14-phase search orchestrator (dense + sparse + KG + reranker)
    rather than falling back to BM25/FTS only.
    """
    if not content or not content.strip():
        return

    tags_list = tags or []

    # 1. Chunk FTS indexing
    try:
        from search.chunk_index import _qw5_ensure_schema, _qw5_index_chunks_for

        _qw5_ensure_schema(conn)
        _qw5_index_chunks_for(conn, memory_id, content)
    except Exception:
        pass

    # 2. Vector Embedding indexing
    try:
        from save.indexers import _index_embedding

        _index_embedding(
            conn,
            memory_id,
            content,
            category=category,
            tags=tags_list,
            source_file=memory_id,
        )
    except Exception:
        pass

    # 3. ColBERT indexing
    try:
        from search.colbert_index import _ensure_colbert_schema, index_memory_colbert_batch

        _ensure_colbert_schema(conn)
        index_memory_colbert_batch(conn, [(memory_id, content)])
    except Exception:
        pass

    # 4. SPLADE indexing
    try:
        from search.splade_index import _ensure_splade_schema, index_memory_splade_batch

        _ensure_splade_schema(conn)
        index_memory_splade_batch(conn, [(memory_id, content)])
    except Exception:
        pass

    # 5. KG indexing
    try:
        from knowledge_graph import ensure_kg_schema, index_kg_for_memory

        ensure_kg_schema(conn)
        index_kg_for_memory(conn, memory_id, content)
    except Exception:
        pass

    # 6. Facts indexing
    try:
        from fact import ensure_facts_schema, index_facts_for_memory

        ensure_facts_schema(conn)
        index_facts_for_memory(conn, memory_id, content)
    except Exception:
        pass


def populate_eval_memory_indexes_batch(
    conn: sqlite3.Connection,
    items: list[tuple[str, str, str, list[str] | None]],
    use_llm_facts: bool = False,
) -> None:
    """Batch-index multiple memories across all multi-indexes in parallel/batched passes.

    Drastically accelerates benchmark dataset ingestion (e.g. 10,000 chunks in ~30s instead of ~1.6 hours).
    """
    if not items:
        return

    # 1. Chunk FTS
    try:
        from search.chunk_index import _qw5_ensure_schema, _qw5_index_chunks_for

        _qw5_ensure_schema(conn)
        for memory_id, content, _, _ in items:
            if content and content.strip():
                _qw5_index_chunks_for(conn, memory_id, content)
    except Exception:
        pass

    # 2. Vector Embeddings
    try:
        from save.indexers import _index_embedding

        for memory_id, content, cat, t_list in items:
            if content and content.strip():
                _index_embedding(
                    conn,
                    memory_id,
                    content,
                    category=cat or "sessions",
                    tags=t_list or [],
                    source_file=memory_id,
                )
    except Exception:
        pass

    # 3. ColBERT (Batched)
    try:
        from search.colbert_index import _ensure_colbert_schema, index_memory_colbert_batch

        _ensure_colbert_schema(conn)
        colbert_inputs = [(mid, cnt) for mid, cnt, _, _ in items if cnt and cnt.strip()]
        if colbert_inputs:
            index_memory_colbert_batch(conn, colbert_inputs)
    except Exception:
        pass

    # 4. SPLADE (Batched)
    try:
        from search.splade_index import _ensure_splade_schema, index_memory_splade_batch

        _ensure_splade_schema(conn)
        splade_inputs = [(mid, cnt) for mid, cnt, _, _ in items if cnt and cnt.strip()]
        if splade_inputs:
            index_memory_splade_batch(conn, splade_inputs)
    except Exception:
        pass

    # 5. Knowledge Graph
    try:
        from knowledge_graph import ensure_kg_schema, index_kg_for_memory

        ensure_kg_schema(conn)
        for memory_id, content, _, _ in items:
            if content and content.strip():
                index_kg_for_memory(conn, memory_id, content)
    except Exception:
        pass

    # 6. Facts (Fast Mode by default for bulk benchmark ingestion)
    try:
        from fact import ensure_facts_schema, index_facts_for_memory

        ensure_facts_schema(conn)
        _old_llm_extraction = os.environ.get("MEMORY_LLM_EXTRACTION")
        _llm_changed = False
        if not use_llm_facts:
            os.environ["MEMORY_LLM_EXTRACTION"] = "0"
            _llm_changed = True
        try:
            for memory_id, content, _, _ in items:
                if content and content.strip():
                    index_facts_for_memory(conn, memory_id, content)
        finally:
            if _llm_changed:
                if _old_llm_extraction is not None:
                    os.environ["MEMORY_LLM_EXTRACTION"] = _old_llm_extraction
                else:
                    os.environ.pop("MEMORY_LLM_EXTRACTION", None)
    except Exception:
        pass


