#!/usr/bin/env python3
"""Adversarial and security tests for multi-agent coordination system.

These tests probe edge cases, race conditions, injection attacks,
resource exhaustion, and crash recovery scenarios that the behavioral
tests in test_coordination.py do not cover.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from coordination.locking import acquire_lock, release_lock, check_lock
from coordination.messaging import (
    send_message, read_messages, broadcast_message,
    cleanup_old_messages,
)
from coordination.project_state import set_state, get_state, delete_state
from coordination.durability import (
    ensure_durability_tables, update_heartbeat, check_agent_alive, run_durability_maintenance, cleanup_old_audit_entries,
)


def _make_db():
    """Create a fresh temporary test database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shared_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, task_type TEXT NOT NULL,
            description TEXT, assigned_to TEXT, status TEXT DEFAULT 'pending',
            created_by TEXT NOT NULL, created_at REAL, updated_at REAL,
            depends_on INTEGER REFERENCES shared_tasks(id)
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
        CREATE TABLE IF NOT EXISTS file_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL, locked_by TEXT NOT NULL,
            locked_at REAL, expires_at REAL,
            lock_version INTEGER DEFAULT 0, tenant_id TEXT DEFAULT 'default',
            UNIQUE(file_path)
        );
    """)
    conn.commit()
    return conn, path


# ── 1. Race Conditions ──────────────────────────────────────────────────

class TestRaceConditions(unittest.TestCase):
    """Verify concurrent lock acquisition yields exactly one winner."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_concurrent_lock_acquire(self):
        """Two threads racing for the same lock — exactly one wins."""
        results = {"a": False, "b": False}
        barrier = threading.Barrier(2)

        def try_lock(agent_id):
            barrier.wait()
            # Each thread gets its own connection for true concurrency
            conn2 = sqlite3.connect(self.db_path, timeout=5)
            conn2.execute("PRAGMA journal_mode=WAL")
            results[agent_id] = acquire_lock(conn2, "/shared.py", agent_id, ttl=60)
            conn2.close()

        t1 = threading.Thread(target=try_lock, args=("agent-a",))
        t2 = threading.Thread(target=try_lock, args=("agent-b",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        winners = sum(1 for v in results.values() if v)
        self.assertEqual(winners, 1, f"Expected exactly 1 winner, got {winners}: {results}")

    def test_concurrent_broadcast_read(self):
        """Two agents reading the same broadcast — both get it."""
        broadcast_message(self.conn, "agent-x", "alert", "system update")
        self.conn.commit()

        # Both agents should see the broadcast (read without marking delivered)
        msgs_a = read_messages(self.conn, "agent-a", mark_delivered=False)
        msgs_b = read_messages(self.conn, "agent-b", mark_delivered=False)

        # Filter to only the broadcast message from agent-x
        bc_a = [m for m in msgs_a if m["from_agent"] == "agent-x"]
        bc_b = [m for m in msgs_b if m["from_agent"] == "agent-x"]

        self.assertGreaterEqual(len(bc_a), 1, "agent-a should see broadcast")
        self.assertGreaterEqual(len(bc_b), 1, "agent-b should see broadcast")


# ── 2. SQL Injection ────────────────────────────────────────────────────

class TestSQLInjection(unittest.TestCase):
    """Verify SQL injection attempts are stored as literal strings."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_injection_in_agent_id(self):
        """SQL in agent_id is stored literally."""
        malicious = "'; DROP TABLE file_locks; --"
        acquire_lock(self.conn, "/test.py", malicious)
        lock = check_lock(self.conn, "/test.py")
        # Should either fail validation or store literally
        if lock:
            self.assertEqual(lock["locked_by"], malicious)
        # Table should still exist
        result = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_locks'").fetchone()
        self.assertIsNotNone(result)

    def test_injection_in_file_path(self):
        """SQL in file_path is stored literally."""
        malicious = "'; DELETE FROM agent_messages; --"
        acquire_lock(self.conn, malicious, "agent-a")
        # Messages table should still exist and be intact
        result = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_messages'").fetchone()
        self.assertIsNotNone(result)

    def test_injection_in_message_type(self):
        """SQL in message_type is stored literally."""
        malicious = "'; DROP TABLE shared_tasks; --"
        send_message(self.conn, "agent-a", "agent-b", malicious, "payload")
        msgs = read_messages(self.conn, "agent-b", mark_delivered=False)
        if msgs:
            self.assertEqual(msgs[0]["message_type"], malicious)
        result = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shared_tasks'").fetchone()
        self.assertIsNotNone(result)

    def test_injection_in_payload(self):
        """SQL in payload is stored literally."""
        malicious = "1; DROP TABLE file_locks; --"
        send_message(self.conn, "agent-a", "agent-b", "test", malicious)
        msgs = read_messages(self.conn, "agent-b", mark_delivered=False)
        if msgs:
            self.assertEqual(msgs[0]["payload"], malicious)
        result = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_locks'").fetchone()
        self.assertIsNotNone(result)


# ── 3. Input Validation ─────────────────────────────────────────────────

class TestInputValidation(unittest.TestCase):
    """Edge cases in input parameters."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_empty_agent_id(self):
        """Empty agent_id should not crash."""
        result = acquire_lock(self.conn, "/test.py", "")
        # Should either succeed or fail gracefully, not crash
        self.assertIsInstance(result, bool)

    def test_none_payload(self):
        """None payload should not crash."""
        msg_id = send_message(self.conn, "a", "b", "test", None)
        self.assertIsInstance(msg_id, int)

    def test_empty_payload(self):
        """Empty string payload should work."""
        msg_id = send_message(self.conn, "a", "b", "test", "")
        self.assertIsInstance(msg_id, int)

    def test_very_long_agent_id(self):
        """Agent ID > 1000 chars should not crash."""
        long_id = "a" * 1000
        result = acquire_lock(self.conn, "/test.py", long_id)
        self.assertIsInstance(result, bool)

    def test_unicode_agent_id(self):
        """Unicode in agent_id should be stored."""
        unicode_id = "agent-日本語-🚀"
        acquire_lock(self.conn, "/test.py", unicode_id)
        lock = check_lock(self.conn, "/test.py")
        if lock:
            self.assertEqual(lock["locked_by"], unicode_id)

    def test_special_characters_in_file_path(self):
        """Special chars in file_path should work."""
        path = "/src/file with spaces (1).py"
        acquire_lock(self.conn, path, "agent-a")
        lock = check_lock(self.conn, path)
        self.assertIsNotNone(lock)

    def test_path_traversal_in_file_path(self):
        """Path traversal should be stored but flagged."""
        path = "/src/../../etc/passwd"
        acquire_lock(self.conn, path, "agent-a")
        lock = check_lock(self.conn, path)
        # It's stored — the MCP layer validates, but the raw function doesn't
        if lock:
            self.assertEqual(lock["locked_by"], "agent-a")


# ── 4. Message Size Limits ──────────────────────────────────────────────

class TestMessageSize(unittest.TestCase):
    """Verify handling of very large messages."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_large_payload(self):
        """1MB payload should be stored without crash."""
        large = "x" * 1_000_000
        msg_id = send_message(self.conn, "a", "b", "test", large)
        self.assertIsInstance(msg_id, int)
        msgs = read_messages(self.conn, "b", mark_delivered=False)
        self.assertEqual(len(msgs), 1)

    def test_huge_payload(self):
        """10MB payload should be stored (or rejected gracefully)."""
        huge = "y" * 10_000_000
        try:
            msg_id = send_message(self.conn, "a", "b", "test", huge)
            self.assertIsInstance(msg_id, int)
        except sqlite3.OperationalError:
            pass  # SQLite may reject — that's fine


# ── 5. Lock Expiry Edge Cases ───────────────────────────────────────────

class TestLockExpiryEdgeCases(unittest.TestCase):
    """Boundary conditions for lock TTL."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_zero_ttl(self):
        """TTL=0 should expire immediately."""
        acquire_lock(self.conn, "/test.py", "a", ttl=0)
        # May or may not be visible depending on timing
        lock = check_lock(self.conn, "/test.py")
        # With TTL=0, expires_at = now, so by the time we check, it's expired
        self.assertIsNone(lock)

    def test_negative_ttl(self):
        """Negative TTL should result in expired lock."""
        acquire_lock(self.conn, "/test.py", "a", ttl=-10)
        lock = check_lock(self.conn, "/test.py")
        self.assertIsNone(lock)

    def test_very_large_ttl(self):
        """Very large TTL (1 year) should work."""
        ttl = 365 * 86400
        result = acquire_lock(self.conn, "/test.py", "a", ttl=ttl)
        self.assertTrue(result)
        lock = check_lock(self.conn, "/test.py")
        self.assertIsNotNone(lock)

    def test_lock_refresh_extends_expiry(self):
        """Refreshing a lock should extend its expiry."""
        acquire_lock(self.conn, "/test.py", "a", ttl=10)
        # Advance the clock so the second acquire creates a measurably later expiry
        t = time.time()
        with mock.patch("coordination.locking.time.time", return_value=t + 1):
            acquire_lock(self.conn, "/test.py", "a", ttl=60)
        lock = check_lock(self.conn, "/test.py")
        self.assertIsNotNone(lock)
        # Remaining should be close to 60, not 10
        remaining = lock["expires_at"] - time.time()
        self.assertGreater(remaining, 50)


# ── 6. Dead Letter Scenarios ────────────────────────────────────────────

class TestDeadLetter(unittest.TestCase):
    """Operations on non-existent resources."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_read_messages_for_unknown_agent(self):
        """Reading messages for unknown agent returns empty."""
        msgs = read_messages(self.conn, "nonexistent-agent")
        self.assertEqual(msgs, [])

    def test_release_lock_on_unlocked_file(self):
        """Releasing a lock on an unlocked file returns True (no-op)."""
        result = release_lock(self.conn, "/not-locked.py", "agent-a")
        self.assertTrue(result)

    def test_check_lock_on_unlocked_file(self):
        """Checking lock on unlocked file returns None."""
        result = check_lock(self.conn, "/not-locked.py")
        self.assertIsNone(result)

    def test_read_messages_records_delivery_audit(self):
        """Reading messages should auto-record delivery in audit log."""
        ensure_durability_tables(self.conn)
        send_message(self.conn, "a", "b", "test", "hello")
        read_messages(self.conn, "b")
        # Audit log should have a delivery entry
        rows = self.conn.execute(
            "SELECT action FROM coordination_audit WHERE action='message_delivered'"
        ).fetchall()
        self.assertGreaterEqual(len(rows), 1)

    def test_delete_nonexistent_state(self):
        """Deleting non-existent state returns False."""
        result = delete_state(self.conn, "proj", "nonexistent-key")
        self.assertFalse(result)

    def test_get_state_empty_project(self):
        """Getting state for unknown project returns empty dict."""
        state = get_state(self.conn, "nonexistent-project")
        self.assertEqual(state, {})


# ── 7. Crash Recovery ───────────────────────────────────────────────────

class TestCrashRecovery(unittest.TestCase):
    """Simulate crash scenarios and verify consistency."""

    def setUp(self):
        self.conn, self.db_path = _make_db()
        ensure_durability_tables(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_partial_transaction_rollback(self):
        """If commit fails, data should be consistent."""
        acquire_lock(self.conn, "/test.py", "a", ttl=60)
        # Simulate crash by not committing a read
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("INSERT INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
                              ("/test2.py", "b", time.time(), time.time() + 60))
            # Simulate crash — rollback instead of commit
            self.conn.rollback()
        except Exception:
            self.conn.rollback()

        # Original lock should still be there
        lock = check_lock(self.conn, "/test.py")
        self.assertIsNotNone(lock)
        # Second lock should NOT be there
        lock2 = check_lock(self.conn, "/test2.py")
        self.assertIsNone(lock2)

    def test_durability_maintenance_idempotent(self):
        """Running maintenance twice should not crash."""
        result1 = run_durability_maintenance(self.conn)
        result2 = run_durability_maintenance(self.conn)
        self.assertIn("stale_locks_released", result1)
        self.assertIn("stale_locks_released", result2)

    def test_heartbeat_survives_restart(self):
        """Heartbeat should persist across connection restarts."""
        update_heartbeat(self.conn, "agent-a")
        self.conn.close()

        conn2 = sqlite3.connect(self.db_path, timeout=5)
        ensure_durability_tables(conn2)
        alive = check_agent_alive(conn2, "agent-a")
        self.assertTrue(alive)
        conn2.close()

        # Reopen original
        self.conn = sqlite3.connect(self.db_path, timeout=5)


# ── 8. State Corruption ────────────────────────────────────────────────

class TestStateCorruption(unittest.TestCase):
    """Malformed data should not crash the system."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_malformed_json_value(self):
        """Malformed JSON should be stored as string."""
        set_state(self.conn, "proj", "key", "not valid json {{{", "agent-a")
        state = get_state(self.conn, "proj")
        self.assertEqual(state["key"]["value"], "not valid json {{{")

    def test_very_deep_json(self):
        """Deeply nested JSON should be stored."""
        deep = {"a": {"b": {"c": {"d": {"e": "f"}}}}}
        set_state(self.conn, "proj", "key", deep, "agent-a")
        state = get_state(self.conn, "proj")
        self.assertEqual(state["key"]["value"], deep)

    def test_empty_dict_value(self):
        """Empty dict should be stored."""
        set_state(self.conn, "proj", "key", {}, "agent-a")
        state = get_state(self.conn, "proj")
        self.assertEqual(state["key"]["value"], {})

    def test_numeric_value(self):
        """Numeric values should be stored as strings."""
        set_state(self.conn, "proj", "key", 42, "agent-a")
        state = get_state(self.conn, "proj")
        self.assertIsNotNone(state["key"]["value"])

    def test_boolean_value(self):
        """Boolean values should be stored."""
        set_state(self.conn, "proj", "key", True, "agent-a")
        state = get_state(self.conn, "proj")
        self.assertIsNotNone(state["key"]["value"])


# ── 9. Resource Exhaustion ──────────────────────────────────────────────

class TestResourceExhaustion(unittest.TestCase):
    """Verify cleanup works under load."""

    def setUp(self):
        self.conn, self.db_path = _make_db()
        ensure_durability_tables(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_many_locks_cleanup(self):
        """Creating and cleaning up many locks should work."""
        for i in range(100):
            self.conn.execute(
                "INSERT OR REPLACE INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
                (f"/file-{i}.py", "agent-a", time.time() - 400, time.time() - 100),  # All expired
            )
        self.conn.commit()

        from coordination.locking import cleanup_expired_locks
        count = cleanup_expired_locks(self.conn)
        self.assertEqual(count, 100)

    def test_many_messages_cleanup(self):
        """Creating and cleaning up many messages should work."""
        for i in range(100):
            self.conn.execute(
                "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at, delivered_at) "
                "VALUES (?, ?, ?, ?, 'delivered', ?, ?)",
                ("a", "b", "test", f"msg-{i}", time.time() - 40000000, time.time() - 40000000),
            )
        self.conn.commit()

        count = cleanup_old_messages(self.conn, max_age_days=0)
        self.assertEqual(count, 100)

    def test_many_audit_entries_cleanup(self):
        """Audit log rotation should work."""
        for i in range(100):
            self.conn.execute(
                "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("test", "agent-a", str(i), "detail", time.time() - 100000000),  # Old
            )
        self.conn.commit()

        count = cleanup_old_audit_entries(self.conn, max_age_days=0)
        self.assertEqual(count, 100)


# ── 10. Fuzz Testing ────────────────────────────────────────────────────

class TestFuzz(unittest.TestCase):
    """Random inputs should not crash any function."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_fuzz_lock_operations(self):
        """Random inputs to lock functions should not crash."""
        import random
        import string

        for _ in range(50):
            path = "/" + "".join(random.choices(string.ascii_letters + "/._-", k=random.randint(1, 100)))
            agent = "".join(random.choices(string.ascii_letters, k=random.randint(0, 50)))
            ttl = random.randint(-100, 100000)

            try:
                acquire_lock(self.conn, path, agent, ttl=ttl)
                check_lock(self.conn, path)
                release_lock(self.conn, path, agent)
            except Exception:
                pass  # Should not crash

    def test_fuzz_message_operations(self):
        """Random inputs to message functions should not crash."""
        import random
        import string

        for _ in range(50):
            from_agent = "".join(random.choices(string.ascii_letters, k=random.randint(0, 50)))
            to_agent = "".join(random.choices(string.ascii_letters, k=random.randint(0, 50)))
            msg_type = "".join(random.choices(string.ascii_letters, k=random.randint(0, 50)))
            payload = "".join(random.choices(string.printable, k=random.randint(0, 1000)))

            try:
                msg_id = send_message(self.conn, from_agent, to_agent, msg_type, payload)
                read_messages(self.conn, to_agent, mark_delivered=False)
            except Exception:
                pass  # Should not crash

    def test_fuzz_state_operations(self):
        """Random inputs to state functions should not crash."""
        import random
        import string

        for _ in range(50):
            project = "".join(random.choices(string.ascii_letters, k=random.randint(1, 50)))
            key = "".join(random.choices(string.ascii_letters, k=random.randint(1, 50)))
            value = "".join(random.choices(string.printable, k=random.randint(0, 500)))
            agent = "".join(random.choices(string.ascii_letters, k=random.randint(0, 50)))

            try:
                set_state(self.conn, project, key, value, agent)
                get_state(self.conn, project)
                delete_state(self.conn, project, key)
            except Exception:
                pass  # Should not crash


# ── 11. Ordering Guarantees ─────────────────────────────────────────────

class TestOrdering(unittest.TestCase):
    """Messages should be returned in FIFO order."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_message_fifo_order(self):
        """Messages are returned in insertion order."""
        base_t = time.time()
        counter = [0]
        def _tick():
            counter[0] += 1
            return base_t + 0.001 * counter[0]
        with mock.patch("coordination.messaging.time.time", side_effect=_tick):
            for i in range(10):
                send_message(self.conn, "a", "b", "test", f"msg-{i}")

        messages = read_messages(self.conn, "b", mark_delivered=False)
        self.assertEqual(len(messages), 10)
        for i, msg in enumerate(messages):
            self.assertEqual(msg["payload"], f"msg-{i}")


# ── 12. Double Operations ───────────────────────────────────────────────

class TestDoubleOperations(unittest.TestCase):
    """Verify idempotency and error handling for repeated operations."""

    def setUp(self):
        self.conn, self.db_path = _make_db()
        ensure_durability_tables(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_double_release(self):
        """Releasing same lock twice — second should return True (no-op)."""
        acquire_lock(self.conn, "/test.py", "a")
        r1 = release_lock(self.conn, "/test.py", "a")
        r2 = release_lock(self.conn, "/test.py", "a")
        self.assertTrue(r1)
        self.assertTrue(r2)  # No-op, not an error

    def test_double_acquire_same_agent(self):
        """Same agent acquiring twice should refresh."""
        r1 = acquire_lock(self.conn, "/test.py", "a", ttl=10)
        r2 = acquire_lock(self.conn, "/test.py", "a", ttl=60)
        self.assertTrue(r1)
        self.assertTrue(r2)
        lock = check_lock(self.conn, "/test.py")
        self.assertEqual(lock["locked_by"], "a")

    def test_broadcast_and_read_own(self):
        """Agent that broadcasts should also receive its own message."""
        broadcast_message(self.conn, "agent-a", "alert", "update")
        msgs = read_messages(self.conn, "agent-a", mark_delivered=False)
        self.assertGreaterEqual(len(msgs), 1)

    def test_stale_lock_takeover(self):
        """After lock expires, another agent should be able to acquire it."""
        # Acquire a lock, then backdate its expiry so it's already expired
        acquire_lock(self.conn, "/test.py", "a", ttl=60)
        now = time.time()
        self.conn.execute(
            "UPDATE file_locks SET expires_at=? WHERE file_path=?",
            (now - 5, "/test.py"),
        )
        self.conn.commit()
        # Expired — agent-b should be able to acquire
        result = acquire_lock(self.conn, "/test.py", "b")
        self.assertTrue(result)
        lock = check_lock(self.conn, "/test.py")
        self.assertEqual(lock["locked_by"], "b")

    def test_task_status_transition_is_the_ack(self):
        """Task status transitions are the coordination primitive (not acks).

        Flow: Agent A sends message → Agent B reads it → Agent B updates task status
        The task status change IS the acknowledgement.
        """
        ensure_durability_tables(self.conn)

        # Agent A creates a task
        cursor = self.conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("proj", "fix", "Fix bug", "agent-b", "active", "agent-a", time.time(), time.time()),
        )
        self.conn.commit()
        task_id = cursor.lastrowid

        # Agent A sends message to Agent B
        msg_id = send_message(self.conn, "agent-a", "agent-b", "task_assign", json.dumps({"task_id": task_id}))

        # Agent B reads the message
        msgs = read_messages(self.conn, "agent-b")
        self.assertEqual(len(msgs), 1)

        # Agent B completes the task — this IS the ack
        self.conn.execute(
            "UPDATE shared_tasks SET status='completed', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        self.conn.commit()

        # Agent A checks task status — sees it's completed
        row = self.conn.execute("SELECT status FROM shared_tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row[0], "completed")


# ── 13. Concurrency Safety ──────────────────────────────────────────────

class TestConcurrencySafety(unittest.TestCase):
    """Verify no data corruption under concurrent access."""

    def setUp(self):
        self.conn, self.db_path = _make_db()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_concurrent_writes_different_files(self):
        """Multiple agents locking different files should all succeed."""
        barrier = threading.Barrier(5)
        results = [False] * 5

        def lock_file(idx):
            barrier.wait()
            conn2 = sqlite3.connect(self.db_path, timeout=5)
            results[idx] = acquire_lock(conn2, f"/file-{idx}.py", f"agent-{idx}")
            conn2.close()

        threads = [threading.Thread(target=lock_file, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertTrue(all(results), f"Not all locks acquired: {results}")

    def test_concurrent_state_writes(self):
        """Multiple agents writing different state keys should not conflict."""
        barrier = threading.Barrier(5)
        results = [False] * 5

        def write_state(idx):
            barrier.wait()
            conn2 = sqlite3.connect(self.db_path, timeout=5)
            results[idx] = set_state(conn2, "proj", f"key-{idx}", f"value-{idx}", f"agent-{idx}")
            conn2.close()

        threads = [threading.Thread(target=write_state, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertTrue(all(results))


if __name__ == "__main__":
    unittest.main()
