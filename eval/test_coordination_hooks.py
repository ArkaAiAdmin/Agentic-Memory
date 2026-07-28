#!/usr/bin/env python3
"""Tests for coordination integration hooks.

Tests the full integration:
1. Save pipeline file locking (acquire_save_lock / release_save_lock)
2. Project state updates (update_project_activity / clear_project_activity)
3. Lock conflict messaging (queue_lock_conflict_message)
4. Cron task auto-creation + dispatch (create_and_dispatch_task)
5. Session start task claiming (claim_pending_tasks)
6. Search context enrichment (get_coordination_context)
7. Supersession task creation (detect_supersession_tasks)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from coordination.hooks import (
    acquire_save_lock, release_save_lock,
    create_coordination_task, create_contradiction_tasks, create_integrity_tasks,
    claim_pending_tasks,
)


def _make_db():
    """Create a temporary test database with coordination tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shared_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, task_type TEXT NOT NULL,
            description TEXT, assigned_to TEXT, status TEXT DEFAULT 'pending',
            created_by TEXT NOT NULL, created_at REAL, updated_at REAL,
            depends_on INTEGER REFERENCES shared_tasks(id)
        );
        CREATE TABLE IF NOT EXISTS file_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL, locked_by TEXT NOT NULL,
            locked_at REAL, expires_at REAL, UNIQUE(file_path)
        );
        CREATE TABLE IF NOT EXISTS coordination_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL, agent_id TEXT NOT NULL,
            target TEXT, detail TEXT, timestamp REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
            updated_by TEXT NOT NULL, updated_at REAL,
            UNIQUE(project_id, key)
        );
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
            message_type TEXT NOT NULL, payload TEXT,
            status TEXT DEFAULT 'pending', created_at REAL, delivered_at REAL
        );
        CREATE TABLE IF NOT EXISTS agent_heartbeats (
            agent_id TEXT PRIMARY KEY,
            last_heartbeat REAL NOT NULL,
            session_id TEXT,
            project_id TEXT
        );
    """)
    conn.commit()
    return conn, path


class TestSaveLocking(unittest.TestCase):
    """Tests for save pipeline file locking integration."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_acquire_save_lock(self):
        """Can acquire a save lock on a file."""
        result = acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        self.assertTrue(result["acquired"])
        row = self.conn.execute("SELECT locked_by FROM file_locks WHERE file_path=?", ("/memory/lessons/test.md",)).fetchone()
        self.assertEqual(row[0], "agent-a")

    def test_release_save_lock(self):
        """Can release a save lock."""
        acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        release_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        row = self.conn.execute("SELECT locked_by FROM file_locks WHERE file_path=?", ("/memory/lessons/test.md",)).fetchone()
        self.assertIsNone(row)

    def test_acquire_lock_conflict(self):
        """Cannot acquire a lock held by another agent."""
        acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        result = acquire_save_lock("/memory/lessons/test.md", "agent-b", conn=self.conn)
        self.assertFalse(result["acquired"])

    def test_acquire_lock_same_agent_refreshes(self):
        """Same agent can refresh its own lock."""
        acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        result = acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        self.assertTrue(result["acquired"])

    def test_release_only_releases_own_lock(self):
        """Releasing only removes lock if owned by the same agent."""
        acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        release_save_lock("/memory/lessons/test.md", "agent-b", conn=self.conn)
        row = self.conn.execute("SELECT locked_by FROM file_locks WHERE file_path=?", ("/memory/lessons/test.md",)).fetchone()
        self.assertEqual(row[0], "agent-a")


