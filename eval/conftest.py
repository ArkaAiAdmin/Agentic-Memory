"""conftest: pytest configuration for the agentic-memory test suite.

What this does:
1. Excludes the standalone `test_all_*.py` scripts (they're manual
   runners, not pytest tests).
2. Provides a `bootstrap_temp_db` helper + `temp_db_path` pytest
   fixture for the H21 migration: tests should use this instead of
   inline `_init_schema()` calls.
3. Cleans up any stale auto-save daemon at session start to prevent
   lock contention with test DBs (the daemon holds a flock on the
   production memory dir that can interfere with test DB operations).
"""

import os
import sys
from pathlib import Path

# Prevent libomp / torch OpenMP segfaults on macOS when multiple
# native libraries (torch, scipy, sklearn) each bundle conflicting
# copies of libomp. Must be set before torch is ever imported.
import faulthandler

faulthandler.enable()
faulthandler.dump_traceback_later(15, repeat=True)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ["MEMORY_LLM_EXTRACTION"] = "0"
# Session-wide test env: set once here instead of each file doing
# os.environ["X"] = "1" at module top level (causes cross-test pollution).
os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "1"
os.environ.setdefault("MEMORY_ADAPTIVE_RETENTION", "1")
os.environ.setdefault("MEMORY_LLM_HYBRID", "0")
os.environ.setdefault("MEMORY_QUALITY_GATES", "1")

# 2026-06-20: MEMORY_DB_PATH is intentionally NOT set here. The
# 14 production-DB tests in test_p0_p1_p2_fixes.py skip when
# the env var is unset. They DO pass when the env var is set
# (verified: 30/30 pass with MEMORY_DB_PATH pointing at the
# live DB), but the live DB has FK violations (137 critical
# findings in user_profile_access_log as of 2026-06-20) that
# cause cross-pollution failures in other test files. The
# p0_p1_p2 tests should be re-enabled after the FK cleanup
# (separate work item).

import pytest


# ---------------------------------------------------------------------------
# Session-level daemon cleanup: before any test runs, kill the auto-save
# daemon for the default memory dir if it's running.  Tests use isolated
# temp DBs and don't need the daemon; a leftover daemon from a previous
# session can interfere with flcok-based test DB operations.
# ---------------------------------------------------------------------------
def _cleanup_auto_save_daemon() -> None:
    """Best-effort: stop the auto-save daemon for the default memory dir."""
    try:
        manifest_path = (
            Path(os.environ.get("MEMORY_CONFIG_DIR", Path.home() / ".config" / "agentic-memory"))
            / ".auto_save_daemon_manifest.json"
        )
        if manifest_path.exists():
            import json
            manifest = json.loads(manifest_path.read_text())
            for key, info in list(manifest.items()):
                pid = info.get("pid", 0)
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                        os.kill(pid, 15)
                    except (OSError, ProcessLookupError):
                        pass
            manifest_path.write_text("{}")
    except Exception:
        pass

_cleanup_auto_save_daemon()

# Make sibling modules (eval/_fixtures.py) importable when this conftest
# is loaded. Pytest doesn't add the conftest's directory to sys.path
# automatically, but the bootstrap helper is needed at conftest-load time.
_CONFTEST_DIR = Path(__file__).resolve().parent
if str(_CONFTEST_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFTEST_DIR))

# 2026-06-21: cron scripts moved to cron/ subdirectory. Add the cron/
# directory to sys.path so `import cron_backup` etc. still work in tests.
_CRON_DIR = _CONFTEST_DIR.parent / "cron"
if _CRON_DIR.is_dir() and str(_CRON_DIR) not in sys.path:
    sys.path.insert(0, str(_CRON_DIR))

from _fixtures import bootstrap_temp_db  # noqa: E402


# H21 migration goal: get every test onto a fixture that gives it a
# fully-bootstrapped temp DB. The canonical pattern is to copy the live
# prod schema (which has all 6 migrations applied). Tests that use this
# pattern pass reliably.
#
# See: projects/h21-fix-plan-2026-06-16
#      test_no_silent_search_failures.py for the working pattern.
#
# bootstrap_temp_db is defined in eval/_fixtures.py so test files can
# import it directly. The temp_db_path fixture below uses it.


@pytest.fixture
def temp_db_path(tmp_path):
    """Pytest fixture: yields a fully-bootstrapped temp DB path.

    Usage in a test:
        def test_x(self, temp_db_path):
            db = temp_db_path
            with open_db(db) as conn:
                ...
    """
    db = tmp_path / "memory.db"
    bootstrap_temp_db(db)
    return db


