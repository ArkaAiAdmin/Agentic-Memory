"""Behavioral tests for Sprint 1 — Fact/Belief Separation.

Verifies:
1. Agent asserts belief → belief_status=active, epistemic_source=agent
2. Contradicting fact arrives → old belief gets belief_status=retracted, new gets active
3. Agent reinforces a low-confidence belief → belief_confidence increases
4. Agent queries 'show my active beliefs' → returns filtered results
5. Auto-saved fact → epistemic_source=auto_save, NOT agent
6. Belief with evidence chain → chain is stored and queryable
7. Fact type taxonomy is set correctly based on extraction method
8. Evidence chain staleness detection works
"""

import os
import sys
import sqlite3
import time

sys.path.insert(0, str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))))
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))

import pytest
import fact as fe
from fact.fact_schema import ensure_facts_schema
from belief import (
    ensure_beliefs_schema,
    ensure_belief_assertion,
    get_beliefs_for_fact,
    get_active_beliefs,
    update_belief_status,
    handle_evidence_chain_staleness,
    retract_dependent_beliefs,
)


@pytest.fixture
def conn():
    """Create an in-memory SQLite database with the full schema."""
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    # Create supporting tables
    c.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS kg_entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
    ensure_facts_schema(c)
    ensure_beliefs_schema(c)

    yield c
    c.close()


class TestBeliefCreation:
    """Agent asserts a belief → belief_status=active, epistemic_source=agent"""

    def test_agent_asserted_belief_is_active(self, conn):
        conn.execute("INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
                     ("test/m1", "test content"))
        fid = fe._upsert_fact(conn, "test", "is_a", "fact", 0.9, time.time(),
                              belief_status="active", epistemic_source="agent",
                              fact_type="agent_inference")
        assert fid is not None
        ba_id = ensure_belief_assertion(conn, fid, memory_id="test/m1",
                                         belief_status="active",
                                         epistemic_source="agent",
                                         asserting_agent_id="agent-1")
        assert ba_id is not None
        ba = get_beliefs_for_fact(conn, fid)
        assert ba is not None
        assert ba["belief_status"] == "active"
        assert ba["epistemic_source"] == "agent"
        assert ba["asserting_agent_id"] == "agent-1"

    def test_auto_saved_fact_has_auto_save_source(self, conn):
        conn.execute("INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
                     ("auto_save/tool_1", "auto saved content"))
        fid = fe._upsert_fact(conn, "tool_output", "has_value", "42", 0.8, time.time(),
                              belief_status="active", epistemic_source="auto_save",
                              fact_type="observation")
        assert fid is not None
        ba_id = ensure_belief_assertion(conn, fid, memory_id="auto_save/tool_1",
                                         belief_status="active",
                                         epistemic_source="auto_save")
        assert ba_id is not None
        ba = get_beliefs_for_fact(conn, fid)
        assert ba is not None
        assert ba["epistemic_source"] == "auto_save"
        assert ba["epistemic_source"] != "agent"

    def test_fact_type_is_stored(self, conn):
        for fact_type in ("observation", "agent_inference", "external_stated", "hypothesis", "derived"):
            fid = fe._upsert_fact(conn, "test", "type_is", fact_type, 1.0, time.time(),
                                  fact_type=fact_type)
            assert fid is not None
            row = conn.execute("SELECT fact_type FROM kg_facts WHERE id = ?", (fid,)).fetchone()
            assert row is not None
            assert row[0] == fact_type


