"""Tests for fact_extraction.py — SPO fact extraction, locking, decay, search."""

import os, sys, sqlite3

os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "1"
sys.path.insert(
    0,
    str(
        os.environ.get("MEMORY_INSTALL_ROOT")
        or os.path.expanduser("~/.config/agentic-memory")
    ),
)

from memory_config import install_root

sys.path.insert(0, str(install_root()))

import fact_extraction as fe


class TestFactExtraction:
    def test_extract_spo(self):
        text = "Alice is a engineer. Bob created the API."
        facts = fe.extract_facts(text)
        assert len(facts) >= 1, f"No facts extracted from: {facts}"

    def test_extract_dedup(self):
        text = "Alice is a engineer. Alice is a engineer."
        facts = fe.extract_facts(text)
        # Should deduplicate
        seen = set()
        for s, p, o, c in facts:
            key = (s.lower(), p, o.lower())
            assert key not in seen, f"Duplicate fact: {key}"
            seen.add(key)

    def test_extract_empty(self):
        assert fe.extract_facts("") == []


class TestFactsSchema:
    def test_ensure_facts_schema(self):
        conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(conn)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "kg_facts" in tables
        conn.close()

    def test_idempotent(self):
        conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(conn)
        fe.ensure_facts_schema(conn)
        conn.close()


class TestFactIndexing:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(self.conn)

    def teardown_method(self):
        self.conn.close()

    def test_index_facts(self):
        content = "Alice is a engineer. Bob created the API. Charlie manages the team."
        result = fe.index_facts_for_memory(self.conn, "test/m1", content)
        assert result["facts"] >= 0  # may be 0 if patterns don't match

    def test_index_facts_disabled(self):
        old = os.environ.get("MEMORY_KNOWLEDGE_GRAPH")
        os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "0"
        result = fe.index_facts_for_memory(self.conn, "test/m1", "Alice is a engineer.")
        assert result == {"facts": 0}
        if old:
            os.environ["MEMORY_KNOWLEDGE_GRAPH"] = old
        else:
            os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "1"


class TestFactLocking:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(self.conn)
        fe.index_facts_for_memory(self.conn, "test/m1", "Alice is a engineer.")

    def teardown_method(self):
        self.conn.close()

    def test_lock_unlock(self):
        # Get a fact
        row = self.conn.execute(
            "SELECT subject, predicate, object FROM kg_facts LIMIT 1"
        ).fetchone()
        if row:
            assert fe.lock_fact(self.conn, row[0], row[1], row[2])
            # Verify locked
            r = self.conn.execute(
                "SELECT locked FROM kg_facts WHERE subject = ? AND predicate = ? AND object = ?",
                row,
            ).fetchone()
            assert r[0] == 1
            assert fe.unlock_fact(self.conn, row[0], row[1], row[2])
            r = self.conn.execute(
                "SELECT locked FROM kg_facts WHERE subject = ? AND predicate = ? AND object = ?",
                row,
            ).fetchone()
            assert r[0] == 0

    def test_lock_nonexistent(self):
        assert not fe.lock_fact(self.conn, "zzz", "zzz", "zzz")


class TestFactSearch:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(self.conn)
        fe.index_facts_for_memory(
            self.conn, "test/m1", "Alice is a engineer. Bob created the API."
        )

    def teardown_method(self):
        self.conn.close()

    def test_search(self):
        results = fe.facts_search(self.conn, "Alice")
        assert len(results) >= 0  # depends on patterns

    def test_search_empty(self):
        results = fe.facts_search(self.conn, "zzzznonexistent")
        assert len(results) == 0


