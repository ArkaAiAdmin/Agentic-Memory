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
import signal
import sys
from pathlib import Path

WORKTREE_ROOT = str(Path(__file__).resolve().parent.parent)

# Auth mode: tests exercise functionality, not auth enforcement. The secure
# default for deployments is "closed" (fail-closed authorizer); the test
# suite opts into the legacy "open" behavior so the existing functional
# tests stay green. New adversarial auth tests set MEMORY_AUTH_MODE="closed"
# explicitly within the test.
os.environ.setdefault("MEMORY_AUTH_MODE", "open")

# ---------------------------------------------------------------------------
# Test embedding config — activates when MEMORY_TEST_EMBEDDING=1
# ---------------------------------------------------------------------------
# Set env vars BEFORE any infra module imports so that get_config() picks
# them up on first call (including from background threads in
# EmbeddingSearch._load_model).  These are no-ops unless MEMORY_TEST_EMBEDDING=1.
_TEST_EMBEDDING = os.environ.get("MEMORY_TEST_EMBEDDING", "0") == "1"
_TEST_MODEL_ID = "intfloat/e5-small-v2"


def _should_redirect(p) -> bool:
    p_str = str(p)
    if ".venv" in p_str or "site-packages" in p_str:
        return False
    return (p_str.endswith("/.config/agentic-memory") or p_str.endswith("/.config/agentic-memory/")) and "agentic-memory-wt-packaging" not in p_str

class PathRedirector(list):
    def insert(self, index, value):
        if _should_redirect(value):
            value = WORKTREE_ROOT
        super().insert(index, value)

    def append(self, value):
        if _should_redirect(value):
            value = WORKTREE_ROOT
        super().append(value)

    def extend(self, values):
        super().extend(
            WORKTREE_ROOT if _should_redirect(v) else v
            for v in values
        )

    def __setitem__(self, index, value):
        if _should_redirect(value):
            value = WORKTREE_ROOT
        super().__setitem__(index, value)

    def __iadd__(self, values):
        return super().__iadd__(
            WORKTREE_ROOT if _should_redirect(v) else v
            for v in values
        )

# Initialize PathRedirector with current sys.path redirected
initial_paths = []
for p in sys.path:
    if _should_redirect(p):
        initial_paths.append(WORKTREE_ROOT)
    else:
        initial_paths.append(p)

sys.path = PathRedirector(initial_paths)

import time

# Prevent libomp / torch OpenMP segfaults on macOS when multiple
# native libraries (torch, scipy, sklearn) each bundle conflicting
# copies of libomp. Must be set before torch is ever imported.
import faulthandler

faulthandler.enable()
faulthandler.dump_traceback_later(15, repeat=False)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")

# ---------------------------------------------------------------------------
# Tenant-isolation bootstrap for test connections.
#
# The tenant-isolation hardening routes memory reads through the
# `tenant_memories` TEMP VIEW (and the `tenant_id()` SQLite function), which
# is created on every connection handed out by infra/db.py's connection
# pool. Many tests, however, open raw `sqlite3.connect(...)` connections to
# their bootstrapped temp DBs and never go through the pool — so the view
# (and the function) are absent, and any query against `tenant_memories`
# raises "no such table: tenant_memories" / "no such function: tenant_id".
#
# Rather than seeding the view in ~20 individual test files, we seed it on
# every sqlite connection opened during the test session *once the `memories`
# table already exists*. The seeding is idempotent and best-effort: it is a
# no-op for the bootstrap/migration connection (which opens against an empty
# file and builds the schema afterwards, so seeding there would break
# migration 042's RENAME), for connections that already have the view (e.g.
# those created by the pool), and for any connection where `memories` is not
# yet present. This mirrors the production connection-setup contract for the
# test environment.
# ---------------------------------------------------------------------------
import sqlite3 as _sqlite3

_ami_original_connect = _sqlite3.connect


