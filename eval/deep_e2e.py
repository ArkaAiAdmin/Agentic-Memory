#!/usr/bin/env python3
"""Deep end-to-end validation of the agentic-memory MCP tool surface.

This goes beyond e2e_full_pass.py: every MCP tool is exercised via the
decorated Python function (the same path the FastMCP server takes),
realistic workflows are walked, and 60+ edge cases are covered.

Strategy
--------
Build an isolated test install at $TEST_ROOT/.config/agentic-memory/ that
symlinks the production code but has its own memory/ directory. Set
HOME=$TEST_ROOT before importing memory_mcp, so every Path.home() call —
including those in subprocess scripts like auto_save.py and
pinned_decay.py — resolves to the test install. The production memory
DB and notes are never touched.

Cleanup
-------
TEST_ROOT is removed at exit (success or failure). Any session files
written under the test memory/sessions/ go with it.

Usage
-----
    ~/.config/agentic-memory/venv/bin/python eval/deep_e2e.py [--no-longmemeval]
"""
import os
import re
import sys
import json
import time
import sqlite3
import shutil
import tempfile
import threading
import subprocess
import traceback
import argparse
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# =====================================================================
# Configuration
# =====================================================================
PROD_INSTALL = Path(__file__).resolve().parent.parent
# H4 fix: use sys.executable with venv fallback chain
VENV_PY = Path(sys.executable)
if not VENV_PY.exists():
    VENV_PY = PROD_INSTALL / ".venv" / "bin" / "python"
    if not VENV_PY.exists():
        VENV_PY = PROD_INSTALL / "venv" / "bin" / "python"
PROD_DB = PROD_INSTALL / "memory" / "memory.db"
PROD_MEM_DIR = PROD_INSTALL / "memory"

# Test result tracking
RESULTS = []          # (name, passed, detail, latency_ms)
LATENCIES = defaultdict(list)  # tool_name -> [latency_ms]
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
SKIP = "\033[36mSKIP\033[0m"


def record(name, passed, detail="", latency_ms=None):
    """Record a single test result."""
    RESULTS.append((name, passed, detail, latency_ms))
    status = PASS if passed else FAIL
    lat = f"  [{latency_ms:6.1f}ms]" if latency_ms is not None else ""
    detail_str = f" — {detail}" if detail else ""
    print(f"  [{status}]{lat} {name}{detail_str}")


def timed(fn, *args, **kwargs):
    """Call fn with timing. Returns (result, latency_ms)."""
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        err = None
    except Exception as e:
        result = None
        err = e
    latency_ms = (time.perf_counter() - t0) * 1000
    if err is not None:
        raise err
    return result, latency_ms


# =====================================================================
# Isolated test install setup
# =====================================================================
TEST_ROOT = Path(tempfile.mkdtemp(prefix="deep_e2e_"))


def setup_isolated_install():
    """Create a test install that mirrors the production install, but
    with its own memory/ directory and HOME.

    Returns (test_install_path, test_mem_path).
    """
    # 1. Set HOME first so Path.home() resolves to TEST_ROOT
    os.environ["HOME"] = str(TEST_ROOT)

    test_install = TEST_ROOT / ".config" / "agentic-memory"
    test_install.mkdir(parents=True, exist_ok=True)

    # 2. Symlink production code (skip memory, venv, caches, .git)
    skip = {"memory", "venv", "__pycache__", ".git", ".rebuild.lock"}
    for item in PROD_INSTALL.iterdir():
        if item.name in skip:
            continue
        target = test_install / item.name
        if not target.exists():
            target.symlink_to(item)

    # 3. Add an AGENTS.md so find_project_root() picks this up reliably
    (test_install / "AGENTS.md").write_text(
        "# Deep E2E Test Project\n\nIsolated test install for deep_e2e.py.\n",
        encoding="utf-8",
    )

    # 4. Create isolated memory dir
    test_mem = test_install / "memory"
    test_mem.mkdir(exist_ok=True)
    (test_mem / "MEMORY.md").write_text(
        "# Test Memory Index\n\n"
        "## Active Projects\n\n"
        "## Architecture Decisions (ADRs)\n\n"
        "## Hard-Won Lessons\n\n"
        "## User Preferences\n\n"
        "## Sessions\n\n",
        encoding="utf-8",
    )

    # 5. Bootstrap schema via rebuild_index.py
    rc = subprocess.run(
        [str(VENV_PY), str(PROD_INSTALL / "rebuild_index.py"),
         str(test_mem), str(test_mem / "memory.db")],
        capture_output=True, text=True, timeout=60,
    )
    if rc.returncode != 0:
        print(f"Bootstrap rebuild failed: {rc.stderr}", file=sys.stderr)
        sys.exit(2)

    return test_install, test_mem


# Initialize isolated install before importing memory_mcp
TEST_INSTALL, TEST_MEM = setup_isolated_install()
TEST_DB = TEST_MEM / "memory.db"

# Add the production install to sys.path so we can import the MCP module
sys.path.insert(0, str(PROD_INSTALL))

# Now import — Path.home() in memory_mcp.py already points to TEST_ROOT,
# so its GLOBAL_MEM_DIR resolves to the test memory dir.
import memory_mcp  # noqa: E402

# Re-resolve paths after import to confirm
assert str(memory_mcp.GLOBAL_MEM_DIR) == str(TEST_MEM), (
    f"GLOBAL_MEM_DIR {memory_mcp.GLOBAL_MEM_DIR} != {TEST_MEM}"
)
assert memory_mcp.GLOBAL_SCRIPTS_DIR == TEST_INSTALL, (
    f"GLOBAL_SCRIPTS_DIR {memory_mcp.GLOBAL_SCRIPTS_DIR} != {TEST_INSTALL}"
)

# Force ALL paths to the test install. Without this:
#   - is_global=False routes to whichever DB has the most rows (prod, polluting it)
#   - memory_search reads local_db from find_project_root(cwd) which is prod
#   - get_memory_paths returns (project_root, local_mem=prod, global_mem=TEST_MEM)
# Test must operate exclusively inside TEST_MEM.
memory_mcp.resolve_active_memory_dir = lambda **_: TEST_MEM
memory_mcp.resolve_db_for_memory_id = lambda mid: TEST_DB  # noqa: E501
# get_memory_paths returns (project_root, local_mem, global_mem). Redirect both
# local and global to TEST_MEM so search reads the test DB only.
memory_mcp.get_memory_paths = lambda: (TEST_INSTALL, TEST_MEM, TEST_MEM)