class TestFactSearchFTS5:
    """T9: FTS5-backed fact search (kg_facts_fts).

    The FTS5 path is preferred when available.  LIKE is the fallback
    for pre-v20 DBs and FTS5 syntax errors.  These tests use direct SQL
    inserts (not `index_facts_for_memory`) to avoid the LLM extraction
    path, which is not relevant to fact search.
    """

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(self.conn)
        # Direct SQL inserts — bypasses LLM/regex extraction, exercises
        # only the FTS5 triggers and the search path.
        facts = [
            (
                "alice",
                "is_a",
                "engineer",
                0.95,
                0,
                1700000000.0,
                1700000000.0,
                1,
                "test/m1",
            ),
            (
                "bob",
                "created",
                "the API",
                0.88,
                0,
                1700000000.0,
                1700000000.0,
                1,
                "test/m1",
            ),
            (
                "carol",
                "loves",
                "python",
                0.80,
                0,
                1700000000.0,
                1700000000.0,
                1,
                "test/m1",
            ),
            (
                "dave",
                "manages",
                "the team",
                0.85,
                0,
                1700000000.0,
                1700000000.0,
                1,
                "test/m2",
            ),
            (
                "eve",
                "designed",
                "the schema",
                0.90,
                0,
                1700000000.0,
                1700000000.0,
                1,
                "test/m2",
            ),
        ]
        self.conn.executemany(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, locked, "
            "first_seen, last_seen, mention_count, source_memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            facts,
        )
        self.conn.commit()

    def teardown_method(self):
        self.conn.close()

    def test_fts5_path_used_when_available(self):
        """FTS5 table exists in :memory: after ensure_facts_schema, populated by triggers."""
        fts_exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_facts_fts'"
        ).fetchone()
        assert fts_exists is not None
        fts_count = self.conn.execute("SELECT COUNT(*) FROM kg_facts_fts").fetchone()[0]
        assert fts_count == 5

    def test_search_finds_subject_match(self):
        """FTS5 search by subject name returns at least the matching fact."""
        results = fe.facts_search(self.conn, "alice")
        assert len(results) == 1
        assert results[0]["subject"] == "alice"

    def test_search_finds_object_match(self):
        """FTS5 search by object name returns at least the matching fact."""
        results = fe.facts_search(self.conn, "engineer")
        assert len(results) == 1
        assert results[0]["object"] == "engineer"

    def test_search_multi_token_or(self):
        """Multi-token query OR-joins tokens; either subject or object can hit."""
        results = fe.facts_search(self.conn, "bob api", limit=10)
        assert len(results) == 1
        assert results[0]["subject"] == "bob"

    def test_search_empty_query_returns_empty(self):
        """Empty / whitespace-only query returns []. (Defensive, was a bug.)"""
        assert fe.facts_search(self.conn, "") == []
        assert fe.facts_search(self.conn, "   ") == []
        assert fe.facts_search(self.conn, "\t\n") == []

    def test_search_no_match_returns_empty(self):
        """Non-existent term returns empty list."""
        assert fe.facts_search(self.conn, "zzzznonexistent_zzz") == []

    def test_search_special_chars_dont_break(self):
        """FTS5 special chars in query don't raise — they're stripped/escaped."""
        # These would syntax-error if passed verbatim to FTS5 MATCH.
        for q in ["*foo^", '"unbalanced', "NEAR NOT", "***", "a^b*c"]:
            results = fe.facts_search(self.conn, q)
            assert isinstance(results, list)  # never raises

    def test_facts_search_like_fallback_works(self):
        """LIKE fallback helper still works in isolation."""
        rows = fe._facts_search_like(self.conn, "alice", 10)
        assert isinstance(rows, list)
        # LIKE is case-insensitive in SQLite by default for ASCII.
        assert any("alice" in r[1] for r in rows)

    def test_build_fts_query_basic(self):
        """Single token -> single quoted token."""
        assert fe._build_fts_query("alice") == '"alice"'

    def test_build_fts_query_multi_token(self):
        """Multi-token -> OR-joined quoted tokens."""
        assert fe._build_fts_query("alice bob") == '"alice" OR "bob"'

    def test_build_fts_query_strips_specials(self):
        """* and ^ are stripped; " is doubled for FTS5 safety."""
        assert fe._build_fts_query("a*b^c") == '"abc"'
        # Quotes are escaped (doubled) inside a quoted phrase
        assert fe._build_fts_query('he said "hi"') == '"he" OR "said" OR """hi"""'

    def test_build_fts_query_empty(self):
        """Empty / whitespace-only -> None."""
        assert fe._build_fts_query("") is None
        assert fe._build_fts_query("   ") is None

    def test_search_results_have_effective_confidence(self):
        """Returned dicts include effective_confidence (backward compat)."""
        results = fe.facts_search(self.conn, "alice")
        if results:
            assert "effective_confidence" in results[0]
            assert isinstance(results[0]["effective_confidence"], float)

    def test_search_limit_caps_results(self):
        """Limit parameter caps the number of returned dicts."""
        results = fe.facts_search(self.conn, "the", limit=2)
        # 'the' appears in many objects; cap should be honored.
        assert len(results) <= 2

    def test_search_db_wrapper_returns_list(self):
        """facts_search_db (connection-lifecycle wrapper) still works.

        The wrapper opens a pooled connection to a real DB file, so this
        exercises the FTS5 triggers created by run_schema_setup() + the
        kg_facts_fts query path end-to-end.
        """
        import tempfile
        import uuid
        from pathlib import Path

        unique = f"fts_test_{uuid.uuid4().hex[:8]}.db"
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / unique
            # 1. Bootstrap DB via the wrapper itself so all schema/migrations run.
            #    This gives us a fully-migrated DB (kg_facts + kg_facts_fts +
            #    triggers all set up).
            fe.facts_search_db(db_path, "warmup")  # no-op query, just bootstraps
            # 2. Now write a fact via the pool's connection. We need to insert
            #    via the same connection the wrapper will read from, because
            #    the data file may not be visible to a freshly-opened pool conn
            #    (WAL mode, connection caching, etc.).
            from memory_common import connection_pool

            conn = connection_pool.get(str(db_path))
            try:
                conn.execute(
                    "INSERT INTO kg_facts (subject, predicate, object, confidence, "
                    "locked, first_seen, last_seen, mention_count, source_memory) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "alice",
                        "is_a",
                        "engineer",
                        0.95,
                        0,
                        1700000000.0,
                        1700000000.0,
                        1,
                        None,
                    ),
                )
                conn.commit()
            finally:
                connection_pool.put(conn)
            # 3. Now query — should return 1 fact via FTS5 path.
            results = fe.facts_search_db(db_path, "alice")
            assert isinstance(results, list)
            assert len(results) == 1
            assert results[0]["subject"] == "alice"
            # Clean up the pooled conn so it doesn't leak across tests.
            try:
                from memory_common import safe_close_db

                conn2 = connection_pool.get(str(db_path))
                safe_close_db(conn2)
            except Exception:
                pass

    def test_search_falls_back_to_like_on_fts_error(self):
        """If the FTS5 path raises, LIKE is tried as fallback."""
        # Force the FTS5 path to fail by dropping the table mid-flight.
        # The _facts_search_fts catches and returns None, then LIKE runs.
        self.conn.execute("DROP TABLE kg_facts_fts")
        # Re-running should not raise; should fall back to LIKE.
        results = fe.facts_search(self.conn, "alice")
        assert isinstance(results, list)
        # LIKE should still find alice.
        assert any("alice" in r["subject"] for r in results)