def _ami_patched_connect(*args, **kwargs):
    conn = _ami_original_connect(*args, **kwargs)
    try:
        # M10 fix: make tenant_id configurable via env var
        _tid = os.environ.get("TEST_TENANT_ID", "default")
        conn.create_function("tenant_id", 0, lambda: _tid)
        # Only seed the view once `memories` already exists in this database.
        # The bootstrap/migration connection opens against an empty file and
        # builds the schema afterwards; seeding the view there would create a
        # dependency that breaks migration 042's `ALTER ... RENAME TO memories`
        # (a view referencing `memories` forces "no such table: main.memories"
        # at RENAME time). The test's later connection opens against the
        # already-built file and gets the view here.
        _has = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type IN ('table','view') AND name='memories'"
        ).fetchone()
        if _has:
            conn.execute(
                "CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS "
                "SELECT * FROM memories WHERE tenant_id = tenant_id()"
            )
            # Add INSTEAD OF trigger so writes through the view work
            # (SQLite views are not directly writable).
            # B30 fix: dynamically resolve actual columns from the table
            # instead of using _MEMORIES_COLUMNS directly, because
            # _MEMORIES_COLUMNS may include columns (e.g. data_subject_sub)
            # that are added by later numbered migrations and don't exist
            # yet in a bare/mid-migration memories table.  Referencing a
            # non-existent column in the trigger body causes FK-validation
            # failures during DDL like `CREATE TABLE ... REFERENCES
            # memories(id)` — see eval/test_db_drift.py cascade failure.
            try:
                existing_cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(memories)"
                    ).fetchall()
                }
            except Exception:
                existing_cols = set()
            from infra.db import _MEMORIES_COLUMNS
            trigger_cols = [
                c for c in _MEMORIES_COLUMNS if c in existing_cols
            ]
            if trigger_cols:
                cols = ", ".join(f"NEW.{c}" for c in trigger_cols)
                col_list = ", ".join(trigger_cols)
                try:
                    conn.execute("DROP TRIGGER IF EXISTS _tenant_memories_update")
                    conn.execute(
                        f"CREATE TEMP TRIGGER _tenant_memories_update "
                        f"INSTEAD OF UPDATE ON tenant_memories BEGIN "
                        f"UPDATE memories SET "
                        f"({col_list}) = (SELECT {cols}) "
                        f"WHERE id = OLD.id; END"
                    )
                except Exception:
                    pass
    except Exception:
        pass
    return conn


_sqlite3.connect = _ami_patched_connect


def embedding_available() -> bool:
    """Check if the embedding model (model2vec) is loaded and usable."""
    try:
        from infra.embedding_search import get_embedding_search
        es = get_embedding_search()
        # M11 fix: check _model_loaded flag instead of es.model to avoid
        # race with background loader thread.
        return es._model_loaded and es.model is not None
    except Exception:
        return False
# NOTE: KMP_DUPLICATE_LIB_OK and OMP_NUM_THREADS stay at module level
# because they must be set before torch is first imported. All other
# test env vars are in the session-scoped _test_env fixture below.

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
    """Best-effort: stop the auto-save daemon for the default memory dir.

    Sends SIGTERM and waits up to 5 s for the process to exit before
    escalating to SIGKILL.  Without the wait, a stale daemon can hold
    the production flock across test runs and cause cascade failures.
    """
    killed: list[int] = []
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
                        os.kill(pid, signal.SIGTERM)
                        killed.append(pid)
                    except (OSError, ProcessLookupError):
                        pass
            manifest_path.write_text("{}")
    except Exception:
        pass

    # M14 fix: give each PID its own 5s deadline instead of sharing one
    for pid in killed:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
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

_TEST_ENV_VARS = {
    "MEMORY_LLM_EXTRACTION": "0",
    "MEMORY_KNOWLEDGE_GRAPH": "1",
    "MEMORY_ADAPTIVE_RETENTION": "1",
    "MEMORY_LLM_HYBRID": "0",
    "MEMORY_QUALITY_GATES": "1",
    # CQRS journal disabled in tests (overrides memory.toml). Most tests
    # directly read memory.db after writes and expect synchronous behavior.
    # Dedicated write-journal integration tests opt in via their own config.
    "MEMORY_WRITE_JOURNAL_ENABLED": "0",
    # Downgrade config drift enforcement from hard_fail to warn so that
    # env-var overrides (like MEMORY_WRITE_JOURNAL_ENABLED=0 above) don't
    # block startup. Tests intentionally override TOML flags.
    "MEMORY_FAIL_ON_INTEGRITY_DRIFT": "0",
}

