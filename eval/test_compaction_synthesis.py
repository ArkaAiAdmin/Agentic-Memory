#!/usr/bin/env python3
"""Tests for compaction synthesis fixes (T1-T6).

Covers:
- _synthesize_session_summary derives content from tool activity
- _build_work_items handles KG tools
- _get_recent_compaction has no age filter
- format_summary renders session_notes
"""

import json
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers to create a minimal fake context_monitor module so we can import
# the functions under test without depending on the full app stack.
# ---------------------------------------------------------------------------

_CM_DIR = Path(__file__).resolve().parent.parent / "context_monitor.py"
# We import the real module for integration tests; unit tests mock state.


def _make_state(tools_so_far: int = 5, notable: list | None = None) -> dict:
    return {
        "tool_call_count": tools_so_far,
        "total_tool_calls": tools_so_far,
        "tools_since_checkpoint": tools_so_far,
        "last_checkpoint_time": time.time(),
        "session_start_time": time.time() - 600,
        "notable_tools": notable or [],
        "last_compaction_time": 0.0,
    }


def _make_autosave(tool: str, preview: str = "some change") -> dict:
    return {
        "file": f"auto-2026-06-26_12-00-00+00-00-{tool}.md",
        "tool": tool,
        "content_preview": preview,
    }


# ---------------------------------------------------------------------------
# T2: _synthesize_session_summary
# ---------------------------------------------------------------------------


class TestSynthesizeSessionSummary:
    """Unit tests for _synthesize_session_summary via the real module."""

    def _call(self, autosaves, notable_tools, state):
        from context_monitor import _synthesize_session_summary

        return _synthesize_session_summary(autosaves, notable_tools, state)

    def test_empty_inputs_produce_placeholder_sections(self):
        result = self._call([], [], _make_state())
        assert "No conclusions derived" in result["conclusions"]
        assert "No insights derived" in result["insights"]
        assert "No todo list derived" in result["todos"]
        assert "No next steps derived" in result["next_steps"]

    def test_edit_autosave_appears_in_conclusions(self):
        autos = [_make_autosave("edit", "Edit AGENTS.md: bump version")]
        result = self._call(autos, [], _make_state())
        assert "AGENTS.md" in result["conclusions"]
        assert "bump version" in result["conclusions"]

    def test_memory_save_appears_in_conclusions(self):
        autos = [_make_autosave("memory_save", "Decision: use CRDT merge")]
        result = self._call(autos, [], _make_state())
        assert "CRDT merge" in result["conclusions"]

    def test_todowrite_appears_in_todos(self):
        notable = [
            {
                "tool": "todowrite",
                "time": time.time(),
                "preview": "Sprint 7: fix compaction",
            }
        ]
        result = self._call([], notable, _make_state(notable=notable))
        assert "Sprint 7" in result["todos"]

    def test_git_commit_in_bash_appears_in_insights(self):
        notable = [
            {
                "tool": "bash",
                "time": time.time(),
                "preview": "$ git commit -m 'fix: repair KG orphans'",
            }
        ]
        result = self._call([], notable, _make_state())
        assert "repair kg orphans" in result["insights"]

    def test_conclusions_deduped(self):
        autos = [
            _make_autosave("edit", "Edit foo.py"),
            _make_autosave("edit", "Edit foo.py"),
        ]
        result = self._call(autos, [], _make_state())
        lines = [
            line.strip("- ")
            for line in result["conclusions"].split("\n")
            if line.strip() and "derived" not in line
        ]
        assert len(lines) == 1

    def test_next_steps_from_memory_save(self):
        autos = [
            _make_autosave("memory_save", "Next step: write tests for compaction.")
        ]
        result = self._call(autos, [], _make_state())
        assert "write tests" in result["next_steps"]

    def test_next_steps_from_bash_preview(self):
        notable = [
            {
                "tool": "bash",
                "time": time.time(),
                "preview": "$ ./venv/bin/python -m pytest eval/  # TODO: add coverage gate",
            }
        ]
        result = self._call([], notable, _make_state())
        assert "TODO" in result["next_steps"] or "coverage gate" in result["next_steps"]

    def test_insights_keyword_filter(self):
        autos = [_make_autosave("memory_save", "Fixed migration ordering bug.")]
        result = self._call(autos, [], _make_state())
        assert "Fixed migration ordering bug" in result["insights"]

    def test_non_insight_memory_save_not_in_insights(self):
        autos = [_make_autosave("memory_save", "Remember to buy milk.")]
        result = self._call(autos, [], _make_state())
        assert "buy milk" not in result["insights"]

    def test_max_limits_enforced(self):
        autos = [_make_autosave("edit", f"Edit file_{i}.py") for i in range(20)]
        result = self._call(autos, [], _make_state())
        lines = [
            line
            for line in result["conclusions"].split("\n")
            if line.startswith("- ") and "derived" not in line
        ]
        assert len(lines) <= 6

    def test_agentic_memory_namespaced_tools_in_conclusions(self):
        autos = [_make_autosave("agentic-memory_memory_save", "Decided on v22 schema.")]
        result = self._call(autos, [], _make_state())
        assert "v22 schema" in result["conclusions"]


