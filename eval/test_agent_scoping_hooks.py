import os
import sys
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_context import get_agent, clear_agent
from memory_bootstrap import get_pinned_notes, get_high_importance, get_recent_notes, get_stats
from recall import _fetch_pinned, _fetch_recent_digests, _fetch_high_importance, session_recap
from eval._fixtures import bootstrap_temp_db_clean


def test_agent_context_env_fallback():
    # 1. Test fallback when thread-local is empty and env var is set
    clear_agent()
    orig_env = os.environ.get("MEMORY_AGENT_ID")
    if "MEMORY_AGENT_ID" in os.environ:
        del os.environ["MEMORY_AGENT_ID"]

    # No environment variable set, thread-local is empty -> default agent
    ctx = get_agent()
    assert ctx.agent_id == "default"
    assert ctx.namespace == "default"

    # Set environment variable
    os.environ["MEMORY_AGENT_ID"] = "agent-test-1"
    clear_agent()  # clear thread-local so it recalculates
    ctx = get_agent()
    assert ctx.agent_id == "agent-test-1"
    assert ctx.namespace == "agent-test-1"

    # Thread-local takes precedence over environment variable
    from agent_context import init_agent
    init_agent("thread-agent-2")
    ctx = get_agent()
    assert ctx.agent_id == "thread-agent-2"
    assert ctx.namespace == "thread-agent-2"

    # Cleanup
    clear_agent()
    if orig_env is not None:
        os.environ["MEMORY_AGENT_ID"] = orig_env
    elif "MEMORY_AGENT_ID" in os.environ:
        del os.environ["MEMORY_AGENT_ID"]