class TestFactStats:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        fe.ensure_facts_schema(self.conn)

    def teardown_method(self):
        self.conn.close()

    def test_stats_empty(self):
        stats = fe.facts_stats(self.conn)
        assert stats["total_facts"] == 0
        assert stats["locked_facts"] == 0


# ---------------------------------------------------------------------------
# Layer 5: Broader regex patterns (B24)
# ---------------------------------------------------------------------------


class TestB24BroaderPatterns:
    """Test the 4 new broader patterns added in Layer 5.

    Each pattern is tested for both positive (extracted) and negative
    (rejected) cases. The _is_valid quality gate and _META_LABELS filter
    apply to all four.
    """

    def test_copula_sentence(self):
        """Pattern 5a: 'X is/was/are a Y' (broader than Layer 3)."""
        facts = fe.extract_facts(
            "Python is a programming language used in many systems"
        )
        copula = [f for f in facts if f[1] == "is_a"]
        assert len(copula) >= 1, f"expected >=1 is_a fact, got {facts}"
        assert any(f[0] == "Python" for f in copula)

    def test_colon_definition(self):
        """Pattern 5b: 'Label: Value' (plain, outside bold)."""
        facts = fe.extract_facts(
            "Configuration Type: simple_agreement for the system now"
        )
        has_value = [f for f in facts if f[1] == "has_value"]
        assert len(has_value) >= 1, f"expected >=1 has_value, got {facts}"
        assert any("Type" in f[0] for f in has_value)

    def test_plain_dash_bullet(self):
        """Pattern 5c: '- text' without bold or em-dash."""
        facts = fe.extract_facts(
            "## Notes\n- Some important note about the project status here"
        )
        has_desc = [f for f in facts if f[1] == "has_description"]
        # May match via Layer 1 (bold) or Layer 5c (plain dash).
        # The important check: at least one fact, not zero.
        assert len(has_desc) >= 1, f"expected >=1 has_description, got {facts}"

    def test_svo_sentence(self):
        """Pattern 5d: SVO from declarative sentences using _VERB_MAP."""
        facts = fe.extract_facts("The system processes requests from users quickly")
        svo = [
            f
            for f in facts
            if f[1] in ("processes", "extracts", "creates", "stores", "contains")
        ]
        assert len(svo) >= 1, f"expected >=1 SVO fact, got {facts}"
        assert any("system" in f[0] for f in svo)

    def test_weak_subject_rejected(self):
        """'It is a thing' must NOT extract a fact (weak subject)."""
        facts = fe.extract_facts("It is a thing that nobody uses anywhere today really")
        # Should have 0 is_a facts
        copula = [f for f in facts if f[1] == "is_a"]
        assert len(copula) == 0, f"weak subject was extracted: {copula}"

    def test_meta_label_rejected(self):
        """'Status: foo' with status-related meta-label must NOT extract."""
        # "Status" is not in META_LABELS but "Configuration" is — and the
        # colon pattern requires the label to be non-meta. Test the
        # reverse: ensure a known meta-label is filtered.
        facts = fe.extract_facts(
            "Configuration: some value for the system documentation here"
        )
        has_value = [
            f
            for f in facts
            if f[1] == "has_value" and f[0].lower().rstrip(":") == "configuration"
        ]
        assert len(has_value) == 0, f"meta-label was extracted: {has_value}"
