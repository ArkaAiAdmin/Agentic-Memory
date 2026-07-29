"""Shared utilities for CRDT field and note-level merge."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)


def _write_note_markdown_file(
    db_path: str | Path | None,
    note_id: str,
    content: str,
    conn: AnyConnection,
) -> None:
    """Write merged CRDT content to the note's markdown file.

    This is the shared .md write path used by both field-level CRDT
    (``crdt_field._finalize_crdt_save``) and note-level CRDT
    (``crdt_merge._write_merged_markdown``).

    Best-effort: if the write fails (e.g. disk full, permission error),
    we log and return — the DB write is the source of truth and the .md
    can be regenerated later via ``recover_orphan_files`` or the next save.
    """
    if db_path is None:
        logger.debug(
            "crdt: no db_path provided; skipping .md write for %s",
            note_id,
        )
        return
    try:
        row = conn.execute(
            "SELECT source_file FROM tenant_memories WHERE id=?",
            (note_id,),
        ).fetchone()
        if not row or not row[0]:
            logger.debug(
                "crdt: note %s has no source_file; skipping .md write",
                note_id,
            )
            return
        source_file = row[0]
        memory_root = Path(db_path).parent
        if not source_file.endswith(".md"):
            source_file = source_file + ".md"
        md_path = memory_root / source_file
        body: str = content
        try:
            from save_pipeline import _build_memory_file

            category_str = note_id.split("/", 1)[0] if "/" in note_id else "imported"
            slug = note_id.split("/", 1)[-1]
            markdown, _fm, _now, _md = _build_memory_file(
                content,
                category_str,
                slug,
                tags_list=[],
                pinned=False,
            )
            body = markdown
        except Exception as build_exc:
            logger.debug(
                "crdt: _build_memory_file failed (%s); falling back to raw content",
                build_exc,
            )
            body = content
        from infra.memory_common import safe_atomic_write

        try:
            safe_atomic_write(md_path, body, encoding="utf-8")
            logger.info("crdt: wrote merged content to %s", md_path)
        except Exception as write_exc:
            logger.warning(
                "crdt: failed to write merged .md %s: %s. "
                "Run --recover-orphan-files to regenerate.",
                md_path,
                write_exc,
            )
    except Exception as outer_exc:
        logger.warning(
            "crdt: _write_note_markdown_file failed for %s: %s",
            note_id,
            outer_exc,
        )
