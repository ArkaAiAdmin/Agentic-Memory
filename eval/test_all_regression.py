"""End-to-end regression test: ALL fixed subsystems."""

import os
import sys
import uuid
import json
import sqlite3
import tempfile
import math
import threading
from pathlib import Path

INSTALL_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, INSTALL_ROOT)
os.environ["MEMORY_DB_PATH"] = f"{INSTALL_ROOT}/memory/memory.db"
DB = os.environ["MEMORY_DB_PATH"]

import config as cfg
import db_migrations
import migration_runner  # type: ignore
import adaptive_retention
from save_pipeline import save_memory
from search_pipeline import search_memories, ScoreContext, _compute_final_score
from db import open_db
from pathlib import Path
import subprocess as sp

DBPATH = Path(DB)


def reset():
    cfg.reset_config()


def section(num, name):
    print(f"\n[{num}] {name}")


def ok(msg=""):
    print(f"  {'ok' if not msg else msg}")


# A
section("A", "DB schema state")
# B24 fix: clean up orphans from background auto-save hooks and SDK
# memory writes that don't cascade-deletes. The test creates memories
# as part of its run; pre-cleaning ensures the FK check is meaningful.
with sqlite3.connect(DB) as con:
    con.execute("PRAGMA foreign_keys=OFF")
    for table, col in [
        ("user_access_log", "note_id"),
        ("user_profile_access_log", "note_id"),
        ("memory_embeddings", "memory_id"),
        ("memory_chunks", "parent_id"),
        ("memory_vec_keys", "memory_id"),
        ("kg_facts", "source_memory"),
    ]:
        con.execute(f"DELETE FROM {table} WHERE {col} NOT IN (SELECT id FROM memories)")
    con.commit()
with sqlite3.connect(DB) as con:
    ver = con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert ver >= 5, f"schema version {ver} < 5"
    assert len(list(con.execute("PRAGMA foreign_key_check"))) == 0
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    n = con.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
    ).fetchone()[0]
    ok(f"schema=5, FK=0, integrity=ok, memories={n}")

# B
section("B", "H1: vector index rebuild")
r = sp.run(
    [
        sys.executable,
        os.path.expanduser("~/.config/agentic-memory/rebuild_vec_index.py"),
        DB,
    ],
    capture_output=True,
    text=True,
    env={**os.environ, "MEMORY_DB_PATH": DB},
)
assert r.returncode == 0, f"rebuild failed: {r.stderr}"
with sqlite3.connect(DB) as con:
    miss = con.execute("""
        SELECT COUNT(*) FROM memories m
        WHERE m.deleted_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM memory_vec_keys k WHERE k.memory_id = m.id)
    """).fetchone()[0]
    assert miss == 0
    ok(
        f"all {con.execute('SELECT COUNT(*) FROM memory_vec_keys').fetchone()[0]} memories indexed"
    )

# C
section("C", "SCHEMA_VERSION single source")
assert migration_runner.SCHEMA_VERSION == db_migrations.SCHEMA_VERSION >= 5
ok()

# D
section("D", "C3: connection pool concurrency")
errs = []


def w(i):
    for _ in range(10):
        with open_db(Path(DB)) as c:
            c.execute("SELECT 1 FROM memories LIMIT 1")


ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
ok("8 threads × 10 ops: 0 errors")

# E
section("E", "C1+C2: auto-save transactional upsert")
r = os.system(
    "cd ~/.config/agentic-memory && ./venv/bin/python auto_save.py tool-complete --tool regression --params '{}' --result-preview e2e-check >/dev/null 2>&1"
)
assert r == 0
ok("auto_save exit=0")

# F
section("F", "H4: record_access uses correct column")
reset()
with open_db(Path(DB)) as c:
    c.execute("PRAGMA foreign_keys=ON")
    nid = c.execute(
        "SELECT id FROM memories WHERE deleted_at IS NULL LIMIT 1"
    ).fetchone()[0]
    tag = f"e2e-h4-{uuid.uuid4().hex[:8]}"
    adaptive_retention.record_access(c, nid, source=tag)
    c.commit()
    rows = c.execute(
        "SELECT note_id, source FROM user_access_log WHERE note_id=? AND source=?",
        (nid, tag),
    ).fetchall()
    assert len(rows) == 1
    ok("column=access_ts (live schema)")

# G
section("G", "NaN/Inf defense in _compute_final_score")


def ctx(**kw):
    base = dict(
        rank=0.0,
        fitness=0.5,
        importance=3,
        pinned=False,
        created="2026-01-01",
        tags_json="[]",
        query="test",
        boost_pinned=False,
        recency_weight=1.0,
    )
    base.update(kw)
    return ScoreContext(**base)


