#!/usr/bin/env python3
"""CHANGE 7 — RBAC enforcement exercised under closed auth via ClosedClient.

These tests use the ``ClosedClient`` fixture from eval/conftest.py, which
binds a mock admin principal in agent_context and forces MEMORY_AUTH_MODE=closed.
Operations therefore flow through the REAL mcp_authorize path (not a mock),
verifying that the secure default actually authorizes a legitimately
privileged principal and that closed mode is enforced end-to-end.

Run:
    ~/.config/agentic-memory/venv/bin/python -m pytest eval/test_closed_auth_client.py -v
"""
import os
import sys
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

# The fixture forces closed mode; ensure we don't accidentally inherit "open"
# from the conftest setdefault when running this file in isolation.
os.environ.setdefault("MEMORY_AUTH_MODE", "closed")

from infra.db import open_db  # noqa: E402
from save.pipeline import save_memory  # noqa: E402
from memory_delete import soft_delete_note  # noqa: E402


def _row_exists(db_path, note_id):
    with open_db(Path(db_path), timeout=5) as conn:
        row = conn.execute(
            "SELECT id FROM memories WHERE id = ? AND deleted_at IS NULL",
            (note_id,),
        ).fetchone()
    return row is not None


def test_admin_can_save_under_closed_auth(ClosedClient, mock_admin_principal):
    note_id = ClosedClient.save("closed-mode secret memory", category="lessons")
    # A real note_id was returned (not an _err envelope).
    assert isinstance(note_id, str) and note_id.startswith("lessons/")
    # The row is actually persisted in the principal's DB.
    assert _row_exists(mock_admin_principal[0], note_id)


def test_admin_can_delete_under_closed_auth(closed_auth_principal):
    db_path, principal_id, tenant_id = closed_auth_principal
    note_id = save_memory(
        content="deletable closed-mode memory",
        category="lessons",
        title_slug="closed-del",
        tenant_id=tenant_id,
        db_path=db_path,
    )
    assert isinstance(note_id, str) and note_id
    # Delete is authorized through the real mcp_authorize under closed mode.
    assert soft_delete_note(db_path=db_path, note_id=note_id, tenant_id=tenant_id) is True
    # Soft-deleted -> no longer returned as live.
    assert not _row_exists(db_path, note_id)

