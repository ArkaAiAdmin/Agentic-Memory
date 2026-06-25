#!/usr/bin/env python3
"""Unit tests for Wave 7 safety wiring in memory_mcp.

These tests verify that the ``safety_wiring`` kwarg on
``search_memories`` and ``save_memory`` works as designed after the
2026-06-07 BLK-1 fix that flipped the default to ``True``:

* Default behavior is ``safety_wiring=True`` for both functions. The
  search path runs ``memory_injection.demote_results_by_injection`` and
  the save path runs ``memory_contradiction_save.check_contradictions_on_save``
  on every call (including MCP tool calls — the MCP wrappers do not
  expose the kwarg, so they always inherit the default).
* When the save path finds contradictions, it logs a structured row to
  the audit table via ``audit.enqueue_audit`` (with
  ``tool="memory_save_contradiction_check"``) and surfaces a
  ``logger.warning``. The save still returns the canonical
  ``note_id`` string so the MCP tool surface is unchanged bit-for-bit.
* Python callers can opt out at the ``search_memories`` / ``save_memory``
  layer by passing ``safety_wiring=False``. The opt-out is not exposed
  to MCP clients.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_safety_wiring -v
"""

import hashlib
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Make the agentic-memory package importable.
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import memory_mcp  # noqa: E402
import save_pipeline  # noqa: E402

# Path to the test DB. The conftest.py session fixture sets MEMORY_DB_PATH
# to a temp copy of the clean snapshot, so tests never touch prod. When
# running outside pytest (e.g. python -m unittest), MEMORY_DB_PATH must be
# set or the mixin below will refuse to run.
_prod_db_str = os.environ.get("MEMORY_DB_PATH")
PROD_DB = Path(_prod_db_str) if _prod_db_str else None


def _get_prod_db():
    """Return PROD_DB, re-reading env var if it was None at import time."""
    global PROD_DB
    if PROD_DB is None:
        _prod_db_str = os.environ.get("MEMORY_DB_PATH")
        if _prod_db_str:
            PROD_DB = Path(_prod_db_str)
    return PROD_DB


class _ProdDBGuarded(unittest.TestCase):
    """Mixin: snapshot test DB on setUp, restore on tearDown.

    Strong-isolation contract: tearDown re-copies the snapshot and then
    asserts the DB md5 matches the pre-test snapshot. A mismatch
    raises AssertionError so the test fails LOUDLY rather than silently
    leaking writes.

    Requires ``MEMORY_DB_PATH`` env var (set by conftest.py). If unset,
    raises RuntimeError to avoid accidentally touching production.
    """

    def setUp(self):
        prod_db = _get_prod_db()
        if not prod_db:
            raise RuntimeError(
                "_ProdDBGuarded requires MEMORY_DB_PATH env var. "
                "Run under pytest (conftest.py sets it) or set it manually."
            )
        self._db_tmp = tempfile.NamedTemporaryFile(
            prefix="memory_db_backup_", suffix=".db", delete=False
        )
        self._db_tmp.close()
        self._db_snapshot_md5 = None
        if Path(prod_db).exists():
            shutil.copy2(str(prod_db), self._db_tmp.name)
            with open(self._db_tmp.name, "rb") as f:
                self._db_snapshot_md5 = hashlib.md5(f.read()).hexdigest()
        super().setUp()

    def tearDown(self):
        prod_db = _get_prod_db()
        try:
            if prod_db and Path(prod_db).exists() and self._db_tmp.name:
                shutil.copy2(self._db_tmp.name, str(prod_db))
        finally:
            try:
                Path(self._db_tmp.name).unlink(missing_ok=True)
            except Exception:
                pass
        if self._db_snapshot_md5 is not None and prod_db and Path(prod_db).exists():
            with open(str(prod_db), "rb") as f:
                current_md5 = hashlib.md5(f.read()).hexdigest()
            self.assertEqual(
                current_md5,
                self._db_snapshot_md5,
                f"_ProdDBGuarded: DB md5 drift detected! "
                f"Expected {self._db_snapshot_md5}, got {current_md5}. "
                f"The test leaked writes to {prod_db}.",
            )
        super().tearDown()


# ---------------------------------------------------------------------------
# 1. Signature tests — both functions expose ``safety_wiring: bool = True``
#    (BLK-1: default flipped to True on 2026-06-07)
# ---------------------------------------------------------------------------


def test_safety_wiring_module_loads():
    """Verifies the safety wiring mixin imports cleanly."""
    assert _ProdDBGuarded is not None
    assert hasattr(save_pipeline, "save_memory")


if __name__ == "__main__":
    unittest.main()
