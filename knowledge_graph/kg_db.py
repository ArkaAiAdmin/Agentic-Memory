from __future__ import annotations

import logging
import re
import sqlite3
import sys
import time

from .kg_extract import (
    _content_hash,
    _extraction_cache_get,
    _extraction_cache_put,
    _is_file_path_entity,
    _MARKDOWN_STOPWORDS,
    extract_entities,
)

logger = logging.getLogger(__name__)


def _get_edge_weight_params() -> tuple[float, float]:
    try:
        from _lazy_imports import get_config

        c = get_config()
        return (c.kg_edge_weight_increment, c.kg_edge_weight_cap)
    except Exception:
        return (0.1, 10.0)


def _upsert_entity(
    conn: sqlite3.Connection, name: str, entity_type: str, now: float
) -> int:
    """Insert or update an entity. Returns the entity ID."""
    normalized = name.lower().strip()
    row = conn.execute(
        "SELECT id FROM kg_entities WHERE name = ? AND entity_type = ?",
        (normalized, entity_type),
    ).fetchone()
    if row:
        entity_id = row[0]
        conn.execute(
            "UPDATE kg_entities SET mentions = mentions + 1, updated_at = datetime('now') "
            "WHERE id = ?",
            (entity_id,),
        )
        return int(entity_id)
    else:
        try:
            cur = conn.execute(
                "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) "
                "VALUES (?, ?, datetime('now'), datetime('now'))",
                (normalized, entity_type),
            )
            if cur.lastrowid is None or cur.lastrowid == 0:
                raise RuntimeError(f"Failed to upsert entity: {name}")
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Race condition: another thread inserted between our SELECT and INSERT
            row = conn.execute(
                "SELECT id FROM kg_entities WHERE name = ? AND entity_type = ?",
                (normalized, entity_type),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE kg_entities SET mentions = mentions + 1, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (row[0],),
                )
                return int(row[0])
            raise


def _upsert_edge(
    conn: sqlite3.Connection,
    source_id: int,
    target_id: int,
    relation: str,
    now: float,
    context: str = "",
) -> None:
    """Insert or update an edge."""
    row = conn.execute(
        "SELECT id FROM kg_edges WHERE source_id = ? AND target_id = ? AND relation = ?",
        (source_id, target_id, relation),
    ).fetchone()
    if row:
        inc, cap = _get_edge_weight_params()
        conn.execute(
            "UPDATE kg_edges SET weight = MIN(weight + ?, ?) WHERE id = ?",
            (inc, cap, row[0]),
        )
    else:
        # H8 fix: mirror the IntegrityError retry pattern from
        # _upsert_entity (lines 381-394). A concurrent writer may INSERT
        # the same edge between our SELECT and our INSERT.
        try:
            conn.execute(
                "INSERT INTO kg_edges (source_id, target_id, relation, created_at, valid_at) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
                (source_id, target_id, relation),
            )
        except sqlite3.IntegrityError:
            # Re-SELECT and update the existing row's weight.
            row = conn.execute(
                "SELECT id FROM kg_edges WHERE source_id = ? AND target_id = ? AND relation = ?",
                (source_id, target_id, relation),
            ).fetchone()
            if row is not None:
                inc, cap = _get_edge_weight_params()
                conn.execute(
                    "UPDATE kg_edges SET weight = MIN(weight + ?, ?) WHERE id = ?",
                    (inc, cap, row[0]),
                )


def invalidate_edge(conn: sqlite3.Connection, edge_id: int) -> bool:
    """Mark an edge as invalid (soft delete). Returns True if an edge was invalidated."""
    now = time.time()
    try:
        cur = conn.execute(
            "UPDATE kg_edges SET invalid_at = datetime('now') WHERE id = ? AND invalid_at IS NULL",
            (edge_id,),
        )
        return cur.rowcount > 0
    except Exception:
        logger.warning("Failed to invalidate KG edge %s", edge_id)
        return False


def get_active_edges_for_entity(
    conn: sqlite3.Connection, entity_id: int, limit: int = 50
) -> list[dict]:
    """Get only non-invalidated edges for an entity."""
    edges = conn.execute(
        """SELECT e.id, s.name, s.entity_type, e.relation,
                  t.name, t.entity_type, e.weight
           FROM kg_edges e
           JOIN kg_entities s ON e.source_id = s.id
           JOIN kg_entities t ON e.target_id = t.id
           WHERE (e.source_id = ? OR e.target_id = ?)
             AND e.invalid_at IS NULL
           ORDER BY e.weight DESC
           LIMIT ?""",
        (entity_id, entity_id, limit),
    ).fetchall()
    return [
        {
            "id": e[0],
            "source": e[1],
            "source_type": e[2],
            "relation": e[3],
            "target": e[4],
            "target_type": e[5],
            "weight": e[6],
        }
        for e in edges
    ]


