"""One-shot backfill: compute fingerprints for all existing kg_entities rows."""
import sqlite3
import hashlib
import sys
from pathlib import Path

DB = Path("memory/memory.db")
assert DB.exists(), f"DB not found: {DB}"

conn = sqlite3.connect(str(DB))
rows = conn.execute(
    "SELECT entity_id, name, entity_type, description, created_at FROM kg_entities"
).fetchall()

canonical = lambda s: " ".join(s.lower().strip().split())

for entity_id, name, etype, desc, created_at in rows:
    payload = f"{canonical(name or '')}|{canonical(etype or '')}|{canonical(desc or '')}"
    fp = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        "UPDATE kg_entities SET fingerprint = ?, inception_at = ? WHERE entity_id = ?",
        (fp, created_at or "unknown", entity_id),
    )

conn.commit()
print(f"Backfilled {len(rows)} entities.")
conn.close()
