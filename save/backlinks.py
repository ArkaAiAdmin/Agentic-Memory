"""Backlink generation functions for the save pipeline.

Extracted from save_pipeline.py (2026-06-20) as part of the god-module
decomposition. Contains:

- _auto_fts_backlinks: FTS5 content-overlap backlinks
- _auto_semantic_backlinks: embedding-space semantic edges in KG
- _auto_backlink_multi_part: "part-1/part-2/part-3" series backlinks

Behavior is identical to the inline versions. Re-exported from
save_pipeline for backward compat.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from memory_common import open_db, atomic_write

logger = logging.getLogger(__name__)


def _auto_fts_backlinks(db, note_id: str, content: str, max_links: int = 3) -> None:
    """Create bidirectional backlinks based on FTS5 content overlap.

    Extracts significant terms from content, queries memories_fts for notes
    with shared terms, and inserts bidirectional backlinks into the backlinks
    table.  Does not modify any file on disk.
    """
    import re as _re

    try:
        words = _re.findall(r"\b[a-zA-Z]{3,}\b", content.lower())
        if not words:
            return
        search_terms = list(set(words))[:20]
        if not search_terms:
            return

        related = []
        seen = set()
        for term in search_terms[:5]:
            try:
                rows = db.execute(
                    """SELECT m.id
                       FROM memories_fts fts
                       JOIN memories m ON m.rowid = fts.rowid
                       WHERE memories_fts MATCH ?
                       AND m.id != ?
                       AND m.deleted_at IS NULL
                       ORDER BY rank
                       LIMIT ?""",
                    (term, note_id, max_links),
                ).fetchall()
                for (rid,) in rows:
                    if rid not in seen:
                        seen.add(rid)
                        related.append(rid)
            except Exception:
                logger.warning(
                    "FTS backlink search failed for term '%s' on note %s", term, note_id
                )
                continue

        if not related:
            return

        for target_id in related[:max_links]:
            db.execute(
                "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
                (note_id, target_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
                (target_id, note_id),
            )
    except Exception as e:
        logger.debug("Auto-FTS-backlinks skipped for %s: %s", note_id, e)


def _auto_semantic_backlinks(
    db, note_id: str, content: str, top_k: int = 5, db_path: str | None = None
) -> None:
    """Create KG 'semantically_related' edges between this memory and its
    nearest neighbors in embedding space.

    Uses the usearch vector index (via ``EmbeddingSearch.search_by_vector``).
    Inserts bidirectional ``semantically_related`` edges into ``kg_edges``.

    The similarity threshold (0.30) is deliberately low to cast a wide net;
    the ranking pipeline's RRF fusion will surface the truly relevant ones.
    """
    try:
        row = db.execute(
            "SELECT embedding FROM memory_embeddings WHERE memory_id = ?", (note_id,)
        ).fetchone()
        if not row:
            return
        import numpy as np

        query_vec = np.frombuffer(row[0], dtype=np.float32).astype(np.float32)
        if np.linalg.norm(query_vec) < 1e-8:
            return

        from _lazy_imports import get_embedding_search

        es = get_embedding_search()
        if es.model is None:
            return

        if db_path is None:
            db_path = db.execute("PRAGMA database_list").fetchone()[2]

        results = es.search_by_vector(query_vec, db_path, limit=top_k, db=db)
        if not results:
            return

        scores = [
            (r["id"], r["score"])
            for r in results
            if r["score"] >= 0.30 and r["id"] != note_id
        ]
        if not scores:
            return
        # Ensure relation entity exists
        db.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES ('semantically_related', 'relation')"
        )
        rel_row = db.execute(
            "SELECT id FROM kg_entities WHERE name = 'semantically_related' AND entity_type = 'relation'"
        ).fetchone()
        if not rel_row:
            return
        rel_id = rel_row[0]
        # Get or create entity for this memory
        db.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, 'memory')",
            (note_id,),
        )
        src_row = db.execute(
            "SELECT id FROM kg_entities WHERE name = ? AND entity_type = 'memory'",
            (note_id,),
        ).fetchone()
        if not src_row:
            return
        src_id = src_row[0]
        edges_created = 0
        for target_id, score in scores:
            db.execute(
                "INSERT OR IGNORE INTO kg_entities (name, entity_type) VALUES (?, 'memory')",
                (target_id,),
            )
            tgt_row = db.execute(
                "SELECT id FROM kg_entities WHERE name = ? AND entity_type = 'memory'",
                (target_id,),
            ).fetchone()
            if not tgt_row:
                continue
            tgt_id = tgt_row[0]
            # Bidirectional edges
            db.execute(
                "INSERT OR IGNORE INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, ?)",
                (src_id, tgt_id, "semantically_related", score),
            )
            db.execute(
                "INSERT OR IGNORE INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, ?)",
                (tgt_id, src_id, "semantically_related", score),
            )
            edges_created += 1
        if edges_created > 0:
            logger.debug(
                "Auto-backlinks: created %d semantic edges for %s",
                edges_created,
                note_id,
            )
    except Exception as e:
        logger.debug("Auto-semantic-backlinks skipped for %s: %s", note_id, e)


def _auto_backlink_multi_part(
    db_path: Path,
    note_id: str,
    category: str,
    title_slug: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[list]:
    """Auto-backlink multi-part memories (e.g., part-1, part-2, part-3).

    Detects if title_slug matches pattern like 'foo-part-N' or 'foo-part-N-of-M'
    and adds backlinks to all other parts in the same series.

    Memories table has no ``title_slug`` column — it is encoded in the
    ``id`` as ``{category}/{title_slug}``.  This function queries by ``id``
    LIKE pattern and extracts the slug from the ``id``.

    Behavior:
        * ``conn=None`` (legacy call): opens its own connection, does
          DB UPDATEs + ``.md`` writes + commit, returns ``None``.
        * ``conn`` provided (save_memory path): does DB UPDATEs only on
          the caller's connection.  Returns a list of ``(pid, new_content)``
          tuples for the caller to write to ``.md`` files *after* its
          DB transaction is committed, so a crash between the UPDATE and
          the .md write leaves DB and disk consistently un-done (the next
          saga step would roll both back together).

    Returns ``None`` for non-matching slugs (preserves the legacy
    "returns nothing" contract that mutation tests assert on).
    """
    import re

    match = re.match("^(.+)-part-(\\d+)(?:-of-(\\d+))?$", title_slug)
    if not match:
        return None
    base, part_num_str, total_str = match.groups()
    part_num = int(part_num_str)
    total = int(total_str) if total_str else None
    like_pattern = f"{category}/{base}-part-%"

    def _query(db):
        return db.execute(
            "SELECT id FROM memories WHERE id LIKE ?", (like_pattern,)
        ).fetchall()

    def _read_content(db, pid):
        row = db.execute("SELECT content FROM memories WHERE id = ?", (pid,)).fetchone()
        return row[0] if row else None

    def _write_content(db, pid, nc):
        db.execute("UPDATE memories SET content = ? WHERE id = ?", (nc, pid))

    if conn is not None:
        parts = _query(conn)
    else:
        with open_db(db_path) as db:
            parts = _query(db)
    if len(parts) < 2:
        return None
    siblings = []
    for (pid,) in parts:
        slug = pid.split("/", 1)[1] if "/" in pid else pid
        m = re.match("^(.+)-part-(\\d+)(?:-of-(\\d+))?$", slug)
        if m:
            pnum = int(m.group(2))
            siblings.append((pnum, pid, slug))
    if not siblings:
        return None
    siblings.sort(key=lambda x: x[0])
    sibling_ids = [f"[[{pid}]]" for _, pid, _ in siblings]
    prefix = f"**Part of:** {', '.join(sibling_ids)}\n\n"
    import re as _re

    _backlink_re = _re.compile("^\\*\\*Part of:\\*\\*.*?\\n\\n", _re.MULTILINE)
    if conn is not None:
        pending_writes: list[tuple[str, str]] = []
        for _, pid, _ in siblings:
            content = _read_content(conn, pid)
            if content is None:
                continue
            stripped = _backlink_re.sub("", content)
            new_content = prefix + stripped
            if new_content != content:
                _write_content(conn, pid, new_content)
                pending_writes.append((pid, new_content))
        # Note: caller-provided conn means the caller owns the transaction
        # boundary. We do NOT commit here — the caller is responsible for
        # committing (or rolling back) the outer transaction.
        return pending_writes if pending_writes else None
    else:
        with open_db(db_path) as db:
            for _, pid, _ in siblings:
                content = _read_content(db, pid)
                if content is None:
                    continue
                stripped = _backlink_re.sub("", content)
                new_content = prefix + stripped
                if new_content != content:
                    _write_content(db, pid, new_content)
                    _md_path = db_path.parent / f"{pid}.md"
                    if _md_path.exists():
                        atomic_write(_md_path, new_content, encoding="utf-8")
            db.commit()
        return None
