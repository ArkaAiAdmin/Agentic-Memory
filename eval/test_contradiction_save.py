#!/usr/bin/env python3
"""Unit tests for memory_contradiction_save.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_contradiction_save -v
"""
import sys
import sqlite3
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest import mock

# Make the agentic-memory package importable.
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import memory_contradiction_save as mcs  # noqa: E402
from memory_common import open_db  # noqa: E402


# ---------------------------------------------------------------------------
# Test DB schema — mirrors prod (memories + soft-delete column)
# ---------------------------------------------------------------------------


def _bootstrap_test_db(db_path: Path) -> None:
    """Create a minimal memories table with the columns referenced by
    memory_contradiction_save.check_contradictions_on_save.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                deleted_at TEXT,
                deleted_by TEXT
            );
            """
        )
        conn.commit()


def _insert_note(
    db_path: Path,
    note_id: str,
    content: str = "hello world",
    source_file: str = "lessons/test.md",
    created_at: Optional[str] = None,
    deleted_at: Optional[str] = None,
) -> None:
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO memories
                (id, content, source_file, tags, created_at, updated_at,
                 observed_at, deleted_at, deleted_by)
            VALUES (?, ?, ?, '[]', ?, ?, ?, ?, NULL)
            """,
            (note_id, content, source_file, created_at, now, now, deleted_at),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="contradiction_save_test_")
        self.db_path = Path(self.tmpdir) / "test.db"
        _bootstrap_test_db(self.db_path)

    def tearDown(self):
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 1. Empty DB → empty list
# ---------------------------------------------------------------------------


class TestEmptyDB(_Base):
    def test_empty_db_no_contradictions(self):
        result = mcs.check_contradictions_on_save(
            self.db_path, "Anything goes here.", new_id="lessons/new",
        )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# 2. Basic detection
# ---------------------------------------------------------------------------


class TestDetection(_Base):
    def test_contradiction_detected(self):
        # Existing: "The deployment flag X is true." — pos="is true"
        # New:      "The deployment flag X is false." — neg="is false"
        # Shared:   "deployment flag" — enough subject overlap to land
        # in the high-confidence tier.
        _insert_note(
            self.db_path,
            "lessons/existing-flag",
            "The deployment flag X is true and active.",
            created_at="2026-06-01T00:00:00",
        )
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "The deployment flag X is false and disabled.",
            new_id="lessons/new-flag",
        )
        self.assertGreaterEqual(len(result), 1)
        top = result[0]
        self.assertEqual(top["existing_note_id"], "lessons/existing-flag")
        self.assertIn(top["confidence"], ("low", "medium", "high"))
        self.assertEqual(top["pair"], ("lessons/existing-flag", "lessons/new-flag"))
        # The type must mention both phrases.
        self.assertIn("is true", top["contradiction_type"])
        self.assertIn("is false", top["contradiction_type"])

    def test_no_contradiction_for_unrelated(self):
        # Different topics, no shared significant vocab → no findings.
        _insert_note(
            self.db_path,
            "lessons/pgbouncer",
            "Production traffic must flow through pgbouncer connection pooling.",
            created_at="2026-06-01T00:00:00",
        )
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "Always run database migrations during the maintenance window.",
            new_id="lessons/migrations",
        )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# 4. top_n limit
# ---------------------------------------------------------------------------


class TestTopNLimit(_Base):
    def test_top_n_limit(self):
        # Insert 30 existing notes. The detector should only be called
        # for at most top_n of them.
        for i in range(30):
            _insert_note(
                self.db_path,
                f"lessons/note-{i:02d}",
                f"Note {i}: workflow documentation strategy pattern.",
                created_at=f"2026-06-{(i % 28) + 1:02d}T00:00:00",
            )

        call_counter = {"n": 0}

        def counting_check(*args, **kwargs):
            call_counter["n"] += 1
            return []

        with mock.patch.object(mcs, "_phrase_check_pair", side_effect=counting_check):
            result = mcs.check_contradictions_on_save(
                self.db_path,
                "Workflow documentation strategy pattern is false.",
                new_id="lessons/new",
                top_n=5,
            )

        self.assertEqual(result, [])
        self.assertLessEqual(
            call_counter["n"], 5,
            f"expected at most 5 candidate scans, got {call_counter['n']}",
        )


# ---------------------------------------------------------------------------
# 5. Self-exclusion
# ---------------------------------------------------------------------------


class TestSelfExclusion(_Base):
    def test_self_excluded(self):
        _insert_note(
            self.db_path,
            "lessons/same-id",
            "The deployment flag X is true.",
            created_at="2026-06-01T00:00:00",
        )
        # Save with the SAME id — should not match itself.
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "The deployment flag X is false.",
            new_id="lessons/same-id",
        )
        self.assertEqual(
            result, [],
            "a note must not be flagged as contradicting itself",
        )


# ---------------------------------------------------------------------------
# 6. Soft-deleted notes excluded
# ---------------------------------------------------------------------------


class TestDeletedExclusion(_Base):
    def test_deleted_notes_excluded(self):
        # Two existing notes. The first is soft-deleted (deleted_at set).
        _insert_note(
            self.db_path,
            "lessons/active",
            "The deployment flag X is true.",
            created_at="2026-06-02T00:00:00",
        )
        _insert_note(
            self.db_path,
            "lessons/tombstoned",
            "The deployment flag X is true.",
            created_at="2026-06-01T00:00:00",
            deleted_at="2026-06-03T00:00:00",
        )
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "The deployment flag X is false.",
            new_id="lessons/new",
        )
        ids = [r["existing_note_id"] for r in result]
        self.assertIn("lessons/active", ids)
        self.assertNotIn(
            "lessons/tombstoned", ids,
            "soft-deleted note must be excluded from the candidate set",
        )


# ---------------------------------------------------------------------------
# 7 & 8. Snippet
# ---------------------------------------------------------------------------


class TestSnippet(unittest.TestCase):
    def test_snippet_truncation(self):
        # 250 'a' chars + a space — clearly longer than 200.
        long = "a" * 250 + " trailing"
        snippet = mcs._make_snippet(long, max_chars=200)
        # 200 chars of 'a' + "..." = 203 max. With word-boundary, it's
        # the 200 'a's cut at the boundary; the trailing space means
        # the boundary IS the 200th char (last space is at 200, just
        # before the space). Strict upper bound is 203.
        self.assertLessEqual(len(snippet), 203)
        self.assertTrue(snippet.endswith("..."))

    def test_snippet_word_boundary(self):
        # Build a string of distinct words that crosses 200 chars at a
        # word boundary, with the 201st char being mid-word.
        words = ["alpha"] * 50  # 50 * 5 = 250 chars, all single words separated by space
        content = " ".join(words)
        snippet = mcs._make_snippet(content, max_chars=200)
        # The truncation must end at a space. So either:
        #   - The last 7 chars of the snippet are "...", preceded by a
        #     complete word (no partial "alp" at the end).
        #   - Equivalently: the substring right before "..." must not
        #     end with a partial "alpha".
        if snippet.endswith("..."):
            stem = snippet[:-3]
            # stem should not end with a partial word. If we hard-cut
            # mid-word, the last 1-3 chars would be a prefix of "alpha".
            # word_boundary truncation means stem ends with a full
            # "alpha" (or is shorter than 5 chars).
            if len(stem) >= 5:
                self.assertTrue(
                    stem.endswith("alpha"),
                    f"expected word-boundary truncation, got {stem!r}",
                )

    def test_snippet_short_content_unchanged(self):
        # Content shorter than max_chars → no ellipsis, returned as-is.
        content = "short content"
        self.assertEqual(mcs._make_snippet(content, max_chars=200), content)

    def test_snippet_empty_content(self):
        self.assertEqual(mcs._make_snippet(""), "")
        self.assertEqual(mcs._make_snippet(None), "")


# ---------------------------------------------------------------------------
# 9 & 10. Sort order
# ---------------------------------------------------------------------------


class TestSort(_Base):
    def test_confidence_sort(self):
        # Existing C: high-confidence contradiction.
        # Existing D: low-confidence contradiction.
        # The order in the DB doesn't matter — we expect HIGH first.
        _insert_note(
            self.db_path,
            "lessons/d-low",
            "Database performance is true.",
            created_at="2026-06-01T00:00:00",
        )
        _insert_note(
            self.db_path,
            "lessons/c-high",
            "Database connection pool is true.",
            created_at="2026-06-02T00:00:00",
        )
        # New content has BOTH a low- and a high-confidence trigger.
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "Database caching is false. Database connection pool is false.",
            new_id="lessons/new",
        )
        # Both findings should be present.
        self.assertEqual(len(result), 2)
        # High must come first.
        self.assertEqual(result[0]["confidence"], "high")
        self.assertEqual(result[1]["confidence"], "low")
        self.assertEqual(result[0]["existing_note_id"], "lessons/c-high")
        self.assertEqual(result[1]["existing_note_id"], "lessons/d-low")

    def test_recency_sort_within_confidence(self):
        # Two high-confidence contradictions. The newer existing note
        # must sort first.
        _insert_note(
            self.db_path,
            "lessons/older",
            "Database table is true.",
            created_at="2026-06-01T00:00:00",
        )
        _insert_note(
            self.db_path,
            "lessons/newer",
            "Database column is true.",
            created_at="2026-06-15T00:00:00",
        )
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "Database table is false. Database column is false.",
            new_id="lessons/new",
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["existing_note_id"], "lessons/newer")
        self.assertEqual(result[1]["existing_note_id"], "lessons/older")
        self.assertEqual(result[0]["confidence"], "high")
        self.assertEqual(result[1]["confidence"], "high")


# ---------------------------------------------------------------------------
# 11. Max content chars truncation
# ---------------------------------------------------------------------------


class TestContentTruncation(_Base):
    def test_max_content_chars_truncation(self):
        # Existing note with 5000 chars. The detector must be called
        # with content truncated to <= 1000 chars.
        long_content = "Database connection pool documentation. " * 200  # ~6800 chars
        self.assertGreater(len(long_content), 5000)
        _insert_note(
            self.db_path,
            "lessons/long",
            long_content,
            created_at="2026-06-01T00:00:00",
        )

        captured = {}


        def capture_check(existing_id, existing_content, new_id, new_content, helpers):
            captured["existing_len"] = len(existing_content)
            captured["new_len"] = len(new_content)
            return []

        with mock.patch.object(mcs, "_phrase_check_pair", side_effect=capture_check):
            mcs.check_contradictions_on_save(
                self.db_path,
                "Database connection pool is false.",
                new_id="lessons/new",
            )
        self.assertLessEqual(
            captured["existing_len"],
            mcs._MAX_CONTENT_CHARS_FOR_DETECTOR,
            f"existing content not truncated: {captured['existing_len']} chars",
        )
        self.assertLessEqual(
            captured["new_len"],
            mcs._MAX_CONTENT_CHARS_FOR_DETECTOR,
            f"new content not truncated: {captured['new_len']} chars",
        )


# ---------------------------------------------------------------------------
# 12. Import error → silent return
# ---------------------------------------------------------------------------


class TestImportErrorSilent(_Base):
    def test_import_error_silent(self):
        _insert_note(
            self.db_path,
            "lessons/existing",
            "The deployment flag X is true.",
            created_at="2026-06-01T00:00:00",
        )
        with mock.patch.object(
            mcs, "_import_detector_helpers", return_value=None,
        ):
            with self.assertLogs(mcs.logger, level=logging.WARNING) as cm:
                result = mcs.check_contradictions_on_save(
                    self.db_path,
                    "The deployment flag X is false.",
                    new_id="lessons/new",
                )
        self.assertEqual(result, [])
        # And a warning must have been logged.
        self.assertTrue(
            any("detector unavailable" in msg.lower() for msg in cm.output),
            f"expected a detector-unavailable warning, got: {cm.output}",
        )


# ---------------------------------------------------------------------------
# 13. DB error → silent return
# ---------------------------------------------------------------------------


class TestDBErrorSilent(_Base):
    def test_db_error_silent(self):
        # The module imports `open_db` inside the function via
        # `from memory_common import open_db`. Patch the source module
        # so the lookup at call time resolves to a raising callable.
        import memory_common
        with mock.patch.object(
            memory_common, "open_db",
            side_effect=sqlite3.OperationalError("simulated DB failure"),
        ):
            with self.assertLogs(mcs.logger, level=logging.WARNING) as cm:
                result = mcs.check_contradictions_on_save(
                    self.db_path,
                    "The deployment flag X is false.",
                    new_id="lessons/new",
                )
        self.assertEqual(result, [])
        self.assertTrue(
            any("db" in msg.lower() for msg in cm.output),
            f"expected a DB-related warning, got: {cm.output}",
        )


# ---------------------------------------------------------------------------
# 14. Returned dict shape
# ---------------------------------------------------------------------------


class TestDictShape(_Base):
    def test_returns_expected_dict_shape(self):
        _insert_note(
            self.db_path,
            "lessons/existing",
            "The deployment flag X is true.",
            created_at="2026-06-01T00:00:00",
        )
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "The deployment flag X is false.",
            new_id="lessons/new",
        )
        self.assertGreaterEqual(len(result), 1)
        f = result[0]
        # All 5 required keys present.
        for key in (
            "existing_note_id",
            "existing_content_snippet",
            "contradiction_type",
            "confidence",
            "pair",
        ):
            self.assertIn(key, f, f"missing key: {key}")
        # Types.
        self.assertIsInstance(f["existing_note_id"], str)
        self.assertIsInstance(f["existing_content_snippet"], str)
        self.assertIsInstance(f["contradiction_type"], str)
        self.assertIsInstance(f["confidence"], str)
        self.assertIsInstance(f["pair"], tuple)
        self.assertEqual(len(f["pair"]), 2)
        # confidence is one of the three labels.
        self.assertIn(f["confidence"], ("low", "medium", "high"))
        # pair is (existing_id, new_id).
        self.assertEqual(f["pair"][0], f["existing_note_id"])
        self.assertEqual(f["pair"][1], "lessons/new")


# ---------------------------------------------------------------------------
# Bonus: min_confidence filter actually filters
# ---------------------------------------------------------------------------


class TestMinConfidenceFilter(_Base):
    def test_min_confidence_drops_low(self):
        # Same setup as test_confidence_sort, but with min_confidence="high".
        # Only the high-confidence finding must survive.
        _insert_note(
            self.db_path,
            "lessons/d-low",
            "Database performance is true.",
            created_at="2026-06-01T00:00:00",
        )
        _insert_note(
            self.db_path,
            "lessons/c-high",
            "Database connection pool is true.",
            created_at="2026-06-02T00:00:00",
        )
        result = mcs.check_contradictions_on_save(
            self.db_path,
            "Database caching is false. Database connection pool is false.",
            new_id="lessons/new",
            min_confidence="high",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["confidence"], "high")
        self.assertEqual(result[0]["existing_note_id"], "lessons/c-high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
