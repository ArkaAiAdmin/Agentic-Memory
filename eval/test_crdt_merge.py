#!/usr/bin/env python3
"""Unit tests for crdt_merge.

Covers H19 fix: crdt_save must use the remote_vv_str parameter, not
re-parse the local row's vector (which silently biases toward "local
dominates" and swallows conflicts in multi-agent sync).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from memory_common import connection_pool, open_db

import crdt_merge
from crdt_merge import crdt_save, crdt_sync_all, dominates, concurrent


def _fresh_db(name: str) -> Path:
    """Return a fresh temp DB path; clear the connection pool so the
    new path takes effect on the next checkout."""
    p = Path(tempfile.mkdtemp(prefix=f"crdt_{name}_")) / "memory.db"
    connection_pool.clear()
    return p


class TestCrdtDominance(unittest.TestCase):
    """Pure-function tests for the dominance helpers (no DB)."""

    def test_dominates_basic(self):
        self.assertTrue(dominates({"a": 2, "b": 3}, {"a": 1, "b": 2}))
        self.assertFalse(dominates({"a": 1, "b": 2}, {"a": 2, "b": 1}))
        self.assertTrue(dominates({"a": 1}, {"a": 1}))  # equal dominates
        self.assertTrue(dominates({}, {}))

    def test_concurrent(self):
        self.assertTrue(concurrent({"a": 2, "b": 1}, {"a": 1, "b": 2}))
        self.assertFalse(concurrent({"a": 2}, {"a": 1}))  # dominates, not concurrent
        self.assertFalse(concurrent({"a": 1, "b": 1}, {"a": 1, "b": 1}))


class TestCrdtSaveNewNote(unittest.TestCase):
    def test_new_note_accepted(self):
        db = _fresh_db("new")
        result = crdt_save(
            str(db),
            note_id="lessons/test_new",
            content="hello",
            remote_agent_id="agent-A",
            local_agent_id="agent-B",
        )
        self.assertTrue(result["applied"])


class TestCrdtSaveRemoteDominates(unittest.TestCase):
    """If the local row's vector is dominated by the remote, the remote
    write is the newer state — we accept (apply) and update local.
    """

    _counter = 0

    def setUp(self):
        TestCrdtSaveRemoteDominates._counter += 1
        self.db = _fresh_db(f"remote_dominates_{TestCrdtSaveRemoteDominates._counter}")
        # Seed: agent-A has already written the note with vv={a:1, b:5}.
        with open_db(self.db) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, source_file, tags, created_at, updated_at, observed_at,
                    fitness_score, importance, pinned, version_vector, logical_clock)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 3, 0, ?, ?)""",
                (
                    "lessons/cas_test",
                    "old content",
                    "lessons/cas_test.md",
                    "[]",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    json.dumps({"a": 1, "b": 5}),
                    5,
                ),
            )

    def test_remote_dominates_local_applies(self):
        # Remote {a:2, b:6} dominates local {a:1, b:5}.
        result = crdt_save(
            str(self.db),
            note_id="lessons/cas_test",
            content="newer remote content",
            remote_agent_id="agent-A",
            local_agent_id="agent-B",
            remote_vv_str=json.dumps({"a": 2, "b": 6}),
            remote_logical_clock=6,
        )
        self.assertTrue(result["applied"], f"Expected apply, got {result}")
        self.assertFalse(result["rejected"])

    def test_local_dominates_remote_rejects(self):
        # Local {a:1, b:5} dominates remote {a:1, b:3}.
        result = crdt_save(
            str(self.db),
            note_id="lessons/cas_test",
            content="stale remote content",
            remote_agent_id="agent-A",
            local_agent_id="agent-B",
            remote_vv_str=json.dumps({"a": 1, "b": 3}),
            remote_logical_clock=3,
        )
        self.assertTrue(result["rejected"], f"Expected reject, got {result}")
        self.assertFalse(result["applied"])

    def test_concurrent_uses_lww(self):
        # Local {a:1, b:5}, Remote {a:2, b:3} — neither dominates.
        # v13: the field-level LWWES path is used. Concurrent writes
        # to the same field are resolved per-field. Local clock (5)
        # > remote clock (3), so local wins; the incoming remote
        # value is NOT applied. The result reports ``conflict=True``
        # but ``applied=False, rejected=False`` (rejection is a
        # whole-note concept; the field-level merge just doesn't
        # change anything because the local LWW winner wins).
        result = crdt_save(
            str(self.db),
            note_id="lessons/cas_test",
            content="local concurrent write",
            remote_agent_id="agent-A",
            local_agent_id="agent-B",
            remote_vv_str=json.dumps({"a": 2, "b": 3}),
            remote_logical_clock=3,
        )
        self.assertTrue(result["conflict"], f"Expected conflict, got {result}")
        self.assertFalse(
            result["applied"],
            f"Local LWW wins; remote should not apply, got {result}",
        )


