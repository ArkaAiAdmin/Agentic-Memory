"""Behavior tests for the 12-verb surface in mcp_verbs.py.

Phase A (2026-07-01): Tests verify each verb exists, is registered on
the MCP surface, delegates to the correct function under normal
conditions, and returns a well-formed error when the delegate fails.
"""

import sys
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

import pytest

# ── Import all verbs once ──────────────────────────────────────────────

from mcp_verbs import (
    memory_search,
    memory_save,
    memory_delete,
    memory_recall,
    memory_note,
    memory_learn,
    memory_audit,
    memory_organize,
    memory_share,
    memory_graph,
    memory_profile,
    memory_session_start,
    memory_advanced,
)


class TestVerbImport:
    """Each verb is importable and callable."""

    def test_all_verbs_importable(self):
        for fn in [
            memory_search, memory_save, memory_delete,
            memory_recall, memory_note, memory_learn,
            memory_audit, memory_organize, memory_share,
            memory_graph, memory_profile,
            memory_session_start, memory_advanced,
        ]:
            assert callable(fn)


class TestVerbRegistration:
    """Each verb is registered on the MCP instance."""

    def test_all_verbs_registered_as_tools(self):
        from mcp_instance import mcp
        import mcp_verbs  # noqa: F401 — trigger registration

        import anyio
        tools = anyio.run(mcp.list_tools)
        registered = {t.name for t in tools}
        expected = {
            "memory_search", "memory_save", "memory_delete",
            "memory_recall", "memory_note", "memory_learn",
            "memory_audit", "memory_organize", "memory_share",
            "memory_graph", "memory_profile", "memory_session_start",
            "memory_advanced",
        }
        missing = expected - registered
        assert not missing, f"Verbs not found on MCP surface: {missing}"

    def test_no_duplicate_registrations(self):
        from mcp_instance import mcp

        import anyio
        tools = anyio.run(mcp.list_tools)
        names = [t.name for t in tools]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"Duplicate tool registrations: {dupes}"


class TestVerbCoreToolsMatch:
    """CORE_TOOLS in tool_registry matches the verb surface."""

    def test_core_tools_contains_all_verbs(self):
        from tool_registry import CORE_TOOLS

        for verb in [
            "memory_search", "memory_save", "memory_delete",
            "memory_recall", "memory_note", "memory_learn",
            "memory_audit", "memory_organize", "memory_share",
            "memory_graph", "memory_profile", "memory_session_start",
            "memory_advanced",
        ]:
            assert verb in CORE_TOOLS, f"{verb} missing from CORE_TOOLS"


class TestVerbSearchBehavior:
    """memory_search: returns ranked results for a query."""

    def test_search_returns_results_blob(self):
        with mock.patch("search.orchestrator.search_memories") as ms:
            ms.return_value = {"results_blob": "ranked results"}
            result = memory_search(query="test")
            assert "ranked results" in result


class TestVerbSaveBehavior:
    """memory_save: persists content and returns success."""

    def test_save_returns_success(self):
        with mock.patch("save_pipeline.save_memory") as ms:
            ms.return_value = "Successfully saved memory: memory/lessons/test.md"
            result = memory_save(content="x", category="lessons")
            assert "Successfully saved" in result


class TestVerbDeleteBehavior:
    """memory_delete: removes a note and returns confirmation."""

    def test_delete_returns_confirmation(self):
        with mock.patch("mcp_memory.memory_delete") as md:
            md.return_value = "Deleted: lessons/test-note"
            result = memory_delete(note_id="lessons/test-note")
            assert "Deleted" in result
            md.assert_called_once_with(note_id="lessons/test-note", hard=False)

    def test_delete_hard(self):
        with mock.patch("mcp_memory.memory_delete") as md:
            md.return_value = "Purged: lessons/test-note"
            result = memory_delete(note_id="lessons/test-note", hard=True, confirm=True)
            assert "Purged" in result
            md.assert_called_once_with(note_id="lessons/test-note", hard=True)


class TestVerbRecallBehavior:
    """memory_recall: returns session/thread context."""

    def test_recall_returns_context(self):
        with mock.patch("search.orchestrator.search_memories") as ms:
            ms.return_value = {"results_blob": "recall results"}
            result = memory_recall(query="what happened")
            assert "recall results" in result


class TestVerbNoteBehavior:
    """memory_note: CRUD on a specific note."""

    def test_note_read(self):
        with mock.patch("search.orchestrator.search_memories") as mr:
            mr.return_value = {"results_blob": "note content"}
            result = memory_note(note_id="lessons/my-note", action="read")
            assert "note content" in result

    def test_note_delete(self):
        with mock.patch("mcp_memory.memory_delete") as md:
            md.return_value = "Deleted"
            result = memory_note(note_id="lessons/my-note", action="delete")
            assert "Deleted" in result

    def test_note_update(self):
        with mock.patch("save_pipeline.save_memory") as ms:
            ms.return_value = "Updated"
            result = memory_note(
                note_id="lessons/my-note", action="update", content="new"
            )
            assert "Updated" in result

    def test_note_unknown_action_returns_error(self):
        result = memory_note(note_id="x", action="invalid")
        assert "Error" in result


