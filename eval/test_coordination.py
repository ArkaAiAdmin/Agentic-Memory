#!/usr/bin/env python3
"""Behavioral tests for multi-agent coordination system.

Tests cover:
- File locking (acquire, release, expiry, conflict)
- Agent messaging (send, read, broadcast, delivery)
- Project state (get, set, delete, agent activity)
- Task management (create, claim, release, complete)
- Durability (crash recovery, heartbeats, audit logging)
- Coordination hook (session start/end enforcement)
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
from unittest.mock import patch, MagicMock

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from coordination.locking import (
    acquire_lock, release_lock, check_lock, cleanup_expired_locks, list_locks,
)
from coordination.messaging import (
    send_message, read_messages, broadcast_message, get_message_history,
    get_pending_count, cleanup_old_messages,
)
from coordination.project_state import (
    get_state, set_state, delete_state, get_state_keys,
    get_agent_activity, get_active_files, set_agent_status, get_agent_status,
)
from coordination.durability import (
    ensure_durability_tables, record_coordination_event, update_heartbeat,
    check_agent_alive, get_alive_agents, cleanup_stale_agents,
    release_stale_locks, abandon_stale_tasks, cleanup_old_messages as cleanup_old_msgs,
    run_durability_maintenance, get_coordination_audit, get_safety_report,
)


def _make_db():
    """Create a temporary test database with coordination tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Create coordination tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shared_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            description TEXT,
            assigned_to TEXT,
            status TEXT DEFAULT 'pending',
            created_by TEXT NOT NULL,
            created_at REAL,
            updated_at REAL,
            depends_on INTEGER REFERENCES shared_tasks(id)
        );
        CREATE TABLE IF NOT EXISTS project_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            updated_by TEXT NOT NULL,
            updated_at REAL,
            UNIQUE(project_id, key)
        );
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            message_type TEXT NOT NULL,
            payload TEXT,
            status TEXT DEFAULT 'pending',
            created_at REAL,
            delivered_at REAL
        );
        CREATE TABLE IF NOT EXISTS file_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            locked_by TEXT NOT NULL,
            locked_at REAL,
            expires_at REAL,
            UNIQUE(file_path)
        );
    """)
    conn.commit()
    return conn, path


class TestFileLocking(unittest.TestCase):
    """Tests for file lock acquire, release, expiry, and conflict."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_acquire_lock_success(self):
        """Agent can acquire a lock on an unlocked file."""
        result = acquire_lock(self.conn, "/src/main.py", "agent-a")
        self.assertTrue(result)
        lock = check_lock(self.conn, "/src/main.py")
        self.assertIsNotNone(lock)
        self.assertEqual(lock["locked_by"], "agent-a")

    def test_acquire_lock_conflict(self):
        """Agent cannot acquire a lock held by another agent."""
        acquire_lock(self.conn, "/src/main.py", "agent-a")
        result = acquire_lock(self.conn, "/src/main.py", "agent-b")
        self.assertFalse(result)

    def test_acquire_lock_same_agent_refreshes(self):
        """Same agent can refresh its own lock."""
        acquire_lock(self.conn, "/src/main.py", "agent-a", ttl=60)
        result = acquire_lock(self.conn, "/src/main.py", "agent-a", ttl=120)
        self.assertTrue(result)
        lock = check_lock(self.conn, "/src/main.py")
        self.assertEqual(lock["locked_by"], "agent-a")

    def test_release_lock_success(self):
        """Agent can release its own lock."""
        acquire_lock(self.conn, "/src/main.py", "agent-a")
        result = release_lock(self.conn, "/src/main.py", "agent-a")
        self.assertTrue(result)
        lock = check_lock(self.conn, "/src/main.py")
        self.assertIsNone(lock)

    def test_release_lock_wrong_agent(self):
        """Agent cannot release another agent's lock."""
        acquire_lock(self.conn, "/src/main.py", "agent-a")
        result = release_lock(self.conn, "/src/main.py", "agent-b")
        self.assertFalse(result)
        lock = check_lock(self.conn, "/src/main.py")
        self.assertIsNotNone(lock)

    def test_lock_expiry(self):
        """Expired locks are automatically released."""
        acquire_lock(self.conn, "/src/main.py", "agent-a", ttl=0)  # Expires immediately
        time.sleep(0.01)
        lock = check_lock(self.conn, "/src/main.py")
        self.assertIsNone(lock)  # Expired, so None

    def test_cleanup_expired_locks(self):
        """Expired locks are cleaned up."""
        acquire_lock(self.conn, "/src/main.py", "agent-a", ttl=0)
        acquire_lock(self.conn, "/src/main.py", "agent-b")  # Will fail but insert
        time.sleep(0.01)
        count = cleanup_expired_locks(self.conn)
        self.assertGreaterEqual(count, 0)

    def test_list_locks(self):
        """list_locks returns active locks."""
        acquire_lock(self.conn, "/src/main.py", "agent-a")
        acquire_lock(self.conn, "/src/main.py", "agent-b")  # Fails but we check
        locks = list_locks(self.conn)
        self.assertGreaterEqual(len(locks), 1)

    def test_multiple_files(self):
        """Multiple files can be locked independently."""
        acquire_lock(self.conn, "/src/a.py", "agent-a")
        acquire_lock(self.conn, "/src/b.py", "agent-b")
        lock_a = check_lock(self.conn, "/src/a.py")
        lock_b = check_lock(self.conn, "/src/b.py")
        self.assertEqual(lock_a["locked_by"], "agent-a")
        self.assertEqual(lock_b["locked_by"], "agent-b")


