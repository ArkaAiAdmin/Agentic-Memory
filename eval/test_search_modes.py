import os
os.environ["HF_HUB_OFFLINE"] = "1"

import pytest
import sqlite3
import time
from pathlib import Path
from search.orchestrator import search_memories
from save.pipeline import save_memory

def _ensure_kg_facts_fts(conn: sqlite3.Connection) -> None:
    """Ensure kg_facts_fts virtual table exists and trigger is set."""
    conn.execute("DROP TABLE IF EXISTS kg_facts_fts")
    for trig in ("kg_facts_fts_ai", "kg_facts_fts_ad", "kg_facts_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    conn.execute(
        "CREATE VIRTUAL TABLE kg_facts_fts USING fts5("
        "subject, predicate, object, context, "
        "content='kg_facts', content_rowid='id', "
        "tokenize='porter unicode61')"
    )
    conn.execute(
        "CREATE TRIGGER kg_facts_fts_ai AFTER INSERT ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); END"
    )
    conn.execute(
        "CREATE TRIGGER kg_facts_fts_ad AFTER DELETE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); END"
    )
    conn.execute(
        "CREATE TRIGGER kg_facts_fts_au AFTER UPDATE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); END"
    )
    conn.commit()

def test_colbert_projection_weights_loaded():
    """Verify that ColBERT projection weights are loaded from checkpoint and are not random."""
    from infra.colbert_encoder import _get_colbert_model
    model, tokenizer, projection = _get_colbert_model()
    if model is not None:
        # Check that weight is loaded and not all zero
        weight = projection.linear.weight
        assert weight is not None
        assert weight.shape == (128, 768)
        assert weight.sum().item() != 0.0
        # Check bias does not exist (bias=False)
        assert getattr(projection.linear, "bias", None) is None

def test_splade_vocab_size_mapping():
    """Verify that SPLADE maps to the true vocabulary space (up to 30522) and not just 0-767."""
    from infra.splade_encoder import encode_sparse
    sparse = encode_sparse("python machine learning optimization")
    if sparse is not None:
        # Check that we have vocabulary dimensions beyond 767
        vocab_ids = [vid for vid, _ in sparse]
        assert any(vid > 767 for vid in vocab_ids), f"Only found low vocab IDs: {vocab_ids}"
        assert all(0 <= vid < 30522 for vid in vocab_ids)

def test_search_mode_fts(tmp_path):
    """Test that fts mode runs FTS-only search.

    Uses a clean DB (no pre-existing prod notes) so the FTS query can only
    match the two notes saved here — the dirty ``temp_db_path`` fixture copies
    the entire live DB, which pollutes FTS ranking with unrelated sessions.
    """
    from eval._fixtures import bootstrap_temp_db_clean

    db_path = tmp_path / "memory.db"
    bootstrap_temp_db_clean(db_path)
    save_memory(content="Python is a coding language.", category="lessons", title_slug="python-lang", db_path=str(db_path), safety_wiring=False)
    save_memory(content="JavaScript is also widely used.", category="lessons", title_slug="js-lang", db_path=str(db_path), safety_wiring=False)

    # FTS search for Python
    res = search_memories(db_path, "Python", mode="fts")
    assert res["count"] > 0, f"Expected results but got 0. Output: {res.get('output')}"
    contents = [r["content"] for r in res["results"]]
    assert any("Python" in c for c in contents), (
        f"Expected 'Python' in at least one result content, got: {contents[:3]}"
    )

    # FTS search for JavaScript
    res = search_memories(db_path, "JavaScript", mode="fts")
    assert res["count"] > 0, f"Expected results but got 0. Output: {res.get('output')}"
    contents = [r["content"] for r in res["results"]]
    assert any("JavaScript" in c for c in contents), (
        f"Expected 'JavaScript' in at least one result content, got: {contents[:3]}"
    )

def test_search_mode_semantic(temp_db_path):
    """Test that semantic mode runs without error and returns correct response shape.

    NOTE: Content-correctness (i.e. finding 'apples' for 'fruit') is NOT asserted here
    because the usearch vector index is shared across all DBs (it's built from the
    production install, not per-temp_db_path). Asserting which specific memories rank
    first belongs in eval/longmemeval. This test only verifies the mode parameter is
    wired correctly end-to-end.
    """
    save_memory(content="I love eating delicious apples.", category="lessons", title_slug="eating-apples", db_path=str(temp_db_path), safety_wiring=False)
    save_memory(content="Programming in C++.", category="lessons", title_slug="cpp-prog", db_path=str(temp_db_path), safety_wiring=False)

    res = search_memories(temp_db_path, "fruit", mode="semantic")

    # Must return a well-formed response dict regardless of result count
    assert isinstance(res, dict), f"Expected dict, got {type(res)}"
    assert "results" in res, f"Missing 'results' key: {res.keys()}"
    assert "count" in res, f"Missing 'count' key: {res.keys()}"
    assert isinstance(res["results"], list), "results must be a list"
    assert res["count"] == len(res["results"]), (
        f"count={res['count']} does not match len(results)={len(res['results'])}"
    )

def test_search_mode_facts(temp_db_path):
    """Test that facts mode returns empty results but populates related_facts."""
    conn = sqlite3.connect(str(temp_db_path))
    _ensure_kg_facts_fts(conn)
    # Insert a fact
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, confidence, locked, first_seen, last_seen, mention_count, source_memory, context, belief_status, epistemic_source, fact_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sky", "color", "blue", 0.9, 0, time.time(), time.time(), 1, None, "generic", "active", "agent", "observation")
    )
    conn.commit()
    conn.close()

    res = search_memories(temp_db_path, "blue", mode="facts", include_facts=True)
    assert res["results"] == []
    assert res["count"] == 0
    assert "related_facts" in res
    assert len(res["related_facts"]) > 0
    assert res["related_facts"][0]["object"] == "blue"

def test_search_mode_graph(temp_db_path):
    """Test that graph mode initiates search from multi-hop traversal."""
    res = search_memories(temp_db_path, "Python programming", mode="graph")
    assert isinstance(res, dict)
    assert "results" in res
