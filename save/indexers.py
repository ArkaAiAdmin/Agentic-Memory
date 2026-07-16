"""Per-signal indexer functions for the save pipeline.

Extracted from save_pipeline.py (2026-06-20) as part of the god-module
decomposition. Each function here is a thin try/except wrapper around
an indexer call for one signal type: chunks, embedding, KG, facts,
adaptive retention (with companion writes), backlinks.

Behavior is identical to the inline versions in the original
save_pipeline. Re-exported there for backward compat.
"""

from __future__ import annotations

import json
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
                "SELECT id FROM tenant_memories WHERE id = ? OR id LIKE ?",
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


def _index_colbert(
    conn, note_id: str, content: str, category: str, tags: list, source_file: str
):
    """Index ColBERT token embeddings for a single memory."""
    try:
        from search.colbert_index import index_memory_colbert

        index_memory_colbert(conn, note_id, content)
    except Exception as ce:
        logger.warning("ColBERT index failed for %s: %s", note_id, ce)


def _index_splade(
    conn, note_id: str, content: str, category: str, tags: list, source_file: str
):
    """Index SPLADE sparse vectors for a single memory."""
    try:
        from search.splade_index import index_memory_splade

        index_memory_splade(conn, note_id, content)
    except Exception as se:
        logger.warning("SPLADE index failed for %s: %s", note_id, se)


def _index_embedding(
    db, note_id: str, content: str, category: str, tags: list, source_file: str
):
    """Refresh the embedding cache for a single row."""
    try:
        from infra._lazy_imports import get_embedding_search

        get_embedding_search().index_embedding(
            db, note_id, content, category=category, tags=tags, source_file=source_file
        )
    except Exception as ee:
        logger.debug("Embedding cache write skipped for %s: %s", note_id, ee)


def _index_chunk_embeddings(db, note_id: str):
    """Embed all chunks for a memory and persist to memory_chunk_embeddings."""
    try:
        from infra.embedding_search import get_embedding_search
        rows = db.execute(
            "SELECT id, content FROM memory_chunks WHERE parent_id = ? ORDER BY chunk_idx",
            (note_id,),
        ).fetchall()
        if not rows:
            return
        chunks = [
            {"chunk_id": row[0], "parent_id": note_id, "content": row[1]}
            for row in rows
        ]
        get_embedding_search().index_chunk_embeddings_batch(db, chunks)
    except Exception as ce:
        logger.debug("Chunk embedding write skipped for %s: %s", note_id, ce)


def _index_kg(db, note_id: str, content: str):
    """Index entities and relations into the knowledge graph."""
    try:
        from knowledge_graph import KG_ENABLED, ensure_kg_schema, index_kg_for_memory

        if KG_ENABLED:
            ensure_kg_schema(db)
            pre_entity_ids = {
                row[0]
                for row in db.execute("SELECT id FROM kg_entities").fetchall()
            }
            index_kg_for_memory(db, note_id, content)
            post_entity_ids = {
                row[0]
                for row in db.execute("SELECT id FROM kg_entities").fetchall()
            }
            new_entity_ids = post_entity_ids - pre_entity_ids
            _record_kg_crdt_ops(db, note_id, new_entity_ids)
    except Exception as ke:
        logger.debug("KG indexing skipped for %s: %s", note_id, ke)


