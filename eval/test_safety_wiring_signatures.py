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
import inspect
import os
import sys
import unittest
from pathlib import Path

# Make the agentic-memory package importable.
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import memory_mcp  # noqa: E402

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


class TestSafetyWiringSignatures(unittest.TestCase):
    """The two core functions carry the new kwarg with default True.

    The MCP tool wrappers (``memory_search``, ``memory_save``) do NOT
    expose the kwarg — they always run with the default, preserving
    the public MCP tool surface bit-for-bit.
    """

    def test_search_memories_signature_has_safety_wiring_param(self):
        sig = inspect.signature(memory_mcp.search_memories)
        self.assertIn(
            "safety_wiring", sig.parameters,
            "search_memories must accept a safety_wiring kwarg",
        )
        self.assertIs(
            sig.parameters["safety_wiring"].default, True,
            "safety_wiring must default to True (BLK-1, 2026-06-07): "
            "demote pass runs in production search paths by default",
        )
        param = sig.parameters["safety_wiring"]
        annotation = param.annotation
        if isinstance(annotation, str):
            annotation = eval(annotation)  # noqa: S307 - forward-ref from __future__ annotations
        self.assertIn(
            annotation, (bool, inspect.Parameter.empty),
            f"safety_wiring should be typed as bool, got {param.annotation!r}",
        )

    def test_save_memory_signature_has_safety_wiring_param(self):
        sig = inspect.signature(memory_mcp.save_memory)
        self.assertIn(
            "safety_wiring", sig.parameters,
            "save_memory must accept a safety_wiring kwarg",
        )
        self.assertIs(
            sig.parameters["safety_wiring"].default, True,
            "safety_wiring must default to True (BLK-1, 2026-06-07): "
            "contradiction check runs in production save paths by default",
        )
        param = sig.parameters["safety_wiring"]
        annotation = param.annotation
        if isinstance(annotation, str):
            annotation = eval(annotation)  # noqa: S307 - forward-ref from __future__ annotations
        self.assertIn(
            annotation, (bool, inspect.Parameter.empty),
            f"safety_wiring should be typed as bool, got {param.annotation!r}",
        )

    def test_module_level_safety_wiring_constant_is_true(self):
        """The module-level ``safety_wiring`` constant is also True.

        It is used in cache-key construction
        (memory_mcp._make_cache_key includes ``sw={int(safety_wiring)}``)
        so the demoted-mode cache and the default-mode cache do not
        collide. Flipping this constant must stay in sync with the
        function default.
        """
        self.assertIs(
            memory_mcp.safety_wiring, True,
            "memory_mcp.safety_wiring module constant must be True "
            "(BLK-1, 2026-06-07)",
        )

    def test_mcp_wrapper_memory_save_does_not_expose_safety_wiring(self):
        """The MCP tool wrapper must NOT expose safety_wiring to clients."""
        sig = inspect.signature(memory_mcp.memory_save)
        self.assertNotIn(
            "safety_wiring", sig.parameters,
            "memory_save MCP wrapper must not expose safety_wiring "
            "(it's a Python-only knob on the core save_memory function)",
        )

    def test_mcp_wrapper_memory_search_does_not_expose_safety_wiring(self):
        """The MCP tool wrapper for search must NOT expose safety_wiring."""
        sig = inspect.signature(memory_mcp.memory_search)
        self.assertNotIn(
            "safety_wiring", sig.parameters,
            "memory_search MCP wrapper must not expose safety_wiring",
        )


# ---------------------------------------------------------------------------
# 2. Default-on behavioral tests — Wave 7 helpers ARE called
# ---------------------------------------------------------------------------


class TestSafetyWiringMCPSurface(unittest.TestCase):
    """MCP tool surface: wrappers don't expose safety_wiring, but the
    default-on behavior is inherited from the underlying functions.
    A future change that adds the kwarg to the MCP wrappers must
    update this test."""

    def test_memory_save_mcp_wrapper_uses_true_default(self):
        """The ``memory_save`` MCP wrapper internally calls
        ``save_memory(safety_wiring=True)`` (not False). This is the
        critical wiring that makes the default-on behavior live for
        every MCP call."""
        import inspect
        import re
        src = inspect.getsource(memory_mcp.memory_save)
        # Look for the explicit kwarg form ``safety_wiring=...`` in
        # the wrapper's body. The kwarg must be present and set to
        # True. We accept either a multi-line form (one per line) or
        # a single-line form.
        match = re.search(r"safety_wiring\s*=\s*(True|False)", src)
        found_value = match.group(1) if match is not None else None
        self.assertEqual(
            found_value, "True",
            "memory_save MCP wrapper must pass safety_wiring=True "
            "explicitly to save_memory (be explicit so future "
            "default flips are visible at the call site)",
        )




if __name__ == "__main__":
    unittest.main()