class TestBeliefLifecycle:
    """Belief status changes: retract, deprecate, reinforce"""

    def test_retract_belief(self, conn):
        fid = fe._upsert_fact(conn, "old_belief", "is", "true", 0.9, time.time())
        ensure_belief_assertion(conn, fid, belief_status="active")
        ok = update_belief_status(conn, fid, "retracted", rationale="new evidence contradicts")
        assert ok is True
        ba = get_beliefs_for_fact(conn, fid)
        assert ba["belief_status"] == "retracted"
        # kg_facts should also be updated
        row = conn.execute("SELECT belief_status FROM kg_facts WHERE id = ?", (fid,)).fetchone()
        assert row[0] == "retracted"

    def test_deprecate_belief(self, conn):
        fid = fe._upsert_fact(conn, "dep_belief", "is", "stale", 0.5, time.time())
        ensure_belief_assertion(conn, fid, belief_status="active")
        ok = update_belief_status(conn, fid, "deprecated", rationale="no longer relevant")
        assert ok is True
        ba = get_beliefs_for_fact(conn, fid)
        assert ba["belief_status"] == "deprecated"

    def test_reinforce_belief_updates_confidence(self, conn):
        fid = fe._upsert_fact(conn, "reinforce_me", "has", "value", 0.3, time.time())
        ensure_belief_assertion(conn, fid, belief_status="active", confidence=0.3)
        # Reinforce: update belief_assertions with higher confidence
        ensure_belief_assertion(conn, fid, belief_status="active", confidence=0.9,
                                rationale="verified by evidence")
        ba = get_beliefs_for_fact(conn, fid)
        assert ba["confidence"] == 0.9

    def test_invalid_status_rejected(self, conn):
        fid = fe._upsert_fact(conn, "status_test", "is", "valid", 1.0, time.time())
        ensure_belief_assertion(conn, fid, belief_status="active")
        ok = update_belief_status(conn, fid, "nonexistent")
        assert ok is False


class TestBeliefQuery:
    """Agent queries beliefs with filters"""

    def test_get_active_beliefs_returns_only_active(self, conn):
        fids = []
        for i in range(3):
            fid = fe._upsert_fact(conn, f"entity_{i}", "status_is", "test", 1.0, time.time())
            ensure_belief_assertion(conn, fid, belief_status="active", confidence=0.9)
            fids.append(fid)
        # Deprecate one
        update_belief_status(conn, fids[1], "deprecated")
        active = get_active_beliefs(conn, belief_status="active")
        assert len(active) == 2
        for a in active:
            assert a["belief_status"] == "active"

    def test_get_active_beliefs_filters_by_confidence(self, conn):
        for i in range(3):
            fid = fe._upsert_fact(conn, f"conf_{i}", "confidence_is", str(i), 1.0, time.time())
            ensure_belief_assertion(conn, fid, belief_status="active", confidence=0.1 * (i + 1))
        high_conf = get_active_beliefs(conn, min_confidence=0.2)
        assert len(high_conf) >= 1
        for h in high_conf:
            assert h["confidence"] >= 0.2

    def test_get_active_beliefs_filters_by_source(self, conn):
        fid_a = fe._upsert_fact(conn, "src_test_a", "source_is", "agent", 1.0, time.time())
        fid_b = fe._upsert_fact(conn, "src_test_b", "source_is", "import", 1.0, time.time())
        ensure_belief_assertion(conn, fid_a, epistemic_source="agent")
        ensure_belief_assertion(conn, fid_b, epistemic_source="import")
        agent_beliefs = get_active_beliefs(conn, epistemic_source="agent")
        import_beliefs = get_active_beliefs(conn, epistemic_source="import")
        assert len(agent_beliefs) == 1
        assert len(import_beliefs) == 1
        assert agent_beliefs[0]["epistemic_source"] == "agent"
        assert import_beliefs[0]["epistemic_source"] == "import"