def _record_kg_crdt_ops(
    db, note_id: str, new_entity_ids: set[int] | None = None
) -> None:
    """After entity/fact extraction, write CRDT ops for the written entities/edges.

    Uses the CRDT tables (kg_entity_crdt, kg_edge_crdt) so multi-agent
    peers can sync KG state without data loss (S2). Version vector is
    {agent_id: 1} for new entities. All writes are in the same transaction.

    When ``new_entity_ids`` is provided, only those entities are recorded
    (scope to this save call). When None, falls back to entities with
    mentions > 0 (legacy behavior).
    """
    try:
        from kg.kg_crdt import record_entity_add, record_edge_add, ensure_kg_crdt_schema

        ensure_kg_crdt_schema(db)
        agent_id = "default"
        try:
            from agent_context import get_agent
            _ctx = get_agent()
            if _ctx.agent_id:
                agent_id = _ctx.agent_id
        except (ImportError, AttributeError):
            pass
        if new_entity_ids is not None and new_entity_ids:
            placeholders = ",".join("?" for _ in new_entity_ids)
            entity_rows = db.execute(
                f"SELECT id, name, entity_type FROM kg_entities WHERE id IN ({placeholders})",
                list(new_entity_ids),
            ).fetchall()
        else:
            entity_rows = db.execute(
                "SELECT id, name, entity_type FROM kg_entities WHERE mentions > 0"
            ).fetchall()
        for eid, name, etype in entity_rows:
            existing_row = db.execute(
                "SELECT version_vector FROM kg_entity_crdt WHERE entity_id = ? AND op = 'add' ORDER BY timestamp DESC LIMIT 1",
                (eid,),
            ).fetchone()
            existing_vv = json.loads(existing_row[0]) if existing_row and existing_row[0] else {}
            merged_vv = {k: max(existing_vv.get(k, 0), {agent_id: 1}.get(k, 0))
                         for k in set(existing_vv) | {agent_id}}
            record_entity_add(
                db, entity_id=eid, agent_id=agent_id,
                version_vector=merged_vv, name=name, entity_type=etype or "",
            )
        # Record CRDT ops for edges whose source or target is a new entity
        if new_entity_ids is not None and new_entity_ids:
            placeholders = ",".join("?" for _ in new_entity_ids)
            edge_rows = db.execute(
                f"SELECT source_id, target_id, relation, weight, valid_at "
                f"FROM kg_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                list(new_entity_ids) + list(new_entity_ids),
            ).fetchall()
        else:
            edge_rows = db.execute(
                "SELECT source_id, target_id, relation, weight, valid_at FROM kg_edges"
            ).fetchall()
        from kg.kg_crdt import _edge_key as _ek
        for src, tgt, rel, w, valid_at in edge_rows:
            eid = _ek(src, tgt, rel)
            existing_row = db.execute(
                "SELECT version_vector FROM kg_edge_crdt WHERE edge_id = ? ORDER BY timestamp DESC LIMIT 1",
                (eid,),
            ).fetchone()
            existing_vv = json.loads(existing_row[0]) if existing_row and existing_row[0] else {}
            merged_vv = {k: max(existing_vv.get(k, 0), {agent_id: 1}.get(k, 0))
                         for k in set(existing_vv) | {agent_id}}
            record_edge_add(
                db, source_id=src, target_id=tgt, relation=rel,
                weight=w or 1.0, agent_id=agent_id,
                version_vector=merged_vv, valid_at=valid_at,
            )
    except Exception as crdt_exc:
        logger.debug("KG CRDT op recording skipped for %s: %s", note_id, crdt_exc)


def _index_facts(db, note_id: str, content: str, belief_status: str = "active",
                 epistemic_source: str = "agent", asserting_agent_id: str = "",
                 evidence_chain: list | None = None, fact_type: str = "observation"):
    """Index SPO facts into kg_facts."""
    try:
        from knowledge_graph import KG_ENABLED

        if KG_ENABLED:
            from fact import ensure_facts_schema, index_facts_for_memory
            from belief import ensure_beliefs_schema

            ensure_facts_schema(db)
            ensure_beliefs_schema(db)
            # belief_assertions now created inside index_facts_for_memory (G1)
            index_facts_for_memory(db, note_id, content,
                                   belief_status=belief_status,
                                   epistemic_source=epistemic_source,
                                   fact_type=fact_type)
    except Exception as fe:
        logger.warning("Fact indexing skipped for %s: %s", note_id, fe)


def _index_adaptive_retention(
    db, note_id: str, db_path: str | None = None, tags: list | None = None
):
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
            (note_id, "save", None, _json.dumps(tags or []), _time.time()),
        )
    except Exception as _up_e:
        logger.debug("user_profile_access_log on save failed: %s", _up_e)
    # P1.3 click proxy: when a note is saved, mark all unclicked
    # impressions from the SAME search sessions (query_ids) as clicked.
    # Default window is 7 days — the user's recall-and-save loop isn't
    # always tight; 4h was too short and left most impressions stuck
    # in "pending". Expand via MEMORY_CTR_CLICK_WINDOW_HOURS if needed.
    try:
        import os as _os
        import time as _time

        _click_window_s = (
            float(_os.environ.get("MEMORY_CTR_CLICK_WINDOW_HOURS", "168")) * 3600.0
        )
        _cutoff = _time.time() - _click_window_s
        # Find every search session (query_id) that returned this note
        # within the window. Then mark ALL unclicked, undismissed
        # impressions from those sessions — saving one result implies
        # the search itself was useful, so the whole result set gets
        # the implicit click signal. This is what makes the LTR model
        # actually trainable on everyday usage.
        _qid_rows = db.execute(
            "SELECT DISTINCT query_id FROM memory_ctr_feedback "
            "WHERE id = ? AND returned_at > ? AND clicked_at IS NULL",
            (note_id, _cutoff),
        ).fetchall()
        for (_qid,) in _qid_rows:
            db.execute(
                "UPDATE memory_ctr_feedback SET clicked_at = ? "
                "WHERE query_id = ? AND clicked_at IS NULL "
                "AND dismissed_at IS NULL",
                (_time.time(), _qid),
            )
    except Exception as _ctr_click_e:
        logger.debug("CTR click-proxy failed: %s", _ctr_click_e)
