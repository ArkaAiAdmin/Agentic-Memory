# Implementation Plan — Connection Leaks, Lock Manager, MCP Hang, Worker Recovery

## Executive Summary

The agentic-memory system has four active issues:
1. **Background worker down** since 2026-07-17 (argument parsing crash)
2. **Lock manager circular dependency** — `SQLiteLockManager` opens new SQLite connections to the DB it coordinates, causing deadlocks under contention
3. **Two remaining raw connection hotspots** — `mcp_coordination.py` and `mcp_verbs.py` bypass the connection pool
4. **MCP server hang** — tool invocations never return, though direct Python calls succeed (transport/protocol issue)

Fixes from commits `314d88769` and `4c262f97a` resolved the bulk of the connection leaks, WAL growth, and `session_start` latency. This plan addresses the remaining gaps.

---

## Issue 1: Background Worker Down

**Root Cause:** The worker crashed on 2026-07-17 with:
```
background_worker.py: error: unrecognized arguments: --interval=5
```

**Current State:** The argument parser at `background/background_worker.py:1187` defines `--interval`, so the current code accepts it. The worker should be restartable.

**Fix:**
1. Verify the worker starts cleanly:
   ```bash
   venv/bin/python background/background_worker.py --db memory/memory.db --interval 5 --once
   ```
2. If it starts, restart it as a long-lived service:
   ```bash
   venv/bin/python background/background_worker.py --db memory/memory.db --interval 5
   ```
3. Add a launchd/systemd unit or cron entry to auto-restart on crash.

**Verification:** Worker log shows `worker: starting` and processes at least one task without error.

---

## Issue 2: Lock Manager Circular Dependency

**File:** `infra/lock_manager.py:84-96`

**Root Cause:** `SQLiteLockManager._get_conn()` opens a **new raw `sqlite3.connect()`** on every call. `acquire_lock()`, `release_lock()`, `renew_lock()`, and `is_locked()` each call `_get_conn()` via a `with` block. Under DB contention, these connections queue up, and since the lock manager is trying to coordinate access to the same DB, it can deadlock itself.

**Current Mitigation:** Commit `314d88769` added `busy_timeout=5000` and auto-creates the `system_locks` table. This reduces but does not eliminate the circular dependency.

**Fix — Option A (Preferred): Replace with `fcntl.flock`**
Replace `SQLiteLockManager` with an `fcntl.flock`-based lock manager that uses file-descriptor locking on `<dbpath>.db.flock`. This eliminates the DB dependency entirely.

**Fix — Option B (Minimal): Singleton connection**
Cache a single connection in `SQLiteLockManager.__init__` and reuse it across all operations, closing it only on process exit.

**Recommended:** Option A — `fcntl.flock` is the correct primitive for cross-process file locking. The `db_path_flock` layer already uses `fcntl.flock`; the `SQLiteLockManager` is a redundant, broken reimplementation.

**Implementation:**
```python
# infra/lock_manager.py
import fcntl
import os

class FlockLockManager(LockManager):
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock_fd = None

    def _lock_path(self) -> str:
        return self.db_path + ".flock"

    def acquire_lock(self, lock_name, holder_id, ttl_seconds=60):
        path = self._lock_path()
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return True, str(os.getpid())
        except (IOError, OSError):
            os.close(fd)
            return False, ""

    def release_lock(self, lock_name, lease_token):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None
        return True

    def renew_lock(self, lock_name, lease_token, ttl_seconds=60):
        return True  # flock is held as long as fd is open

    def is_locked(self, lock_name):
        path = self._lock_path()
        if not os.path.exists(path):
            return False
        fd = os.open(path, os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except (IOError, OSError):
            return True
        finally:
            os.close(fd)
```

Then update `get_lock_manager()` to default to `FlockLockManager`:
```python
def get_lock_manager() -> LockManager:
    global _GLOBAL_LOCK_MANAGER
    if _GLOBAL_LOCK_MANAGER is not None:
        return _GLOBAL_LOCK_MANAGER
    engine = os.environ.get("MEMORY_LOCK_ENGINE", "local").strip().lower()
    if engine == "redis":
        ...
    elif engine == "postgres":
        ...
    else:
        from infra.infrastructure import resolve_active_memory_dir
        db_path = resolve_active_memory_dir() / "memory.db"
        _GLOBAL_LOCK_MANAGER = FlockLockManager(db_path)
    return _GLOBAL_LOCK_MANAGER
```