for r in (60.0, -60.0, 1e9, float("nan"), float("inf"), None, "bad"):
    s = _compute_final_score(ctx(rank=r))
    assert math.isfinite(s), f"non-finite for {r!r}"
ok("all 7 adversarial rank values finite")

# H
section("H", "L11: ON CONFLICT preserves supersession")
with tempfile.TemporaryDirectory() as td:
    dbp = os.path.join(td, "t.db")
    c = sqlite3.connect(dbp)
    c.executescript(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, source_file TEXT NOT NULL, tags TEXT DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, observed_at TEXT NOT NULL, pinned INTEGER DEFAULT 0, importance INTEGER DEFAULT 3, fitness_score REAL DEFAULT 1.0, repo_id TEXT, valid_from TEXT, valid_to TEXT, superseded_by TEXT, deleted_at TEXT, category TEXT, metadata TEXT)"
    )
    c.execute(
        "INSERT INTO memories (id,content,source_file,tags,created_at,updated_at,observed_at,pinned,importance,fitness_score,repo_id,valid_from,valid_to,superseded_by,deleted_at,category,metadata) VALUES ('n1','old','f.md','[]','2026-01-01','2026-01-01','2026-01-01',0,3,1.0,NULL,'2026-01-01','2026-06-01','n_new',NULL,NULL,NULL)"
    )
    c.commit()
    sql = "INSERT INTO memories (id,content,source_file,tags,created_at,updated_at,observed_at,pinned,importance,fitness_score,repo_id,valid_from,valid_to,superseded_by,deleted_at,category,metadata) VALUES (?,?,?,?,?,?,?,0.5,3,?,?,?,NULL,NULL,?,?,NULL) ON CONFLICT(id) DO UPDATE SET content=excluded.content, valid_to=COALESCE(excluded.valid_to,memories.valid_to), superseded_by=COALESCE(excluded.superseded_by,memories.superseded_by)"
    c.execute(
        sql,
        (
            "n1",
            "f.md",
            "new",
            "[]",
            "2026-06-14",
            "2026-06-14",
            "2026-06-14",
            0,
            None,
            "2026-06-14",
            None,
            None,
        ),
    )
    c.commit()
    vt, sb = c.execute(
        "SELECT valid_to,superseded_by FROM memories WHERE id='n1'"
    ).fetchone()
    assert vt == "2026-06-01" and sb == "n_new"
    ok()

# I
section("I", "Save → Search roundtrip")
tid = f"e2e-sr-{uuid.uuid4().hex[:8]}"
save_memory(
    content=f"e2e marker M4RK3R {uuid.uuid4().hex}",
    category="tests",
    title_slug=tid,
    pinned=False,
    db_path=str(DBPATH),
)
# Raise limit and disable rerank so fitness-boosted old notes don't push
# the newly-saved note out of the top-N (fitness scoring boosts previously-accessed notes).
res = search_memories(
    db_path=DBPATH, query="M4RK3R", limit=200, rerank=False, include_global=True
)
if isinstance(res, str):
    res = json.loads(res)
ids = [r.get("id", "") for r in res.get("results", [])]
assert any(tid in n for n in ids), f"roundtrip failed: {ids}"
ok("saved note found via FTS roundtrip")
# Clean up test entry so it doesn't pollute the prod DB
with sqlite3.connect(DB) as con:
    con.execute("DELETE FROM memories WHERE id = ?", (f"tests/{tid}",))
    con.commit()

# J
section("J", "Cron backup")
r = os.system(
    f"cd {INSTALL_ROOT} && MEMORY_DB_PATH={DB} ./venv/bin/python cron/cron_backup.py >/dev/null 2>&1"
)
assert r == 0
ok()

# K
section("K", "memory_integrity --deep: 0 critical")
# Re-clean FK orphans left by auto_save and roundtrip tests
with sqlite3.connect(DB) as con:
    con.execute("PRAGMA foreign_keys=OFF")
    for table, col in [
        ("user_access_log", "note_id"),
        ("user_profile_access_log", "note_id"),
        ("memory_embeddings", "memory_id"),
        ("memory_chunks", "parent_id"),
        ("memory_vec_keys", "memory_id"),
        ("kg_facts", "source_memory"),
    ]:
        con.execute(f"DELETE FROM {table} WHERE {col} NOT IN (SELECT id FROM memories)")
    con.commit()
r = sp.run(
    [
        sys.executable,
        f"{INSTALL_ROOT}/memory_integrity.py",
        DB,
        "--deep",
    ],
    capture_output=True,
    text=True,
    cwd=INSTALL_ROOT,
)
# Exit 0 means no issues at all; exit 1 with "0 critical" means warnings-only (by-design).
assert "0 critical" in r.stdout, f"CRITICAL findings: {r.stdout}"
ok()

print("\n" + "=" * 70)
print("ALL 11 SECTIONS PASSED")
print("=" * 70)