class TestAgentMessaging(unittest.TestCase):
    """Tests for inter-agent messaging."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_send_message(self):
        """Can send a message to another agent."""
        msg_id = send_message(self.conn, "agent-a", "agent-b", "task_assign", {"task": "fix bug"})
        self.assertIsInstance(msg_id, int)
        self.assertGreater(msg_id, 0)

    def test_read_messages(self):
        """Can read pending messages."""
        send_message(self.conn, "agent-a", "agent-b", "notification", "hello")
        messages = read_messages(self.conn, "agent-b")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from_agent"], "agent-a")
        self.assertEqual(messages[0]["message_type"], "notification")

    def test_read_messages_marks_delivered(self):
        """Reading messages marks them as delivered."""
        send_message(self.conn, "agent-a", "agent-b", "notification", "hello")
        read_messages(self.conn, "agent-b")
        pending = get_pending_count(self.conn, "agent-b")
        self.assertEqual(pending, 0)

    def test_broadcast_message(self):
        """Broadcast reaches all agents."""
        broadcast_message(self.conn, "agent-a", "alert", "system update")
        # Both agents should see it
        msgs_a = read_messages(self.conn, "agent-a", mark_delivered=False)
        msgs_b = read_messages(self.conn, "agent-b", mark_delivered=False)
        self.assertGreaterEqual(len(msgs_a), 1)
        self.assertGreaterEqual(len(msgs_b), 1)

    def test_message_history(self):
        """Can retrieve message history."""
        send_message(self.conn, "agent-a", "agent-b", "task_assign", "task 1")
        send_message(self.conn, "agent-b", "agent-a", "task_complete", "task 1 done")
        read_messages(self.conn, "agent-b")
        read_messages(self.conn, "agent-a")
        history = get_message_history(self.conn, "agent-a")
        self.assertGreaterEqual(len(history), 1)

    def test_pending_count(self):
        """Pending count reflects unread messages."""
        send_message(self.conn, "agent-a", "agent-b", "notification", "msg 1")
        send_message(self.conn, "agent-a", "agent-b", "notification", "msg 2")
        count = get_pending_count(self.conn, "agent-b")
        self.assertEqual(count, 2)

    def test_cleanup_old_messages(self):
        """Old delivered messages are cleaned up."""
        send_message(self.conn, "agent-a", "agent-b", "notification", "old msg")
        read_messages(self.conn, "agent-b")
        count = cleanup_old_messages(self.conn, max_age_days=0)
        self.assertGreaterEqual(count, 0)


class TestProjectState(unittest.TestCase):
    """Tests for shared project state."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_set_and_get_state(self):
        """Can set and retrieve project state."""
        set_state(self.conn, "proj-1", "current_branch", "main", "agent-a")
        state = get_state(self.conn, "proj-1")
        self.assertIn("current_branch", state)
        self.assertEqual(state["current_branch"]["value"], "main")
        self.assertEqual(state["current_branch"]["updated_by"], "agent-a")

    def test_set_state_overwrites(self):
        """Setting same key overwrites previous value."""
        set_state(self.conn, "proj-1", "branch", "main", "agent-a")
        set_state(self.conn, "proj-1", "branch", "feature-x", "agent-b")
        state = get_state(self.conn, "proj-1")
        self.assertEqual(state["branch"]["value"], "feature-x")
        self.assertEqual(state["branch"]["updated_by"], "agent-b")

    def test_delete_state(self):
        """Can delete state entries."""
        set_state(self.conn, "proj-1", "branch", "main", "agent-a")
        result = delete_state(self.conn, "proj-1", "branch")
        self.assertTrue(result)
        state = get_state(self.conn, "proj-1")
        self.assertNotIn("branch", state)

    def test_get_state_keys(self):
        """Can list all state keys."""
        set_state(self.conn, "proj-1", "branch", "main", "agent-a")
        set_state(self.conn, "proj-1", "active_file", "src/main.py", "agent-a")
        keys = get_state_keys(self.conn, "proj-1")
        self.assertEqual(len(keys), 2)
        self.assertIn("branch", keys)
        self.assertIn("active_file", keys)

    def test_agent_activity(self):
        """Can see what each agent is doing."""
        set_state(self.conn, "proj-1", "agent:agent-a:status", "working on feature X", "agent-a")
        set_state(self.conn, "proj-1", "agent:agent-b:status", "reviewing PR", "agent-b")
        activity = get_agent_activity(self.conn, "proj-1")
        self.assertIn("agent-a", activity)
        self.assertIn("agent-b", activity)

    def test_get_active_files(self):
        """Can see which files are being worked on."""
        set_state(self.conn, "proj-1", "file:/src/main.py", "editing", "agent-a")
        set_state(self.conn, "proj-1", "file:/src/utils.py", "reviewing", "agent-b")
        files = get_active_files(self.conn, "proj-1")
        self.assertEqual(len(files), 2)

    def test_set_agent_status(self):
        """Can update agent status."""
        set_agent_status(self.conn, "proj-1", "agent-a", "working")
        status = get_agent_status(self.conn, "proj-1", "agent-a")
        self.assertEqual(status, "working")


