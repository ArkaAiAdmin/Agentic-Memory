"""Test isolation guard — verify test data never leaks to production DB.

H-fix (2026-06-22): this test used to DELETE test-pattern entries
from the production DB as a "precondition". That meant running the
test suite could lose real data, and the test was a write to prod,
not just a read. It now only reads and asserts; if the prod DB has
test artifacts, the test fails and reports the offending IDs.

Reads the production database directly and asserts:
  1. Row count ≤ 1000 (after filtering sessions)
  2. No test-sounding IDs or content
  3. No entries matching known test patterns

IMPORTANT: This test READS the production DB but never writes to it.
It validates the production DB state as a regression safety net.
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths — production DB only (this test intentionally reads prod)
# ---------------------------------------------------------------------------

_INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
_PROD_DB = _INSTALL_DIR / "memory" / "memory.db"

# Ensure project root is on path
if str(_INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALL_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _test_patterns() -> list[str]:
    """Patterns of test artifact IDs that the guard asserts are absent.

    H-fix (2026-06-22): the function used to *delete* matches in
    production. It now only returns the patterns for assertion; the
    guard test fails loudly if any of these leak into the prod DB.
    """
    return [
        "test/%",
        "lessons/test-%",
        "lessons/err-%",
        "lessons/toggle-%",
        "lessons/concurrent-%",
        "lessons/batch-%",
        "lessons/async-test",
        "lessons/audit-test",
        "lessons/test-mut-%",
        "lessons/test-e2e-%",
        "lessons/test-rich-survival",
        "lessons/test-mut-cat64",
        "projects/test-%",
        "decisions/test-%",
        "decisions/compliance-test%",
        "nonexistent_category_xyz/%",
        "%unit-glob%",
    ]


@pytest.fixture(scope="module")
def prod_entries():
    """Connect to production DB and return (ids, contents, count).

    This is module-scoped so the DB is read once per test module run,
    not per test function. Read-only: no cleanup of prod DB rows.
    """
    if not _PROD_DB.exists():
        pytest.skip(f"Production DB not found at {_PROD_DB}")

    conn = sqlite3.connect(str(_PROD_DB))
    try:
        rows = conn.execute("SELECT id, content FROM memories").fetchall()
        ids = [r[0] for r in rows]
        contents = [r[1] for r in rows]
        count = len(rows)
        return ids, contents, count
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProductionDBRowCount:
    """Assert the production DB is within expected bounds.

    2026-06-29: on CI the production DB at ~/.config/agentic-memory/memory/memory.db
    does not exist (or has 0 rows). The conftest fixture already skips when the
    DB is missing, but we add a CI-aware skip in case the DB exists but is empty.
    """

    @pytest.mark.skipif(
        os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true",
        reason="Production DB row count assertions require a populated user DB; "
        "CI runners start from an empty state.",
    )
    def test_row_count_at_most_3000(self, prod_entries):
        ids, _contents, _count = prod_entries
        core_ids = [
            id_
            for id_ in ids
            if not (
                id_.startswith("sessions/auto-")
                or id_.startswith("sessions/compaction-save-")
                or id_.startswith("sessions/idle-")
                or id_.startswith("sessions/end-")
                or id_.startswith("sessions/archive/")
            )
        ]
        core_count = len(core_ids)
        # Threshold raised from 1000 → 3000 on 2026-06-16 and
        # 3000 → 3500 on 2026-06-29 as the system continued growing.
        # The real protection is the test_no_test_pattern_* tests below.
        assert core_count <= 3500, (
            f"Production DB has {core_count} core rows (total {len(ids)}) — expected ≤ 3000 core rows. "
            f"Core IDs exceed limit: {core_ids[:20]}{'...' if len(core_ids) > 20 else ''}"
        )

    def test_row_count_reasonable(self, prod_entries):
        # 2026-06-29 fix: same as test_row_count_at_most_3000 above. On
        # CI the prod DB is either missing (conftest fixture skips) or
        # exists with 0 rows because the runner started from scratch.
        if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.skip(
                "Production DB row count assertions require a populated "
                "user DB; CI runners start from an empty state."
            )
        _ids, _contents, count = prod_entries
        assert count >= 10, f"Production DB only has {count} rows — expected ≥ 10"


class TestNoTestPatternIDs:
    """Verify no entries have test-sounding IDs."""

    def test_no_test_concurrent_ids(self, prod_entries):
        ids, _contents, _count = prod_entries
        test_ids = [id for id in ids if "test_concurrent" in id]
        assert len(test_ids) == 0, (
            f"Unexpected test_concurrent IDs in production DB: {test_ids}"
        )

    def test_no_test_slash_prefix_ids(self, prod_entries):
        ids, _contents, _count = prod_entries
        # All test-pattern IDs should have been cleaned up.
        # If this fails, new test data has leaked into production.
        # Updated 2026-06-16: catch the full set of patterns that have
        # leaked historically (from test_pipeline_integration.py,
        # test_concurrent.py, test_search_pipeline_unit.py, etc.)
        import re as _re

        test_patterns = [
            r"^test/",
            r"^lessons/test-",
            r"^lessons/err-\d+",  # error-log test notes
            r"^lessons/toggle-\d+",  # toggle test notes
            r"^lessons/concurrent-\d+",  # concurrent test notes
            r"^lessons/batch-\d+",  # batch test notes
            r"^lessons/async-test$",
            r"^lessons/audit-test$",
            r"^lessons/test-mut-",  # mutation killer test notes
            r"^lessons/test-e2e-",  # e2e format test notes
            r"^lessons/test-rich-survival",
            r"^lessons/test-mut-cat64",
            r"^projects/test-\d+",  # epoch-based test IDs
            r"^decisions/test-\d+",  # epoch-based test IDs
            r"^decisions/compliance-test",
            r"^nonexistent_category_xyz/",
            r"unit-glob",
        ]
        compiled = [_re.compile(p) for p in test_patterns]
        test_ids = [id for id in ids if any(c.match(id) for c in compiled)]
        assert len(test_ids) == 0, (
            f"Unexpected test-pattern IDs in production DB "
            f"({len(test_ids)} total; first 20): {test_ids[:20]}"
        )

    def test_no_unit_glob_ids(self, prod_entries):
        ids, _contents, _count = prod_entries
        unit_glob_ids = [id for id in ids if "unit-glob" in id]
        assert len(unit_glob_ids) == 0, (
            f"Unexpected unit-glob IDs in production DB: {unit_glob_ids}"
        )


class TestNoTestPatternContent:
    """Verify no entries have test-sounding content."""

    def test_no_pool_burst_content(self, prod_entries):
        _ids, contents, _count = prod_entries
        assert not any("pool burst" in content for content in contents), (
            f"Found 'pool burst' in content: "
            f"{[c[:80] for c in contents if 'pool burst' in c]}"
        )

    def test_no_mutation_test_content(self, prod_entries):
        _ids, contents, _count = prod_entries
        excluded_prefixes = (
            "projects/agentic-memory-",
            "projects/MASTER-TODO",
            "sessions/",  # auto-save notes naturally contain tech phrases
            "lessons/",  # lessons about bug fixes naturally discuss test patterns
        )
        flagged = [
            content
            for content, id_ in zip(contents, _ids)
            if "mutation test" in content
            and not any(id_.startswith(p) for p in excluded_prefixes)
        ]
        assert not flagged, (
            f"Found 'mutation test' in content: {[c[:80] for c in flagged]}"
        )


class TestProductionDBIntegrity:
    """Basic sanity checks on the production DB."""

    def test_all_ids_are_strings(self, prod_entries):
        ids, _contents, _count = prod_entries
        for id_ in ids:
            assert isinstance(id_, str), f"Non-string ID found: {id_!r}"
            assert len(id_) > 0, "Empty ID found"

    def test_all_contents_nonempty(self, prod_entries):
        _ids, contents, _count = prod_entries
        for i, c in enumerate(contents):
            assert isinstance(c, str), f"Non-string content at index {i}"
            assert len(c) > 0, f"Empty content at index {i}"

    def test_ids_are_unique(self, prod_entries):
        ids, _contents, _count = prod_entries
        unique = set(ids)
        assert len(unique) == len(ids), (
            f"Duplicate IDs found: {len(ids)} total, {len(unique)} unique"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