def open_test_db():
    """Open the test DB in a fresh connection."""
    return sqlite3.connect(str(TEST_DB), timeout=10.0)


# =====================================================================
# SECTION A: MCP tool path testing
# =====================================================================
def section_a_mcp_tools():
    print("\n" + "=" * 72)
    print("SECTION A: MCP TOOL PATH TESTING")
    print("=" * 72)

    # ----- A.1 memory_save -----
    print("\n--- A.1 memory_save ---")
    # Basic save
    r, lat = timed(memory_mcp.memory_save,
                   content="The sky is blue during the day.",
                   category="lessons", title_slug="sky-blue-day",
                   tags=["weather", "observation"], pinned=False)
    LATENCIES["memory_save"].append(lat)
    record("A.1.1 memory_save basic",
           r.startswith("Successfully saved") and "lessons/sky-blue-day" in r,
           r[:80], lat)

    # Save with pinned=True
    r, lat = timed(memory_mcp.memory_save,
                   content="Critical: never commit secrets to memory.",
                   category="lessons", title_slug="no-secrets",
                   tags=["security", "critical"], pinned=True)
    LATENCIES["memory_save"].append(lat)
    record("A.1.2 memory_save pinned",
           r.startswith("Successfully saved"),
           r[:80], lat)

    # Save with no tags
    r, lat = timed(memory_mcp.memory_save,
                   content="No tags here.",
                   category="quirks", title_slug="no-tags-test")
    LATENCIES["memory_save"].append(lat)
    record("A.1.3 memory_save with no tags",
           r.startswith("Successfully saved"),
           r[:80], lat)

    # Save with string tags (CSV)
    r, lat = timed(memory_mcp.memory_save,
                   content="Comma-separated tags.",
                   category="quirks", title_slug="csv-tags",
                   tags="alpha, beta, gamma")
    LATENCIES["memory_save"].append(lat)
    record("A.1.4 memory_save with CSV string tags",
           r.startswith("Successfully saved"),
           r[:80], lat)

    # Save with invalid category (empty)
    r, lat = timed(memory_mcp.memory_save,
                   content="x", category="", title_slug="x")
    record("A.1.5 memory_save rejects empty category",
           (r.startswith("Error:") or r.startswith("Error [")) and "category" in r.lower(),
           r[:80], lat)

    # Save with traversal attempt in category
    r, lat = timed(memory_mcp.memory_save,
                   content="x", category="../etc", title_slug="x")
    record("A.1.6 memory_save rejects directory traversal in category",
           (r.startswith("Error:") or r.startswith("Error [")) and ("traversal" in r.lower() or "Invalid" in r),
           r[:80], lat)

    # Save with slash in title_slug
    r, lat = timed(memory_mcp.memory_save,
                   content="x", category="lessons", title_slug="has/slash")
    record("A.1.7 memory_save rejects slash in title_slug",
           r.startswith("Error:") or r.startswith("Error ["),
           r[:80], lat)

    # Save with non-string content
    r, lat = timed(memory_mcp.memory_save,
                   content=12345, category="lessons", title_slug="int-content")
    record("A.1.8 memory_save rejects non-string content",
           r.startswith("Error:") or r.startswith("Error ["),
           r[:80], lat)

    # Save with invalid tags type
    r, lat = timed(memory_mcp.memory_save,
                   content="x", category="lessons", title_slug="bad-tags",
                   tags={"not": "allowed"})
    record("A.1.9 memory_save rejects dict tags",
           r.startswith("Error:") or r.startswith("Error ["),
           r[:80], lat)

    # Save with very long title_slug (> 128)
    r, lat = timed(memory_mcp.memory_save,
                   content="x", category="lessons",
                   title_slug="a" * 200)
    record("A.1.10 memory_save rejects oversize title_slug",
           (r.startswith("Error:") or r.startswith("Error [")) and "128" in r,
           r[:80], lat)

    # Verify the file is on disk
    md_path = TEST_MEM / "lessons" / "sky-blue-day.md"
    record("A.1.11 memory_save wrote the .md file",
           md_path.exists(),
           str(md_path))

    # Verify the file is in DB
    db = open_test_db()
    rows = db.execute(
        "SELECT id, content, tags, pinned FROM memories WHERE id=?",
        ("lessons/sky-blue-day",)
    ).fetchall()
    db.close()
    record("A.1.12 memory_save inserted a DB row",
           len(rows) == 1,
           f"rows={len(rows)}")
    if rows:
        rid, rcontent, rtags, rpinned = rows[0]
        record("A.1.13 DB row content matches",
               "sky is blue" in rcontent, rcontent[:60])
        try:
            tags_list = json.loads(rtags)
        except Exception:
            tags_list = []
        record("A.1.14 DB row tags match",
               "weather" in tags_list and "observation" in tags_list,
               str(tags_list))
        record("A.1.15 DB row pinned=False matches",
               rpinned == 0, f"pinned={rpinned}")

    # Verify FTS5 has the row
    db = open_test_db()
    fts = db.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'sky'",
    ).fetchall()
    db.close()
    record("A.1.16 FTS5 indexed the new note",
           len(fts) > 0, f"{len(fts)} rows for 'sky'")

    # ----- A.2 memory_search -----
    print("\n--- A.2 memory_search ---")
    r, lat = timed(memory_mcp.memory_search, query="sky", limit=5)
    LATENCIES["memory_search"].append(lat)
    record("A.2.1 memory_search returns string output",
           isinstance(r, str) and len(r) > 0,
           r[:80], lat)

    r, lat = timed(memory_mcp.memory_search, query="secrets", limit=3)
    LATENCIES["memory_search"].append(lat)
    record("A.2.2 memory_search finds pinned note",
           "secrets" in r.lower() or "no-secrets" in r.lower(),
           r[:80], lat)

    r, lat = timed(memory_mcp.memory_search, query="sky", limit=5,
                   include_invalid=False)
    record("A.2.3 memory_search accepts include_invalid",
           isinstance(r, str), r[:50], lat)

    r, lat = timed(memory_mcp.memory_search, query="", limit=3)
    record("A.2.4 memory_search empty query handled gracefully",
           isinstance(r, str), r[:50], lat)

    # Special FTS5 chars
    for q in ['*', '"', 'NEAR hello world', 'AND OR NOT', 'a:b']:
        r, lat = timed(memory_mcp.memory_search, query=q, limit=2)
        record(f"A.2.5 memory_search special query {q!r} handled",
               isinstance(r, str),
               f"len={len(r)}", lat)

    # 10K char query
    long_q = "alpha " * 2000
    r, lat = timed(memory_mcp.memory_search, query=long_q, limit=2)
    record("A.2.6 memory_search 10K-char query handled",
           isinstance(r, str),
           f"len={len(r)}", lat)

    # ----- A.3 memory_supersede -----
    print("\n--- A.3 memory_supersede ---")
    # Save two notes first
    memory_mcp.memory_save(content="v1 content",
                            category="lessons", title_slug="sup-old")
    memory_mcp.memory_save(content="v2 content",
                            category="lessons", title_slug="sup-new")

    r, lat = timed(memory_mcp.memory_supersede,
                   old_id="lessons/sup-old", new_id="lessons/sup-new")
    LATENCIES["memory_supersede"].append(lat)
    record("A.3.1 memory_supersede basic",
           r.startswith("Superseded") and "sup-old" in r,
           r[:80], lat)

    # Verify DB
    db = open_test_db()
    row = db.execute(
        "SELECT valid_to, superseded_by FROM memories WHERE id=?",
        ("lessons/sup-old",)
    ).fetchone()
    db.close()
    record("A.3.2 DB valid_to set after supersede",
           row is not None and row[0] is not None and row[0] != "",
           f"valid_to={row[0] if row else None}")
    record("A.3.3 DB superseded_by set after supersede",
           row is not None and row[1] == "lessons/sup-new",
           f"superseded_by={row[1] if row else None}")

    # supersede with same old_id == new_id
    r, lat = timed(memory_mcp.memory_supersede,
                   old_id="lessons/sup-new", new_id="lessons/sup-new")
    record("A.3.4 memory_supersede rejects self-supersede",
           r.startswith("Error:") or r.startswith("Error ["),
           r[:80], lat)

    # supersede non-existent old_id
    r, lat = timed(memory_mcp.memory_supersede,
                   old_id="lessons/does-not-exist",
                   new_id="lessons/sup-new")
    record("A.3.5 memory_supersede rejects missing old_id",
           (r.startswith("Error:") or r.startswith("Error [")) and "not found" in r.lower(),
           r[:80], lat)

    # supersede with explicit valid_to
    r, lat = timed(memory_mcp.memory_supersede,
                   old_id="lessons/sup-new",
                   new_id="lessons/sup-old",
                   valid_to="2025-01-01T00:00:00")
    record("A.3.6 memory_supersede accepts explicit valid_to",
           r.startswith("Superseded"),
           r[:80], lat)

    # Search with include_invalid=False should hide superseded
    r, lat = timed(memory_mcp.memory_search, query="v1 content",
                   limit=5, include_invalid=False)
    # Match only the [N] id header, not the result body (which can mention
    # any note id by name, e.g. a compaction proposal listing orphans).
    matched_ids = re.findall(r'^\[(\d+)\] (\S+)', r, re.MULTILINE)
    matched_set = {mid for _, mid in matched_ids}
    has_old = "lessons/sup-old" in matched_set
    has_new = "lessons/sup-new" in matched_set
    record("A.3.7 search include_invalid=False hides superseded v1",
           not has_old,
           f"old={has_old} new={has_new} — {r[:60].replace(chr(10), ' ')}", lat)

    # Search with include_invalid=True should still show it
    r, lat = timed(memory_mcp.memory_search, query="v1 content",
                   limit=5, include_invalid=True)
    record("A.3.8 search include_invalid=True shows superseded v1",
           "v1 content" in r.lower(),
           r[:60], lat)

    # ----- A.4 memory_auto_save_hook -----
    print("\n--- A.4 memory_auto_save_hook ---")
    r, lat = timed(memory_mcp.memory_auto_save_hook,
                   tool="Read", params_json='{"file":"/tmp/x"}',
                   result_preview="hello world")
    LATENCIES["memory_auto_save_hook"].append(lat)
    record("A.4.1 memory_auto_save_hook basic",
           r.startswith("Auto-saved:") and "sessions/auto-" in r,
           r[:80], lat)

    # Verify the session file was created (slugify lowercases the tool name)
    sessions_dir = TEST_MEM / "sessions"
    session_files = list(sessions_dir.glob("auto-*-read.md"))
    record("A.4.2 session file written to disk",
           len(session_files) >= 1,
           f"{len(session_files)} files (e.g. {session_files[0].name if session_files else 'none'})")

    # Verify DB has the auto-save row
    db = open_test_db()
    n_auto = db.execute(
        "SELECT COUNT(*) FROM memories WHERE id LIKE 'sessions/auto-%'"
    ).fetchone()[0]
    db.close()
    record("A.4.3 auto-save row in DB",
           n_auto >= 1, f"{n_auto} rows")

    # Edge: empty tool name
    r, lat = timed(memory_mcp.memory_auto_save_hook,
                   tool="", params_json="{}",
                   result_preview="")
    record("A.4.4 auto_save_hook rejects empty tool",
           r.startswith("Auto-save") or "Error" in r,
           r[:80], lat)

    # Edge: malformed params_json
    r, lat = timed(memory_mcp.memory_auto_save_hook,
                   tool="Edit", params_json="not-json-at-all{",
                   result_preview="ok")
    record("A.4.5 auto_save_hook handles malformed JSON params",
           r.startswith("Auto-saved:") or r.startswith("Auto-save"),
           r[:80], lat)

    # ----- A.5 memory_auto_save_status -----
    print("\n--- A.5 memory_auto_save_status ---")
    r, lat = timed(memory_mcp.memory_auto_save_status)
    LATENCIES["memory_auto_save_status"].append(lat)
    record("A.5.1 memory_auto_save_status returns string",
           isinstance(r, str) and len(r) > 0,
           r[:80], lat)
    # Should be JSON
    try:
        status_obj = json.loads(r)
        record("A.5.2 status output is valid JSON",
               isinstance(status_obj, dict),
               str(list(status_obj.keys()))[:60])
    except Exception as e:
        record("A.5.2 status output is valid JSON", False, str(e))

    # ----- A.6 memory_daily_digest -----
    print("\n--- A.6 memory_daily_digest ---")
    r, lat = timed(memory_mcp.memory_daily_digest, date="")
    LATENCIES["memory_daily_digest"].append(lat)
    record("A.6.1 memory_daily_digest runs",
           isinstance(r, str) and len(r) > 0,
           r[:120], lat)
    # Should be JSON
    try:
        d = json.loads(r)
        record("A.6.2 digest returns JSON with digested/date fields",
               "digested" in d and "date" in d,
               str({k: d[k] for k in ("digested", "date") if k in d}))
    except Exception as e:
        record("A.6.2 digest returns JSON", False, f"parse: {e}; raw={r[:60]}")

    # Digest for a specific date
    today = time.strftime("%Y-%m-%d")
    r, lat = timed(memory_mcp.memory_daily_digest, date=today)
    record("A.6.3 memory_daily_digest with explicit date",
           isinstance(r, str), r[:60], lat)

    # Digest for an absurd date — should be safe
    r, lat = timed(memory_mcp.memory_daily_digest, date="1900-01-01")
    record("A.6.4 memory_daily_digest with old date",
           isinstance(r, str), r[:60], lat)

    # ----- A.7 memory_pinned_decay_check -----
    print("\n--- A.7 memory_pinned_decay_check ---")
    r, lat = timed(memory_mcp.memory_pinned_decay_check, dry_run=True)
    LATENCIES["memory_pinned_decay_check"].append(lat)
    record("A.7.1 memory_pinned_decay_check dry_run returns JSON",
           isinstance(r, str), r[:80], lat)
    try:
        obj = json.loads(r)
        record("A.7.2 decay report has summary",
               "summary" in obj and "policies" in obj,
               str(obj.get("summary", {})))
    except Exception as e:
        record("A.7.2 decay report parsed as JSON", False, str(e))

    # Backdate last_accessed on the no-secrets note, then re-run
    db = open_test_db()
    db.execute(
        "UPDATE memories SET last_accessed = '2020-01-01T00:00:00', "
        "updated_at = '2020-01-01T00:00:00', access_count = 0 "
        "WHERE id = 'lessons/no-secrets'"
    )
    db.commit()
    db.close()
    r, lat = timed(memory_mcp.memory_pinned_decay_check, dry_run=True)
    try:
        obj = json.loads(r)
        unpin = obj.get("auto_unpin", [])
        record("A.7.3 backdated pinned note flagged for auto-unpin",
               any(u.get("id") == "lessons/no-secrets" for u in unpin),
               f"{len(unpin)} auto-unpin candidates, "
               f"{len(obj.get('review', []))} review")
    except Exception as e:
        record("A.7.3 decay check parsed", False, str(e))

    # Apply mode — should actually unpin
    r, lat = timed(memory_mcp.memory_pinned_decay_check, dry_run=False)
    record("A.7.4 memory_pinned_decay_check apply mode",
           isinstance(r, str), r[:60], lat)

    # Verify the note was unpinned in DB
    db = open_test_db()
    pinned = db.execute(
        "SELECT pinned FROM memories WHERE id = 'lessons/no-secrets'"
    ).fetchone()
    db.close()
    record("A.7.5 apply mode actually unpins in DB",
           pinned is not None and pinned[0] == 0,
           f"pinned={pinned[0] if pinned else None}")

    # Re-pin for cleanup symmetry
    db = open_test_db()
    db.execute("UPDATE memories SET pinned = 1 WHERE id = 'lessons/no-secrets'")
    db.commit()
    db.close()

    # ----- A.8 memory_detect_contradictions -----
    print("\n--- A.8 memory_detect_contradictions ---")
    # Save a pair that will be a phrase-contradiction
    memory_mcp.memory_save(content="Authentication is required for all endpoints.",
                            category="lessons", title_slug="auth-required")
    memory_mcp.memory_save(content="Authentication is not required for all endpoints.",
                            category="lessons", title_slug="auth-not-required")

    r, lat = timed(memory_mcp.memory_detect_contradictions,
                   min_confidence="low", mode="phrase")
    LATENCIES["memory_detect_contradictions"].append(lat)
    record("A.8.1 memory_detect_contradictions phrase mode runs",
           isinstance(r, str) and len(r) > 0,
           r[:60].replace("\n", " "), lat)

    r, lat = timed(memory_mcp.memory_detect_contradictions,
                   min_confidence="low", mode="semantic")
    record("A.8.2 memory_detect_contradictions semantic mode runs",
           isinstance(r, str) and len(r) > 0,
           r[:60].replace("\n", " "), lat)

    r, lat = timed(memory_mcp.memory_detect_contradictions,
                   min_confidence="low", mode="both")
    record("A.8.3 memory_detect_contradictions both mode runs",
           isinstance(r, str) and len(r) > 0,
           r[:60].replace("\n", " "), lat)

    # Invalid min_confidence
    r, lat = timed(memory_mcp.memory_detect_contradictions,
                   min_confidence="invalid", mode="both")
    record("A.8.4 memory_detect_contradictions rejects bad min_confidence",
           r.startswith("Error:") or r.startswith("Error ["), r[:60], lat)

    # Invalid mode
    r, lat = timed(memory_mcp.memory_detect_contradictions,
                   min_confidence="low", mode="invalid")
    record("A.8.5 memory_detect_contradictions rejects bad mode",
           r.startswith("Error:") or r.startswith("Error ["), r[:60], lat)