@pytest.fixture(scope="session", autouse=False)
def _test_embedding_setup():
    """Activate the test embedding model (intfloat/e5-small-v2) for embedding tests.

    When MEMORY_TEST_EMBEDDING=1: sets env vars + resets the config singleton
    so EmbeddingSearch._load_model picks up sentence-transformers + e5-small-v2.
    When MEMORY_TEST_EMBEDDING is not set: no-op (embedding tests are skipped).

    Embedding test files opt in with:
        @pytest.mark.usefixtures("_test_embedding_setup")
    """
    if not _TEST_EMBEDDING:
        yield
        return
    os.environ["MEMORY_EMBEDDING_BACKEND"] = "sentence-transformers"
    os.environ["MEMORY_EMBEDDING_MODEL_ID"] = _TEST_MODEL_ID
    os.environ["MEMORY_EMBEDDING_MODEL_REVISION"] = ""
    try:
        from config import reset_config
        reset_config()
    except Exception:
        pass
    try:
        from infra.embedding_search import reset_embedding_search
        reset_embedding_search()
    except Exception:
        pass
    yield


@pytest.fixture(scope="session", autouse=True)
def _test_session_env():
    _saved = {}
    for k, v in _TEST_ENV_VARS.items():
        _saved[k] = os.environ.get(k)
        os.environ[k] = v
    # H12 fix: set embedding env vars in fixture with cleanup
    if _TEST_EMBEDDING:
        for k, v in {
            "MEMORY_EMBEDDING_BACKEND": "sentence-transformers",
            "MEMORY_EMBEDDING_MODEL_ID": _TEST_MODEL_ID,
            "MEMORY_EMBEDDING_MODEL_REVISION": "",
        }.items():
            _saved[k] = os.environ.get(k)
            os.environ.setdefault(k, v)
    yield
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _clear_reranker_caches_between_tests():
    """Reset reranker caches between tests to prevent cross-test pollution.

    Only activates when the reranker module is already imported.
    """
    yield
    import sys
    if "search.rerankers" in sys.modules:
        from search.rerankers import clear_reranker_caches
        clear_reranker_caches()


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
# CHANGE 7 — RBAC-in-CI fixtures.
#
# The main test suite opts into the legacy "open" auth mode (see the
# MEMORY_AUTH_MODE setdefault near the top of this file) so functional tests
# stay green.  Security / auth-enforcement tests must exercise the SECURE
# default ("closed", fail-closed authorizer) against a REAL principal + RBAC
# store — not a mocked authorizer.  These fixtures provide exactly that:
#
#   * closed_auth_env   — switches the process into MEMORY_AUTH_MODE=closed
#                         for the duration of the test and restores it after.
#   * mock_admin_principal — a fully-migrated temp DB with default roles
#                         seeded and a mock admin principal (memory:admin +
#                         ops:admin) granted, scoped to its own tenant.
#   * ClosedClient      — an AgenticMemoryClient bound to that DB with the
#                         admin principal activated in agent_context, so its
#                         save/search/delete calls flow through the real
#                         mcp_authorize path under closed mode.
#
# A dedicated CI job (see .github/workflows/ci.yml "security-closed-auth")
# runs the auth/security test subset with these fixtures; the global
# MEMORY_AUTH_MODE default is left as "open" so the rest of the suite is
# unaffected.
# ---------------------------------------------------------------------------


@pytest.fixture
def closed_auth_env(monkeypatch):
    """Force the fail-closed auth mode for the duration of a test.

    Yields the mode string ("closed") so callers can assert against it.
    Restores the previous mode afterwards (the suite default is "open").
    """
    monkeypatch.setenv("MEMORY_AUTH_MODE", "closed")
    yield "closed"
    # monkeypatch reverts the env var automatically on teardown.


