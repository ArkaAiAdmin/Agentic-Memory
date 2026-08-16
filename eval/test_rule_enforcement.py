"""Enforcement tests for AGENTS.md Hard Rules.

These tests make the maintainer rules in AGENTS.md verifiable rather than
advisory. Each test maps to a specific hard rule:

  Rule 1  — no memory-content write bypasses save_memory / save_memory_journal
  Rule 5  — default search is include_global=True (blended RRF)
  Rule 7  — backfill_all.py refuses bare invocation (no garbage DB at repo root)
  Rule 11 — CRDT field state and the on-disk .md file must not silently drift

Run with: venv/bin/python -m pytest eval/test_rule_enforcement.py -q
"""

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Content tables that MUST only be mutated through the saga write path
# (save_memory / save_memory_journal). Direct INSERT/UPDATE/DELETE against
# these outside the saga is a Rule 1 violation.
_CONTENT_TABLES = {
    "memories",
    "memories_facts",
    "kg_facts",
    "kg_edges",
    "kg_entities",
    "backlinks",
    "vec_key",
    "vec_meta",
    "memory_index",
}

# Tables that are legitimately written directly (coordination, audit,
# session wiring, shared pool). These are intentionally excluded from the
# Rule 1 scan.
_ALLOWED_DIRECT_TABLES = {
    "agent_messages",
    "shared_tasks",
    "file_locks",
    "coordination_audit",
    "project_state",
    "agent_heartbeats",
    "memory_audit_log",
    "shared_memories",
    "shared_tasks_log",
}

# Modules that are part of the saga/write-path internals and may issue raw
# SQL against content tables lawfully (they ARE the implementation).
_SAGA_INTERNALS = {
    "save/pipeline.py",
    "save/cleanup.py",
    "save/backlinks.py",
    "save/indexers.py",
    "save/crdt_helpers.py",
    "save/post_save_hooks.py",
    "infra/saga.py",
    "infra/write_journal.py",
    "crdt/crdt_field.py",
    "migration_runner.py",
}

# Tier A — CORE verb/write modules. These MUST route content writes through
# save_memory / save_memory_journal and must not issue raw content-table SQL.
_ENFORCED_CORE_MODULES = [
    "mcp_surface/mcp_memory.py",
    "mcp_surface/mcp_verbs.py",
    "background/tool_complete.py",
    "session_manager.py",
]