---

## Issue 3: Remaining Raw Connection Hotspots

### 3a. `mcp_coordination.py:25`

**Current:**
```python
conn = sqlite3.connect(str(db_path), timeout=10)
conn.execute("PRAGMA journal_mode=WAL")
return conn
```

**Fix:** Replace `_get_conn()` with pooled access:
```python
from infra.db import connection_pool, safe_close_db

def _get_conn():
    db_path = str(resolve_active_memory_dir() / "memory.db")
    conn = connection_pool.get(db_path, timeout=10.0)
    return conn
```

And ensure every call site calls `safe_close_db(conn)` after use. The current code returns the conn to the caller; the caller must return it.

### 3b. `mcp_verbs.py:275` (`_supplement_with_pending`)

**Current:**
```python
_conn = _sqlite3.connect(str(journal_path), timeout=30.0)
_conn.execute("PRAGMA busy_timeout = 30000;")
_conn.row_factory = _sqlite3.Row
...
_conn.close()
```

**Fix:** Use `open_db()`:
```python
from infra.db import open_db
with open_db(Path(journal_path), timeout=30.0, pooled=True, write=False) as _conn:
    _conn.row_factory = sqlite3.Row
    ...
```

---

## Issue 4: MCP Server Hang

**Symptom:** All MCP tool calls hang indefinitely. Direct Python calls to the same functions complete in milliseconds.

**Diagnosis:** The MCP server process (`memory_mcp.py --agent-id OPENCODE`, pid 52932) is alive but unresponsive to tool invocations. This is a transport/protocol issue, not a code issue.

**Possible causes:**
1. **MCP server deadlock** — a previous request never completed, blocking the server's request-processing loop
2. **OpenCode client timeout** — the client gives up before the server responds
3. **JSON-RPC framing issue** — the server receives the request but never sends a response

**Fix:**
1. Kill and restart the MCP server process:
   ```bash
   kill 52932
   # OpenCode should auto-restart it via MCP client config
   ```
2. If restart doesn't help, add request timeout + heartbeat to the MCP server:
   - In `mcp_instance.py` or the MCP server entry point, add a watchdog thread that logs the server's processing state every 5s
   - Add a per-request timeout so a hung tool call doesn't block forever
3. Check for MCP server logs — look for `stderr` output from the server process

---

## Issue 5: WAL Autocheckpoint Coverage Audit

**Verified Coverage:**
- `infra/db.py:410` — `_ConnectionPool.get()` ✅
- `infra/db.py:858` — `open_db()` non-pooled path ✅
- `infra/db_write_queue.py:304` — write queue loop ✅
- `background/background_worker.py:1776` — worker connection ✅

**Remaining gaps:** Any raw `sqlite3.connect()` in files listed in Issue 3 should also set `wal_autocheckpoint=500`. After migrating those files to use the pool/open_db, this is automatically covered.

---

## Implementation Order

| Priority | Issue | Effort | Risk |
|----------|-------|--------|------|
| P0 | Restart background worker | 5 min | None |
| P0 | Kill + restart MCP server | 5 min | None |
| P1 | Fix lock manager (Option A: flock) | 30 min | Low — well-tested primitive |
| P1 | Migrate mcp_coordination.py to pool | 15 min | Low |
| P2 | Migrate mcp_verbs.py to pool | 15 min | Low |
| P2 | Add MCP request timeout + watchdog | 30 min | Low |

**Total estimated effort: ~2 hours**

---

## Test Plan

After all fixes:
1. `venv/bin/python -m pytest eval/test_connection_leak.py -v` — must pass 3/3
2. `venv/bin/python -m pytest eval/test_recall.py eval/test_mcp_verbs.py eval/test_cross_process_safety.py -v` — must pass all
3. `venv/bin/python -c "from mcp_search import memory_session_start; print(memory_session_start())"` — must complete in <1s
4. Verify worker starts: `venv/bin/python background/background_worker.py --db memory/memory.db --interval 5 --once`
5. Verify MCP tools respond: call `memory_search`, `memory_save`, `memory_audit` via MCP
