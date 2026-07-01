"""Per-signal indexer functions for the save pipeline.

Extracted from save_pipeline.py (2026-06-20) as part of the god-module
decomposition. Each function here is a thin try/except wrapper around
an indexer call for one signal type: chunks, embedding, KG, facts,
adaptive retention (with companion writes), backlinks.

Behavior is identical to the inline versions in the original
save_pipeline. Re-exported there for backward compat.
"""

from __future__ import annotations

import logging
import re

from search_pipeline import _qw5_index_chunks_for

logger = logging.getLogger(__name__)


def _index_backlinks(db, note_id: str, content: str):
    """Extract [[wiki-links]] from content and insert bidirectional backlinks.

    When A references [[B]], we insert:
      - source_id=A, target_id=B  (A links to B)
      - source_id=B, target_id=A  (B is referenced by A)

    The reverse link ensures that searching for B surfaces A as a
    related memory, even though B's content never mentions A.

    BLK-1 (2026-06-10): target_id now stores the full note_id
    (e.g. ``lessons/foo``) when the target memory exists.  This
    ensures ``hard_delete_note`` can cascade-delete backlinks
    correctly.  When the target does not yet exist, the slug is
    stored as a forward-reference that will be resolved on the
    target's first save.
    """
    links = re.findall("\\[\\[(.*?)\\]\\]", content)
    for link in links:
        target = link.split("|")[0].strip()
        target_slug = target.replace(".md", "").lower().replace("\\", "/")
        if (
            not target_slug
            or target_slug == note_id.lower()
            or target_slug == note_id.split("/", 1)[-1].lower()
        ):
            continue

        # Try to resolve the full note_id for the target
        try:
            row = db.execute(
                "SELECT id FROM memories WHERE id = ? OR id LIKE ?",
                (target_slug, f"%/{target_slug}"),
            ).fetchone()
            target_id = row[0] if row else target_slug
        except Exception:
            logger.warning(
                "Failed to resolve backlink target '%s' for note %s",
                target_slug,
                note_id,
            )
            target_id = target_slug

        # Forward link: note_id -> target_id
        db.execute(
            "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
            (note_id, target_id),
        )
        # Reverse link: target_id -> note_id (bidirectional)
        db.execute(
            "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
            (target_id, note_id),
        )


def _index_chunks(db, note_id: str, content: str):
    """Index content chunks for long notes (QW5)."""
    try:
        _qw5_index_chunks_for(db, note_id, content)
    except Exception as ce:
        logger.warning("QW5 chunk indexing failed for %s: %s", note_id, ce)


def _index_embedding(
    db, note_id: str, content: str, category: str, tags: list, source_file: str
):
    """Refresh the embedding cache for a single row.

    Produces the main model2vec / sentence-transformers embedding
    (full document, 256-dim) and stores it in the ``embedding`` BLOB
    column.  This is what search uses for cosine similarity.
    """
    try:
        from infra._lazy_imports import get_embedding_search

        get_embedding_search().index_embedding(
            db, note_id, content, category=category, tags=tags, source_file=source_file
        )
    except Exception as ee:
        logger.debug("Embedding cache write skipped for %s: %s", note_id, ee)


def _index_kg(db, note_id: str, content: str):
    """Index entities and relations into the knowledge graph."""
    try:
        from knowledge_graph import KG_ENABLED, ensure_kg_schema, index_kg_for_memory

        if KG_ENABLED:
            ensure_kg_schema(db)
            index_kg_for_memory(db, note_id, content)
    except Exception as ke:
        logger.debug("KG indexing skipped for %s: %s", note_id, ke)


def _index_facts(db, note_id: str, content: str):
    """Index SPO facts into kg_facts."""
    try:
        from knowledge_graph import KG_ENABLED

        if KG_ENABLED:
            from fact import ensure_facts_schema, index_facts_for_memory

            ensure_facts_schema(db)
            index_facts_for_memory(db, note_id, content)
    except Exception as fe:
        logger.warning("Fact indexing skipped for %s: %s", note_id, fe)


def _index_adaptive_retention(db, note_id: str, db_path: str | None = None):
    """Create adaptive retention schema and record a 'save' access event.

    Also performs the companion writes that are part of the same save
    transaction:
      1. user_profile_access_log: personalization side-channel.
         Mirrors the search-side call in search_pipeline. We inline
         the SQL rather than calling user_profile.record_access
         because that function opens its own pooled connection —
         which deadlocks against the parent saga's uncommitted
         write lock. (See C1–C10 audit saga fragility notes.)
      2. CTR click-proxy (P1.3): if this note_id had a `returned`
         CTR event in the last MEMORY_CTR_CLICK_WINDOW_HOURS hours,
         mark it `clicked_at=now`. Works because the user's
         recall-and-save loop is tight.
    """
    try:
        from adaptive_retention import ensure_adaptive_schema, record_access

        ensure_adaptive_schema(db)
        record_access(db, note_id, source="save")
    except Exception as ar:
        logger.debug("Adaptive retention indexing skipped for %s: %s", note_id, ar)
    # Companion write to user_profile_access_log (different table from
    # adaptive_retention's user_access_log) so personalization has data.
    # Mirrors the search-side call added in search_pipeline.
    # Done on the parent connection (same as adaptive_retention above)
    # so this stays inside the Saga transaction. The original
    # user_profile.record_access opens its own pooled connection, but
    # that connection deadlocks against the parent's uncommitted write
    # lock. Inlining the SQL here keeps everything in one transaction.
    try:
        import time as _time
        import json as _json

        db.execute(
            "INSERT INTO user_profile_access_log "
            "(note_id, source, category, tags, accessed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (note_id, "save", None, _json.dumps([]), _time.time()),
        )
    except Exception as _up_e:
        logger.debug("user_profile_access_log on save failed: %s", _up_e)
    # P1.3 click proxy: if this note_id had a `returned` CTR event in
    # the last 4 hours, mark it `clicked_at=now`. The proxy works
    # because the user's recall-and-save loop is tight (they save
    # shortly after reading the recalled note). 4h is configurable via
    # MEMORY_CTR_CLICK_WINDOW_HOURS for users with different cadences.
    try:
        import os as _os
        import time as _time

        _click_window_s = (
            float(_os.environ.get("MEMORY_CTR_CLICK_WINDOW_HOURS", "4")) * 3600.0
        )
        _cutoff = _time.time() - _click_window_s
        # Mark the most recent unclicked returned event as clicked.
        # If multiple query_ids surfaced the same note, we click them
        # all (they all count as a click).
        _rows = db.execute(
            "SELECT query_id, returned_at FROM memory_ctr_feedback "
            "WHERE id = ? AND returned_at > ? AND clicked_at IS NULL",
            (note_id, _cutoff),
        ).fetchall()
        for _qid, _rat in _rows:
            db.execute(
                "UPDATE memory_ctr_feedback SET clicked_at = ? "
                "WHERE id = ? AND query_id = ?",
                (_time.time(), note_id, _qid),
            )
    except Exception as _ctr_click_e:
        logger.debug("CTR click-proxy failed: %s", _ctr_click_e)