def index_kg_for_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    force_regex_only: bool = False,
) -> dict:
    """Extract and index entities/relations from a memory note.

    Returns stats: {"entities": N, "relations": N, "regex_count": R,
                    "llm_count": L, "cache_hit": bool, "error": str|None,
                    "duration_ms": float}

    Two-stage extraction (P2c):
      Stage 1: pattern-based ``extract_entities``.
      Stage 2: LLM fallback (via ``llm_extraction``) is invoked ONLY
      when regex returned 0 or 1 valid candidates.  This keeps the
      LLM cost bounded to memories the regex can't handle, while
      still letting the LLM rescue the long tail of messy notes.

    When *force_regex_only* is True, Stage 2 is skipped entirely so
    no LLM model is loaded.  Used by cron/heartbeat backfill to avoid
    accidentally pulling Qwen2.5-3B into memory during a routine drift
    check.

    Extraction results are cached in-process by content hash (P2c.3)
    so a re-save of the same content doesn't re-pay extraction.

    Relations are created via co-occurrence: when two extracted entities
    appear in the same sentence, a ``co_occurs`` edge is created between
    them.
    """
    import sys

    if not sys.modules["knowledge_graph"].KG_ENABLED:
        # Backwards-compatible return shape for the disabled branch.
        # Existing callers (and tests) compare against the original
        # 2-key dict; the extra stats keys are only meaningful when
        # extraction actually runs.
        return {"entities": 0, "relations": 0}

    start = time.time()
    stats: dict = {
        "entities": 0,
        "relations": 0,
        "regex_count": 0,
        "llm_count": 0,
        "cache_hit": False,
        "error": None,
        "duration_ms": 0.0,
    }

    # P2c.3 — content-hash cache check
    ch = _content_hash(content)
    cached = _extraction_cache_get(ch)
    if cached is not None:
        logger.debug("KG extraction cache hit for %s (hash=%s)", memory_id, ch)
        stats["cache_hit"] = True
        stats["regex_count"] = len(cached)
        # Cached set is the post-filter, post-LLM result; treat as
        # the final entity list directly.
        entities = cached
    else:
        # P2c.1 — Stage 1: regex extraction
        try:
            from _lazy_imports import get_config

            _min_occ = int(get_config().entity_min_occurrences)
        except Exception:
            _min_occ = 2
        regex_entities = extract_entities(content, min_occurrences=_min_occ)
        stats["regex_count"] = len(regex_entities)
        entities = list(regex_entities)

        # P2c.2 — Stage 2: LLM fallback when regex returned too few entities
        if not force_regex_only:
            try:
                from _lazy_imports import get_config

                _fallback_threshold = int(get_config().kg_llm_fallback_min_entities)
            except Exception:
                _fallback_threshold = 2
        if not force_regex_only and len(entities) < _fallback_threshold:
            try:
                from llm_extraction import extract_entities_via_llm

                llm_raw = extract_entities_via_llm(content)
            except Exception as e:
                # P2a.1 — visible error, not silent pass
                logger.exception("KG LLM extraction failed for %s: %s", memory_id, e)
                stats["error"] = f"llm: {type(e).__name__}: {e}"
                llm_raw = []

            if llm_raw:
                for ent in llm_raw:
                    name = (ent.get("name") or "").strip()
                    etype = (ent.get("type") or "concept").strip() or "concept"
                    if not name or len(name) < 2:
                        continue
                    name_lower = name.lower()
                    # Apply the same stop-words and file-path filter
                    # to LLM output as we do to regex output.
                    if name_lower in _MARKDOWN_STOPWORDS:
                        continue
                    if _is_file_path_entity(name):
                        continue
                    entities.append((name, etype))
                stats["llm_count"] = len(entities) - stats["regex_count"]

        # Cache the post-filter entity list for future saves of the
        # same content.
        try:
            _extraction_cache_put(ch, entities)
        except Exception as cache_exc:
            logger.debug("KG extraction cache write failed: %s", cache_exc)

    now = time.time()
    entity_ids = {}
    entity_count = 0
    for name, etype in entities:
        eid = _upsert_entity(conn, name, etype, now)
        entity_ids[name.lower().strip()] = eid
        entity_count += 1
    stats["entities"] = entity_count

    # Co-occurrence relations: two extracted entities in the same sentence
    # get a co_occurs edge.  This avoids the name mismatch problem where
    # extract_relations() used a different regex than extract_entities().
    relation_count = 0
    seen_pairs: set[tuple[int, int]] = set()
    sentences = re.split(r"[.!?\n]+", content)
    from _lazy_imports import get_config

    _KG_COCCUR_ENTITY_CAP = getattr(get_config(), "kg_coccurr_entity_cap", 20)
    # Collect all pairs first, then batch upsert (H4 fix: batch co-occurrence edges)
    all_pairs: list[tuple[int, int, str]] = []  # (source_id, target_id, context)
    for sentence in sentences:
        sent_lower = sentence.lower()
        present = [
            eid
            for name, eid in entity_ids.items()
            if re.search(rf"\b{re.escape(name)}\b", sent_lower)
        ]
        if len(present) > _KG_COCCUR_ENTITY_CAP:
            present = present[:_KG_COCCUR_ENTITY_CAP]
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                pair = (min(present[i], present[j]), max(present[i], present[j]))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    all_pairs.append((pair[0], pair[1], sentence[:200]))

    # Batch upsert all co_occurs edges
    if all_pairs:
        # Check existing edges in batch
        placeholders = ",".join("(?, ?)" for _ in all_pairs)
        flat_params = [p for pair in all_pairs for p in (pair[0], pair[1])]
        existing = conn.execute(
            f"SELECT source_id, target_id FROM kg_edges WHERE (source_id, target_id) IN ({placeholders}) AND relation = 'co_occurs'",
            flat_params,
        ).fetchall()
        existing_pairs = {(row[0], row[1]) for row in existing}

        new_pairs = [p for p in all_pairs if (p[0], p[1]) not in existing_pairs]
        if new_pairs:
            now_ts = now
            conn.executemany(
                "INSERT INTO kg_edges (source_id, target_id, relation, created_at, valid_at) VALUES (?, ?, 'co_occurs', ?, ?)",
                [(p[0], p[1], now_ts, now_ts) for p in new_pairs],
            )
            relation_count += len(new_pairs)
        # Update weight for existing pairs
        update_pairs = [p for p in all_pairs if (p[0], p[1]) in existing_pairs]
        if update_pairs:
            inc, cap = _get_edge_weight_params()
            conn.executemany(
                "UPDATE kg_edges SET weight = MIN(weight + ?, ?) WHERE source_id = ? AND target_id = ? AND relation = 'co_occurs'",
                [(inc, cap, p[0], p[1]) for p in update_pairs],
            )
            relation_count += len(update_pairs)

    stats["relations"] = relation_count

    stats["duration_ms"] = round((time.time() - start) * 1000.0, 2)

    # P2a.2 — write per-memory stats row.  Best-effort; failure here
    # must not break the save path.
    try:
        _write_extraction_stats(conn, memory_id, stats)
    except Exception as stat_exc:
        logger.debug("kg_extraction_stats write failed for %s: %s", memory_id, stat_exc)

    return stats


def _write_extraction_stats(
    conn: sqlite3.Connection, memory_id: str, stats: dict
) -> None:
    """Insert one row into ``kg_extraction_stats`` for the given memory.

    Defensive about the table not existing yet (it is created by
    ``ensure_kg_schema`` and by the migration system; this is just a
    belt-and-suspenders ``CREATE IF NOT EXISTS`` so an unwary caller
    doesn't crash the save path.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kg_extraction_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            entities_extracted INTEGER DEFAULT 0,
            regex_count INTEGER DEFAULT 0,
            llm_count INTEGER DEFAULT 0,
            duration_ms REAL DEFAULT 0,
            error TEXT,
            created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO kg_extraction_stats "
        "(memory_id, entities_extracted, regex_count, llm_count, "
        " duration_ms, error) VALUES (?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            int(stats.get("entities", 0)),
            int(stats.get("regex_count", 0)),
            int(stats.get("llm_count", 0)),
            float(stats.get("duration_ms", 0.0)),
            stats.get("error"),
        ),
    )