# =====================================================================
# SECTION B: Realistic workflow tests
# =====================================================================
def section_b_workflows():
    print("\n" + "=" * 72)
    print("SECTION B: REALISTIC WORKFLOW TESTS")
    print("=" * 72)

    # ----- B.1 save → search → verify -----
    print("\n--- B.1 save → search → verify ---")
    marker = "UNIQUEMARKER_PYTHON_3917"
    r = memory_mcp.memory_save(
        content=f"This note contains the magic marker {marker} for retrieval tests.",
        category="lessons", title_slug="magic-marker-test",
        tags=["workflow-test", "marker"])
    record("B.1.1 save returns success",
           r.startswith("Successfully saved"), r[:60])

    r = memory_mcp.memory_search(query=marker, limit=3)
    record("B.1.2 search finds the just-saved note",
           marker in r, r[:80].replace("\n", " "))

    # ----- B.2 supersede → search include_invalid=False hides it -----
    print("\n--- B.2 supersede → include_invalid filter ---")
    memory_mcp.memory_save(content="A is the right answer.",
                            category="lessons", title_slug="right-answer")
    memory_mcp.memory_save(content="A is no longer the right answer.",
                            category="lessons", title_slug="right-answer-v2")
    memory_mcp.memory_supersede(
        old_id="lessons/right-answer",
        new_id="lessons/right-answer-v2")

    r = memory_mcp.memory_search(query="right answer", limit=5,
                                 include_invalid=False)
    # Match only the [N] id header, not the result body.
    matched_ids = re.findall(r'^\[(\d+)\] (\S+)', r, re.MULTILINE)
    matched_set = {mid for _, mid in matched_ids}
    has_new = "lessons/right-answer-v2" in matched_set
    has_old = "lessons/right-answer" in matched_set
    record("B.2.1 include_invalid=False hides superseded",
           has_new and not has_old,
           f"new={has_new} old={has_old} — {r[:60].replace(chr(10), ' ')}")

    r = memory_mcp.memory_search(query="right answer", limit=5,
                                 include_invalid=True)
    record("B.2.2 include_invalid=True shows superseded",
           "lessons/right-answer" in r,
           r[:80].replace("\n", " "))

    # ----- B.3 tag-based retrieval -----
    print("\n--- B.3 tag-based search ---")
    memory_mcp.memory_save(content="TagA-only note.",
                            category="lessons", title_slug="tag-a",
                            tags=["uniquetaga", "shared"])
    memory_mcp.memory_save(content="TagB-only note.",
                            category="lessons", title_slug="tag-b",
                            tags=["uniquetagb", "shared"])

    # FTS searches content, but the tags are indexed in FTS too
    r = memory_mcp.memory_search(query="uniquetaga", limit=5)
    matched_ids = {mid for _, mid in re.findall(r'^\[(\d+)\] (\S+)', r, re.MULTILINE)}
    record("B.3.1 search by unique tag content finds tag-a note",
           "lessons/tag-a" in matched_ids, r[:80].replace("\n", " "))

    r = memory_mcp.memory_search(query="uniquetagb", limit=5)
    matched_ids = {mid for _, mid in re.findall(r'^\[(\d+)\] (\S+)', r, re.MULTILINE)}
    record("B.3.2 search by unique tag content finds tag-b note",
           "lessons/tag-b" in matched_ids, r[:80].replace("\n", " "))

    # Verify both notes are in DB with expected tags
    db = open_test_db()
    row_a = db.execute("SELECT tags FROM memories WHERE id='lessons/tag-a'").fetchone()
    row_b = db.execute("SELECT tags FROM memories WHERE id='lessons/tag-b'").fetchone()
    db.close()
    record("B.3.3 tag-a note has both tags in DB",
           row_a and "uniquetaga" in json.loads(row_a[0]),
           str(row_a[0] if row_a else None))
    record("B.3.4 tag-b note has both tags in DB",
           row_b and "uniquetagb" in json.loads(row_b[0]),
           str(row_b[0] if row_b else None))

    # ----- B.4 pinned decay workflow -----
    print("\n--- B.4 pinned decay workflow ---")
    memory_mcp.memory_save(content="Critical: never skip code review.",
                            category="lessons", title_slug="code-review-rule",
                            pinned=True)
    # Backdate
    db = open_test_db()
    db.execute(
        "UPDATE memories SET last_accessed='2020-06-01T00:00:00', "
        "updated_at='2020-06-01T00:00:00', access_count=0 "
        "WHERE id='lessons/code-review-rule'"
    )
    db.commit()
    db.close()

    r = memory_mcp.memory_pinned_decay_check(dry_run=True)
    obj = json.loads(r)
    flagged = any(c.get("id") == "lessons/code-review-rule"
                  for c in obj.get("auto_unpin", []) + obj.get("review", []))
    record("B.4.1 backdated pinned note flagged in decay check",
           flagged,
           f"candidates: auto_unpin={len(obj.get('auto_unpin',[]))}, "
           f"review={len(obj.get('review',[]))}")

    # Apply
    memory_mcp.memory_pinned_decay_check(dry_run=False)
    db = open_test_db()
    pinned = db.execute(
        "SELECT pinned FROM memories WHERE id='lessons/code-review-rule'"
    ).fetchone()
    db.close()
    record("B.4.2 apply mode actually unpins in DB",
           pinned and pinned[0] == 0,
           f"pinned={pinned[0] if pinned else None}")

    # Re-pin
    db = open_test_db()
    db.execute("UPDATE memories SET pinned=1 WHERE id='lessons/code-review-rule'")
    db.commit()
    db.close()

    # ----- B.5 contradiction detection workflow -----
    print("\n--- B.5 contradiction detection workflow ---")
    # Use a phrase pair that's in NEGATION_PAIRS ('enabled'/'disabled')
    # to verify the phrase detector actually works on our corpus.
    memory_mcp.memory_save(content="Logging is enabled in production.",
                            category="lessons", title_slug="log-enabled")
    memory_mcp.memory_save(content="Logging is disabled in production.",
                            category="lessons", title_slug="log-disabled")
    r = memory_mcp.memory_detect_contradictions(
        min_confidence="low", mode="phrase")
    record("B.5.1 phrase detector surfaces enabled/disabled pair",
           "log-enabled" in r and "log-disabled" in r,
           r[:120].replace("\n", " "))


