#!/usr/bin/env python3
"""Tests for coordination integration hooks.

Tests the three integration points:
1. Save pipeline file locking (acquire_save_lock / release_save_lock)
2. Cron task auto-creation (create_contradiction_tasks / create_integrity_tasks)
3. Session start task claiming (claim_pending_tasks)
"""
from __future__ import annotations

import json
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
        self.assertTrue(result)
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
        self.assertFalse(result)

    def test_acquire_lock_same_agent_refreshes(self):
        """Same agent can refresh its own lock."""
        acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        result = acquire_save_lock("/memory/lessons/test.md", "agent-a", conn=self.conn)
        self.assertTrue(result)

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
        self.assertGreater(task_id, 0)

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


if __name__ == "__main__":
    unittest.main()