def test_scoping_bootstrap_and_recall(tmp_path):
    db_path = tmp_path / "memory.db"
    bootstrap_temp_db_clean(db_path)

    # Insert mock memories
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        
        # 1. Scoped pinned memory for coder-1
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("agents/coder-1/lessons/pin1", "scoped pinned note 1", "lessons", 5, 0.8, 1, str(now), now_iso, now_iso, "agents/coder-1/lessons/pin1", 1, 1.0)
        )
        # 2. Global pinned memory
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("lessons/global-pin", "global pinned note", "lessons", 5, 0.9, 1, str(now), now_iso, now_iso, "lessons/global-pin", 1, 1.0)
        )
        # 3. Scoped high importance for coder-1
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("agents/coder-1/lessons/high1", "scoped high importance", "lessons", 5, 0.95, 0, str(now), now_iso, now_iso, "agents/coder-1/lessons/high1", 1, 1.0)
        )
        # 4. Global high importance
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("lessons/global-high", "global high importance", "lessons", 5, 0.91, 0, str(now), now_iso, now_iso, "lessons/global-high", 1, 1.0)
        )
        # 5. Scoped recent memory for coder-1
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("agents/coder-1/lessons/recent1", "scoped recent note", "lessons", 3, 0.5, 0, str(now), now_iso, now_iso, "agents/coder-1/lessons/recent1", 1, 1.0)
        )
        # 6. Global recent memory
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("lessons/global-recent", "global recent note", "lessons", 3, 0.5, 0, str(now), now_iso, now_iso, "lessons/global-recent", 1, 1.0)
        )
        # 7. Scoped session note for coder-1
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("agents/coder-1/sessions/sess1", "scoped session note", "sessions", 3, 0.5, 0, str(now), now_iso, now_iso, "agents/coder-1/sessions/sess1", 1, 1.0)
        )
        # 8. Global session note
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("sessions/global-sess", "global session note", "sessions", 3, 0.5, 0, str(now), now_iso, now_iso, "sessions/global-sess", 1, 1.0)
        )
        # 9. Coder-2 scoped note (private to coder-2)
        conn.execute(
            """INSERT INTO memories (id, content, category, importance, importance_score, pinned, updated_at, created_at, observed_at, source_file, logical_clock, fitness_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("agents/coder-2/lessons/pin2", "coder-2 private note", "lessons", 5, 0.99, 1, str(now), now_iso, now_iso, "agents/coder-2/lessons/pin2", 1, 1.0)
        )
        
        # 10. KG setup
        conn.execute("INSERT INTO kg_entities (id, name, entity_type) VALUES (1, 'entity1', 'concept')")
        conn.execute("INSERT INTO kg_entities (id, name, entity_type) VALUES (2, 'entity2', 'concept')")
        conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, source_memory, subject_entity_id, object_entity_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("coder-1", "uses", "scoping", "agents/coder-1/lessons/pin1", 1, 2)
        )
        conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, source_memory, subject_entity_id, object_entity_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("coder-2", "uses", "isolation", "agents/coder-2/lessons/pin2", 2, 1)
        )
        conn.commit()
    finally:
        conn.close()

    orig_env = os.environ.get("MEMORY_AGENT_ID")

    # --- TEST 1: default namespace (only global notes returned) ---
    clear_agent()
    if "MEMORY_AGENT_ID" in os.environ:
        del os.environ["MEMORY_AGENT_ID"]

    conn = sqlite3.connect(str(db_path))
    try:
        pinned = get_pinned_notes(conn)
        high = get_high_importance(conn)
        recent = get_recent_notes(conn)
        stats = get_stats(conn)

        pinned_ids = [n["id"] for n in pinned]
        assert "lessons/global-pin" in pinned_ids
        assert "agents/coder-1/lessons/pin1" not in pinned_ids
        assert "agents/coder-2/lessons/pin2" not in pinned_ids

        high_ids = [n["id"] for n in high]
        assert "lessons/global-high" in high_ids
        assert "agents/coder-1/lessons/high1" not in high_ids

        recent_ids = [n["id"] for n in recent]
        assert "lessons/global-recent" in recent_ids
        assert "agents/coder-1/lessons/recent1" not in recent_ids

        assert stats["total_notes"] == 4  # global-pin, global-high, global-recent, global-sess
        assert stats["pinned"] == 1
        assert stats["kg_facts"] == 0  # coder-1/2 facts are scoped
        assert stats["kg_entities"] == 0

        # recall.py functions
        rec_pinned = _fetch_pinned(conn, 10)
        rec_pinned_ids = [n["id"] for n in rec_pinned]
        assert "lessons/global-pin" in rec_pinned_ids
        assert "agents/coder-1/lessons/pin1" not in rec_pinned_ids

        rec_digests = _fetch_recent_digests(conn, 7, 10)
        rec_digest_ids = [n["id"] for n in rec_digests]
        assert "sessions/global-sess" in rec_digest_ids
        assert "agents/coder-1/sessions/sess1" not in rec_digest_ids

        rec_high = _fetch_high_importance(conn, 10)
        rec_high_ids = [n["id"] for n in rec_high]
        assert "lessons/global-high" in rec_high_ids
        assert "agents/coder-1/lessons/high1" not in rec_high_ids

        recap = session_recap(str(db_path))
        assert "global session note" in recap
        assert "scoped session note" not in recap
    finally:
        conn.close()

    # --- TEST 2: coder-1 namespace (coder-1 + global notes returned, coder-2 excluded) ---
    os.environ["MEMORY_AGENT_ID"] = "coder-1"
    clear_agent()

    conn = sqlite3.connect(str(db_path))
    try:
        pinned = get_pinned_notes(conn)
        high = get_high_importance(conn)
        recent = get_recent_notes(conn)
        stats = get_stats(conn)

        pinned_ids = [n["id"] for n in pinned]
        assert "lessons/global-pin" in pinned_ids
        assert "agents/coder-1/lessons/pin1" in pinned_ids
        assert "agents/coder-2/lessons/pin2" not in pinned_ids

        high_ids = [n["id"] for n in high]
        assert "lessons/global-high" in high_ids
        assert "agents/coder-1/lessons/high1" in high_ids

        recent_ids = [n["id"] for n in recent]
        assert "lessons/global-recent" in recent_ids
        assert "agents/coder-1/lessons/recent1" in recent_ids

        assert stats["total_notes"] == 8  # 4 global + 4 coder-1 notes
        assert stats["pinned"] == 2  # global-pin + coder-1-pin
        assert stats["kg_facts"] == 1  # coder-1 fact
        assert stats["kg_entities"] == 2  # entity1 and entity2

        # recall.py functions
        rec_pinned = _fetch_pinned(conn, 10)
        rec_pinned_ids = [n["id"] for n in rec_pinned]
        assert "lessons/global-pin" in rec_pinned_ids
        assert "agents/coder-1/lessons/pin1" in rec_pinned_ids
        assert "agents/coder-2/lessons/pin2" not in rec_pinned_ids

        rec_digests = _fetch_recent_digests(conn, 7, 10)
        rec_digest_ids = [n["id"] for n in rec_digests]
        assert "agents/coder-1/sessions/sess1" in rec_digest_ids
        assert "sessions/global-sess" not in rec_digest_ids

        rec_high = _fetch_high_importance(conn, 10)
        rec_high_ids = [n["id"] for n in rec_high]
        assert "lessons/global-high" in rec_high_ids
        assert "agents/coder-1/lessons/high1" in rec_high_ids

        recap = session_recap(str(db_path))
        assert "scoped session note" in recap
        assert "global session note" not in recap
    finally:
        conn.close()
        
    # Cleanup env
    clear_agent()
    if orig_env is not None:
        os.environ["MEMORY_AGENT_ID"] = orig_env
    elif "MEMORY_AGENT_ID" in os.environ:
        del os.environ["MEMORY_AGENT_ID"]
