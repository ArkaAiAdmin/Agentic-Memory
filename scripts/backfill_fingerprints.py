"""One-shot backfill: compute fingerprints for all existing kg_entities rows.

Production kg_entities schema: id (not entity_id), no description column.
Fingerprint = sha256(canonicalize(name) | canonicalize(entity_type))
(description is empty for all legacy entities, so fingerprint = sha256(name|type|)).
"""
import sqlite3
import hashlib
import sys
from pathlib import Path

DB = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("memory/memory.db")
assert DB.exists(), f"DB not found: {DB}"

canonical = lambda s: " ".join(s.lower().strip().split())

conn = sqlite3.connect(str(DB))
rows = conn.execute(
    "SELECT id, name, entity_type FROM kg_entities WHERE fingerprint IS NULL"
).fetchall()

count = 0
for row_id, name, etype in rows:
    payload = f"{canonical(name or '')}|{canonical(etype or '')}|"
    fp = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        "UPDATE kg_entities SET fingerprint = ?, inception_at = created_at WHERE id = ?",
        (fp, row_id),
    )
    count += 1

total = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
conn.commit()
print(f"Backfilled {count}/{total} entities ({total - count} already had fingerprints).")
conn.close()