class TestEvidenceChain:
    """Evidence chain tracking and staleness"""

    def test_evidence_chain_stored_and_queryable(self, conn):
        # Create supporting facts
        fid1 = fe._upsert_fact(conn, "evidence_a", "supports", "b", 0.9, time.time())
        fid2 = fe._upsert_fact(conn, "evidence_b", "supports", "c", 0.8, time.time())
        # Assert a belief with evidence chain
        fid_main = fe._upsert_fact(conn, "main_claim", "derived_from", "evidence", 0.7, time.time())
        ensure_belief_assertion(conn, fid_main, evidence_chain=[fid1, fid2])
        ba = get_beliefs_for_fact(conn, fid_main)
        assert ba is not None
        assert ba["evidence_chain"] == [fid1, fid2]

    def test_evidence_chain_staleness_detects_superseded(self, conn):
        # Create original fact with evidence chain
        fid_support = fe._upsert_fact(conn, "support_fact", "supports", "main", 0.9, time.time())
        fid_main = fe._upsert_fact(conn, "main_claim", "is", "true", 0.7, time.time())
        ensure_belief_assertion(conn, fid_main, evidence_chain=[fid_support])
        # Create superseding fact and link
        fid_new = fe._upsert_fact(conn, "new_support", "supports", "main", 0.95, time.time())
        conn.execute("UPDATE kg_facts SET superseded_by = ? WHERE id = ?",
                     (fid_new, fid_support))
        conn.commit()
        # Run staleness check
        result = handle_evidence_chain_staleness(conn, batch_size=100)
        assert result["deprecated"] >= 1
        # Main belief should now be deprecated
        ba = get_beliefs_for_fact(conn, fid_main)
        assert ba["belief_status"] == "deprecated"

    def test_retract_dependent_beliefs_cascade(self, conn):
        fid_base = fe._upsert_fact(conn, "base_fact", "is", "foundation", 0.9, time.time())
        fid_dep = fe._upsert_fact(conn, "dependent", "depends_on", "base", 0.7, time.time())
        ensure_belief_assertion(conn, fid_dep, evidence_chain=[fid_base])
        count = retract_dependent_beliefs(conn, fid_base)
        assert count >= 1
        ba = get_beliefs_for_fact(conn, fid_dep)
        assert ba["belief_status"] == "deprecated"


class TestFactTypeTaxonomy:
    """Fact type is set correctly based on extraction method"""

    def test_index_facts_sets_fact_type(self, conn):
        """Verify index_facts_for_memory sets fact_type correctly."""
        # Use the full indexing path
        conn.execute("INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
                     ("test/belief_fact_type", "Alice is a engineer. Bob created the API."))
        result = fe.index_facts_for_memory(conn, "test/belief_fact_type",
                                            "Alice is a engineer. Bob created the API.",
                                            fact_type="observation")
        assert result is not None
        rows = conn.execute("SELECT fact_type FROM kg_facts").fetchall()
        assert len(rows) > 0
        for row in rows:
            assert row[0] in ("observation", "agent_inference", "external_stated",
                              "hypothesis", "derived")

    def test_fact_type_inference_different_values(self, conn):
        """Test different fact_type values can be set per memory."""
        conn.execute("INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
                     ("test/ft1", "System X processes requests."))
        conn.execute("INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
                     ("test/ft2", "Hypothesis: the system scales linearly."))
        fe.index_facts_for_memory(conn, "test/ft1",
                                   "System X processes requests.",
                                   fact_type="observation")
        fe.index_facts_for_memory(conn, "test/ft2",
                                   "Hypothesis: the system scales linearly.",
                                   fact_type="hypothesis")
        rows = conn.execute(
            "SELECT DISTINCT fact_type FROM kg_facts ORDER BY fact_type"
        ).fetchall()
        types = [r[0] for r in rows]
        assert "observation" in types
        assert "hypothesis" in types