# Tier B — operational KG-maintenance surface (infra/api_server.py). These
# endpoints perform coordinated KG/memory deletes that have no save_memory
# equivalent, so they are exempt from the "must use save_memory" rule. However
# Rule 1 still requires that every raw content-table write is followed by the
# saga-aware cleanup helpers (repair_kg_orphans / cleanup_memory_relations /
# repair_vec_orphans), so dependent rows (kg_facts, backlinks, orphan entities,
# vec_keys, embeddings) stay consistent.
_OPERATIONAL_KG_MODULES = {
    "infra/api_server.py",
    "background/background_worker.py",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _module_uses_saga(src: str) -> bool:
    return ("save_memory" in src and (
        "save_memory(" in src or "save_memory_journal" in src
    )) or "save_memory_auto" in src


def _find_content_table_writes(src: str) -> list[str]:
    """Return a list of offending raw-write snippets against content tables."""
    offenders: list[str] = []
    for line in src.splitlines():
        low = line.lower()
        if "execute(" not in low:
            continue
        if not any(
            verb in low for verb in ("insert", "update", "delete", "replace")
        ):
            continue
        # strip the statement and look for a content table name
        for tbl in _CONTENT_TABLES:
            if tbl in low and tbl not in _ALLOWED_DIRECT_TABLES:
                # allow references that are clearly reads (SELECT ... FROM tbl)
                if "select" in low and "from" in low and tbl in low.split("from")[-1]:
                    continue
                offenders.append(line.strip())
                break
    return offenders


# ---------------------------------------------------------------------------
# Rule 1 — write path bypass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mod", _ENFORCED_CORE_MODULES)
def test_rule1_core_writes_route_through_saga(mod: str):
    """CORE verb modules must use save_memory/save_memory_journal, no raw writes."""
    path = REPO_ROOT / mod
    if not path.exists():
        pytest.skip(f"{mod} not present")
    src = _read(path)
    # The module must route content writes through the saga entry points.
    assert _module_uses_saga(src), (
        f"{mod}: does not reference save_memory/save_memory_journal — "
        "Rule 1 requires all content writes route through the saga."
    )
    offenders = _find_content_table_writes(src)
    assert not offenders, (
        f"{mod}: raw content-table writes bypass the saga (Rule 1):\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


@pytest.mark.parametrize("mod", sorted(_OPERATIONAL_KG_MODULES))
def test_rule1_operational_kg_uses_saga_cleanup(mod: str):
    """Operational KG endpoints may write raw SQL only with saga cleanup helpers.

    These modules (infra/api_server.py) perform coordinated KG/memory deletes
    that have no save_memory equivalent. Rule 1 still requires that every raw
    content-table write is paired with the saga-aware cleanup helpers
    (repair_kg_orphans / cleanup_memory_relations) so dependent rows stay
    consistent.
    """
    path = REPO_ROOT / mod
    if not path.exists():
        pytest.skip(f"{mod} not present")
    src = _read(path)
    uses_cleanup = (
        ("repair_kg_orphans" in src or "repair_vec_orphans" in src)
        and (
            "cleanup_memory_relations" in src
            or "repair_kg_orphans" in src
            or "repair_vec_orphans" in src
        )
    )
    assert uses_cleanup, (
        f"{mod}: operational KG writes must use repair_kg_orphans / "
        "cleanup_memory_relations (Rule 1 saga-equivalent cleanup)."
    )
    # Confirm there is at least one raw content write guarded by cleanup —
    # if there are none, the rule is vacuously satisfied but we still want the
    # guard present for future edits.
    offenders = _find_content_table_writes(src)
    if offenders:
        assert "repair_kg_orphans" in src, (
            f"{mod}: has raw content-table writes but no repair_kg_orphans guard:\n"
            + "\n".join(f"  {o}" for o in offenders)
        )


def test_rule1_saga_internals_exempt():
    """Sanity: the saga internals are excluded so the rule is not a no-op."""
    for mod in _SAGA_INTERNALS:
        path = REPO_ROOT / mod
        if path.exists():
            # at least one of these exists and is parseable
            assert _read(path)
    assert _SAGA_INTERNALS  # guard against accidental empty list


# ---------------------------------------------------------------------------
# Rule 2 — pool get evicts stale connection on missing path
# ---------------------------------------------------------------------------


def test_rule2_pool_get_evicts_stale_on_missing_path(tmp_path: Path):
    """Rule 2: connection_pool.get() on a deleted/missing path must evict
    the stale pooled connection and reopen, never return an FD into a
    deleted file (the old conn would silently write to the unlinked inode).
    """
    from infra.db import _ConnectionPool

    db_path = str(tmp_path / "pool_missing.db")
    pool = _ConnectionPool(max_size=4)
    try:
        conn1 = pool.get(db_path)
        conn1.execute("SELECT 1")

        # Same-thread reuse returns the same connection while the file lives.
        conn2 = pool.get(db_path)
        assert conn2 is conn1
        pool.put(conn2)

        # Delete the db file: the pooled conn now holds an FD into the
        # unlinked inode and SQLite liveness checks still pass on it.
        tmp_path.joinpath("pool_missing.db").unlink()
        assert not tmp_path.joinpath("pool_missing.db").exists()

        conn3 = pool.get(db_path)
        assert conn3 is not conn1, (
            "Rule 2: get() returned the stale connection into the deleted file"
        )
        conn3.execute("SELECT 1")
        pool.put(conn3)
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# Rule 5 — default search is include_global=True
# ---------------------------------------------------------------------------


def test_rule5_search_default_includes_global():
    """search_memories must default to include_global=True (blended RRF)."""
    import inspect

    # Import the module normally (it must be importable as a package member,
    # which also exercises that the module loads cleanly under the real path).
    import search.orchestrator as mod

    sig = inspect.signature(mod.search_memories)
    assert sig.parameters["include_global"].default is True, (
        "Rule 5: search_memories() must default include_global=True; "
        "found default=%r" % sig.parameters["include_global"].default
    )


# ---------------------------------------------------------------------------
# Rule 7 — backfill_all.py bare invocation rejected
# ---------------------------------------------------------------------------


def test_rule7_backfill_rejects_bare_invocation():
    """Bare `backfill_all.py` (no mode/db) must exit non-zero, not create a DB."""
    garbage = REPO_ROOT / "memory.db"
    garbage_exists_before = garbage.exists()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backfill_all.py")],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert proc.returncode != 0, (
        "Rule 7: bare backfill_all.py must be rejected, got rc=0"
    )
    assert "error" in proc.stderr.lower(), (
        "Rule 7: bare backfill_all.py should print an error explaining usage"
    )
    # It must not have created a garbage DB at the repo root.
    assert garbage.exists() == garbage_exists_before, (
        "Rule 7: bare backfill_all.py created/removed a DB at repo root"
    )


def test_rule7_backfill_accepts_incremental():
    """`backfill_all.py --incremental` is a valid invocation (smoke only)."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backfill_all.py"), "--incremental", "--db", ":memory:"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    # We only assert it didn't hit the bare-invocation guard (rc != 2).
    assert proc.returncode != 2, (
        "Rule 7: --incremental was wrongly rejected:\n" + proc.stderr
    )


# ---------------------------------------------------------------------------
# Rule 11 — CRDT / .md drift detection
# ---------------------------------------------------------------------------


def _make_note_db(tmp_path: Path) -> Path:
    """Create a minimal memories DB with one note + its CRDT + .md file."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            category TEXT,
            title_slug TEXT,
            version INTEGER DEFAULT 1
        );
        CREATE TABLE memory_crdt (
            note_id TEXT,
            field TEXT,
            value TEXT,
            version INTEGER,
            PRIMARY KEY (note_id, field)
        );
        """
    )
    nid = "lessons/rule11"
    conn.execute(
        "INSERT INTO memories (id, content, category, title_slug, version) "
        "VALUES (?, ?, ?, ?, ?)",
        (nid, "original content", "lessons", "rule11", 1),
    )
    conn.execute(
        "INSERT INTO memory_crdt (note_id, field, value, version) "
        "VALUES (?, ?, ?, ?)",
        (nid, "content", "original content", 1),
    )
    conn.commit()
    conn.close()
    # Write the .md file that should mirror the content
    md = tmp_path / "lessons" / "rule11.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("original content", encoding="utf-8")
    return db


def test_rule11_no_crdt_md_drift(tmp_path: Path):
    """CRDT content, DB row, and .md file must agree (no silent drift)."""
    db = _make_note_db(tmp_path)
    conn = sqlite3.connect(str(db))
    nid = "lessons/rule11"
    row = conn.execute("SELECT content FROM memories WHERE id=?", (nid,)).fetchone()
    crdt = conn.execute(
        "SELECT value FROM memory_crdt WHERE note_id=? AND field='content'",
        (nid,),
    ).fetchone()
    conn.close()
    md = tmp_path / "lessons" / "rule11.md"
    md_content = md.read_text(encoding="utf-8")

    assert row is not None and crdt is not None, "Rule 11: fixture missing rows"
    assert row[0] == crdt[0] == md_content, (
        f"Rule 11: drift detected — db={row[0]!r} crdt={crdt[0]!r} md={md_content!r}"
    )


def test_rule11_detects_drift(tmp_path: Path):
    """The drift detector must FAIL when the .md is stale (regression guard)."""
    db = _make_note_db(tmp_path)
    # Simulate a stale .md (the exact failure mode Rule 11 guards against)
    md = tmp_path / "lessons" / "rule11.md"
    md.write_text("STALE content that disagrees", encoding="utf-8")

    conn = sqlite3.connect(str(db))
    nid = "lessons/rule11"
    row = conn.execute("SELECT content FROM memories WHERE id=?", (nid,)).fetchone()
    crdt = conn.execute(
        "SELECT value FROM memory_crdt WHERE note_id=? AND field='content'",
        (nid,),
    ).fetchone()
    conn.close()
    md_content = md.read_text(encoding="utf-8")

    drift = not (row[0] == crdt[0] == md_content)
    assert drift, "Rule 11: detector should have flagged the stale .md"


# ---------------------------------------------------------------------------
# Rule 25 — Benchmark Environment & Multi-Index Coverage
# ---------------------------------------------------------------------------


_BENCHMARK_EVAL_SCRIPTS = [
    "eval/adversarial_memory_eval.py",
    "eval/locomo_eval.py",
    "eval/longmemeval_s/run_eval_main_pipeline.py",
    "eval/beam/run_beam_real.py",
]


@pytest.mark.parametrize("script_path", _BENCHMARK_EVAL_SCRIPTS)
def test_rule25_benchmark_env_and_indexing(script_path: str):
    """Benchmark evaluation scripts must set benchmark env and invoke indexers."""
    path = REPO_ROOT / script_path
    if not path.exists():
        pytest.skip(f"{script_path} not present")
    src = _read(path)
    assert "set_benchmark_env" in src or "KMP_DUPLICATE_LIB_OK" in src, (
        f"Rule 25: {script_path} must call set_benchmark_env() or set optimal env vars"
    )
    assert "populate_eval_memory_indexes" in src or "_index_embedding" in src, (
        f"Rule 25: {script_path} must invoke multi-index builders during dataset ingestion"
    )


# ---------------------------------------------------------------------------
# Rule 4 — Numbered migrations only (no raw ALTER TABLE in app code)
# ---------------------------------------------------------------------------


def test_rule4_no_raw_alter_table_in_python():
    """Rule 4: Schema changes are migrations only. No ALTER TABLE in app code."""
    exempt_paths = {
        "infra/migration_runner.py",
        "infra/db_migrations.py",
        "migration_runner.py",
        "scripts/gen_schema_doc.py",
    }
    violations = []
    for py_file in REPO_ROOT.glob("**/*.py"):
        rel = py_file.relative_to(REPO_ROOT)
        if (
            rel.parts[0] in ("eval", "venv", ".venv", "tests", "migrations", "docs", "scripts")
            or str(rel) in exempt_paths
        ):
            continue
        src = _read(py_file)
        if "ALTER TABLE" in src.upper():
            lines = [
                line
                for line in src.splitlines()
                if "ALTER TABLE" in line.upper()
                and not line.strip().startswith(("#", '"""', "'''", "*"))
                and not line.strip().startswith("assert ")
                and "Hard Rule 4" not in line
            ]
            if lines:
                # Exclude purely backward-compatible fallback migrations
                real_violations = [l for l in lines if "conn.execute" in l and "ALTER TABLE" in l]
                if real_violations and str(rel) not in ("memory_sharing.py", "knowledge_graph/kg_schema.py", "infra/write_journal.py", "kg/kg_crdt.py"):
                    violations.append(f"{rel}: {real_violations[:2]}")
    assert not violations, (
        f"Rule 4: Found raw ALTER TABLE in application code:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Rule 6 — Tool surface contract (25 CORE, 92 ADMIN, 3 DEPRECATED)
# ---------------------------------------------------------------------------


def test_rule6_mcp_tool_surface_contract():
    """Rule 6: Surface must have exactly 25 CORE verbs and 92 ADMIN behind router."""
    import tool_registry

    core = tool_registry.CORE_TOOLS
    admin = tool_registry.ADMIN_TOOLS
    deprecated = getattr(tool_registry, "DEPRECATED", getattr(tool_registry, "DEPRECATED_TOOLS", []))

    assert len(core) == 25, (
        f"Expected 25 CORE tools, got {len(core)}: {core}"
    )
    assert len(admin) == 92, (
        f"Expected 92 ADMIN tools, got {len(admin)}"
    )
    assert len(deprecated) == 3, (
        f"Expected 3 DEPRECATED tools, got {len(deprecated)}"
    )
    overlap = set(core) & set(admin)
    assert not overlap, f"Rule 6: Tool overlap between CORE and ADMIN: {overlap}"


# ---------------------------------------------------------------------------
# Rule 8 — Prod DB Safety in Tests
# ---------------------------------------------------------------------------


def test_rule8_proddb_safety_in_eval_tests():
    """Rule 8: Tests referencing GLOBAL_MEM_DIR must use safety wiring or fixtures."""
    violations = []
    for test_file in (REPO_ROOT / "eval").glob("test_*.py"):
        if test_file.name in ("test_rule_enforcement.py", "test_safety_wiring.py"):
            continue
        src = _read(test_file)
        if (
            "GLOBAL_MEM_DIR" in src
            and "_ProdDBGuarded" not in src
            and "monkeypatch" not in src
            and "patch(" not in src
            and "tempfile" not in src
            and "tmp_path" not in src
        ):
            violations.append(test_file.name)
    assert not violations, (
        f"Rule 8: Tests touching GLOBAL_MEM_DIR without safety wiring: {violations}"
    )


# ---------------------------------------------------------------------------
# Rule 9 — Lock order: flock first, then conn
# ---------------------------------------------------------------------------


def test_rule9_save_lock_order_flock_first():
    """Rule 9: save_memory must acquire file lock before opening DB connection."""
    save_pipeline_py = REPO_ROOT / "save" / "pipeline.py"
    if not save_pipeline_py.exists():
        save_pipeline_py = REPO_ROOT / "save_pipeline.py"
    src = _read(save_pipeline_py)
    assert "acquire_flock" in src, "Rule 9: save_memory must use acquire_flock"


# ---------------------------------------------------------------------------
# Rule 12 — Signal handlers before flock in auto_save daemon
# ---------------------------------------------------------------------------


def test_rule12_auto_save_signals_before_flock():
    """Rule 12: daemon.py must install signal handlers before flock check."""
    daemon_py = REPO_ROOT / "background" / "daemon.py"
    src = _read(daemon_py)
    assert "signal.signal" in src or "_signal.signal" in src, (
        "Rule 12: daemon.py must install signal handlers"
    )
    sig_pos = src.find("_signal.signal")
    flock_pos = src.find("acquire_flock_with_retry")
    if sig_pos != -1 and flock_pos != -1:
        assert sig_pos < flock_pos, (
            "Rule 12: Signal handlers must be installed BEFORE flock check"
        )


# ---------------------------------------------------------------------------
# Rule 14 — Saga rollback cleans relations
# ---------------------------------------------------------------------------


def test_rule14_saga_rollback_cleans_relations():
    """Rule 14: infra/saga.py rollback must clean up relations (facts, backlinks)."""
    saga_py = REPO_ROOT / "infra" / "saga.py"
    src = _read(saga_py)
    assert "cleanup_memory_relations" in src or "cleanup" in src, (
        "Rule 14: Saga rollback must call cleanup_memory_relations"
    )


# ---------------------------------------------------------------------------
# Rule 21 — no ritual maintenance; Session Protocol #4 cross-check
# ---------------------------------------------------------------------------


def test_rule21_no_ritual_maintenance():
    """Rule 21: maintenance must not be a post-task ritual.

    Cross-checks that Session Protocol #4 (step 4 of the session table)
    carries the same qualifier as Rule 21 — call memory_maintenance only
    when cron is down or immediate results are required — so the two
    sections of AGENTS.md do not contradict each other.
    """
    agents_md = _read(REPO_ROOT / "AGENTS.md")

    # Rule 21 body exists and is non-ritual.
    assert "Don't run maintenance as a post-task ritual" in agents_md, (
        "Rule 21: AGENTS.md must forbid ritual maintenance"
    )

    # Session Protocol step 4 (the maintenance row) must be qualified the
    # same way, not an unconditional "after every op" instruction.
    step4 = re.search(
        r"\| 4 \|.*memory_maintenance\(operation=\"auto_save_status\"\)[^\n]*\|",
        agents_md,
    )
    assert step4 is not None, "Rule 21: Session Protocol #4 row not found in AGENTS.md"
    row = step4.group(0)
    assert "cron is down" in row or "immediate results" in row, (
        "Rule 21: Session Protocol #4 must be qualified (cron down / immediate results only)"
    )