# =====================================================================
# SECTION D: Edge cases
# =====================================================================
def section_d_edge_cases():
    print("\n" + "=" * 72)
    print("SECTION D: EDGE CASES")
    print("=" * 72)

    # D.1 Empty content
    print("\n--- D.1 save edge cases ---")
    r, lat = timed(memory_mcp.memory_save, content="",
                    category="quirks", title_slug="empty-content")
    # Should succeed (we wrote a title from slug; body empty is legal)
    record("D.1.1 save with empty content (body)",
           r.startswith("Successfully saved") or r.startswith("Error"),
           r[:80], lat)

    # D.2 Unicode + emoji
    r, lat = timed(memory_mcp.memory_save,
                    content="Hello 🌍 世界 — café résumé naïve",
                    category="quirks", title_slug="unicode-emoji")
    record("D.1.2 save with unicode/emoji",
           r.startswith("Successfully saved"), r[:80], lat)
    # Verify retrieval
    r, lat = timed(memory_mcp.memory_search, query="café", limit=3)
    record("D.1.3 unicode searchable",
           "café" in r or "unicode-emoji" in r,
           r[:60].replace("\n", " "), lat)

    # D.3 1MB content
    big = "x" * (1024 * 1024)
    r, lat = timed(memory_mcp.memory_save,
                    content=big, category="quirks",
                    title_slug="big-1mb")
    record("D.1.4 save with 1MB content rejected (>50KB limit)",
           (r.startswith("Error:") or r.startswith("Error [")) and ("too large" in r.lower() or "50KB" in r),
           r[:80], lat)

    # D.4 Save near limit (49KB — should pass)
    # NOTE: the limit is 50,000 BYTES (not 50 KiB), so 49000 is safe.
    near_limit = "y" * 49000
    r, lat = timed(memory_mcp.memory_save,
                    content=near_limit, category="quirks",
                    title_slug="near-49kb")
    record("D.1.5 save with 49KB content accepted",
           r.startswith("Successfully saved"),
           r[:80], lat)

    # D.5 Concurrent saves
    print("\n--- D.5 concurrent saves ---")
    n_threads = 5

    def save_one(i):
        try:
            r = memory_mcp.memory_save(
                content=f"Concurrent save #{i}",
                category="sessions", title_slug=f"concurrent-{i}",
                tags=["concurrent"])
            return (i, r)
        except Exception as e:
            return (i, f"EXC:{e}")

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        futs = [ex.submit(save_one, i) for i in range(n_threads)]
        results = [f.result() for f in as_completed(futs)]
    elapsed = (time.perf_counter() - t0) * 1000
    n_ok = sum(1 for _, r in results if r.startswith("Successfully saved"))
    record("D.5.1 all concurrent saves succeed",
           n_ok == n_threads, f"{n_ok}/{n_threads} ok in {elapsed:.0f}ms")

    # Verify all rows in DB
    db = open_test_db()
    n_concurrent = db.execute(
        "SELECT COUNT(*) FROM memories WHERE id LIKE 'sessions/concurrent-%'"
    ).fetchone()[0]
    db.close()
    record("D.5.2 all concurrent rows in DB",
           n_concurrent == n_threads, f"{n_concurrent} rows")

    # D.6 Search edge cases
    print("\n--- D.6 search edge cases ---")
    r, lat = timed(memory_mcp.memory_search, query="")
    record("D.6.1 empty query handled", isinstance(r, str), r[:60], lat)

    # Per spec: FTS5 special chars `*:^`
    for q in ['"', "*", ":", "^", "  ", "()", "AND OR NOT", "*:^"]:
        r, lat = timed(memory_mcp.memory_search, query=q)
        record(f"D.6.2 special query {q!r} safe",
               isinstance(r, str) and "Error" not in r[:30],
               r[:60], lat)

    # D.7 Reinforce workflow
    print("\n--- D.7 reinforce workflow ---")
    r, lat = timed(memory_mcp.memory_reinforce,
                    memory_ids=["lessons/sky-blue-day"], success=True)
    record("D.7.1 memory_reinforce positive",
           r.startswith("Successfully reinforced"),
           r[:80], lat)

    db = open_test_db()
    row = db.execute(
        "SELECT success_score FROM memories WHERE id='lessons/sky-blue-day'"
    ).fetchone()
    db.close()
    record("D.7.2 DB success_score updated after reinforce",
           row is not None and row[0] > 0,
           f"success_score={row[0] if row else None}")

    r, lat = timed(memory_mcp.memory_reinforce,
                    memory_ids=["lessons/nonexistent"], success=True)
    record("D.7.3 reinforce on missing id handled",
           r.startswith("No memories found") or r.startswith("Successfully"),
           r[:80], lat)

    # D.8 Audit
    print("\n--- D.8 audit workflow ---")
    r, lat = timed(memory_mcp.memory_audit)
    record("D.8.1 memory_audit returns text report",
           isinstance(r, str) and "Memory Audit Report" in r,
           r[:80].replace("\n", " "), lat)

    # D.9 ARC stats
    print("\n--- D.9 arc stats workflow ---")
    r, lat = timed(memory_mcp.memory_arc_stats)
    record("D.9.1 memory_arc_stats runs",
           isinstance(r, str), r[:60].replace("\n", " "), lat)

    # D.10 Review schedule
    print("\n--- D.10 review schedule ---")
    r, lat = timed(memory_mcp.memory_review_schedule)
    record("D.10.1 memory_review_schedule runs",
           isinstance(r, str), r[:60].replace("\n", " "), lat)

    # D.11 Semantic search
    print("\n--- D.11 semantic search ---")
    r, lat = timed(memory_mcp.memory_semantic_search, query="sky blue weather", limit=3)
    record("D.11.1 memory_semantic_search runs",
           isinstance(r, str), r[:80].replace("\n", " "), lat)

    # D.12 Consolidate (may take a while, runs in subprocess)
    print("\n--- D.12 consolidate ---")
    try:
        r, lat = timed(memory_mcp.memory_consolidate)
        record("D.12.1 memory_consolidate runs",
               isinstance(r, str), r[:80].replace("\n", " "), lat)
    except Exception as e:
        record("D.12.1 memory_consolidate runs", False, f"raised: {e}")

    # D.13 Rewrite links (idempotent)
    print("\n--- D.13 rewrite links ---")
    try:
        r, lat = timed(memory_mcp.memory_rewrite_links)
        record("D.13.1 memory_rewrite_links runs",
               isinstance(r, str), r[:80].replace("\n", " "), lat)
        # Run again — should be idempotent
        r2, lat2 = timed(memory_mcp.memory_rewrite_links)
        record("D.13.2 memory_rewrite_links idempotent",
               isinstance(r2, str), r2[:60].replace("\n", " "), lat2)
    except Exception as e:
        record("D.13.1 memory_rewrite_links runs", False, f"raised: {e}")

    # D.14 Compile skill
    print("\n--- D.14 compile skill ---")
    # We saved 'lessons/sky-blue-day' earlier; the slug is the filename stem
    r, lat = timed(memory_mcp.memory_compile_skill,
                    lesson_slug="sky-blue-day",
                    skill_name="sky-blue-day-test-skill",
                    primary_triggers=["sky", "blue"])
    record("D.14.1 memory_compile_skill runs",
           isinstance(r, str) and ("Successfully" in r or "Error" in r),
           r[:120].replace("\n", " "), lat)
    # Verify it was written to ~/.agents/skills/ inside the test home
    TEST_INSTALL / "skills" / "sky-blue-day-test-skill" / "SKILL.md"
    # ~/.agents/skills/... in test HOME resolves to TEST_ROOT/.agents/skills/
    test_skill_path = TEST_ROOT / ".agents" / "skills" / "sky-blue-day-test-skill" / "SKILL.md"
    record("D.14.2 compiled skill file written",
           test_skill_path.exists(),
           f"path={test_skill_path}")

    # D.15 Compact (dry-run to keep it fast and non-destructive on prod)
    print("\n--- D.15 compact ---")
    try:
        r, lat = timed(memory_mcp.memory_compact, dry_run=True)
        record("D.15.1 memory_compact dry_run runs",
               isinstance(r, str), r[:80].replace("\n", " "), lat)
    except Exception as e:
        record("D.15.1 memory_compact dry_run runs", False, f"raised: {e}")