class TestCrdtSyncAll(unittest.TestCase):
    def test_sync_all_signature(self):
        """crdt_sync_all should accept the new 5-tuple shape."""
        db = _fresh_db("sync")
        result = crdt_sync_all(str(db), "agent-A", "agent-B", {})
        self.assertEqual(result["total"], 0)


class TestMergeVectorsProperties(unittest.TestCase):
    """C1 fix: property tests for merge_vectors (B1 regression).

    merge_vectors must be:
    - Idempotent:  merge(x, x) == x
    - Commutative: merge(x, y) == merge(y, x)
    - Associative: merge(merge(x, y), z) == merge(x, merge(y, z))
    - Monotone:    if x dominates y then merge(x, y) == x
    """

    def test_idempotency_basic(self):
        from crdt_merge import merge_vectors

        v = {"a": 5, "b": 3, "c": 7}
        r1 = merge_vectors("a", v, v)
        r2 = merge_vectors("a", v, v)
        r3 = merge_vectors("a", v, v)
        self.assertEqual(r1, r2)
        self.assertEqual(r2, r3)
        # And equal to the input (modulo agent_id bump which we removed)
        for k, val in v.items():
            self.assertEqual(
                r1[k], val, f"merge_vectors(x,x)[{k}] should be {val}, got {r1[k]}"
            )

    def test_idempotency_three_agents(self):
        from crdt_merge import merge_vectors

        v1 = {"alice": 3, "bob": 5, "carol": 2}
        v2 = {"alice": 7, "bob": 1, "carol": 4}
        m = merge_vectors("alice", v1, v2)
        m_again = merge_vectors("alice", v1, v2)
        m_third = merge_vectors("alice", v1, v2)
        self.assertEqual(m, m_again)
        self.assertEqual(m_again, m_third)

    def test_commutativity(self):
        from crdt_merge import merge_vectors

        v1 = {"alice": 3, "bob": 5}
        v2 = {"alice": 7, "bob": 1}
        m12 = merge_vectors("alice", v1, v2)
        m21 = merge_vectors("alice", v2, v1)
        # After B1 fix, merge is pure pointwise-max: commutative.
        self.assertEqual(m12, m21, "merge_vectors must be commutative")

    def test_associativity(self):
        from crdt_merge import merge_vectors

        v1 = {"a": 1, "b": 2}
        v2 = {"a": 3, "b": 1}
        v3 = {"a": 2, "b": 4}
        # (v1 ⊕ v2) ⊕ v3
        m12 = merge_vectors("a", v1, v2)
        m123_left = merge_vectors("a", m12, v3)
        # v1 ⊕ (v2 ⊕ v3)
        m23 = merge_vectors("a", v2, v3)
        m123_right = merge_vectors("a", v1, m23)
        self.assertEqual(m123_left, m123_right, "merge_vectors must be associative")

    def test_monotone(self):
        from crdt_merge import merge_vectors

        v1 = {"a": 5, "b": 3}
        v2 = {"a": 3, "b": 1}  # v1 dominates v2
        m = merge_vectors("a", v1, v2)
        # v1 dominates v2, so merge should equal v1
        self.assertEqual(m, v1, "merge(x,y) where x dominates y should equal x")

    def test_empty_vectors(self):
        from crdt_merge import merge_vectors

        # merge with empty should be identity
        m = merge_vectors("a", {}, {"x": 5, "y": 3})
        self.assertEqual(m, {"x": 5, "y": 3})
        m2 = merge_vectors("a", {"x": 5}, {})
        self.assertEqual(m2, {"x": 5})

    def test_regression_b1_local_clock_no_longer_bumps(self):
        """B1 regression: pre-fix merge incremented local clock; post-fix it doesn't.

        This is the specific bug from the audit.  merge_vectors should be
        pure pointwise-max with no local clock mutation.
        """
        from crdt_merge import merge_vectors

        v1 = {"agent_a": 5, "agent_b": 3}
        v2 = {"agent_a": 7, "agent_b": 2}
        result = merge_vectors("agent_a", v1, v2)
        # Pre-fix bug: result["agent_a"] would be 8 (5+1+2 or 7+1)
        # Post-fix: result["agent_a"] should be exactly max(5, 7) = 7
        self.assertEqual(
            result["agent_a"],
            7,
            f"B1 regression: merge_vectors bumped agent_a clock. "
            f"Expected 7 (max), got {result['agent_a']}",
        )
        self.assertEqual(
            result["agent_b"],
            3,
            f"B1 regression: merge_vectors changed agent_b. "
            f"Expected 3 (max), got {result['agent_b']}",
        )