# ---------------------------------------------------------------------------
# T1/T3: notable tool tracking + _build_work_items KG branches
# ---------------------------------------------------------------------------


class TestNotableToolTracking:
    """Integration tests for track_tool_call notable set."""

    def _setup_tmp_state(self, tmp_path):
        """Patch the context_monitor module to use a temp sessions dir."""
        import context_monitor as cm

        cm.SESSIONS_DIR = tmp_path / "sessions"
        cm.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        cm.STATE_FILE = cm.SESSIONS_DIR / ".context_monitor_state.json"
        cm._STATE_LOCK_PATH = cm.SESSIONS_DIR / ".context_monitor_state.json.flock"

    def test_kg_tool_is_notable(self, tmp_path):
        self._setup_tmp_state(tmp_path)
        from context_monitor import track_tool_call

        track_tool_call("memory_graph_search", '{"query":"test"}', "result preview")
        state_file = tmp_path / "sessions" / ".context_monitor_state.json"
        state = json.loads(state_file.read_text())
        assert any(t["tool"] == "memory_graph_search" for t in state["notable_tools"])

    def test_namespaced_kg_tool_is_notable(self, tmp_path):
        self._setup_tmp_state(tmp_path)
        from context_monitor import track_tool_call

        track_tool_call(
            "agentic-memory_memory_create_entities", "{}", "created 3 entities"
        )
        state_file = tmp_path / "sessions" / ".context_monitor_state.json"
        state = json.loads(state_file.read_text())
        assert any(
            t["tool"] == "agentic-memory_memory_create_entities"
            for t in state["notable_tools"]
        )

    def test_temporal_query_is_notable(self, tmp_path):
        self._setup_tmp_state(tmp_path)
        from context_monitor import track_tool_call

        track_tool_call("memory_temporal_query", "{}", "found contradictions")
        state_file = tmp_path / "sessions" / ".context_monitor_state.json"
        state = json.loads(state_file.read_text())
        assert any(t["tool"] == "memory_temporal_query" for t in state["notable_tools"])

    def test_question_tool_is_notable(self, tmp_path):
        self._setup_tmp_state(tmp_path)
        from context_monitor import track_tool_call

        track_tool_call("question", '{"question":"defer or merge?"}', "options A/B")
        state_file = tmp_path / "sessions" / ".context_monitor_state.json"
        state = json.loads(state_file.read_text())
        assert any(t["tool"] == "question" for t in state["notable_tools"])


