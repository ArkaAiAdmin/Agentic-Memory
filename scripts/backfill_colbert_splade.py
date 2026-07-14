"""Backfill ColBERT tokens and SPLADE vectors for all memories missing them."""

import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_colbert_splade")

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "memory" / "memory.db"

sys.path.insert(0, str(REPO_ROOT))

from search.colbert_index import _ensure_colbert_schema, index_memory_colbert_batch
from search.splade_index import _ensure_splade_schema, index_memory_splade_batch


BATCH_SIZE = 32


def _ensure_schemas(conn):
    _ensure_colbert_schema(conn)
    _ensure_splade_schema(conn)
    conn.commit()


def backfill():
    import sqlite3

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _ensure_schemas(conn)

    total_row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    colbert_row = conn.execute("SELECT COUNT(DISTINCT memory_id) FROM colbert_tokens").fetchone()[0]
    splade_row = conn.execute("SELECT COUNT(DISTINCT memory_id) FROM splade_tokens").fetchone()[0]

    logger.info("Total memories: %d (colbert=%d, splade=%d)", total_row, colbert_row, splade_row)

    rows = conn.execute(
        """
        SELECT m.id, m.content, m.category
        FROM memories m
        LEFT JOIN colbert_tokens ct ON ct.memory_id = m.id
        LEFT JOIN splade_tokens st ON st.memory_id = m.id
        WHERE ct.memory_id IS NULL OR st.memory_id IS NULL
        """
    ).fetchall()

    total = len(rows)
    if total == 0:
        logger.info("Nothing to backfill")
        conn.close()
        return

    logger.info("Found %d memories missing ColBERT and/or SPLADE indexes", total)

    colbert_done = 0
    splade_done = 0
    colbert_errors = 0
    splade_errors = 0

    for start in range(0, total, BATCH_SIZE):
        batch_rows = rows[start:start + BATCH_SIZE]
        batch = [(r["id"], r["content"] or "") for r in batch_rows]

        try:
            t = index_memory_colbert_batch(conn, batch)
            colbert_done += 1 if t else 0
        except Exception as e:
            logger.warning("ColBERT batch failed at %d: %s", start, e)
            colbert_errors += len(batch)

        try:
            s = index_memory_splade_batch(conn, batch)
            splade_done += 1 if s else 0
        except Exception as e:
            logger.warning("SPLADE batch failed at %d: %s", start, e)
            splade_errors += len(batch)

        if (start // BATCH_SIZE + 1) % 50 == 0 or start + BATCH_SIZE >= total:
            conn.commit()

        if (start // BATCH_SIZE + 1) % 5 == 0 or start + BATCH_SIZE >= total:
            done = min(start + BATCH_SIZE, total)
            logger.info(
                "Progress: %d/%d (colbert=%d, splade=%d, colbert_errors=%d, splade_errors=%d)",
                done, total, colbert_done, splade_done, colbert_errors, splade_errors,
            )

    conn.commit()

    ct_count = conn.execute("SELECT COUNT(DISTINCT memory_id) FROM colbert_tokens").fetchone()[0]
    st_count = conn.execute("SELECT COUNT(DISTINCT memory_id) FROM splade_tokens").fetchone()[0]
    logger.info("Backfill complete: colbert_tokens=%d memories, splade_tokens=%d memories, errors=%d/%d",
                ct_count, st_count, colbert_errors + splade_errors, total)

    conn.close()


if __name__ == "__main__":
    backfill()
