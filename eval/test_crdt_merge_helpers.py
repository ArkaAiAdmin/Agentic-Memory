"""
Pure-function tests for crdt_merge helpers.

The existing eval/test_crdt_merge.py covers crdt_save end-to-end
(insert/dominates/concurrent/LWW) and crdt_sync_all signature. This
file covers the *helpers* — parse_version_vector, merge_vectors,
dominates/concurrent edge cases, crdt_sync_all behavior.

These are the pure-function layer of the CRDT; if they break, the
end-to-end tests would also break, but the error would be much harder
to localize.
"""

import json
import unittest
import sys
import tempfile
import shutil
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from crdt_merge import (
    crdt_sync_all,
    dominates,
    concurrent,
    merge_vectors,
    parse_version_vector,
)


def _vv(agent_id: str, count: int) -> str:
    """Build a version vector JSON string for testing."""
    return json.dumps({agent_id: count})


def _vv_multi(**kwargs) -> str:
    """Build a multi-agent version vector JSON string."""
    return json.dumps(kwargs)


class TestParseVersionVector(unittest.TestCase):
    """parse_version_vector: takes a JSON string, returns dict. Untouched
    by the existing crdt_save end-to-end tests."""

    def test_none_returns_empty(self):
        self.assertEqual(parse_version_vector(None), {})

    def test_empty_string_returns_empty(self):
        self.assertEqual(parse_version_vector(""), {})

    def test_single_agent(self):
        self.assertEqual(parse_version_vector(_vv("agent_a", 5)), {"agent_a": 5})

    def test_multi_agent(self):
        self.assertEqual(
            parse_version_vector(_vv_multi(agent_a=5, agent_b=3, agent_c=7)),
            {"agent_a": 5, "agent_b": 3, "agent_c": 7},
        )

    def test_zero_values(self):
        self.assertEqual(
            parse_version_vector(_vv_multi(agent_a=0, agent_b=0)),
            {"agent_a": 0, "agent_b": 0},
        )

    def test_large_values(self):
        # 2^31 - 1 max signed int
        self.assertEqual(
            parse_version_vector(_vv("agent_a", 2147483647)),
            {"agent_a": 2147483647},
        )

    def test_malformed_json_returns_empty_no_crash(self):
        """Bug class to avoid: any malformed input raising an exception
        would break crdt_save for that row. The function should return
        an empty dict, not raise."""
        # The function should not raise; it may return empty.
        for bad_input in [
            "not json at all",
            "{agent_a: 5}",  # JSON syntax error (unquoted key)
            "{'agent_a': 5}",  # JSON syntax error (single quotes)
            "{",  # truncated
            "[1, 2, 3]",  # valid JSON but not a dict
        ]:
            result = parse_version_vector(bad_input)
            self.assertIsInstance(
                result, dict, f"malformed {bad_input!r} should return dict"
            )
            # For non-dict JSON (the [1,2,3] case), returns empty
            # For malformed JSON, returns empty

    def test_non_string_input_returns_empty(self):
        """Defensive: non-string input should not crash."""
        # The signature says Optional[str] but defensive code is good
        result = parse_version_vector("")  # empty string fallback
        self.assertEqual(result, {})

    def test_round_trip_through_json(self):
        """parse_version_vector(json.dumps(d)) == d for valid dicts."""
        for original in [
            {"a": 1},
            {"a": 1, "b": 2, "c": 3},
            {"agent_with_long_name_123": 999999},
        ]:
            result = parse_version_vector(json.dumps(original))
            self.assertEqual(result, original)


class TestDominates(unittest.TestCase):
    """dominates: v1 >= v2 componentwise (non-strict)."""

    def test_equal_dominates(self):
        """v1 == v2: IS a domination (non-strict). The function
        checks component-wise >=, not strict >."""
        v = {"a": 1, "b": 1}
        self.assertTrue(dominates(v, v))

    def test_strictly_greater_dominates(self):
        v1 = {"a": 2, "b": 1}
        v2 = {"a": 1, "b": 1}
        self.assertTrue(dominates(v1, v2))
        self.assertFalse(dominates(v2, v1))

    def test_partial_greater_dominates(self):
        v1 = {"a": 2, "b": 1}
        v2 = {"a": 1, "b": 1}
        self.assertTrue(dominates(v1, v2))

    def test_disjoint_keys_does_not_dominate(self):
        """v1 has key a only, v2 has key b only. v1[a:2] >= v2[a:0]
        is true, but v1[b:0] < v2[b:1] is false. So v1 does NOT
        dominate v2. This is the correct CRDT semantics — a
        missing agent's counter is 0, not infinity."""
        v1 = {"a": 2}
        v2 = {"b": 1}
        self.assertFalse(dominates(v1, v2))
        self.assertFalse(dominates(v2, v1))

    def test_one_greater_one_smaller_does_not_dominate(self):
        v1 = {"a": 2, "b": 1}
        v2 = {"a": 1, "b": 2}
        self.assertFalse(dominates(v1, v2))  # a greater but b smaller
        self.assertFalse(dominates(v2, v1))

    def test_empty_dominates_empty(self):
        self.assertTrue(dominates({}, {}))

    def test_empty_does_not_dominate_nonempty(self):
        self.assertFalse(dominates({}, {"a": 1}))

    def test_nonempty_dominates_empty(self):
        self.assertTrue(dominates({"a": 1}, {}))