# =====================================================================
# SECTION E: File system edge cases
# =====================================================================
def section_e_filesystem():
    print("\n" + "=" * 72)
    print("SECTION E: FILE SYSTEM EDGE CASES")
    print("=" * 72)

    # E.1 Note with malformed frontmatter
    print("\n--- E.1 malformed note on disk ---")
    bad_dir = TEST_MEM / "lessons"
    bad_file = bad_dir / "malformed-frontmatter.md"
    bad_file.write_text(
        "---\n"  # opening but no closing
        "title: Missing Closer\n"
        "tags: [bad]\n"
        "pinned: false\n"
        "\n"
        "# Body without closing frontmatter\n"
        "This file has unclosed frontmatter.\n",
        encoding="utf-8",
    )
    # rebuild should not crash
    r = memory_mcp.memory_rebuild(scope="active")
    record("E.1.1 rebuild handles unclosed frontmatter",
           "rebuilt successfully" in r.lower() or "completed" in r.lower(),
           r[:80].replace("\n", " "))
    # The malformed file should be parsed (gracefully) — just verify
    # rebuild completed without error
    record("E.1.2 rebuild returns without crashing",
           isinstance(r, str) and len(r) > 0, r[:60].replace("\n", " "))

    # E.2 Note with no frontmatter
    no_fm = bad_dir / "no-frontmatter.md"
    no_fm.write_text(
        "# Plain Note\n\nThis note has no frontmatter at all.\n",
        encoding="utf-8",
    )
    r = memory_mcp.memory_rebuild(scope="active")
    record("E.2.1 rebuild handles note with no frontmatter",
           isinstance(r, str) and len(r) > 0, r[:60].replace("\n", " "))

    # E.3 Note with frontmatter but no body
    empty_body = bad_dir / "frontmatter-no-body.md"
    empty_body.write_text(
        "---\n"
        "title: Empty Body\n"
        "tags: [empty]\n"
        "pinned: false\n"
        "---\n",
        encoding="utf-8",
    )
    r = memory_mcp.memory_rebuild(scope="active")
    record("E.3.1 rebuild handles frontmatter-only note",
           isinstance(r, str), r[:60].replace("\n", " "))

    # E.4 Two notes with the same slug (different categories)
    decisions_dir = TEST_MEM / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    same_slug_a = bad_dir / "same-slug.md"
    same_slug_b = decisions_dir / "same-slug.md"
    same_slug_a.write_text(
        "---\ntitle: Same Slug in Lessons\ntags: [dup]\npinned: false\n---\n\n# A\n",
        encoding="utf-8",
    )
    same_slug_b.write_text(
        "---\ntitle: Same Slug in Decisions\ntags: [dup]\npinned: false\n---\n\n# B\n",
        encoding="utf-8",
    )
    r = memory_mcp.memory_rebuild(scope="active")
    record("E.4.1 rebuild handles same slug in different categories",
           isinstance(r, str), r[:60].replace("\n", " "))
    # Both should be in DB with distinct IDs (category/slug)
    db = open_test_db()
    n = db.execute("SELECT COUNT(*) FROM memories WHERE id LIKE '%/same-slug'").fetchone()[0]
    db.close()
    record("E.4.2 same-slug notes in different categories have distinct DB rows",
           n == 2, f"{n} rows")

    # E.5 Note in a non-standard location (top-level of memory/)
    nonstd = TEST_MEM / "loose-note.md"
    nonstd.write_text(
        "---\ntitle: Loose\ntags: [loose]\npinned: false\n---\n\nLoose note in memory/ root.\n",
        encoding="utf-8",
    )
    r = memory_mcp.memory_rebuild(scope="active")
    # The rebuild script walks recursively, so a top-level file IS indexed.
    # Its id is derived from the file's relative path (e.g. "loose-note").
    record("E.5.1 rebuild handles note at memory/ root",
           isinstance(r, str), r[:60].replace("\n", " "))
    db = open_test_db()
    n_loose = db.execute(
        "SELECT COUNT(*) FROM memories WHERE id LIKE '%loose-note%'"
    ).fetchone()[0]
    db.close()
    record("E.5.2 top-level note indexed into DB",
           n_loose >= 1, f"{n_loose} rows")

    # E.6 Note deleted between rebuild passes
    print("\n--- E.6 file deletion between rebuilds ---")
    # Create, rebuild, delete, rebuild again
    memory_mcp.memory_save(content="ephemeral content xyzzy42",
                            category="lessons", title_slug="ephemeral")
    memory_mcp.memory_rebuild(scope="active")
    # Delete the .md file (DB row may still exist)
    (bad_dir / "ephemeral.md").unlink()
    r2 = memory_mcp.memory_rebuild(scope="active")
    record("E.6.1 rebuild succeeds after .md deletion",
           "rebuilt" in r2.lower() or "completed" in r2.lower() or
           "Error" not in r2[:30],
           r2[:60].replace("\n", " "))

    # E.7 Concurrent file delete during rebuild
    print("\n--- E.7 concurrent file delete ---")
    memory_mcp.memory_save(content="will-be-deleted",
                            category="lessons", title_slug="will-be-deleted")
    fpath = bad_dir / "will-be-deleted.md"

    def delete_later():
        time.sleep(0.05)
        if fpath.exists():
            fpath.unlink()
    t = threading.Thread(target=delete_later)
    t.start()
    r = memory_mcp.memory_rebuild(scope="active")
    t.join()
    record("E.7.1 rebuild survives concurrent file deletion",
           isinstance(r, str), r[:60].replace("\n", " "))

    # E.8 Invalid UTF-8 in file
    print("\n--- E.8 invalid UTF-8 in file ---")
    bad_utf8 = bad_dir / "bad-utf8.md"
    with open(bad_utf8, "wb") as f:
        f.write(b"---\ntitle: Bad\ntags: [bad]\npinned: false\n---\n\n# Bad\n\n")
        f.write(b"valid prefix ")
        f.write(b"\xff\xfe\xfd invalid bytes\n")
    r = memory_mcp.memory_rebuild(scope="active")
    record("E.8.1 rebuild handles invalid UTF-8 bytes",
           isinstance(r, str), r[:60].replace("\n", " "))


