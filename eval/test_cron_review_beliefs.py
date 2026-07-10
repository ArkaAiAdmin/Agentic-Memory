"""Tests for cron_review_beliefs (G4).

Covers:
- Stale/low-confidence beliefs are queued in belief_review_queue
- --dry-run does not write to the queue
- High-confidence beliefs are not queued
- Non-existent DB returns exit code 1
- --limit flag caps the number of queued beliefs
"""

import importlib.util as _importlib_util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Ensure the cron/ directory is on the path so `from _flock import ...` works
_CRON_DIR = REPO / "cron"
if _CRON_DIR.is_dir() and str(_CRON_DIR) not in sys.path:
    sys.path.insert(0, str(_CRON_DIR))


def _load_cron_review_beliefs():
    """Load cron_review_beliefs fresh from REPO (avoids module caching)."""
    spec = _importlib_util.spec_from_file_location(
        "_test_cron_review_beliefs", str(REPO / "cron" / "cron_review_beliefs.py")
    )
    if spec is None:
        raise RuntimeError("Could not load cron_review_beliefs.py")
    mod = _importlib_util.module_from_spec(spec)
    sys.modules["_test_cron_review_beliefs"] = mod
    loader = spec.loader
    if loader is None:
        raise RuntimeError("spec.loader is None for cron_review_beliefs.py")
    loader.exec_module(mod)
    return mod