@pytest.fixture
def mock_admin_principal(tmp_path):
    """A migrated temp DB with a mock admin principal granted full admin.

    Yields ``(db_path, principal_id, tenant_id)``.  The principal holds both
    ``memory:admin`` and ``ops:admin`` roles, so it passes the real
    ``mcp_authorize`` check under closed mode for any memory/ops action.

    Setup uses a raw ``sqlite3.Connection`` (not the proxy-backed
    ``open_db``) so the RBAC helper signatures, which require a concrete
    ``sqlite3.Connection``, type-check cleanly.
    """
    import sqlite3

    from infra.db_migrations import run_schema_setup
    from infra.rbac import seed_default_roles, grant_role

    db_path = tmp_path / "closed_auth.db"
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        seed_default_roles(conn)
        conn.commit()
    finally:
        conn.close()

    principal_id = "mock-admin-principal"
    tenant_id = "closed-auth-tenant"
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute(
            "INSERT INTO principals (id, kind, tenant_id) VALUES (?, 'service', ?)",
            (principal_id, tenant_id),
        )
        grant_role(conn, principal_id, "role:memory:admin:default")
        grant_role(conn, principal_id, "role:ops:admin:default")
        conn.commit()
    finally:
        conn.close()

    yield str(db_path), principal_id, tenant_id


@pytest.fixture
def closed_auth_principal(closed_auth_env, mock_admin_principal):
    """Activate the mock admin principal in agent_context, contamination-safe.

    Saves and restores the surrounding agent_context state (current agent +
    attached principal_id) so security tests that flip the process into closed
    auth mode never leak principal state into later (open-mode) tests.  Yields
    ``(db_path, principal_id, tenant_id)``.
    """
    import agent_context as _agent_context

    db_path, principal_id, tenant_id = mock_admin_principal

    # Save prior state for restoration.
    _saved_current = getattr(_agent_context._AGENT_CONTEXT, "current", None)
    _saved_principal = getattr(_agent_context._AGENT_CONTEXT, "principal_id", None)

    # AgentContext is frozen, so the principal id is attached to the
    # threading.local itself (which is what the pipeline reads) rather than to
    # the context object.  The agent_id is set to the principal's tenant so the
    # save-path tenant fallback resolves to the SAME tenant the principal is
    # bound to — otherwise tenant-scoped delete would refuse the row written by
    # this principal.
    _agent_context.init_agent(agent_id=tenant_id, namespace=tenant_id, principal_id=principal_id)

    yield db_path, principal_id, tenant_id

    # Restore prior state exactly (contamination-safe teardown).
    if _saved_current is None:
        try:
            del _agent_context._AGENT_CONTEXT.current
        except AttributeError:
            pass
    else:
        _agent_context._AGENT_CONTEXT.current = _saved_current
    if _saved_principal is None:
        try:
            del _agent_context._AGENT_CONTEXT.principal_id
        except AttributeError:
            pass
    else:
        _agent_context._AGENT_CONTEXT.principal_id = _saved_principal


