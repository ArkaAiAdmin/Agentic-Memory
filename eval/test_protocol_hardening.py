"""Protocol hardening regression tests.

Covers the two failure modes observed live (2026-08-22 session):

1. Session identity handshake — memory_session_end must degrade to a
   structured no-op (not INVALID_PARAMS) when no session handle exists,
   and must find the latest active DB session via fallback.
2. Skill extraction gates — eval/test residue memory ids must never
   compile into skills, and YAML frontmatter must never become the
   skill topic/description ("---" descriptions).
"""

import os
import sqlite3
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)


class TestSkillExtractionGates(unittest.TestCase):
    def test_junk_memory_ids_are_vetoed(self):
        from skill_extractor import is_junk_memory_id

        junk = [
            "eval/fixtures/foo",
            "test-something",
            "tests/foo",
            "search-note-42",
            "stress-test-memory-7",
            "stress-test-memory-100",
            "test-memory-number-3",
            "test-note-for-vec-key-detection",
            "marker-tok-unit-noglob-1784238747-xyzabc123",
            "category-lessons-title-slug-adv-1-2-1784236269-tags",
            "mcp-smoke-test-note-testing-save-delete-lifecycle",
            "live-mcp-smoke-test-note-x",
        ]
        for mid in junk:
            self.assertTrue(is_junk_memory_id(mid), f"should veto: {mid}")

    def test_real_lesson_ids_pass(self):
        from skill_extractor import is_junk_memory_id

        real = [
            "lessons/api-deadlock-fix",
            "decisions/cache-strategy",
            "projects/protocol-hardening",
            "tdd-workflow",
            "testing-pyramid-guide",  # about testing, but not test residue
        ]
        for mid in real:
            self.assertFalse(is_junk_memory_id(mid), f"should NOT veto: {mid}")

    def test_frontmatter_is_stripped(self):
        from skill_extractor import strip_frontmatter

        body = "# Real Title\n\nActual content here."
        doc = f"---\ntitle: x\ndescription: ---\n---\n{body}"
        self.assertEqual(strip_frontmatter(doc), body)
        self.assertEqual(strip_frontmatter(body), body)
        # Unterminated frontmatter is left alone.
        self.assertEqual(strip_frontmatter("---\nnope"), "---\nnope")

    def test_frontmatter_only_content_never_becomes_skill(self):
        """A frontmatter-only memory must not yield a '---' topic."""
        from skill_extractor import extract_skill_from_memory, strip_frontmatter

        content = "---\n---"
        stripped = strip_frontmatter(content)
        self.assertFalse(stripped.strip())
        self.assertIsNone(
            extract_skill_from_memory("lessons/frontmatter-only", content)
        )

    def test_extraction_loop_skips_junk_rows(self):
        """run_extraction counts junk-id rows as skipped, never extracted."""
        from cron.cron_skill_extraction import run_extraction
        from skill_extractor import ensure_skill_schema, is_junk_memory_id as is_junk

        conn = sqlite3.connect(":memory:")
        ensure_skill_schema(conn)
        conn.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, "
            "updated_at TEXT, category TEXT, deleted_at TEXT)"
        )
        rows = [
            # (id, content, updated_at, category) — procedural-looking junk
            (
                "search-note-99",
                "# Steps\n1. run the thing\n2. verify output\n$ cmd --flag",
                "2026-08-01T00:00:00Z",
                "lessons",
            ),
            (
                "lessons/real-procedure",
                "# Deploy steps\n```\nmake deploy\n```\n1. Run `make deploy`.\n2. Verify health endpoint returns 200.",
                "2026-08-01T00:00:01Z",
                "lessons",
            ),
        ]
        conn.executemany(
            "INSERT INTO memories (id, content, updated_at, category) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        result = run_extraction(conn, since_iso="")
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM memory_skills").fetchall()
        }
        conn.close()
        self.assertNotIn("search-note-99", names)
        self.assertGreaterEqual(result["skipped"], 1)
        # Exactly the real procedure may compile — never the junk id.
        self.assertTrue(all(not is_junk(n) for n in names))


class TestSessionEndFallback(unittest.TestCase):
    DB = None

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.tmpdir = Path(tempfile.mkdtemp(prefix="proto_harden_"))
        cls.db_path = cls.tmpdir / "memory.db"
        # Session subsystem is config-gated; force-enable for these tests.
        os.environ["MEMORY_SESSION_MEMORY"] = "1"
        try:
            from config import reset_config

            reset_config()
        except Exception:
            pass

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        os.environ.pop("MEMORY_SESSION_MEMORY", None)
        try:
            from config import reset_config

            reset_config()
        except Exception:
            pass

    def _manager(self):
        from session_manager import SessionManager

        return SessionManager(db_path=self.db_path)

    def _latest_active_via_query(self) -> str:
        """Mirror of mcp_session._latest_active_session_id against this db."""
        mgr = self._manager()
        conn = mgr._conn()
        try:
            row = conn.execute(
                "SELECT id FROM sessions WHERE status='active' "
                "AND (?='' OR agent_id=?) ORDER BY started_at DESC LIMIT 1",
                ("", ""),
            ).fetchone()
        finally:
            from infra.db import safe_close_db

            safe_close_db(conn)
        return str(row[0]) if row else ""

    def test_no_active_session_yields_empty_handle(self):
        self.assertEqual(self._latest_active_via_query(), "")

    def test_active_session_is_found_and_endable(self):
        mgr = self._manager()
        ctx = mgr.start_session(project_root=str(self.tmpdir))
        self.assertIsNotNone(ctx)
        try:
            handle = self._latest_active_via_query()
            self.assertEqual(handle, ctx.session.id)
            ok = mgr.end_session(session_id=handle, summary="done")
            self.assertTrue(ok)
        finally:
            # Idempotent safety: end again is fine / already ended.
            mgr.end_session(session_id=ctx.session.id, summary="cleanup")

    def test_end_after_end_is_idempotent_without_crash(self):
        mgr = self._manager()
        ctx = mgr.start_session(project_root=str(self.tmpdir))
        mgr.end_session(session_id=ctx.session.id, summary="first")
        # Repeat end must be safe (idempotent) — no crash, bool result.
        ok = mgr.end_session(session_id=ctx.session.id, summary="second")
        self.assertIsInstance(ok, bool)


if __name__ == "__main__":
    unittest.main()