class TestVerbLearnBehavior:
    """memory_learn: saves a lesson, optionally as a skill."""

    def test_learn_saves_lesson(self):
        with mock.patch("save_pipeline.save_memory") as ms:
            ms.return_value = "saved"
            result = memory_learn(content="lesson")
            assert "saved" in result

    def test_learn_with_skill(self):
        with (
            mock.patch("save_pipeline.save_memory") as ms,
            mock.patch("mcp_maintenance.memory_compile_skill") as mcs,
        ):
            ms.return_value = "saved"
            mcs.return_value = "compiled"
            result = memory_learn(content="x", as_skill=True, skill_name="my-skill")
            assert "Saved + compiled skill" in result


class TestVerbAuditBehavior:
    """memory_audit: reports recent activity and system health."""

    def test_audit_returns_activity(self):
        with (
            mock.patch("mcp_audit.memory_audit_query") as maq,
            mock.patch("mcp_audit.memory_circuit_breaker_status") as mcb,
        ):
            maq.return_value = "audit"
            mcb.return_value = "cb ok"
            result = memory_audit(hours=24, limit=10)
            assert "Recent Activity" in result
            assert "Circuit Breaker" in result


class TestVerbOrganizeBehavior:
    """memory_organize: runs maintenance batch operations."""

    def test_organize_safe_default(self):
        with (
            mock.patch("mcp_rebuild.memory_compact") as mc,
            mock.patch("mcp_maintenance.memory_consolidate") as mcs,
            mock.patch("mcp_maintenance.memory_rewrite_links") as mrl,
            mock.patch("mcp_rebuild.memory_backfill_all") as mba,
            mock.patch("mcp_memory.memory_purge_expired") as mpe,
        ):
            mc.return_value = "compact ok"
            mcs.return_value = "consolidate ok"
            mrl.return_value = "rewrite ok"
            mba.return_value = "backfill ok"
            mpe.return_value = "purge ok"
            result = memory_organize(target="safe_default")
            assert "compact" in result
            assert "consolidate" in result
            assert "rewrite_links" in result

    def test_organize_target_compact(self):
        with mock.patch("mcp_rebuild.memory_compact") as mc:
            mc.return_value = "compact ok"
            result = memory_organize(target="compact")
            assert "compact ok" in result

    def test_organize_unknown_target_returns_error(self):
        result = memory_organize(target="invalid")
        assert "Error" in result


class TestVerbShareBehavior:
    """memory_share: shares notes with other agents."""

    def test_share_list(self):
        with mock.patch("mcp_sharing.memory_shared_list") as msl:
            msl.return_value = "shared items"
            result = memory_share(note_id="", action="list")
            assert "shared items" in result

    def test_share_unknown_action_returns_error(self):
        result = memory_share(note_id="", action="invalid")
        assert "Error" in result


class TestVerbGraphBehavior:
    """memory_graph: explores the knowledge graph."""

    def test_graph_stats(self):
        with (
            mock.patch("mcp_kg.memory_facts_list") as mfl,
            mock.patch("mcp_kg.memory_graph_stats") as mgs,
        ):
            mfl.return_value = "facts"
            mgs.return_value = "stats"
            result = memory_graph(action="explore")
            assert "KG Facts" in result
            assert "Stats" in result

    def test_graph_unknown_action_returns_error(self):
        result = memory_graph(action="invalid")
        assert "Error" in result


class TestVerbProfileBehavior:
    """memory_profile: shows user/agent/system profile."""

    def test_profile_stats(self):
        with mock.patch("mcp_profile.memory_profile_stats") as mps:
            mps.return_value = "profile stats"
            result = memory_profile(action="stats")
            assert "profile stats" in result

    def test_profile_unknown_action_returns_error(self):
        result = memory_profile(action="invalid")
        assert "Error" in result


class TestVerbSessionStartBehavior:
    """memory_session_start: returns session briefing."""

    def test_session_start_returns_briefing(self):
        with mock.patch("mcp_search.memory_session_start") as mss:
            mss.return_value = "Session briefing data"
            result = memory_session_start(query="test")
            assert "Session" in result
            mss.assert_called_once_with(query="test")


class TestVerbAdvancedBehavior:
    """memory_advanced: pass-through to memory_maintenance."""

    def test_advanced_delegates_to_maintenance(self):
        with mock.patch("mcp_maintenance.memory_maintenance") as mm:
            mm.return_value = "operation done"
            result = memory_advanced(operation="heartbeat", dry_run=True)
            assert "operation done" in result
            mm.assert_called_once_with(operation="heartbeat", dry_run=True)


class TestVerbErrorHandling:
    """All verbs return error strings on failure, not exceptions."""

    @pytest.mark.parametrize("verb_fn,args", [
        (memory_search, {"query": "x"}),
        (memory_save, {"content": "x", "category": "lessons"}),
        (memory_delete, {"note_id": "x"}),
        (memory_recall, {"query": "x"}),
        (memory_note, {"note_id": "x", "action": "read"}),
        (memory_learn, {"content": "x"}),
        (memory_audit, {}),
        (memory_organize, {"target": "invalid"}),
    ])
    def test_error_returns_string_not_exception(self, verb_fn, args):
        result = verb_fn(**args)
        assert isinstance(result, str)