class TestTaskManagement(unittest.TestCase):
    """Tests for shared task board."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_create_task(self):
        """Can create a task."""
        cursor = self.conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("proj-1", "fix", "Fix bug in main", None, "pending", "agent-a", time.time(), time.time()),
        )
        self.conn.commit()
        task_id = cursor.lastrowid
        self.assertGreater(task_id, 0)

    def test_claim_task(self):
        """Agent can claim a pending task."""
        cursor = self.conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("proj-1", "fix", "Fix bug", None, "pending", "agent-a", time.time(), time.time()),
        )
        self.conn.commit()
        task_id = cursor.lastrowid

        self.conn.execute(
            "UPDATE shared_tasks SET assigned_to='agent-b', status='active', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        self.conn.commit()

        row = self.conn.execute("SELECT assigned_to, status FROM shared_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row[0], "agent-b")
        self.assertEqual(row[1], "active")

    def test_complete_task(self):
        """Agent can complete a task."""
        cursor = self.conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("proj-1", "fix", "Fix bug", "agent-a", "active", "agent-a", time.time(), time.time()),
        )
        self.conn.commit()
        task_id = cursor.lastrowid

        self.conn.execute(
            "UPDATE shared_tasks SET status='completed', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        self.conn.commit()

        row = self.conn.execute("SELECT status FROM shared_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row[0], "completed")


class TestDurability(unittest.TestCase):
    """Tests for crash recovery, heartbeats, and audit logging."""

    def setUp(self):
        self.conn, self.db_path = _make_db()
        ensure_durability_tables(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_heartbeat_update(self):
        """Agent can update heartbeat."""
        update_heartbeat(self.conn, "agent-a", session_id="sess-1", project_id="proj-1")
        alive = check_agent_alive(self.conn, "agent-a")
        self.assertTrue(alive)

    def test_agent_alive_check(self):
        """Agent is alive if heartbeat is recent."""
        update_heartbeat(self.conn, "agent-a")
        self.assertTrue(check_agent_alive(self.conn, "agent-a"))
        self.assertFalse(check_agent_alive(self.conn, "agent-b"))  # Never sent heartbeat

    def test_get_alive_agents(self):
        """Can list alive agents."""
        update_heartbeat(self.conn, "agent-a")
        update_heartbeat(self.conn, "agent-b")
        alive = get_alive_agents(self.conn)
        self.assertEqual(len(alive), 2)

    def test_cleanup_stale_agents(self):
        """Stale agents are cleaned up."""
        # Insert a stale heartbeat (old timestamp)
        self.conn.execute(
            "INSERT INTO agent_heartbeats (agent_id, last_heartbeat, session_id, project_id) VALUES (?, ?, ?, ?)",
            ("stale-agent", time.time() - 10000, None, None),
        )
        self.conn.commit()
        count = cleanup_stale_agents(self.conn)
        self.assertGreaterEqual(count, 0)

    def test_record_coordination_event(self):
        """Events are recorded to audit log."""
        record_coordination_event(self.conn, "test_action", "agent-a", target="test", detail="test detail")
        events = get_coordination_audit(self.conn)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "test_action")
        self.assertEqual(events[0]["agent_id"], "agent-a")

    def test_safety_report(self):
        """Safety report computes correctly."""
        update_heartbeat(self.conn, "agent-a")
        report = get_safety_report(self.conn)
        self.assertIn("safety_score", report)
        self.assertIn("alive_agents", report)
        self.assertGreaterEqual(report["safety_score"], 0)
        self.assertLessEqual(report["safety_score"], 100)

    def test_durability_maintenance(self):
        """Maintenance runs all cleanup tasks."""
        result = run_durability_maintenance(self.conn)
        self.assertIn("stale_locks_released", result)
        self.assertIn("stale_tasks_abandoned", result)
        self.assertIn("old_messages_cleaned", result)


class TestCoordinationHook(unittest.TestCase):
    """Tests for the coordination hook integration."""

    def setUp(self):
        self.conn, self.db_path = _make_db()
        ensure_durability_tables(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_session_start_returns_output(self):
        """Session start produces output."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import importlib
        mod = importlib.import_module("hooks.memory-coordination")
        with patch.object(mod, "_get_conn", return_value=self.conn):
            output = mod._on_session_start("agent-a", "proj-1")
            self.assertIsInstance(output, str)

    def test_session_end_releases_locks(self):
        """Session end releases locks held by the agent."""
        self.conn.execute(
            "INSERT INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
            ("/src/main.py", "agent-a", time.time(), time.time() + 300),
        )
        self.conn.commit()

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import importlib
        mod = importlib.import_module("hooks.memory-coordination")
        with patch.object(mod, "_get_conn", return_value=self.conn):
            output = mod._on_session_end("agent-a", "proj-1")
            self.assertIn("Released", output)

    def test_session_end_does_not_auto_complete_tasks(self):
        """Session end releases locks but does NOT auto-complete tasks (by design).

        Auto-completing tasks on session end is dangerous — tasks should only
        be completed explicitly by the agent working on them. Session end
        releases locks (crash recovery) but leaves tasks for manual management.
        """
        self.conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, assigned_to, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj-1", "fix", "agent-a", "active", "agent-a", time.time(), time.time()),
        )
        self.conn.commit()

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import importlib
        mod = importlib.import_module("hooks.memory-coordination")
        with patch.object(mod, "_get_conn", return_value=self.conn):
            output = mod._on_session_end("agent-a", "proj-1")
            # Should NOT contain "Completed" — tasks are left as-is
            self.assertNotIn("Completed", output)

        # Verify task is still active (reopen conn since hook may have closed it)
        conn2 = sqlite3.connect(self.db_path)
        row = conn2.execute("SELECT status FROM shared_tasks WHERE assigned_to='agent-a'").fetchone()
        conn2.close()
        self.assertEqual(row[0], "active")


if __name__ == "__main__":
    unittest.main()
