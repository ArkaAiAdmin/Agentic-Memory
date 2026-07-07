"""LLM-assisted contradiction resolution engine.

Resolves detected contradiction pairs into one of four actions:
  - supersede_b_with_a
  - supersede_a_with_b
  - merge
  - keep_both

LLM strategy selection is gated by ``MEMORY_CONTRADICTION_AUTO_RESOLVE_LLM=1``.
Otherwise the newer note wins deterministically.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from infra.db import AnyConnection


def auto_resolve_contradiction_pair(
    db_path: str | Path,
    note_a: str,
    note_b: str,
    conn: Optional[AnyConnection] = None,
) -> dict[str, Any]:
    """Resolve a single contradiction pair and apply the chosen action.

    Returns a result dict with ``action``, ``source``, ``target``,
    ``rationale``, and optionally ``merged_note_id``.
    """
    from infra.db import open_db

    now_iso = datetime.now(timezone.utc).isoformat()
    db_path_obj = Path(db_path)

    try:
        rows: dict[str, tuple] = {}
        if conn is not None:
            db = conn
            for nid in (note_a, note_b):
                row = db.execute(
                    "SELECT id, content, source_file, created_at, updated_at, metadata FROM memories WHERE id = ?",
                    (nid,),
                ).fetchone()
                if row:
                    rows[nid] = row
        else:
            with open_db(db_path_obj, timeout=30.0) as db:
                for nid in (note_a, note_b):
                    row = db.execute(
                        "SELECT id, content, source_file, created_at, updated_at, metadata FROM memories WHERE id = ?",
                        (nid,),
                    ).fetchone()
                    if row:
                        rows[nid] = row
    except Exception as e:
        return {"action": "error", "error": str(e), "source": note_a, "target": note_b}

    if note_a not in rows or note_b not in rows:
        return {"action": "error", "error": "note(s) not found", "source": note_a, "target": note_b}

    strategy = _pick_strategy(rows[note_a], rows[note_b])
    return _apply_resolution(db_path_obj, note_a, note_b, strategy, now_iso, conn=conn)


def _pick_strategy(row_a: tuple, row_b: tuple) -> str:
    """Choose a resolution strategy based on LLM or deterministic rule."""
    if os.environ.get("MEMORY_CONTRADICTION_AUTO_RESOLVE_LLM") != "1":
        ts_a = row_a[3] or row_a[4] or ""
        ts_b = row_b[3] or row_b[4] or ""
        return "supersede_b_with_a" if ts_a >= ts_b else "supersede_a_with_b"

    provider = _get_provider()
    if provider is None:
        ts_a = row_a[3] or row_a[4] or ""
        ts_b = row_b[3] or row_b[4] or ""
        return "supersede_b_with_a" if ts_a >= ts_b else "supersede_a_with_b"

    try:
        response = provider.generate(
            messages=[
                {"role": "system", "content": _LLM_PROMPT},
                {"role": "user", "content": f"Note A:\n{row_a[1]}\n\nNote B:\n{row_b[1]}"},
            ],
            max_tokens=200,
            temperature=0.0,
        )
        raw = response.get("content", "").strip()
        parsed = json.loads(raw)
        action: str = parsed.get("action", "")
        if action in ("supersede_b_with_a", "supersede_a_with_b", "merge", "keep_both"):
            return action
    except Exception:
        pass
    ts_a = row_a[3] or row_a[4] or ""
    ts_b = row_b[3] or row_b[4] or ""
    return "supersede_b_with_a" if ts_a >= ts_b else "supersede_a_with_b"


def _apply_resolution(
    db_path: Path,
    note_a: str,
    note_b: str,
    strategy: str,
    now_iso: str,
    conn: Optional[AnyConnection] = None,
) -> dict[str, Any]:
    """Execute the chosen resolution against the SQLite database."""
    from save.pipeline import memory_supersede_db, save_memory
    from infra.db import open_db

    rationale = f"auto_resolve ({strategy})"

    if strategy == "supersede_b_with_a":
        ok, err = memory_supersede_db(
            db_path, note_b, note_a, valid_to=now_iso, rationale=rationale, conn=conn,
        )
        if not ok:
            return {"action": "error", "error": err, "source": note_a, "target": note_b}
        return {"action": "superseded", "superseded": note_b, "by": note_a, "strategy": strategy}
    if strategy == "supersede_a_with_b":
        ok, err = memory_supersede_db(
            db_path, note_a, note_b, valid_to=now_iso, rationale=rationale, conn=conn,
        )
        if not ok:
            return {"action": "error", "error": err, "source": note_a, "target": note_b}
        return {"action": "superseded", "superseded": note_a, "by": note_b, "strategy": strategy}
    if strategy == "keep_both":
        return {"action": "kept_both", "source": note_a, "target": note_b, "strategy": strategy}
    if strategy == "merge":
        row_a, row_b = None, None
        if conn is not None:
            db = conn
            row_a = db.execute("SELECT * FROM memories WHERE id=?", (note_a,)).fetchone()
            row_b = db.execute("SELECT * FROM memories WHERE id=?", (note_b,)).fetchone()
        else:
            with open_db(db_path, timeout=30.0) as db:
                row_a = db.execute("SELECT * FROM memories WHERE id=?", (note_a,)).fetchone()
                row_b = db.execute("SELECT * FROM memories WHERE id=?", (note_b,)).fetchone()
        if not row_a or not row_b:
            return {"action": "error", "error": "notes not found for merge", "source": note_a, "target": note_b}
        merged_content = (
            f"# Merged: {note_a} + {note_b}\n\n"
            f"> Auto-merged on {datetime.now(timezone.utc).isoformat()}\n\n"
            f"{row_a[1]}\n\n---\n\n{row_b[1]}"
        )
        merged_id = f"merged/{note_a}__{note_b}"
        try:
            actual_merged_id = save_memory(
                content=merged_content,
                category="merged",
                title_slug=merged_id.split("/")[-1],
                tags=["merged", "auto-merge"],
                importance=3,
                defer_expensive=True,
                db_path=str(db_path),
                _conn=conn,
            )
            if isinstance(actual_merged_id, str) and (actual_merged_id.startswith("{") or "error" in actual_merged_id.lower()):
                return {"action": "error", "error": f"save merged note failed: {actual_merged_id}", "source": note_a, "target": note_b}
        except Exception as e:
            return {"action": "error", "error": f"save merged note failed: {e}", "source": note_a, "target": note_b}

        memory_supersede_db(db_path, note_a, merged_id, valid_to=now_iso, rationale="merged", conn=conn)
        memory_supersede_db(db_path, note_b, merged_id, valid_to=now_iso, rationale="merged", conn=conn)
        return {
            "action": "merged",
            "merged_note_id": merged_id,
            "superseded": [note_a, note_b],
            "by": merged_id,
            "strategy": strategy,
        }
    return {"action": "error", "error": f"unknown strategy {strategy!r}", "source": note_a, "target": note_b}


_LLM_PROMPT = (
    "Two memory notes show contradicting information. Choose ONE action:\n"
    "1. supersede_b_with_a — note B is outdated; note A supersedes it\n"
    "2. supersede_a_with_b — note A is outdated; note B supersedes it\n"
    "3. merge — both have valid complementary insights; consolidate\n"
    "4. keep_both — diverse concurrent perspectives, not a true contradiction\n\n"
    "Respond ONLY with JSON: {\"action\": \"<one_of_above>\", \"rationale\": \"<1 sentence>\"}"
)


def _get_provider() -> Any | None:
    """Return the configured LLM provider or None."""
    try:
        from fact.llm_providers import get_provider
        return get_provider()
    except ImportError:
        return None
