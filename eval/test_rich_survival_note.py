"""
Regression test: the pre-compaction survival note must capture rich content
from memory_save calls, not just command transcripts.

The 2026-06-16 hardening session found that the pre-compaction hook was
saving only filenames ("agentic-memory_memory_save (auto-XXX.md)") for
memory_save autosaves, throwing away the most valuable data — the
agent's actual conclusions/decisions/lessons saved during the session.

This test verifies the fix: when a memory_save auto-save is on disk,
pre_compaction() must include the actual content in the survival note,
not just the slug.

See: decisions/enrich-pre-compaction-survival-note
"""

import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_context_monitor(tmp_path, monkeypatch):
    """Redirect context_monitor to a temp SESSIONS_DIR and reset state."""
    from _fixtures import bootstrap_temp_db_clean

    db_path = tmp_path / "memory.db"
    bootstrap_temp_db_clean(db_path)

    monkeypatch.setenv("AGENTIC_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("GLOBAL_MEM_DIR", str(tmp_path))

    import context_monitor
    monkeypatch.setattr(context_monitor, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(context_monitor, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        context_monitor, "STATE_FILE", tmp_path / "sessions" / ".context_monitor_state.json"
    )
    monkeypatch.setattr(
        context_monitor, "_STATE_LOCK_PATH", tmp_path / "sessions" / ".context_monitor_state.json.flock"
    )

    from context_monitor import _save_state, _load_state

    # Force a fresh session so the autosave filter window includes our seeded file
    state = _load_state()
    state["session_start_time"] = time.time() - 60  # 1 min ago
    state["last_compaction_time"] = 0  # clear dedup window
    state["tool_call_count"] = 0
    _save_state(state)
    return tmp_path / "sessions"


def _seed_memory_save_autosave(
    sessions_dir: Path, content: str, category: str, slug: str
) -> Path:
    """Create a memory_save-shaped auto-save file the way auto_save.py would."""
    ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())
    today = time.strftime("%Y-%m-%d", time.gmtime())
    fname = f"auto-{today}_{ts[11:]}+00-00-agentic-memory_memory_save.md"
    path = sessions_dir / fname
    payload = json.dumps(
        {"content": content, "category": category, "title_slug": slug, "tags": ["test"]}
    )
    path.write_text(f"""---
created: 2026-06-16T01:00:00+00:00
---

# Auto-save: agentic-memory_memory_save

**Tool**: `agentic-memory_memory_save`

## Params
```json
{payload}
```

## Result (preview)
_no result preview_

---
""")
    return path


class TestPreCompactionSurvivalNoteRich:
    """The pre-compaction hook must preserve conclusions, not just commands."""

    def test_survival_note_includes_memory_save_content_not_just_slug(
        self, isolated_context_monitor
    ):
        """The 2026-06-16 bug: the survival note only showed
        'Saving <cat>/<slug>' — losing the actual conclusion. After
        the fix, the note must include the content body."""
        _seed_memory_save_autosave(
            isolated_context_monitor,
            content=(
                "# Decision: the agentic-memory FTS5 escape fix uses "
                "_escape_phrase(t) instead of f'\"{t}\"'. This prevents "
                'silent search failures when KG entities contain / or ".'
            ),
            category="decisions",
            slug="fts5-escape-fix",
        )

        from context_monitor import pre_compaction

        result = pre_compaction(session_id="", message_count=10)

        # Read the resulting survival note
        import sqlite3

        db_path = (
            Path(
                os.environ.get(
                    "AGENTIC_MEMORY_DIR",
                    Path.home() / ".config" / "agentic-memory" / "memory",
                )
            )
            / "memory.db"
        )
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT content FROM memories WHERE id = ?", (result["note_id"],)
        ).fetchone()
        assert row is not None, f"survival note {result['note_id']} not found"
        text = row["content"]

        # The fix: "Recent Conclusions" must contain the actual content
        assert "## Recent Conclusions" in text, "Recent Conclusions section missing"
        assert "_escape_phrase(t)" in text, (
            "actual decision content missing — survival note still has "
            "command-transcript-only behavior"
        )
        assert "fts5-escape-fix" in text, "title slug missing"

    def test_survival_note_has_read_first_warning(self, isolated_context_monitor):
        """The new 'read this first' header is the post-compaction agent's
        roadmap to the most valuable content."""
        from context_monitor import pre_compaction

        result = pre_compaction(session_id="", message_count=5)

        import sqlite3

        db_path = (
            Path(
                os.environ.get(
                    "AGENTIC_MEMORY_DIR",
                    Path.home() / ".config" / "agentic-memory" / "memory",
                )
            )
            / "memory.db"
        )
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT content FROM memories WHERE id = ?", (result["note_id"],)
        ).fetchone()
        assert "Read this note FIRST" in row["content"]

    def test_survival_note_promotes_decisions_to_key_insights(
        self, isolated_context_monitor
    ):
        """Decisions/lessons saved during the session must surface in
        the Key Insights section, ahead of plain session notes."""
        _seed_memory_save_autosave(
            isolated_context_monitor,
            content="The pre-compaction hook must capture content not just slugs",
            category="lessons",
            slug="hook-must-capture-content",
        )

        from context_monitor import pre_compaction

        result = pre_compaction(session_id="", message_count=5)

        import sqlite3

        db_path = (
            Path(
                os.environ.get(
                    "AGENTIC_MEMORY_DIR",
                    Path.home() / ".config" / "agentic-memory" / "memory",
                )
            )
            / "memory.db"
        )
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT content FROM memories WHERE id = ?", (result["note_id"],)
        ).fetchone()
        text = row["content"]
        # Key Insights section must exist and reference the lesson
        ki_start = text.index("## Key Insights")
        ki_end = text.index("## Active Todos")
        ki_section = text[ki_start:ki_end]
        assert "hook-must-capture-content" in ki_section
        assert "lessons" in ki_section

    def test_extract_autosave_summary_handles_mcp_namespaced_tool_name(self):
        """Pre-existing bug: the hook only matched tool=='memory_save' but
        opencode invokes it as 'agentic-memory_memory_save'. The fix
        matches both. This test pins that contract."""
        from context_monitor import _extract_autosave_summary

        content = """## Params
```json
{"content":"the actual conclusion body","category":"decisions","title_slug":"example"}
```
"""
        for tool_name in ("memory_save", "agentic-memory_memory_save"):
            summary = _extract_autosave_summary(content, tool_name)
            assert "the actual conclusion body" in summary, (
                f"tool={tool_name!r} did not capture content"
            )
            assert "example" in summary
            assert "decisions" in summary