class TestCronTaskCreation(unittest.TestCase):
    """Tests for cron task auto-creation integration."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_create_contradiction_tasks(self):
        """Creates tasks for contradictions."""
        contradictions = [
            {"source": "note-a", "target": "note-b", "confidence": "high"},
            {"source": "note-c", "target": "note-d", "confidence": "low"},
        ]
        count = create_contradiction_tasks(contradictions, conn=self.conn)
        self.assertEqual(count, 2)
        rows = self.conn.execute("SELECT task_type FROM shared_tasks").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r[0] == "resolve_contradiction" for r in rows))

    def test_create_contradiction_tasks_skips_self(self):
        """Skips contradictions where source == target."""
        contradictions = [{"source": "note-a", "target": "note-a", "confidence": "high"}]
        count = create_contradiction_tasks(contradictions, conn=self.conn)
        self.assertEqual(count, 0)

    def test_create_integrity_tasks(self):
        """Creates tasks for critical/warning findings only."""
        findings = [
            {"severity": "critical", "message": "DB corrupted", "check": "schema"},
            {"severity": "warning", "message": "FTS out of sync", "check": "fts"},
            {"severity": "info", "message": "All good", "check": "health"},
        ]
        count = create_integrity_tasks(findings, conn=self.conn)
        self.assertEqual(count, 2)

    def test_create_coordination_task_returns_id(self):
        """create_coordination_task returns a task ID."""
        task_id = create_coordination_task("test", "Test task", conn=self.conn)
        self.assertIsNotNone(task_id)

    def test_task_has_audit_record(self):
        """Created tasks have an audit record."""
        task_id = create_coordination_task("test", "Test task", created_by="test-cron", conn=self.conn)
        rows = self.conn.execute(
            "SELECT action, agent_id FROM coordination_audit WHERE action='task_created'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "test-cron")


class TestTaskClaiming(unittest.TestCase):
    """Tests for session start task claiming integration."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_claim_pending_tasks(self):
        """Can claim pending tasks."""
        now = time.time()
        for i in range(3):
            self.conn.execute(
                "INSERT INTO shared_tasks (project_id, task_type, description, status, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                ("default", "fix", f"Task {i}", "cron", now, now),
            )
        self.conn.commit()

        claimed = claim_pending_tasks("agent-a", "default", limit=2, conn=self.conn)
        self.assertEqual(len(claimed), 2)
        self.assertEqual(claimed[0]["task_type"], "fix")

        rows = self.conn.execute("SELECT status, assigned_to FROM shared_tasks WHERE assigned_to='agent-a'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r[0] == "active" for r in rows))

    def test_claim_no_pending_tasks(self):
        """Returns empty list when no tasks are pending."""
        claimed = claim_pending_tasks("agent-a", conn=self.conn)
        self.assertEqual(claimed, [])

    def test_claim_respects_limit(self):
        """Only claims up to the limit."""
        now = time.time()
        for i in range(5):
            self.conn.execute(
                "INSERT INTO shared_tasks (project_id, task_type, description, status, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                ("default", "fix", f"Task {i}", "cron", now, now),
            )
        self.conn.commit()

        claimed = claim_pending_tasks("agent-a", limit=2, conn=self.conn)
        self.assertEqual(len(claimed), 2)

    def test_claim_creates_audit_record(self):
        """Claiming tasks creates audit records."""
        now = time.time()
        self.conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, description, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            ("default", "fix", "Task 1", "cron", now, now),
        )
        self.conn.commit()

        claim_pending_tasks("agent-a", conn=self.conn)
        rows = self.conn.execute(
            "SELECT action FROM coordination_audit WHERE action='task_claimed'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_already_active_tasks_not_claimed(self):
        """Only pending tasks are claimed, not active ones."""
        now = time.time()
        self.conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, description, status, assigned_to, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', 'other-agent', ?, ?, ?)",
            ("default", "fix", "Task 1", "cron", now, now),
        )
        self.conn.commit()

        claimed = claim_pending_tasks("agent-a", conn=self.conn)
        self.assertEqual(len(claimed), 0)