@pytest.fixture
def ClosedClient(closed_auth_principal):
    """An authorized client operating under closed auth mode.

    Returns an ``AgenticMemoryClient`` bound to the admin principal's DB.
    Operations genuinely pass through ``mcp_authorize`` (closed mode); there
    is no mocked authorizer.  Use it in security tests that must verify the
    enforcement path end-to-end:

        def test_something(ClosedClient):
            note_id = ClosedClient.save("secret")
            assert ClosedClient.search("secret")
    """
    from agentic_memory.client import MemoryClient

    db_path, principal_id, tenant_id = closed_auth_principal

    client = MemoryClient(db_path=db_path)
    yield client
    # agent_context + principal_id are restored by closed_auth_principal.


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
        venv_py = Path(str(sys.executable))
    return Path(venv_py)


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
    # 1. Close every connection the pool is still holding FIRST so any
    #    WAL-mode locks held by pool connections are released before we
    #    ask the write queue to stop.
    try:
        from infra.db import connection_pool
        connection_pool.close_all()
    except Exception:
        pass
    # 2. Drain the singleton write queue and join its thread.
    try:
        from infra import db_write_queue

        q = getattr(db_write_queue, "sqlite_write_queue", None)
        if q is not None and hasattr(q, "stop"):
            try:
                q.stop(timeout=30.0)
            except TypeError:
                q.stop()
            except Exception:
                pass
    except Exception:
        pass

    # 3. Stop the auto-save daemon (if still running) before the flaky-test
    #    early-return so we don't leave a stale daemon holding the flock.
    _cleanup_auto_save_daemon()

    # 4. Auto-save a pinned lessons memory for any flaky tests found.
    if not _flaky_items:
        return
    try:
        import tempfile
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        sys_path = str(root)
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from save_pipeline import save_memory  # noqa: E402
        from infra.db_migrations import run_schema_setup
        import sqlite3

        lines = [f"- {nodeid} ({when})" for nodeid, when in _flaky_items]
        content = "Flaky tests detected in pytest session:\n" + "\n".join(lines)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "memory.db"
            conn = sqlite3.connect(str(tmp_db))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            run_schema_setup(conn)
            conn.close()
            save_memory(
                content=content,
                category="lessons",
                title_slug="flaky-tests-detected",
                tags=["flaky"],
                pinned=True,
                db_path=str(tmp_db),
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
        from infra.db import connection_pool

        if connection_pool._pool:  # L7 fix: skip if pool is already empty
            connection_pool.clear()
    except Exception:
        pass
    yield
    try:
        from infra.db import connection_pool

        if connection_pool._pool:
            connection_pool.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_auto_save_state():
    """Autouse fixture: reset auto-save circuit breaker state and stop daemon
    before every test.

    The auto-save module holds circuit-breaker state (failure_times,
    circuit_open_until) at module level. Without this fixture, a test that
    triggers failures leaves the breaker open for the next test, causing
    it to see 'simulated DB locked' instead of the expected error.

    Also stops any leftover auto-save daemon from a previous test in the
    same xdist worker, which can cause worker crashes when the daemon's
    background threads conflict with the test's own threads.
    """
    # L7 fix: skip daemon cleanup if no daemon was ever started in this worker
    if getattr(reset_auto_save_state, "_daemon_ever_started", False):
        _cleanup_auto_save_daemon()
    try:
        from background.auto_save import _auto_save_reset_state, _AUTO_SAVE_STATE

        _auto_save_reset_state()
        _AUTO_SAVE_STATE["failure_times"] = []
        _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
        _AUTO_SAVE_STATE["last_backoff_seconds"] = 0.0
    except Exception:
        pass
    yield
    try:
        from background.auto_save import _auto_save_reset_state

        _cleanup_auto_save_daemon()
        _auto_save_reset_state()
        reset_auto_save_state._daemon_ever_started = True
    except Exception:
        pass
    # Drain the write-queue singleton so a background-thread session
    # from one test does not block the next test's open_db(write=True).
    # The write queue's daemon thread holds session connections and
    # processes cmd_queues sequentially; a stuck or slow session can
    # starve the main queue and cause TimeoutError in start_session().
    try:
        from infra import db_write_queue

        q = getattr(db_write_queue, "sqlite_write_queue", None)
        if q is not None and hasattr(q, "restart"):
            try:
                q.restart(timeout=5.0)
            except Exception:
                pass
        elif q is not None and hasattr(q, "stop"):
            if q._thread is not None and q._thread.is_alive():
                try:
                    q.stop(timeout=5.0)
                except Exception:
                    pass
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
        from infra.memory_common import reset_all_lazy_config_attrs

        reset_all_lazy_config_attrs()
    except Exception:
        pass
    yield
    try:
        from infra.memory_common import reset_all_lazy_config_attrs

        reset_all_lazy_config_attrs()
    except Exception:
        pass
    if saved_reranker_disabled is not None:
        os.environ["MEMORY_RERANKER_DISABLED"] = saved_reranker_disabled