# ---------------------------------------------------------------------------
# 2026-06-29: Path resolution fixtures for tests that previously hardcoded
# ~/.config/agentic-memory or wrote driver scripts to REPO/memory/. On CI
# the project lives at a non-standard path (e.g. /home/runner/work/...) and
# the user install dir does not exist, so hardcoded paths broke a dozen
# tests. Use these fixtures instead of Path.home() / ".config" / "agentic-memory".
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Repo root (directory containing pyproject.toml / conftest.py's parent)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def project_venv_python(project_root) -> Path:
    """Path to the venv python inside the project root.

    On local dev installs: <project_root>/venv/bin/python
    On CI: same path (CI uses the same venv layout).
    Falls back to sys.executable if no project-local venv exists.
    """
    venv_py = project_root / "venv" / "bin" / "python"
    if not venv_py.exists():
        venv_py = project_root / ".venv" / "bin" / "python"
    if not venv_py.exists():
        venv_py = Path(sys.executable)
    return venv_py


@pytest.fixture
def project_memory_dir(project_root, tmp_path):
    """A writable memory/ dir for driver scripts and test artifacts.

    Uses tmp_path by default so the test is hermetic. Tests that need
    a real REPO/memory/ dir can opt in via the `use_real_memory_dir`
    marker, but the default is tmp_path to avoid CI side-effects.
    """
    d = tmp_path / "memory"
    d.mkdir(exist_ok=True)
    return d


collect_ignore = []
for _f in os.listdir(os.path.dirname(__file__)):
    if _f.startswith("test_all_") and _f.endswith(".py"):
        collect_ignore.append(_f)


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (use -m 'not slow' to skip)"
    )


# Phase 2 (Rule #4): pytest plugin that auto-saves a flaky-test
# memory when a session finishes with xpass (or other flaky) tests.
# Best-effort: never blocks test completion.
_flaky_items: list[tuple[str, str]] = []


def pytest_runtest_makereport(item, call):
    """Collect xpass tests as flaky indicators."""
    try:
        report = call.get_result()
    except AttributeError:
        return
    if report.outcome == "passed" and item.get_closest_marker("xfail"):
        when = call.when
        _flaky_items.append((item.nodeid, when))


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Session-end hooks: drain the write queue, close the pool, save lessons.

    2026-06-29 fix: the sqlite_write_queue module-level singleton starts a
    daemon thread on import. When a worker process exits (especially under
    xdist or --forked, or when a test segfaults), the daemon thread is killed
    abruptly without releasing the SQLite connection or the db_path_flock. That
    triggered the exit-139 segfault in the no-extras job: the main process was
    stuck waiting on a queue whose worker thread had already died holding a
    locked DB file, and atexit finalisation then segfaulted during native
    shutdown (sqlite + numpy + usearch). Stopping the queue at session end
    gives the thread a chance to drain pending writes and release the
    flock before the process exits.
    """
    # 1. Drain the singleton write queue and join its thread.
    try:
        from infra import db_write_queue

        q = getattr(db_write_queue, "sqlite_write_queue", None)
        if q is not None and hasattr(q, "stop"):
            try:
                q.stop(timeout=5.0)
            except TypeError:
                # Older signature without timeout.
                q.stop()
            except Exception:
                pass
    except Exception:
        pass
    # 2. Close every connection the pool is still holding. The pool's
    #    __del__ may not run in time on worker shutdown, leaving WAL-mode
    #    file locks held across processes.
    try:
        from db import connection_pool

        connection_pool.close_all()
    except Exception:
        pass

    # 3. Auto-save a pinned lessons memory for any flaky tests found.
    if not _flaky_items:
        return
    try:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        sys_path = str(root)
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from save_pipeline import save_memory  # noqa: E402

        lines = [f"- {nodeid} ({when})" for nodeid, when in _flaky_items]
        content = "Flaky tests detected in pytest session:\n" + "\n".join(lines)
        db_path = root / "memory" / "memory.db"
        if db_path.exists():
            save_memory(
                content=content,
                category="lessons",
                title_slug="flaky-tests-detected",
                tags=["flaky"],
                pinned=True,
                db_path=str(db_path),
            )
    except Exception:
        pass


@pytest.fixture(autouse=True)
def clear_pool_between_tests():
    """Autouse fixture to clear the connection pool before and after every test.

    This prevents tests that leak connections (by calling connection_pool.get()
    and not returning them) from causing PoolExhaustedError in subsequent tests
    now that the pool strictly enforces max_size limits.
    """
    try:
        from db import connection_pool

        connection_pool.clear()
    except Exception:
        pass
    yield
    try:
        from db import connection_pool

        connection_pool.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_lazy_config_cache():
    """Autouse fixture: clear lazy-getattr cache and unset test-only env vars.

    Clears only modules that carry the ``_lazy_config_attr_names`` marker
    (set by make_lazy_getattr or manually for hand-rolled __getattr__
    sites). Test modules that import lazy modules as local names are
    intentionally left untouched.

    Also unsets MEMORY_RERANKER_DISABLED which some test files set at
    module top level (a session-wide leak — each test that needs it
    should use patch.dict for per-test scope).
    """
    import os

    saved_reranker_disabled = os.environ.pop("MEMORY_RERANKER_DISABLED", None)
    try:
        from memory_common import reset_all_lazy_config_attrs

        reset_all_lazy_config_attrs()
    except Exception:
        pass
    yield
    try:
        from memory_common import reset_all_lazy_config_attrs

        reset_all_lazy_config_attrs()
    except Exception:
        pass
    if saved_reranker_disabled is not None:
        os.environ["MEMORY_RERANKER_DISABLED"] = saved_reranker_disabled