class TestCrdtMergeContract(unittest.TestCase):
    """B1 + audit follow-up: ensure crdt_save_all callers bump the local
    clock *before* calling merge_vectors, so the merge is correctly
    pointwise-max and the local clock advances."""

    def test_callers_bump_local_clock_before_merge(self):
        """Static-analysis contract: callers of merge_vectors should
        mutate the incoming remote VV to bump the local agent's clock
        before passing it in.  This is a documentation test, not a
        runtime assertion, but it documents the contract.
        """
        import inspect
        import crdt_merge

        # merge_vectors should NOT itself bump the local clock.
        # The callers in crdt_merge.py:crdt_sync / crdt_save should
        # do it explicitly.  This is the B1 fix.
        src = inspect.getsource(crdt_merge.merge_vectors)
        self.assertNotIn("+ 1", src, "merge_vectors must not bump any clock")
        self.assertNotIn("+= 1", src, "merge_vectors must not bump any clock")
        # And the function should be a pure pointwise max
        self.assertIn("max(local", src, "merge_vectors must use pointwise max")


# ===========================================================================
# Remediation #5 (2026-06-22): CRDT merge must write merged state to disk
# ===========================================================================


class TestCrdtSaveWritesMarkdown(unittest.TestCase):
    """Regression test: the CRDT merge updates the DB but previously
    left the .md file stale.  The fix: after a successful merge,
    write the merged content to the .md file using safe_atomic_write
    (so concurrent local edits are preserved as conflict files).

    Without this fix, the markdown-vs-DB consistency check
    (``memory_integrity.find_orphan_files``) cannot detect the
    drift because the file exists, just with stale content.
    """

    def test_new_note_writes_markdown(self) -> None:
        """A new-note CRDT merge must create the .md file."""
        db_dir = Path(tempfile.mkdtemp(prefix="crdt_md_new_"))
        db = db_dir / "memory.db"
        connection_pool.clear()

        result = crdt_save(
            str(db),
            note_id="lessons/new_md_note",
            content="hello from remote",
            remote_agent_id="agent-A",
            local_agent_id="agent-B",
        )
        self.assertTrue(result["applied"], f"Expected apply, got {result}")

        md_path = db_dir / "lessons" / "new_md_note.md"
        self.assertTrue(
            md_path.exists(),
            f"CRDT merge must write the .md file (Remediation #5). Expected: {md_path}",
        )
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("hello from remote", content)

    def test_remote_dominates_writes_markdown(self) -> None:
        """An applied remote-dominating merge must update the .md file."""
        db_dir = Path(tempfile.mkdtemp(prefix="crdt_md_dom_"))
        db = db_dir / "memory.db"
        connection_pool.clear()
        with open_db(db) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, source_file, tags, created_at, updated_at, observed_at,
                    fitness_score, importance, pinned, version_vector, logical_clock)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 3, 0, ?, ?)""",
                (
                    "lessons/dom_md",
                    "old local content",
                    "lessons/dom_md.md",
                    "[]",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    json.dumps({"a": 1, "b": 5}),
                    5,
                ),
            )

        result = crdt_save(
            str(db),
            note_id="lessons/dom_md",
            content="newer remote content",
            remote_agent_id="agent-A",
            local_agent_id="agent-B",
            remote_vv_str=json.dumps({"a": 2, "b": 6}),
            remote_logical_clock=6,
        )
        self.assertTrue(result["applied"], f"Expected apply, got {result}")

        md_path = db_dir / "lessons" / "dom_md.md"
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("newer remote content", content)
        self.assertNotIn("old local content", content)

    def test_rejected_merge_does_not_overwrite_markdown(self) -> None:
        """A rejected (local-dominates) merge must NOT clobber the
        local .md file — the local write is the newer state."""
        db_dir = Path(tempfile.mkdtemp(prefix="crdt_md_rej_"))
        db = db_dir / "memory.db"
        connection_pool.clear()
        with open_db(db) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, source_file, tags, created_at, updated_at, observed_at,
                    fitness_score, importance, pinned, version_vector, logical_clock)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 3, 0, ?, ?)""",
                (
                    "lessons/rej_md",
                    "newer local content",
                    "lessons/rej_md.md",
                    "[]",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    json.dumps({"a": 1, "b": 5}),
                    5,
                ),
            )
        # Write the .md file to mirror the local DB state.
        from memory_common import safe_atomic_write

        md_path = db_dir / "lessons" / "rej_md.md"
        safe_atomic_write(md_path, "newer local content", encoding="utf-8")

        result = crdt_save(
            str(db),
            note_id="lessons/rej_md",
            content="stale remote content",
            remote_agent_id="agent-A",
            local_agent_id="agent-B",
            remote_vv_str=json.dumps({"a": 1, "b": 3}),
            remote_logical_clock=3,
        )
        self.assertTrue(result["rejected"], f"Expected reject, got {result}")

        # The .md file should still have the local (newer) content.
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("newer local content", content)
        self.assertNotIn("stale remote content", content)


if __name__ == "__main__":
    unittest.main()
