#!/usr/bin/env python3
"""CHANGE 6 — GDPR subject fallback on write.

Every save must tag the row with a ``data_subject_sub`` so per-subject
erasure is possible. When the caller does not supply one, the save path
defaults it to a stable, PII-free hash of the authenticated principal
(falling back to the tenant id). We assert:

  * A save with no ``data_subject_sub`` still writes a non-NULL
    ``data_subject_sub`` column (no more silent drop / schema-drift warning).
  * The row is erasable later by passing the *same* default subject to
    ``gdpr_erase`` (per-subject erasure works without a tenant-wide wipe).
  * A different subject's erase does NOT delete the row (subject scoping).

Run:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_gdpr_subject_fallback -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

os.environ.setdefault("MEMORY_AGENT_ID", "test-agent")
os.environ.setdefault("MEMORY_AUTH_MODE", "open")

from save.pipeline import save_memory, _default_data_subject_sub  # noqa: E402
from infra.gdpr import gdpr_erase  # noqa: E402
from infra.db_migrations import run_schema_setup  # noqa: E402
from infra.memory_common import open_db  # noqa: E402
import sqlite3  # noqa: E402


class TestGdprSubjectFallback(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gdpr_subject_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(conn)
        conn.close()
        self.tenant_id = "test-tenant"

    def tearDown(self):
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def _stored_subject(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT data_subject_sub FROM memories LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def test_save_defaults_subject_and_is_erasable(self):
        note_id = save_memory(
            content="secret user memory",
            category="lessons",
            title_slug="secret-note",
            tenant_id=self.tenant_id,
            db_path=str(self.db_path),
        )
        self.assertTrue(note_id)
        # 1. Column is populated (was previously NULL == un-erasable).
        stored = self._stored_subject()
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertTrue(stored.startswith("sub_"))

        # The default subject matches the principal/tenant hash.
        expected = _default_data_subject_sub(self.tenant_id, "test-agent")
        self.assertEqual(stored, expected)

        # 2. Per-subject erase removes exactly this row.
        conn = sqlite3.connect(str(self.db_path))
        try:
            cert = gdpr_erase(
                conn,
                principal_id="test-principal",
                data_subject_sub=expected,
                tenant_id=self.tenant_id,
            )
        finally:
            conn.close()
        self.assertIsNotNone(cert)
        conn = sqlite3.connect(str(self.db_path))
        try:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id=?", (note_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(remaining, 0)

    def test_other_subject_does_not_erase(self):
        save_memory(
            content="user A memory",
            category="lessons",
            title_slug="user-a-note",
            tenant_id=self.tenant_id,
            db_path=str(self.db_path),
        )
        stored = self._stored_subject()
        # A different subject's erase must not touch the row.
        other = _default_data_subject_sub(self.tenant_id, "someone-else")
        self.assertNotEqual(other, stored)
        conn = sqlite3.connect(str(self.db_path))
        try:
            gdpr_erase(
                conn,
                principal_id="test-principal",
                data_subject_sub=other,
                tenant_id=self.tenant_id,
            )
        finally:
            conn.close()
        conn = sqlite3.connect(str(self.db_path))
        try:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(remaining, 1)

    def test_explicit_subject_is_respected(self):
        explicit = "explicit-subject-abc"
        save_memory(
            content="tagged memory",
            category="lessons",
            title_slug="tagged-note",
            tenant_id=self.tenant_id,
            data_subject_sub=explicit,
            db_path=str(self.db_path),
        )
        self.assertEqual(self._stored_subject(), explicit)


if __name__ == "__main__":
    unittest.main()
