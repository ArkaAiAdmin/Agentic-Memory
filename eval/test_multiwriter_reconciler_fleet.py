"""Subprocess-based tests for the multi-process reconciler fleet — Step 3.

These tests must run in a separate Python process because the fleet uses
``multiprocessing.Process``; sharing SQLite connections and module-level
state across a pytest process and child processes causes deadlocks.

Runner contract (invoked via subprocess):
    python -m eval.test_multiwriter_reconciler_fleet run_fleet <json_config_path>

The JSON config file contains:
    {"journal_path": str, "target_base": str, "n_workers": int}
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _prepopulate_journal(journal_path: Path, n: int = 1000) -> None:
    """Fill journal.db with n pending entries."""
    from infra.write_journal import init_journal_db

    init_journal_db(journal_path)
    conn = sqlite3.connect(str(journal_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executemany(
        "INSERT INTO write_journal "
        "(note_id, agent_id, category, title_slug, content, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        [
            (
                f"note-{i:06d}",
                "fleet-test-agent",
                "lessons",
                f"slug-{i:06d}",
                f"fleet-test-content-{i:06d}",
            )
            for i in range(n)
        ],
    )
    conn.commit()
    conn.close()
    # Reset thread-local conns so the next open is clean.
    from infra.write_journal import _clear_local_conns
    _clear_local_conns()


def _count_journal_status(journal_path: Path) -> dict:
    """Return a dict of status → count for the journal."""
    conn = sqlite3.connect(str(journal_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM write_journal GROUP BY status"
    ).fetchall()
    conn.close()
    return {r["status"]: r["cnt"] for r in rows}


# ---------------------------------------------------------------------------
# Subprocess fleet runner
# ---------------------------------------------------------------------------

def _run_fleet_from_config(config_path: Path) -> None:
    """Entry point for subprocess fleet runs (called via subprocess)."""
    with open(config_path) as fh:
        cfg: dict = json.load(fh)
    journal_path = Path(cfg["journal_path"])
    target_base = Path(cfg["target_base"])
    n_workers = int(cfg["n_workers"])

    from background.background_worker import multiwriter_reconciliation_pool
    multiwriter_reconciliation_pool(journal_path, target_base, n_workers=n_workers)


def _launch_fleet(
    journal_path: Path,
    target_base: Path,
    n_workers: int,
    timeout_s: float = 90.0,
) -> subprocess.Popen:
    """Launch the fleet as a subprocess via the standalone fleet_entry."""
    env = os.environ.copy()
    env.setdefault("MEMORY_DB_FLOCK", "0")
    env.setdefault("PYTHONPATH", str(_REPO_ROOT))

    popen = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "background.fleet_entry",
            str(journal_path),
            str(target_base),
            str(n_workers),
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return popen


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultiwriterFleet:
    """Uses a real in-process fleet run via subprocess."""

    def test_fleet_drains_all_entries(self, tmp_path: Path) -> None:
        n_entries = 500
        n_workers = 4
        journal_path = tmp_path / "journal.db"
        target_base = tmp_path / "mem"
        (target_base / "lessons").mkdir(parents=True, exist_ok=True)
        (target_base / "memory.db").touch()

        _prepopulate_journal(journal_path, n=n_entries)

        popen = _launch_fleet(journal_path, target_base, n_workers, timeout_s=90)
        try:
            stdout, stderr = popen.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            popen.kill()
            pytest.fail(f"Fleet did not finish within 90s. stderr:\n{stderr.read()}")
        print(f"Fleet stdout:\n{stdout.decode()}\nstderr:\n{stderr.decode()}")
        assert popen.returncode == 0, f"Fleet exited with code {popen.returncode}"

        status = _count_journal_status(journal_path)
        assert status.get("applied", 0) == n_entries, (
            f"Expected {n_entries} applied, got {status}"
        )
        assert status.get("pending", 0) == 0
        assert status.get("processing", 0) == 0

        mem_db = target_base / "memory.db"
        conn = sqlite3.connect(str(mem_db))
        n_in_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        assert n_in_mem == n_entries, f"Expected {n_entries} in memories, got {n_in_mem}"

    def test_stuck_entries_reclaimed_on_restart(self, tmp_path: Path) -> None:
        os.environ.setdefault("MEMORY_JOURNAL_STUCK_AGE", "3")
        import importlib
        from infra import write_journal
        importlib.reload(write_journal)
        STUCK = write_journal.STUCK_PROCESSING_MAX_AGE_SECONDS

        n_entries = 200
        n_workers = 4
        journal_path = tmp_path / "journal2.db"
        target_base = tmp_path / "mem2"
        (target_base / "lessons").mkdir(parents=True, exist_ok=True)
        (target_base / "memory.db").touch()

        _prepopulate_journal(journal_path, n=n_entries)

        popen = _launch_fleet(journal_path, target_base, n_workers, timeout_s=20)
        try:
            popen.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        popen.kill()
        popen.wait(timeout=5)

        time.sleep(STUCK + 2)

        from infra.write_journal import reset_stuck_processing
        reset_stuck_processing(journal_path)

        popen2 = _launch_fleet(journal_path, target_base, n_workers, timeout_s=90)
        try:
            stdout, stderr = popen2.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            popen2.kill()
            pytest.fail("Restarted fleet did not finish within 90s")
        assert popen2.returncode == 0, f"Fleet restart failed: {stderr.decode()}"

        status = _count_journal_status(journal_path)
        assert status.get("applied", 0) == n_entries, (
            f"After restart: expected {n_entries} applied, got {status}"
        )


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__" or __name__ == "eval.test_multiwriter_reconciler_fleet":
    import sys as _sys

    if len(_sys.argv) >= 2 and _sys.argv[1] == "run_fleet" and len(_sys.argv) >= 3:
        import json as _json
        from pathlib import Path as _Path
        _run_fleet_from_config(_Path(_sys.argv[2]))