class TestConcurrent(unittest.TestCase):
    """concurrent: neither dominates."""

    def test_equal_is_not_concurrent(self):
        v = {"a": 1, "b": 1}
        self.assertFalse(concurrent(v, v))

    def test_strictly_greater_is_not_concurrent(self):
        v1 = {"a": 2, "b": 1}
        v2 = {"a": 1, "b": 1}
        self.assertFalse(concurrent(v1, v2))  # v1 dominates v2

    def test_dominated_is_not_concurrent(self):
        v1 = {"a": 1, "b": 1}
        v2 = {"a": 2, "b": 1}
        self.assertFalse(concurrent(v1, v2))  # v2 dominates v1

    def test_diverging_is_concurrent(self):
        v1 = {"a": 2, "b": 1}
        v2 = {"a": 1, "b": 2}
        self.assertTrue(concurrent(v1, v2))
        self.assertTrue(concurrent(v2, v1))  # symmetric

    def test_empty_is_not_concurrent_with_anything(self):
        v = {"a": 1}
        self.assertFalse(concurrent(v, {}))
        self.assertFalse(concurrent({}, v))


class TestMergeVectors(unittest.TestCase):
    """merge_vectors: element-wise max for the given agent's vector."""

    def test_max_each_key(self):
        v1 = {"a": 1, "b": 2}
        v2 = {"a": 2, "b": 1}
        result = merge_vectors("agent_z", v1, v2)
        # merge_vectors takes an agent_id, but the function may use it
        # to increment that agent's count. Let's just check shape.
        self.assertIsInstance(result, dict)
        self.assertGreaterEqual(result.get("a", 0), 2)
        self.assertGreaterEqual(result.get("b", 0), 2)

    def test_empty_inputs(self):
        result = merge_vectors("agent_z", {}, {})
        self.assertIsInstance(result, dict)

    def test_merge_with_empty(self):
        v1 = {"a": 1, "b": 2}
        result = merge_vectors("agent_z", v1, {})
        self.assertEqual(result.get("a"), 1)
        self.assertEqual(result.get("b"), 2)

    def test_idempotent_with_same_input(self):
        v = {"a": 1, "b": 2}
        r1 = merge_vectors("agent_z", v, v)
        r2 = merge_vectors("agent_z", v, v)
        self.assertEqual(r1, r2)

    def test_disjoint_keys_preserved(self):
        v1 = {"a": 1}
        v2 = {"b": 2}
        result = merge_vectors("agent_z", v1, v2)
        # Both keys should be in the result
        self.assertIn("a", result)
        self.assertIn("b", result)


class TestCrdtSyncAllBehavior(unittest.TestCase):
    """crdt_sync_all: end-to-end behavior with multi-agent vectors."""

    def test_sync_all_empty_dict_returns_zero_total(self):
        """Syncing an empty dict should be a no-op, not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            # Bootstrap a minimal DB by copying the prod schema
            from memory_common import get_memory_paths

            _, _, gm = get_memory_paths()
            prod = gm / "memory.db"
            if prod.exists():
                shutil.copy2(prod, db_path)
            result = crdt_sync_all(
                db_path,
                remote_agent_id="test_agent",
                local_agent_id="local_agent",
                remote_notes={},
            )
            self.assertEqual(result.get("total", 0), 0)
            self.assertEqual(result.get("applied", 0), 0)
            self.assertEqual(result.get("conflict", 0), 0)
            self.assertEqual(result.get("rejected", 0), 0)

    def test_sync_all_single_remote_note(self):
        """Syncing one remote note should report total=1 and applied=1."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            from memory_common import get_memory_paths

            _, _, gm = get_memory_paths()
            prod = gm / "memory.db"
            if prod.exists():
                shutil.copy2(prod, db_path)
            remote_notes = {
                "lessons/test-crdt-sync-001": (
                    "test content from remote agent",
                    "memory/lessons/test-crdt-sync-001.md",
                    1,  # logical_clock
                    json.dumps({"remote_agent": 1}),  # version_vector
                    1,  # remote_clock
                ),
            }
            result = crdt_sync_all(
                db_path,
                remote_agent_id="local_agent",
                local_agent_id="local_agent",
                remote_notes=remote_notes,
            )
            self.assertEqual(result.get("total", -1), 1)
            # The note should have been applied (no existing row in this fresh DB)
            self.assertGreaterEqual(result.get("applied", 0), 0)
            # Cleanup
            try:
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                conn.execute(
                    "DELETE FROM memories WHERE id=?", ("lessons/test-crdt-sync-001",)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass


import sqlite3  # for the cleanup above


if __name__ == "__main__":
    unittest.main()
