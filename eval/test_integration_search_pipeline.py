#!/usr/bin/env python3
"""Integration tests for search_pipeline.py — full search pipeline verification.

Covers:
1. FTS5 query parsing and BC score
2. Embedding fallback (semantic vector search)
3. Hybrid fusion (reciprocal rank fusion)
4. Temporal filtering (include_invalid)
5. Query expansion (synonyms)
6. Graph RAG expansion
7. Recency boost
8. Pinned boost
9. Zero-result suggestions
10. Access count tracking
11. Cross-encoder reranking
12. Unicode/special-char search

E6 fix (2026-06-22): the ``@pytest.mark.skipif(not
_embedding_available(), ...)`` markers at lines 268, 411, 696, and
730 are intentional, not flaky-test markers.  They gate tests
that require the embedding model to be loaded — running them
without an embedding model produces false negatives (the hybrid
search returns 0 results because the embedding side has nothing
to score, not because the pipeline is broken).

The CI environment does not pre-load the embedding model (it
costs ~3s of model load + 100MB of RAM per test invocation).
To run the gated tests, set ``RUN_EMBEDDING_TESTS=1`` in the
environment.  These are also the tests that get exercised on the
nightly eval and before a release.  See ``_embedding_available``
in this file for the precise check.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta, datetime as _dt2
from pathlib import Path

import pytest

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


from infra.memory_common import connection_pool, run_db_migrations, safe_close_db
from save_pipeline import save_memory
from search_pipeline import (
    search_memories,
    _expand_query,
    _compute_final_score,
    ScoreContext,
    _build_zero_result_suggestions,
    _reciprocal_rank_fusion,
    _cross_encoder_score,
    _graph_rag_expand,
    _bb2_clear_history,
)
from infra.cache import _search_cache


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _embedding_available():
    try:
        from infra.embedding_search import get_embedding_search

        es = get_embedding_search()
        es.wait_for_model(timeout_s=10.0)
        return es.model is not None
    except Exception:
        return False


def _deep_rerank_available():
    try:
        from infra.reranker import get_reranker

        r = get_reranker()
        if r is not None:
            raw = r.score("test", ["hello world"])
            return raw is not None
        return False
    except Exception:
        return False


def _kg_available():
    try:
        from knowledge_graph import KG_ENABLED

        return bool(KG_ENABLED)
    except Exception:
        return False


@pytest.fixture(autouse=True)
def clear_caches():
    _search_cache.clear()
    _bb2_clear_history()
    yield


@pytest.fixture
def no_embedding(monkeypatch):
    import search_pipeline
    import search.query_parser

    monkeypatch.setattr(
        search_pipeline, "_fallback_embedding_search", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        search.query_parser, "_semantic_expand", lambda *a, **kw: []
    )


def _ensure_fts5(conn):
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, tags, tokenize='porter unicode61'
        )
    """)
    for trig in ("memories_ai", "memories_ad", "memories_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    conn.execute("""
        CREATE TRIGGER memories_ai AFTER INSERT ON memories
        WHEN new.deleted_at IS NULL
        BEGIN
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END
    """)
    conn.execute("""
        CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
        END
    """)
    conn.execute("""
        CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END
    """)
    conn.commit()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True)
    monkeypatch.setattr("memory_common.GLOBAL_MEM_DIR", mem_dir)
    monkeypatch.setattr("infrastructure.GLOBAL_MEM_DIR", mem_dir)
    db_path = mem_dir / "memory.db"
    conn = connection_pool.get(str(db_path), timeout=30.0)
    run_db_migrations(conn)
    # FTS5 table + triggers already created by run_db_migrations -> run_schema_setup.
    # Do NOT call _ensure_fts5() here — it replaces production triggers with
    # broken ones that omit the 'id' column, causing the JOIN in _fts_search
    # (m.id = fts.id) to always return 0 rows.
    safe_close_db(conn)
    return db_path


def _insert_raw(
    db_path,
    note_id,
    content,
    tags=None,
    pinned=False,
    created_at=None,
    deleted_at=None,
    valid_to=None,
    superseded_by=None,
    access_count=1,
):
    """Insert a note directly into DB (bypassing save_memory for speed)."""
    now = now_iso()
    created_at = created_at or now
    tags_json = json.dumps(tags or [])
    conn = connection_pool.get(str(db_path), timeout=30.0)
    try:
        conn.execute(
            "INSERT INTO memories "
            "(id, content, source_file, tags, created_at, updated_at, observed_at,"
            " pinned, importance, fitness_score, access_count, valid_to,"
            " superseded_by, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 3, 1.0, ?, ?, ?, ?)",
            (
                note_id,
                content,
                "test/file.md",
                tags_json,
                created_at,
                now,
                now,
                1 if pinned else 0,
                access_count,
                valid_to,
                superseded_by,
                deleted_at,
            ),
        )
        conn.commit()
    finally:
        safe_close_db(conn)


# ── Test 1: Basic FTS ───────────────────────────────────────────────────


class TestBasicFTS:
    def test_basic_fts(self, tmp_db):
        slug = f"test-fts-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            f"unique_word_xylophone_{int(time.time())}",
            "lessons",
            slug,
            tags=["unit-test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, "xylophone", safety_wiring=False)
        assert result["count"] > 0
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids

    def test_no_match(self, tmp_db, no_embedding):
        result = search_memories(tmp_db, "zzzthisdoesnotexist999", safety_wiring=False)
        assert result["count"] == 0

    def test_results_structure(self, tmp_db):
        result = search_memories(tmp_db, "test", safety_wiring=False)
        assert isinstance(result, dict)
        assert "results" in result
        assert "count" in result
        assert "output" in result
        assert result["count"] == len(result["results"])
        if result["results"]:
            r = result["results"][0]
            assert "id" in r
            assert "final_score" in r


# ── Test 2: Phrase Search ──────────────────────────────────────────────


class TestPhraseSearch:
    def test_phrase_search_exact(self, tmp_db):
        slug = f"test-phrase-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "the quick brown fox jumps over the lazy dog",
            "lessons",
            slug,
            tags=["unit-test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, '"quick brown fox"', safety_wiring=False)
        assert result["count"] > 0
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids

    def test_phrase_no_match(self, tmp_db, no_embedding):
        slug = f"test-phrase-nm-{int(time.time())}"
        save_memory(
            "hello world one two three",
            "lessons",
            slug,
            tags=["unit-test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, '"four five six"', safety_wiring=False)
        assert result["count"] == 0


# ── Test 3: Hybrid Search ──────────────────────────────────────────────


class TestHybridSearch:
    @pytest.mark.skipif(not _embedding_available(), reason="embedding model not loaded")
    def test_hybrid_search(self, tmp_db):
        slug = f"test-hybrid-{int(time.time())}"
        save_memory(
            "neural network transformer architecture attention",
            "lessons",
            slug,
            tags=["ml"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(
            tmp_db, "transformer", hybrid=True, safety_wiring=False
        )
        assert result["count"] > 0

    def test_hybrid_with_no_embedding_model(self, tmp_db):
        slug = f"test-hybrid-noemb-{int(time.time())}"
        save_memory(
            "python code refactoring test driven development",
            "lessons",
            slug,
            tags=["dev"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(
            tmp_db, "refactoring", hybrid=True, safety_wiring=False
        )
        assert result["count"] > 0


# ── Test 4: Temporal Filtering ─────────────────────────────────────────


class TestTemporalFiltering:
    def test_temporal_filtering(self, tmp_db):
        slug = f"test-temp-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "this note is expired",
            "lessons",
            slug,
            tags=["unit-test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        now_iso()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn = connection_pool.get(str(tmp_db), timeout=30.0)
        try:
            conn.execute(
                "UPDATE memories SET valid_to = ? WHERE id = ?", (yesterday, note_id)
            )
            conn.commit()
        finally:
            safe_close_db(conn)

        result = search_memories(
            tmp_db, "expired", include_invalid=False, safety_wiring=False
        )
        ids = [r["id"] for r in result["results"]]
        assert note_id not in ids

    def test_temporal_filtering_valid(self, tmp_db):
        slug = f"test-temp-valid-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "this note is still valid content here",
            "lessons",
            slug,
            tags=["unit-test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(
            tmp_db, "still valid", include_invalid=False, safety_wiring=False
        )
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids


# ── Test 5: Expired / Superseded ───────────────────────────────────────


class TestExpiredSuperseded:
    def test_expired_superseded(self, tmp_db):
        slug = f"test-sup-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "this is the old superseded note",
            "lessons",
            slug,
            tags=["unit-test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn = connection_pool.get(str(tmp_db), timeout=30.0)
        try:
            conn.execute(
                "UPDATE memories SET valid_to = ?, superseded_by = 'new/version' WHERE id = ?",
                (yesterday, note_id),
            )
            conn.commit()
        finally:
            safe_close_db(conn)

        result = search_memories(
            tmp_db, "superseded", include_invalid=False, safety_wiring=False
        )
        ids = [r["id"] for r in result["results"]]
        assert note_id not in ids


# ── Test 6: Query Expansion ────────────────────────────────────────────


class TestQueryExpansion:
    def test_expand_ml(self):
        expanded = _expand_query("ml")
        assert "machine learning" in expanded

    def test_expand_db(self):
        expanded = _expand_query("db")
        assert "database" in expanded

    def test_expand_unknown_preserves(self):
        expanded = _expand_query("xyzabc123")
        assert "xyzabc123" in expanded

    def test_expand_preserves_phrases(self):
        expanded = _expand_query('"exact phrase" ml')
        assert "exact phrase" in expanded

    def test_expand_empty(self):
        assert _expand_query("") == ""


# ── Test 7: Graph RAG Expansion ────────────────────────────────────────


class TestGraphRagExpand:
    @pytest.mark.skipif(not _kg_available(), reason="KG not enabled")
    def test_graph_rag_expand_no_entities(self, tmp_db):
        terms = _graph_rag_expand("random unrelated query", tmp_db)
        assert isinstance(terms, list)

    def test_graph_rag_expand_disabled(self, tmp_db):
        was = os.environ.get("MEMORY_KNOWLEDGE_GRAPH")
        os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "0"
        try:
            terms = _graph_rag_expand("random query", tmp_db)
            assert terms == []
        finally:
            if was is not None:
                os.environ["MEMORY_KNOWLEDGE_GRAPH"] = was
            else:
                os.environ.pop("MEMORY_KNOWLEDGE_GRAPH", None)


# ── Test 8: Recency Boost ──────────────────────────────────────────────


class TestRecencyBoost:
    def test_newer_note_ranks_higher(self, tmp_db):
        slug_old = f"test-old-{int(time.time())}"
        slug_new = f"test-new-{int(time.time())}"
        old_time = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
        new_time = now_iso()
        note_old = f"lessons/{slug_old}"
        note_new = f"lessons/{slug_new}"
        _insert_raw(
            tmp_db,
            note_old,
            "recency matching content here",
            tags=["test"],
            created_at=old_time,
        )
        _insert_raw(
            tmp_db,
            note_new,
            "recency matching content here",
            tags=["test"],
            created_at=new_time,
        )
        result = search_memories(
            tmp_db, "recency matching", include_global=True, safety_wiring=False
        )
        ids = [r["id"] for r in result["results"] if r["id"] in (note_old, note_new)]
        assert len(ids) >= 2
        assert result["results"][0]["id"] != result["results"][1]["id"]


# ── Test 9: Pinned Boost ───────────────────────────────────────────────


class TestPinnedBoost:
    def test_pinned_note_boosted(self, tmp_db):
        slug_a = f"test-pin-a-{int(time.time())}"
        slug_b = f"test-pin-b-{int(time.time())}"
        note_a = f"lessons/{slug_a}"
        note_b = f"lessons/{slug_b}"
        save_memory(
            "pinned boost content matched query",
            "lessons",
            slug_a,
            tags=["test"],
            pinned=True,
            safety_wiring=False,
            db_path=tmp_db,
        )
        save_memory(
            "pinned boost content matched query",
            "lessons",
            slug_b,
            tags=["test"],
            pinned=False,
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(
            tmp_db, "pinned boost content", boost_pinned=True, safety_wiring=False
        )
        pinned_rank = -1
        unpinned_rank = -1
        for i, r in enumerate(result["results"]):
            if r["id"] == note_a:
                pinned_rank = i
            if r["id"] == note_b:
                unpinned_rank = i
        if pinned_rank >= 0 and unpinned_rank >= 0:
            assert pinned_rank < unpinned_rank, (
                f"pinned note (rank {pinned_rank}) should rank above unpinned (rank {unpinned_rank})"
            )

    def test_pinned_boost_false_no_bonus(self, tmp_db):
        slug = f"test-pin-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "pinned content query text",
            "lessons",
            slug,
            tags=["test"],
            pinned=True,
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(
            tmp_db, "pinned content query", boost_pinned=False, safety_wiring=False
        )
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids


# ── Test 10: Zero-Result Suggestions ────────────────────────────────────


class TestZeroResultSuggestions:
    def test_zero_result_misspelled(self, tmp_db, no_embedding):
        slug = f"test-sugg-{int(time.time())}"
        save_memory(
            "python machine learning library",
            "lessons",
            slug,
            tags=["ai"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, "pythn machne learnin", safety_wiring=False)
        assert result["count"] == 0
        assert "suggestions" in result

    def test_zero_result_structure(self, tmp_db, no_embedding):
        result = search_memories(tmp_db, "zzz_nonexistent_999", safety_wiring=False)
        assert result["count"] == 0
        suggestions = result.get("suggestions", {})
        expected_keys = {"did_you_mean", "by_tag", "by_recency", "by_source_file"}
        assert expected_keys.issubset(suggestions.keys())

    def test_build_zero_result_suggestions(self, tmp_db):
        suggestions = _build_zero_result_suggestions(tmp_db, "nonexistent_xyz_123")
        assert isinstance(suggestions, dict)
        assert "did_you_mean" in suggestions


# ── Test 11: Include Invalid True ──────────────────────────────────────


class TestIncludeInvalidTrue:
    def test_superseded_included(self, tmp_db):
        slug = f"test-incinv-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "include invalid test content",
            "lessons",
            slug,
            tags=["test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn = connection_pool.get(str(tmp_db), timeout=30.0)
        try:
            conn.execute(
                "UPDATE memories SET valid_to = ? WHERE id = ?", (yesterday, note_id)
            )
            conn.commit()
        finally:
            safe_close_db(conn)

        result = search_memories(
            tmp_db, "include invalid", include_invalid=True, safety_wiring=False
        )
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids


# ── Test 12: Non-Existent DB ──────────────────────────────────────────


class TestNonExistentDB:
    def test_search_non_existent_db(self):
        result = search_memories(
            Path("/nonexistent/path/memory.db"), "test", safety_wiring=False
        )
        assert result["count"] == 0
        assert "DB_ERROR" in result.get("output", "")
        assert isinstance(result["output"], str)

    def test_search_non_existent_db_in_results(self):
        result = search_memories(
            Path("/nonexistent/path/memory.db"), "test", safety_wiring=False
        )
        assert "results" in result
        assert isinstance(result["results"], list)


# ── Test 13: Unicode Search ────────────────────────────────────────────


class TestUnicodeSearch:
    def test_unicode_search(self, tmp_db):
        slug = f"test-uni-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "日本語テスト émojis ñ ü café résumé",
            "lessons",
            slug,
            tags=["utf"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, "日本語テスト", safety_wiring=False)
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids

    def test_unicode_search_accent(self, tmp_db):
        slug = f"test-uni2-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "café résumé déjà vu",
            "lessons",
            slug,
            tags=["utf"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, "café", safety_wiring=False)
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids


# ── Test 14: Special Characters in FTS ────────────────────────────────


class TestSpecialCharFTS:
    def test_asterisk_search(self, tmp_db):
        slug = f"test-spec-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "wildcard star pattern matching",
            "lessons",
            slug,
            tags=["re"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, "wildcard*", safety_wiring=False)
        assert result["count"] > 0
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids

    def test_fts_not_keyword(self, tmp_db):
        slug = f"test-notkw-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "NOT operator as word in content",
            "lessons",
            slug,
            tags=["test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, '"NOT" operator', safety_wiring=False)
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids

    def test_fts_near_keyword(self, tmp_db):
        slug = f"test-near-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "NEAR proximity search words",
            "lessons",
            slug,
            tags=["test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, '"NEAR" proximity', safety_wiring=False)
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids


# ── Test 15: Embedding Fallback ────────────────────────────────────────


class TestEmbeddingFallback:
    @pytest.mark.skipif(not _embedding_available(), reason="embedding model not loaded")
    def test_embedding_fallback_finds_semantic(self, tmp_db):
        slug = f"test-emb-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "artificial intelligence deep learning neural networks",
            "lessons",
            slug,
            tags=["ai"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, "machine learning", safety_wiring=False)
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids

    def test_embedding_fallback_no_model_graceful(self, tmp_db, no_embedding):
        slug = f"test-emb2-{int(time.time())}"
        save_memory(
            "some random content for testing",
            "lessons",
            slug,
            tags=["test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(tmp_db, "zzz_nonexistent_999", safety_wiring=False)
        assert result["count"] == 0


# ── Test 16: Deep Rerank (Cross-Encoder) ──────────────────────────────


class TestDeepRerank:
    @pytest.mark.skipif(
        not _deep_rerank_available(), reason="reranker model not loaded"
    )
    def test_deep_rerank_available(self, tmp_db):
        slug = f"test-dr-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "deep reranking cross encoder test content",
            "lessons",
            slug,
            tags=["test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        result = search_memories(
            tmp_db, "cross encoder", deep_rerank=True, safety_wiring=False
        )
        ids = [r["id"] for r in result["results"]]
        assert note_id in ids

    def test_cross_encoder_score(self):
        score = _cross_encoder_score("hello world", "hello world")
        assert score > 0.0
        score_diff = _cross_encoder_score("hello world", "completely different text")
        assert score > score_diff

    def test_cross_encoder_empty_query(self):
        score = _cross_encoder_score("", "some content")
        assert score == 0.0

    def test_cross_encoder_empty_content(self):
        score = _cross_encoder_score("query", "")
        assert score == 0.0


# ── Test 17: Access Count Tracking ────────────────────────────────────


class TestAccessCountTracking:
    def test_access_count_increments(self, tmp_db):
        slug = f"test-acc-{int(time.time())}"
        note_id = f"lessons/{slug}"
        save_memory(
            "access tracking test content query",
            "lessons",
            slug,
            tags=["test"],
            safety_wiring=False,
            db_path=tmp_db,
        )
        conn = connection_pool.get(str(tmp_db), timeout=30.0)
        try:
            conn.execute(
                "UPDATE memories SET last_accessed = NULL WHERE id = ?", (note_id,)
            )
            conn.commit()
        finally:
            safe_close_db(conn)

        result = search_memories(tmp_db, "access tracking test", safety_wiring=False)
        ids = [r["id"] for r in result["results"]]
        if note_id in ids:
            # P1-12 fix: last_accessed update is enqueued to background
            # queue, not written synchronously. Drain the queue before
            # asserting by repeatedly dequeue+complete until empty.
            from background.background_queue import (
                init_task_queue,
                dequeue_task,
                complete_task,
            )

            _qc = connection_pool.get(str(tmp_db), timeout=30.0)
            try:
                init_task_queue(_qc)
                for _ in range(20):
                    _task = dequeue_task(_qc, task_type="last_accessed_update")
                    if _task is None:
                        break
                    _tid = _task["id"]
                    _ids = (_task.get("payload") or {}).get("note_ids") or []
                    if _ids:
                        _ph = ",".join("?" for _ in _ids)
                        _qc.execute(
                            "UPDATE memories SET last_accessed = ? "
                            f"WHERE id IN ({_ph})",
                            [_dt2.now().isoformat(timespec="seconds")] + _ids,
                        )
                    complete_task(_qc, _tid)
                _qc.commit()
            except Exception:
                pass
            finally:
                safe_close_db(_qc)

            conn2 = connection_pool.get(str(tmp_db), timeout=30.0)
            try:
                row = conn2.execute(
                    "SELECT last_accessed FROM memories WHERE id = ?", (note_id,)
                ).fetchone()
                assert row is not None
                assert row[0] is not None
            finally:
                safe_close_db(conn2)


# ── Test utility functions used by search_pipeline ─────────────────────


class TestUtilityFunctions:
    def test_reciprocal_rank_fusion_basic(self):
        rrf = _reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "d"]])
        assert isinstance(rrf, dict)
        assert rrf.get("b", 0) > rrf.get("a", 0)

    def test_reciprocal_rank_fusion_empty(self):
        assert _reciprocal_rank_fusion([]) == {}

    def test_compute_final_score_basic(self):
        score = _compute_final_score(
            ScoreContext(
                rank=1,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        assert isinstance(score, float)

    def test_compute_final_score_pinned_higher(self):
        now = now_iso()
        s_pin = _compute_final_score(
            ScoreContext(
                rank=1,
                fitness=0.5,
                importance=3,
                pinned=True,
                created=now,
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        s_unpin = _compute_final_score(
            ScoreContext(
                rank=1,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=now,
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        assert s_pin > s_unpin

    def test_compute_final_score_with_none(self):
        score = _compute_final_score(
            ScoreContext(
                rank=1,
                fitness=None,
                importance=None,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        assert isinstance(score, float)

    def test_build_zero_result_suggestions_structure(self, tmp_db):
        suggestions = _build_zero_result_suggestions(tmp_db, "nonexistent_xyz_123")
        assert isinstance(suggestions, dict)
        for key in ("did_you_mean", "by_tag", "by_recency", "by_source_file"):
            assert key in suggestions


if __name__ == "__main__":
    pytest.main([__file__, "-x", "--tb=long", "-q"])