class TestBeliefReviewOrderingTies:
    """Discriminating tests for coverage-first ORDER BY in get_beliefs_due_for_review:
    ORDER BY (ba.last_reviewed_at IS NOT NULL) ASC, ba.last_reviewed_at ASC, ba.confidence ASC
    """

    def test_null_last_reviewed_precedes_reviewed_regardless_of_confidence(self, conn):
        """Case C (NULL last_reviewed_at, high confidence) MUST precede Case D (reviewed, low confidence)."""
        from belief.belief_lifecycle import get_beliefs_due_for_review

        # Case D: reviewed long ago (timestamp 1000.0), low confidence 0.1
        fid_d = fe._upsert_fact(conn, "entity_d", "has", "prop_d", 0.1, 1000.0)
        ba_id_d = ensure_belief_assertion(conn, fid_d, confidence=0.1)
        conn.execute(
            "UPDATE belief_assertions SET last_reviewed_at = 1000.0, confidence = 0.1 WHERE id = ?",
            (ba_id_d,),
        )

        # Case C: never reviewed (last_reviewed_at = NULL), high confidence 0.9
        fid_c = fe._upsert_fact(conn, "entity_c", "has", "prop_c", 0.9, 1000.0)
        ba_id_c = ensure_belief_assertion(conn, fid_c, confidence=0.9)
        conn.execute(
            "UPDATE belief_assertions SET last_reviewed_at = NULL, confidence = 0.9 WHERE id = ?",
            (ba_id_c,),
        )

        results = get_beliefs_due_for_review(conn, staleness_days=None, min_confidence=1.0)
        result_ids = [r["id"] for r in results]
        assert ba_id_c in result_ids
        assert ba_id_d in result_ids
        idx_c = result_ids.index(ba_id_c)
        idx_d = result_ids.index(ba_id_d)
        assert idx_c < idx_d, f"Case C (NULL) must precede Case D (reviewed): {result_ids}"

    def test_older_reviewed_timestamp_precedes_lower_confidence(self, conn):
        """Belief A (last_reviewed_at=1000.0, conf=0.5) MUST precede Belief B (last_reviewed_at=2000.0, conf=0.2)."""
        from belief.belief_lifecycle import get_beliefs_due_for_review

        # Belief B: reviewed more recently (2000.0), lower confidence 0.2
        fid_b = fe._upsert_fact(conn, "entity_b", "has", "prop_b", 0.2, 1000.0)
        ba_id_b = ensure_belief_assertion(conn, fid_b, confidence=0.2)
        conn.execute(
            "UPDATE belief_assertions SET last_reviewed_at = 2000.0, confidence = 0.2 WHERE id = ?",
            (ba_id_b,),
        )

        # Belief A: reviewed earlier (1000.0), higher confidence 0.5
        fid_a = fe._upsert_fact(conn, "entity_a", "has", "prop_a", 0.5, 1000.0)
        ba_id_a = ensure_belief_assertion(conn, fid_a, confidence=0.5)
        conn.execute(
            "UPDATE belief_assertions SET last_reviewed_at = 1000.0, confidence = 0.5 WHERE id = ?",
            (ba_id_a,),
        )

        results = get_beliefs_due_for_review(conn, staleness_days=None, min_confidence=1.0)
        result_ids = [r["id"] for r in results]
        assert ba_id_a in result_ids
        assert ba_id_b in result_ids
        idx_a = result_ids.index(ba_id_a)
        idx_b = result_ids.index(ba_id_b)
        assert idx_a < idx_b, f"Belief A (older review 1000.0) must precede Belief B (newer review 2000.0): {result_ids}"

    def test_equal_timestamp_breaks_ties_by_lowest_confidence(self, conn):
        """When last_reviewed_at is tied, lower confidence precedes higher confidence."""
        from belief.belief_lifecycle import get_beliefs_due_for_review

        fid_low = fe._upsert_fact(conn, "entity_low", "has", "prop_low", 0.3, 1000.0)
        ba_id_low = ensure_belief_assertion(conn, fid_low, confidence=0.3)
        conn.execute(
            "UPDATE belief_assertions SET last_reviewed_at = 1500.0, confidence = 0.3 WHERE id = ?",
            (ba_id_low,),
        )

        fid_high = fe._upsert_fact(conn, "entity_high", "has", "prop_high", 0.8, 1000.0)
        ba_id_high = ensure_belief_assertion(conn, fid_high, confidence=0.8)
        conn.execute(
            "UPDATE belief_assertions SET last_reviewed_at = 1500.0, confidence = 0.8 WHERE id = ?",
            (ba_id_high,),
        )

        results = get_beliefs_due_for_review(conn, staleness_days=None, min_confidence=1.0)
        result_ids = [r["id"] for r in results]
        idx_low = result_ids.index(ba_id_low)
        idx_high = result_ids.index(ba_id_high)
        assert idx_low < idx_high, f"Lower confidence (0.3) must precede higher (0.8) on timestamp tie: {result_ids}"