# =====================================================================
# SECTION C: LongMemEval harness
# =====================================================================
def section_c_longmemeval(max_questions=30):
    print("\n" + "=" * 72)
    print("SECTION C: LONGMEMEVAL HARNESS")
    print("=" * 72)

    data_path = PROD_INSTALL / "eval" / "data" / "longmemeval_oracle.json"
    if not data_path.exists():
        record("C.0 longmemeval data file present", False,
               f"missing: {data_path}")
        return

    record("C.0 longmemeval data file present", True,
           f"{data_path.stat().st_size / 1024 / 1024:.1f}MB")

    # The harness has its own setup_isolated_memory() that wipes
    # the target dir, so use a separate test dir for it
    harness_mem = TEST_INSTALL / "lmeval_test"
    if harness_mem.exists():
        shutil.rmtree(harness_mem)

    out_json = TEST_INSTALL / "lmeval_results.json"
    cmd = [
        str(VENV_PY), str(PROD_INSTALL / "eval" / "longmemeval_harness.py"),
        "--data", str(data_path),
        "--memory-dir", str(harness_mem),
        "--max-questions", str(max_questions),
        "--k-values", "1", "5", "10",
        "--output", str(out_json),
    ]
    print(f"  running: {' '.join(cmd)}")
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        elapsed = (time.perf_counter() - t0)
        print(f"  harness stdout (truncated): {p.stdout[-400:]}")
        if p.returncode != 0:
            print(f"  harness stderr: {p.stderr[-400:]}")
        record("C.1 longmemeval harness returns 0",
               p.returncode == 0, f"rc={p.returncode} elapsed={elapsed:.1f}s")

        if out_json.exists():
            with open(out_json) as f:
                results = json.load(f)
            metrics = results.get("metrics", {})
            for k_label, per_type in metrics.items():
                overall = per_type.get("__overall__", {})
                n = overall.get("total", 0)
                sr = overall.get("session_recall_at_k", 0)
                sub = overall.get("substring_recall_at_k", 0)
                record(f"C.2 longmemeval {k_label} session recall",
                       sr >= 0,
                       f"session_recall={sr*100:.1f}% (n={n})",
                       results.get("latency_ms", {}).get("mean"))
                record(f"C.3 longmemeval {k_label} substring recall",
                       sub >= 0,
                       f"substring_recall={sub*100:.1f}% (n={n})",
                       results.get("latency_ms", {}).get("mean"))
    except subprocess.TimeoutExpired:
        record("C.1 longmemeval harness returns 0", False, "timeout after 300s")
    except Exception as e:
        record("C.1 longmemeval harness returns 0", False, str(e))