class TestBuildWorkItemsKG:
    """KG branches in _build_work_items."""

    def test_memory_graph_search_in_work_items(self):
        from context_monitor import _build_work_items

        notable = [
            {
                "tool": "memory_graph_search",
                "time": time.time(),
                "preview": "entity X related to Y",
            }
        ]
        state = _make_state(notable=notable)
        out = _build_work_items(state)
        assert "KG query" in out

    def test_memory_create_entities_in_work_items(self):
        from context_monitor import _build_work_items

        notable = [
            {
                "tool": "memory_create_entities",
                "time": time.time(),
                "preview": "created Person/Project entities",
            }
        ]
        state = _make_state(notable=notable)
        out = _build_work_items(state)
        assert "KG write" in out

    def test_memory_search_nodes_in_work_items(self):
        from context_monitor import _build_work_items

        notable = [
            {
                "tool": "memory_search_nodes",
                "time": time.time(),
                "preview": "found 5 nodes",
            }
        ]
        state = _make_state(notable=notable)
        out = _build_work_items(state)
        assert "KG search" in out


# ---------------------------------------------------------------------------
# T5: _get_recent_compaction no age filter
# ---------------------------------------------------------------------------


class TestGetRecentCompaction:
    """The compaction recovery must not filter by age."""

    def _mock_sessions_dir(self, tmp_path, monkeypatch):
        """Point memory_bootstrap at a temp sessions dir."""
        import memory_bootstrap as mb

        fake_dir = tmp_path / "sessions"
        fake_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(mb, "_get_sessions_dir", lambda: fake_dir)

    def test_returns_none_when_no_compaction_files(self, tmp_path, monkeypatch):
        self._mock_sessions_dir(tmp_path, monkeypatch)
        from memory_bootstrap import _get_recent_compaction

        assert _get_recent_compaction() is None

    def test_returns_content_of_old_compaction(self, tmp_path, monkeypatch):
        self._mock_sessions_dir(tmp_path, monkeypatch)
        from memory_bootstrap import _get_recent_compaction
        import memory_bootstrap as mb

        sessions = mb._get_sessions_dir()
        old = sessions / "compaction-save-2025-01-01_00-00-00.md"
        old.write_text("# old compaction content")
        result = _get_recent_compaction()
        assert result is not None
        assert "old compaction content" in result

    def test_returns_most_recent_when_multiple(self, tmp_path, monkeypatch):
        self._mock_sessions_dir(tmp_path, monkeypatch)
        from memory_bootstrap import _get_recent_compaction
        import memory_bootstrap as mb

        sessions = mb._get_sessions_dir()
        (sessions / "compaction-save-2025-01-01_00-00-00.md").write_text("# oldest")
        (sessions / "compaction-save-2026-06-26_12-00-00.md").write_text("# newest")
        result = _get_recent_compaction()
        assert result is not None and "newest" in result


# ---------------------------------------------------------------------------
# T6: format_summary includes session_notes
# ---------------------------------------------------------------------------


class TestFormatSummarySessionNotes:
    def test_session_notes_rendered_as_pinned_session_notes_section(self):
        from memory_bootstrap import format_summary

        summary = format_summary(
            pinned=[],
            high_importance=[],
            recent=[],
            stats={"total_notes": 0, "pinned": 0, "kg_entities": 0, "kg_facts": 0},
            preferences=None,
            sessions=None,
            session_notes=[
                {
                    "id": "sessions/summary-abc",
                    "content": "session summary text",
                    "category": "sessions",
                    "importance": 0.9,
                    "tags": "compaction",
                }
            ],
        )
        assert "Pinned Session Notes" in summary
        assert "abc" in summary

    def test_no_session_notes_section_omitted_when_empty(self):
        from memory_bootstrap import format_summary

        summary = format_summary(
            pinned=[],
            high_importance=[],
            recent=[],
            stats={"total_notes": 0, "pinned": 0, "kg_entities": 0, "kg_facts": 0},
            preferences=None,
            sessions=None,
            session_notes=[],
        )
        assert "Pinned Session Notes" not in summary

    def test_empty_message_when_nothing_present(self):
        from memory_bootstrap import format_summary

        summary = format_summary(
            pinned=[],
            high_importance=[],
            recent=[],
            stats={"total_notes": 0, "pinned": 0, "kg_entities": 0, "kg_facts": 0},
            preferences=None,
            sessions=None,
            session_notes=[],
        )
        assert "No preferences" in summary or "No pinned" in summary
