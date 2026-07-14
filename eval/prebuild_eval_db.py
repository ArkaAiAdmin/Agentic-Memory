#!/usr/bin/env python3
"""Pre-build an eval DB with full indexes for golden set."""
import json, os, sqlite3, sys, time, tempfile
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

golden = json.load(open(INSTALL_DIR / "eval" / "real_memory_golden_v2.json"))
memories = golden["memories"]
print(f"Loaded {len(memories)} golden memories")

_DB_DIR = tempfile.mkdtemp(prefix="eval_prebuilt_")
_DB_PATH = Path(_DB_DIR) / "memory.db"
print(f"DB: {_DB_PATH}")

os.environ["MEMORY_DB_PATH"] = str(_DB_PATH)

from infra.db_migrations import run_schema_setup
conn = sqlite3.connect(str(_DB_PATH))
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA foreign_keys = ON")
run_schema_setup(conn)

try:
    from fact import ensure_facts_schema; ensure_facts_schema(conn)
except Exception: pass

try:
    from knowledge_graph import ensure_kg_schema; ensure_kg_schema(conn)
except Exception: pass

from search.chunk_index import _qw5_ensure_schema; _qw5_ensure_schema(conn)
from search.colbert_index import _ensure_colbert_schema; _ensure_colbert_schema(conn)
from search.splade_index import _ensure_splade_schema; _ensure_splade_schema(conn)
conn.commit()

for mem in memories:
    nid = mem.get("note_id", "")
    content = mem.get("content", "")
    tags = json.dumps(mem.get("tags", []))
    source_file = nid
    created = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO memories (id, content, source_file, tags, created_at) VALUES (?, ?, ?, ?, ?)",
        (nid, content, source_file, tags, created),
    )
conn.commit()
print(f"Inserted {len(memories)} memories")

conn.execute('INSERT INTO memories_fts(memories_fts) VALUES("rebuild")')
conn.commit()
print("FTS built")

rows = conn.execute("SELECT id, content FROM memories WHERE content IS NOT NULL AND content != ''").fetchall()
total = len(rows)
print(f"Indexing {total} memories...")

print("Chunks...", end=" ", flush=True)
from search.chunk_index import _qw5_index_chunks_for
for i, (mid, content) in enumerate(rows):
    _qw5_index_chunks_for(conn, mid, content)
    if (i+1) % 100 == 0: conn.commit()
conn.commit()
conn.execute('INSERT INTO memory_chunks_fts(memory_chunks_fts) VALUES("rebuild")')
conn.commit()
print("done")

print("Embeddings...", end=" ", flush=True)
from save.indexers import _index_embedding
for i, (mid, content) in enumerate(rows):
    _index_embedding(conn, mid, content, category="", tags=[], source_file=mid)
    if (i+1) % 25 == 0: conn.commit()
conn.commit()
print("done")

print("ColBERT...", end=" ", flush=True)
from search.colbert_index import index_memory_colbert_batch
batch = [(r[0], r[1]) for r in rows]
for start in range(0, total, 64):
    index_memory_colbert_batch(conn, batch[start:start+64])
    conn.commit()
print("done")

print("SPLADE...", end=" ", flush=True)
from search.splade_index import index_memory_splade_batch
for start in range(0, total, 64):
    index_memory_splade_batch(conn, batch[start:start+64])
    conn.commit()
print("done")

conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.commit()
conn.close()

path_file = INSTALL_DIR / "eval" / "prebuilt_db_path.txt"
path_file.write_text(str(_DB_PATH))
print(f"\nPre-built DB: {_DB_PATH}")