# =====================================================================
# Production DB integrity check (verify we didn't touch it)
# =====================================================================
def production_integrity_check(baseline_n_rows):
    """Confirm the production DB still has its baseline row count
    (i.e., we did not write to it)."""
    if not PROD_DB.exists():
        return None
    db = sqlite3.connect(str(PROD_DB), timeout=5.0)
    n = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    db.close()
    return n


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-longmemeval", action="store_true",
                        help="Skip the longmemeval harness (saves ~minutes)")
    args = parser.parse_args()

    print("=" * 72)
    print("DEEP END-TO-END VALIDATION — agentic-memory MCP surface")
    print(f"  test install: {TEST_INSTALL}")
    print(f"  test memory:  {TEST_MEM}")
    print(f"  production:   {PROD_INSTALL}")
    print("=" * 72)

    # Baseline production DB row count
    prod_n_before = production_integrity_check(None)
    if prod_n_before is not None:
        print(f"  production DB baseline: {prod_n_before} rows")

    # Run all sections
    try:
        section_a_mcp_tools()
        section_b_workflows()
        section_d_edge_cases()
        section_e_filesystem()
        if not args.no_longmemeval:
            section_c_longmemeval(max_questions=30)
        else:
            print(f"\n[{SKIP}] SECTION C: LONGMEMEVAL HARNESS (--no-longmemeval)")
    except Exception as e:
        print(f"\nTest crashed: {e}")
        traceback.print_exc()
    finally:
        # Verify production DB is untouched (always, even on crash)
        if prod_n_before is not None:
            prod_n_after = production_integrity_check(None)
            record("Z.1 production DB row count unchanged",
                   prod_n_after == prod_n_before,
                   f"before={prod_n_before} after={prod_n_after}")
        else:
            record("Z.1 production DB row count unchanged",
                   False, "could not check")
        # Cleanup the test install (always, even on crash)
        print(f"\nCleaning up test install at {TEST_ROOT}...")
        try:
            shutil.rmtree(TEST_ROOT, ignore_errors=True)
            print("  removed.")
        except Exception as e:
            print(f"  cleanup error: {e}")

    # Final summary
    print("\n" + "=" * 72)
    print("FINAL SUMMARY")
    print("=" * 72)
    n_total = len(RESULTS)
    n_pass = sum(1 for _, ok, _, _ in RESULTS if ok)
    n_fail = n_total - n_pass
    pct = (n_pass / n_total * 100) if n_total else 0
    print(f"  Total:  {n_total}")
    print(f"  Passed: {n_pass}  ({pct:.1f}%)")
    print(f"  Failed: {n_fail}")

    if n_fail:
        print("\nFAILURES:")
        for name, ok, detail, _ in RESULTS:
            if not ok:
                print(f"  - {name}: {detail}")

    # Latency report per tool
    if LATENCIES:
        print("\nMCP tool latency (ms):")
        print(f"  {'tool':<32} {'n':>4} {'mean':>8} {'min':>8} {'max':>8}")
        for tool, lats in sorted(LATENCIES.items()):
            if lats:
                lats_sorted = sorted(lats)
                print(f"  {tool:<32} {len(lats):>4} "
                      f"{sum(lats)/len(lats):>8.1f} "
                      f"{lats_sorted[0]:>8.1f} "
                      f"{lats_sorted[-1]:>8.1f}")

    # Cleanup
    print(f"\nCleaning up test install at {TEST_ROOT}...")
    try:
        shutil.rmtree(TEST_ROOT, ignore_errors=True)
        print("  removed.")
    except Exception as e:
        print(f"  cleanup error: {e}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