class TestProjectActivity(unittest.TestCase):
    """Tests for project state updates during save."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_update_project_activity(self):
        """Updates project state with file and agent activity."""
        from coordination.hooks import update_project_activity
        update_project_activity("/memory/lessons/test.md", "agent-a", "writing", conn=self.conn)

        rows = self.conn.execute(
            "SELECT key, value FROM project_state WHERE key LIKE 'file:%'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("file:/memory/lessons/test.md", rows[0][0])

    def test_clear_project_activity(self):
        """Clears file activity and sets agent to idle."""
        from coordination.hooks import update_project_activity, clear_project_activity
        update_project_activity("/memory/lessons/test.md", "agent-a", "writing", conn=self.conn)
        clear_project_activity("/memory/lessons/test.md", "agent-a", conn=self.conn)

        # File activity should be gone
        rows = self.conn.execute(
            "SELECT key FROM project_state WHERE key LIKE 'file:%'"
        ).fetchall()
        self.assertEqual(len(rows), 0)

        # Agent should be idle
        rows = self.conn.execute(
            "SELECT value FROM project_state WHERE key='agent:agent-a:status'"
        ).fetchall()
        self.assertEqual(len(rows), 1)


class TestLockConflictMessaging(unittest.TestCase):
    """Tests for lock conflict messaging."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_queue_lock_conflict_message(self):
        """Sends a message to the lock holder."""
        from coordination.hooks import queue_lock_conflict_message
        queue_lock_conflict_message("/src/main.py", "agent-a", "agent-b", conn=self.conn)

        rows = self.conn.execute(
            "SELECT from_agent, to_agent, message_type FROM agent_messages"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "agent-b")  # from
        self.assertEqual(rows[0][1], "agent-a")  # to
        self.assertEqual(rows[0][2], "lock_conflict")


class TestAgentDispatch(unittest.TestCase):
    """Tests for task creation + agent dispatch."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_create_and_dispatch_task(self):
        """Creates task and sends notification to target agent."""
        from coordination.hooks import create_and_dispatch_task
        task_id = create_and_dispatch_task(
            "fix", "Fix the bug", "agent-b", created_by="system", conn=self.conn,
        )
        self.assertIsNotNone(task_id)

        # Verify task exists
        row = self.conn.execute("SELECT assigned_to, status FROM shared_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row[0], "agent-b")
        self.assertEqual(row[1], "active")

        # Verify notification sent
        rows = self.conn.execute(
            "SELECT to_agent, message_type FROM agent_messages WHERE message_type='task_assigned'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "agent-b")


class TestSearchContext(unittest.TestCase):
    """Tests for search context enrichment."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_get_coordination_context(self):
        """Returns active locks, agent activity, and tasks."""
        from coordination.hooks import (
            get_coordination_context, acquire_save_lock,
            update_project_activity, create_coordination_task,
        )

        # Add some state
        acquire_save_lock("/src/main.py", "agent-a", conn=self.conn)
        update_project_activity("/src/main.py", "agent-a", "writing", conn=self.conn)
        create_coordination_task("fix", "Fix bug", conn=self.conn)

        ctx = get_coordination_context(conn=self.conn)
        self.assertIn("active_locks", ctx)
        self.assertIn("agent_activity", ctx)
        self.assertIn("active_tasks", ctx)
        self.assertEqual(len(ctx["active_locks"]), 1)
        self.assertEqual(len(ctx["agent_activity"]), 1)
        self.assertEqual(len(ctx["active_tasks"]), 1)


class TestSupersessionTasks(unittest.TestCase):
    """Tests for supersession task creation."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_detect_supersession_tasks(self):
        """Creates task when a note supersedes another."""
        from coordination.hooks import detect_supersession_tasks

        # Insert a memory with supersedes
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS memories (note_id TEXT PRIMARY KEY, supersedes TEXT)"
        )
        self.conn.execute(
            "INSERT INTO memories (note_id, supersedes) VALUES (?, ?)",
            ("new-note", "old-note"),
        )
        self.conn.commit()

        task_id = detect_supersession_tasks("new-note", "lessons", conn=self.conn)
        self.assertIsNotNone(task_id)

        # Verify task
        row = self.conn.execute("SELECT task_type, description FROM shared_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row[0], "update_references")
        self.assertIn("supersedes", row[1])

    def test_no_supersession_returns_none(self):
        """Returns None when note doesn't supersede anything."""
        from coordination.hooks import detect_supersession_tasks
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS memories (note_id TEXT PRIMARY KEY, supersedes TEXT)"
        )
        self.conn.execute("INSERT INTO memories (note_id) VALUES (?)", ("new-note",))
        self.conn.commit()

        task_id = detect_supersession_tasks("new-note", "lessons", conn=self.conn)
        self.assertIsNone(task_id)


if __name__ == "__main__":
    unittest.main()