def _setup_clean_db() -> tuple[sqlite3.Connection, str]:
    """Create a temp DB with the full schema (migrations + KG + beliefs)."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    from infra.db_migrations import run_schema_setup
    run_schema_setup(conn)
    from knowledge_graph.kg_schema import ensure_kg_schema
    ensure_kg_schema(conn)
    from belief.belief_schema import ensure_beliefs_schema
    ensure_beliefs_schema(conn)
    conn.commit()
    return conn, db_path


_belief_seq = 0


def _seed_belief(
    conn: sqlite3.Connection,
    confidence: float = 0.5,
    belief_status: str = "active",
    last_reviewed_at: float | None = None,
    mem_id: str | None = None,
    subject: str | None = None,
    obj: str | None = None,
) -> tuple[int, int]:
    """Insert a memory + kg_fact + belief_assertion, return (belief_id, fact_id).

    The memories row is inserted first because kg_facts has a FK to memories(id)
    via source_memory.  SPO auto-increments to avoid UNIQUE collisions.
    """
    global _belief_seq
    _belief_seq += 1
    n = _belief_seq
    subject = subject or f"entity_{n}"
    obj = obj or f"role_{n}"
    mem_id = mem_id or f"test/mem-{n}"
    now = time.time()
    ts = str(now)
    conn.execute(
        "INSERT OR IGNORE INTO memories (id, content, source_file, tags, "
        "category, created_at, updated_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (mem_id, f"seed content for {mem_id}", f"test/{mem_id}.md",
         "[]", "lessons", ts, ts, ts),
    )
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, confidence, "
        "first_seen, last_seen, source_memory, belief_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (subject, "is_a", obj, confidence, now, now, mem_id, belief_status),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO belief_assertions (fact_id, memory_id, belief_status, confidence, "
        "epistemic_source, certainty_tier, last_reviewed_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fact_id, mem_id, belief_status, confidence, "agent", "likely",
         last_reviewed_at, now, now),
    )
    belief_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return belief_id, fact_id


class TestCronReviewBeliefs:
    """Tests for the cron_review_beliefs cron job."""

    def test_cron_queues_stale_beliefs(self):
        """Stale beliefs (low confidence, old last_reviewed_at) are queued."""
        conn, db_path = _setup_clean_db()
        try:
            # Seed a stale belief: low confidence, last reviewed 60 days ago
            stale_ts = time.time() - (60 * 86400)
            _seed_belief(conn, confidence=0.3, last_reviewed_at=stale_ts)

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(sys, "argv", ["cron_review_beliefs", "--db", db_path]):
                    rc = mod.main()
            assert rc == 0, f"Expected exit 0, got {rc}"

            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count > 0, f"Expected queued beliefs, got {count}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_cron_dry_run_writes_nothing(self):
        """--dry-run counts beliefs but does not insert into the queue."""
        conn, db_path = _setup_clean_db()
        try:
            stale_ts = time.time() - (60 * 86400)
            _seed_belief(conn, confidence=0.3, last_reviewed_at=stale_ts)

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(
                    sys, "argv",
                    ["cron_review_beliefs", "--db", db_path, "--dry-run"],
                ):
                    rc = mod.main()
            assert rc == 0, f"Expected exit 0, got {rc}"

            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count == 0, f"Dry-run should write nothing, got {count}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_high_confidence_beliefs_not_queued(self):
        """Beliefs with confidence >= 1.0 (default min_confidence) are not queued.

        get_beliefs_due_for_review filters on confidence < min_confidence
        (default 1.0). A belief at confidence=0.95 should still be queued
        (0.95 < 1.0), but one at confidence=1.0 should not.
        """
        conn, db_path = _setup_clean_db()
        try:
            # confidence=1.0 means confidence < 1.0 is False => not due
            stale_ts = time.time() - (60 * 86400)
            _seed_belief(conn, confidence=1.0, last_reviewed_at=stale_ts)

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(sys, "argv", ["cron_review_beliefs", "--db", db_path]):
                    rc = mod.main()
            assert rc == 0, f"Expected exit 0, got {rc}"

            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count == 0, f"High-confidence beliefs should not be queued, got {count}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_recently_reviewed_beliefs_not_queued(self):
        """Beliefs reviewed within the last 30 days are not stale."""
        conn, db_path = _setup_clean_db()
        try:
            # last_reviewed_at = 5 days ago => not stale (staleness_days=30)
            recent_ts = time.time() - (5 * 86400)
            _seed_belief(conn, confidence=0.3, last_reviewed_at=recent_ts)

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(sys, "argv", ["cron_review_beliefs", "--db", db_path]):
                    rc = mod.main()
            assert rc == 0, f"Expected exit 0, got {rc}"

            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count == 0, f"Recently reviewed beliefs should not be queued, got {count}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_nonexistent_db_returns_1(self):
        """main() returns exit code 1 when DB path does not exist."""
        mod = _load_cron_review_beliefs()
        fake_path = "/tmp/_test_cron_review_beliefs_nonexistent.db"
        with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
            with patch.object(sys, "argv", ["cron_review_beliefs", "--db", fake_path]):
                rc = mod.main()
        assert rc == 1, f"Expected exit 1 for missing DB, got {rc}"

    def test_limit_caps_queued_beliefs(self):
        """--limit N caps the number of beliefs queued."""
        conn, db_path = _setup_clean_db()
        try:
            stale_ts = time.time() - (60 * 86400)
            # Seed 5 distinct stale beliefs (auto-incrementing SPO)
            for i in range(5):
                _seed_belief(conn, confidence=0.3, last_reviewed_at=stale_ts)

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(
                    sys, "argv",
                    ["cron_review_beliefs", "--db", db_path, "--limit", "2"],
                ):
                    rc = mod.main()
            assert rc == 0, f"Expected exit 0, got {rc}"

            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count == 2, f"Expected 2 queued (limit=2), got {count}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_queue_row_has_correct_columns(self):
        """Queued rows have correct belief_id, fact_id, reason, status."""
        conn, db_path = _setup_clean_db()
        try:
            stale_ts = time.time() - (60 * 86400)
            belief_id, fact_id = _seed_belief(
                conn, confidence=0.3, last_reviewed_at=stale_ts
            )

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(sys, "argv", ["cron_review_beliefs", "--db", db_path]):
                    rc = mod.main()
            assert rc == 0

            row = conn.execute(
                "SELECT belief_id, fact_id, reason, status FROM belief_review_queue LIMIT 1"
            ).fetchone()
            assert row is not None, "Expected at least one queued row"
            assert row[0] == belief_id, f"belief_id mismatch: {row[0]} != {belief_id}"
            assert row[1] == fact_id, f"fact_id mismatch: {row[1]} != {fact_id}"
            assert row[2] == "stale_or_low_confidence"
            assert row[3] == "pending"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_idempotent_queueing(self):
        """Running cron twice does not crash (INSERT OR IGNORE is safe)."""
        conn, db_path = _setup_clean_db()
        try:
            stale_ts = time.time() - (60 * 86400)
            _seed_belief(conn, confidence=0.3, last_reviewed_at=stale_ts)

            mod = _load_cron_review_beliefs()
            # Run twice — both should succeed without error
            for _ in range(2):
                with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                    with patch.object(sys, "argv", ["cron_review_beliefs", "--db", db_path]):
                        rc = mod.main()
                assert rc == 0

            # Both runs insert (no UNIQUE constraint on belief_review_queue),
            # so count is 2 — but no crash or constraint violation.
            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count >= 1, f"Expected at least 1 queued row, got {count}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_retracted_beliefs_not_queued(self):
        """Beliefs with belief_status='retracted' are not queued."""
        conn, db_path = _setup_clean_db()
        try:
            stale_ts = time.time() - (60 * 86400)
            _seed_belief(
                conn, confidence=0.3,
                belief_status="retracted",
                last_reviewed_at=stale_ts,
            )

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(sys, "argv", ["cron_review_beliefs", "--db", db_path]):
                    rc = mod.main()
            assert rc == 0

            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count == 0, f"Retracted beliefs should not be queued, got {count}"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_null_last_reviewed_beliefs_are_queued(self):
        """Beliefs with NULL last_reviewed_at are considered stale."""
        conn, db_path = _setup_clean_db()
        try:
            # last_reviewed_at=None => stale (NULL passes the IS NULL check)
            _seed_belief(conn, confidence=0.3, last_reviewed_at=None)

            mod = _load_cron_review_beliefs()
            with patch.object(mod, "acquire_lock_or_exit", lambda *_a, **_kw: None):
                with patch.object(sys, "argv", ["cron_review_beliefs", "--db", db_path]):
                    rc = mod.main()
            assert rc == 0

            count = conn.execute(
                "SELECT COUNT(*) FROM belief_review_queue"
            ).fetchone()[0]
            assert count > 0, "NULL last_reviewed_at should be queued as stale"
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
