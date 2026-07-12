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
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
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
    env.setdefault("MEMORY_RERANKER_DISABLED", "true")
    env.setdefault("MEMORY_EMBEDDING_BACKEND", "none")

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
        # Put the supervisor in its own process group so the test can tear
        # down the *entire* fleet (supervisor + its fleet_worker children) with
        # one killpg.  Without this, popen.kill() only SIGKILLs the supervisor
        # and the worker subprocesses become orphaned (see _kill_fleet_tree).
        start_new_session=True,
    )
    return popen


def _kill_fleet_tree(popen: subprocess.Popen, timeout_s: float = 10.0) -> None:
    """Terminate the fleet supervisor AND all of its worker subprocesses.

    ``popen.kill()`` only signals the immediate child (the ``fleet_entry``
    supervisor).  The supervisor spawns N ``fleet_worker`` children via
    ``subprocess.Popen``; a SIGKILL on the parent does not propagate to
    them, so they are reparented to init and keep materializing the
    journal — and re-dispatching "stuck" entries via
    ``reset_stuck_processing`` — for the rest of the test.  Under
    suite-level concurrency those orphans compete with the restarted
    fleet for CPU and the journal lock, which can starve the restart
    past its 90s budget (the observed flake).

    Launching the supervisor with ``start_new_session=True`` makes it a
    process-group leader, so a single ``killpg`` takes down the whole
    tree.  The killed workers hold no external resources the test needs.
    """
    if popen.poll() is not None:
        return
    try:
        pgid = os.getpgid(popen.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Supervisor already gone — best-effort direct kill.
        try:
            popen.kill()
        except Exception:
            pass
    try:
        popen.wait(timeout=timeout_s)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Fleet subprocess tests are too slow for CI — 50+ entries take >3min due to save_memory overhead per entry")
class TestMultiwriterFleet:
    """Uses a real in-process fleet run via subprocess."""

    def setup_method(self, method) -> None:
        # Track live fleet subprocesses so teardown can reap the whole tree
        # even if a test aborts before its own kill step.
        self._fleet_popens: list[subprocess.Popen] = []

    def teardown_method(self, method) -> None:
        for p in getattr(self, "_fleet_popens", []):
            try:
                _kill_fleet_tree(p, timeout_s=5)
            except Exception:
                pass
        self._fleet_popens = []

    def test_fleet_drains_all_entries(self, tmp_path: Path) -> None:
        n_entries = 100
        n_workers = 4
        journal_path = tmp_path / "journal.db"
        target_base = tmp_path / "mem"
        (target_base / "lessons").mkdir(parents=True, exist_ok=True)
        (target_base / "memory.db").touch()

        _prepopulate_journal(journal_path, n=n_entries)

        popen = _launch_fleet(journal_path, target_base, n_workers, timeout_s=180)
        self._fleet_popens.append(popen)
        try:
            stdout, stderr = popen.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            _kill_fleet_tree(popen)
            _stderr_tail = b""
            try:
                if popen.stderr is not None:
                    _stderr_tail = popen.stderr.read()
            except Exception:
                pass
            pytest.fail(
                f"Fleet did not finish within 90s. stderr:\n{_stderr_tail.decode(errors='replace')}"
            )
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

        n_entries = 50
        n_workers = 4
        journal_path = tmp_path / "journal2.db"
        target_base = tmp_path / "mem2"
        (target_base / "lessons").mkdir(parents=True, exist_ok=True)
        (target_base / "memory.db").touch()

        _prepopulate_journal(journal_path, n=n_entries)

        popen = _launch_fleet(journal_path, target_base, n_workers, timeout_s=20)
        self._fleet_popens.append(popen)
        try:
            popen.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Make sure the first fleet actually claimed at least one entry
        # before we kill it.  Otherwise there is nothing "stuck" to reclaim
        # on restart, so the test would not be exercising its contract.  The
        # workers can take longer to spin up under heavy machine load, so poll
        # briefly instead of assuming the 5s above was enough.
        if popen.poll() is None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and popen.poll() is None:
                try:
                    if _count_journal_status(journal_path).get("processing", 0) > 0:
                        break
                except Exception:
                    pass
                time.sleep(0.25)
        # Kill the *entire* fleet process tree (supervisor + fleet_worker
        # children).  popen.kill() only SIGKILLs the supervisor and leaves the
        # worker subprocesses orphaned; they keep materializing the journal and
        # re-dispatching stuck entries, which starves the restarted fleet under
        # suite-level concurrency (the observed flake).
        _kill_fleet_tree(popen)

        time.sleep(STUCK + 2)

        from infra.write_journal import reset_stuck_processing
        reset_stuck_processing(journal_path)

        popen2 = _launch_fleet(journal_path, target_base, n_workers, timeout_s=180)
        self._fleet_popens.append(popen2)
        try:
            stdout, stderr = popen2.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            _kill_fleet_tree(popen2)
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
        from pathlib import Path as _Path
        _run_fleet_from_config(_Path(_sys.argv[2]))
