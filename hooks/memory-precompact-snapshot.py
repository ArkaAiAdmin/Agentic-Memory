#!/usr/bin/env python3
"""PreCompact lifecycle hook: snapshot raw session state before compaction.

Reads session_id from stdin JSON, copies .opencode/sessions/<id>/events.jsonl
to memory/sessions/compaction-save-<ts>/, writes a task.md with the most recent
user prompt, and logs the event to session_compaction_log.

Reliability rules:
  - Never raises (top-level except BaseException → sys.exit(0))
  - No LLM calls
  - No DB writes outside session_compaction_log
  - No shutil.rmtree
  - Idempotent output dir naming (-1, -2, ... suffix)
"""

import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _log_error import log_error


def _find_events_source(session_id: str, root: Path) -> Path | None:
    """Resolve .opencode/sessions/<session_id>/events.jsonl relative to root."""
    candidate = root / ".opencode" / "sessions" / session_id / "events.jsonl"
    return candidate if candidate.exists() else None


def _resolve_out_dir(sessions_dir: Path, ts: str) -> Path:
    """Pick an idempotent output directory, suffixing -1, -2, etc. if needed."""
    base = sessions_dir / f"compaction-save-{ts}"
    if not base.exists():
        return base
    for i in range(1, 1000):
        candidate = sessions_dir / f"compaction-save-{ts}-{i}"
        if not candidate.exists():
            return candidate
    return base


def _extract_user_prompt(events_path: Path) -> str:
    """Scan the last 50 lines of events.jsonl for the most recent user prompt.

    Checks, in order:
      1. event_type == "user_prompt" or "user.message"
      2. message.updated with info.role == "user"

    Returns the extracted text or an empty string.
    """
    if not events_path.exists():
        return ""

    try:
        lines = events_path.read_text(encoding="utf-8").rstrip("\n").split("\n")
    except OSError:
        return ""

    tail = lines[-50:] if len(lines) > 50 else lines

    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        et = event.get("event_type", "")

        # Check 1: direct user_prompt / user.message event types
        if et in ("user_prompt", "user.message"):
            text = event.get("text") or event.get("content") or ""
            if isinstance(text, str) and text.strip():
                return text.strip()[:500]

        # Check 2: message.updated with info.role == "user"
        if et == "message.updated":
            info = event.get("info")
            if isinstance(info, dict) and info.get("role") == "user":
                text = info.get("text") or info.get("content") or ""
                if isinstance(text, str) and text.strip():
                    return text.strip()[:500]

                summary = info.get("summary")
                if isinstance(summary, dict):
                    text = summary.get("text") or ""
                    if isinstance(text, str) and text.strip():
                        return text.strip()[:500]

                    diffs = summary.get("diffs")
                    if isinstance(diffs, dict):
                        for part in diffs.values():
                            if isinstance(part, dict):
                                patch = part.get("patch", "")
                                if patch.strip():
                                    return str(patch.strip()[:500])

    return ""


def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    try:
        raw = sys.stdin.read()
        hook_data = {}
        if raw.strip():
            try:
                hook_data = json.loads(raw)
            except json.JSONDecodeError:
                pass
    except OSError:
        hook_data = {}

    session_id = hook_data.get("session_id", "")

    if not session_id:
        print(json.dumps({"ok": False, "error": "no session_id in stdin"}))
        return

    from infra.memory_common import get_memory_paths

    project_root, local_mem, _global_mem = get_memory_paths()

    # Locate the events source
    events_source = _find_events_source(session_id, project_root)

    # Output dir under memory/sessions/
    sessions_dir = local_mem / "sessions"
    out_dir = _resolve_out_dir(sessions_dir, ts)
    out_dir.mkdir(parents=True, exist_ok=True)


    # Copy events.jsonl if it exists
    if events_source is not None:
        dest = out_dir / "events.jsonl"
        shutil.copy2(str(events_source), str(dest))
        try:
            len(
                events_source.read_text(encoding="utf-8").rstrip("\n").split("\n")
            )
        except OSError:
            pass

    # Extract user prompt and write task.md
    task_snippet = ""
    if events_source is not None:
        task_snippet = _extract_user_prompt(events_source)

    if task_snippet:
        task_md = out_dir / "task.md"
        task_md.write_text(f"# Task\n\n{task_snippet}\n", encoding="utf-8")

    # Insert row into session_compaction_log
    try:
        db_path = local_mem / "memory.db"
        if db_path.exists():
            from infra.db import open_db

            log_id = str(uuid.uuid4())
            compacted_at = datetime.now(timezone.utc).isoformat()
            recovered = "[]"
            metadata = json.dumps(
                {"source": "precompact_snapshot", "out_dir": str(out_dir)}
            )
            version_vector = "{}"

            with open_db(db_path, write=True) as conn:
                conn.execute(
                    """INSERT INTO session_compaction_log
                       (id, session_id, compacted_at, tokens_before, tokens_after,
                        summary_note_id, recovered_note_ids, metadata, version_vector)
                       VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?)""",
                    (
                        log_id,
                        session_id,
                        compacted_at,
                        recovered,
                        metadata,
                        version_vector,
                    ),
                )
                conn.commit()
    except Exception as e:
        log_error(e, context="memory-precompact-snapshot.db_insert")

    print(json.dumps({"ok": True, "out_dir": str(out_dir)}))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _hook_e:
        try:
            log_error(_hook_e, context="memory-precompact-snapshot.top_level")
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": str(_hook_e)}))
        sys.exit(0)
