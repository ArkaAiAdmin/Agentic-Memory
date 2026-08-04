"""SQLite Single-Writer thread queue for Agentic Memory.

Channels all mutating SQL queries (INSERT, UPDATE, DELETE) and transactional
write operations to a single background worker thread to completely eliminate
lock contention and database locks in concurrent multithreaded workflows.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union, cast

logger = logging.getLogger(__name__)


class ProxyCursor:
    description: Optional[tuple]
    _data: list
    lastrowid: Optional[int]
    rowcount: Optional[int]
    _idx: int

    def __init__(
        self,
        description: Optional[tuple],
        fetchall_data: list,
        lastrowid: Optional[int],
        rowcount: Optional[int],
    ) -> None:
        self.description = description
        self._data = fetchall_data
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self._idx = 0

    def fetchone(self) -> Optional[tuple]:
        if self._idx < len(self._data):
            r = self._data[self._idx]
            self._idx += 1
            return r  # type: ignore[no-any-return]
        return None

    def fetchall(self) -> list:
        r = self._data[self._idx :]
        self._idx = len(self._data)
        return r

    def close(self) -> None:
        pass

    def __iter__(self) -> ProxyCursor:
        return self

    def __next__(self) -> tuple:
        r = self.fetchone()
        if r is None:
            raise StopIteration
        return r


class ProxyCursorObject:
    connection: ProxyConnection
    description: Optional[tuple]
    lastrowid: Optional[int]
    rowcount: Optional[int]
    _data: list
    _idx: int

    def __init__(self, connection: ProxyConnection) -> None:
        self.connection = connection
        self.description = None
        self.lastrowid = None
        self.rowcount = -1
        self._data = []
        self._idx = 0

    def execute(self, sql: str, params: Any = ()) -> ProxyCursorObject:
        cursor = self.connection.execute(sql, params)
        self.description = cursor.description
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self._data = cursor._data
        self._idx = 0
        return self

    def executemany(self, sql: str, params_seq: Any) -> ProxyCursorObject:
        cursor = self.connection.executemany(sql, params_seq)
        self.description = cursor.description
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self._data = []
        self._idx = 0
        return self

    def fetchone(self) -> Optional[tuple]:
        if self._idx < len(self._data):
            r = self._data[self._idx]
            self._idx += 1
            return r  # type: ignore[no-any-return]
        return None

    def fetchall(self) -> list:
        r = self._data[self._idx :]
        self._idx = len(self._data)
        return r

    def close(self) -> None:
        pass

    def __iter__(self) -> ProxyCursorObject:
        return self

    def __next__(self) -> tuple:
        r = self.fetchone()
        if r is None:
            raise StopIteration
        return r


class ProxyConnection:
    def __init__(self, cmd_queue: queue.Queue, resp_queue: queue.Queue, session_id: Optional[int] = None) -> None:
        self._cmd_queue = cmd_queue
        self._resp_queue = resp_queue
        self._session_id = session_id
        self.row_factory: Optional[Any] = None
        self._closed = False
        self._in_txn = False

    def cursor(self) -> ProxyCursorObject:
        return ProxyCursorObject(self)

    @property
    def in_transaction(self) -> bool:
        return self._in_txn

    def _check_dead(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed connection.")
        if self._session_id is not None and self._session_id in sqlite_write_queue._dead_sessions:
            raise sqlite3.OperationalError("write session timed out; transaction rolled back")

    def execute(self, sql: str, params: Any = ()) -> ProxyCursor:
        self._check_dead()
        if sql.strip().upper().startswith("BEGIN"):
            self._in_txn = True
            return ProxyCursor(None, [], None, None)
        self._cmd_queue.put(("execute", (sql, params)))
        timeout = float(os.environ.get("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S", "30.0"))
        try:
            status, res = self._resp_queue.get(timeout=timeout)
        except queue.Empty:
            raise sqlite3.OperationalError("Write queue response timeout")
        if status == "error":
            raise res
        self._in_txn = True
        lastrowid, rowcount, fetchall_data, description = res
        if self.row_factory:
            dummy_conn = sqlite3.connect(":memory:")
            try:
                dummy = dummy_conn.cursor()
                if description:
                    cols = [f"NULL AS [{d[0]}]" for d in description]
                    dummy.execute("SELECT " + ", ".join(cols))
                fetchall_data = [self.row_factory(dummy, r) for r in fetchall_data]
            finally:
                dummy_conn.close()
        return ProxyCursor(description, fetchall_data, lastrowid, rowcount)

    def executemany(self, sql: str, params_seq: Any) -> ProxyCursor:
        self._check_dead()
        self._cmd_queue.put(("executemany", (sql, list(params_seq))))
        timeout = float(os.environ.get("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S", "30.0"))
        try:
            status, res = self._resp_queue.get(timeout=timeout)
        except queue.Empty:
            raise sqlite3.OperationalError("Write queue response timeout")
        if status == "error":
            raise res
        self._in_txn = True
        lastrowid, rowcount, _, description = res
        return ProxyCursor(description, [], lastrowid, rowcount)

    def executescript(self, sql_script: str) -> ProxyConnection:
        """Execute a raw SQL script over the write queue.

        NOTE (H21): executescript() in SQLite implicitly issues COMMIT before
        executing script statements. Take care when calling on an active session.
        """
        self._check_dead()
        self._cmd_queue.put(("executescript", sql_script))
        timeout = float(os.environ.get("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S", "30.0"))
        try:
            status, res = self._resp_queue.get(timeout=timeout)
        except queue.Empty:
            raise sqlite3.OperationalError("Write queue response timeout")
        if status == "error":
            raise res
        self._in_txn = True
        return self

    def commit(self) -> None:
        if self._closed:
            return
        self._check_dead()
        self._cmd_queue.put(("commit", None))
        timeout = float(os.environ.get("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S", "30.0"))
        try:
            status, res = self._resp_queue.get(timeout=timeout)
        except queue.Empty:
            raise sqlite3.OperationalError("Write queue response timeout")
        if status == "error":
            raise res
        self._in_txn = False

    def rollback(self) -> None:
        if self._closed:
            return
        self._check_dead()
        self._cmd_queue.put(("rollback", None))
        timeout = float(os.environ.get("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S", "30.0"))
        try:
            status, res = self._resp_queue.get(timeout=timeout)
        except queue.Empty:
            raise sqlite3.OperationalError("Write queue response timeout")
        if status == "error":
            raise res
        self._in_txn = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._in_txn = False
        self._cmd_queue.put(("close", None))
        timeout = float(os.environ.get("MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S", "10.0"))
        try:
            self._resp_queue.get(timeout=timeout)
        except Exception:
            pass

    def __enter__(self) -> ProxyConnection:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class SQLiteWriteQueue:
    """Thread-safe queue that processes database writes serially on a background thread.

    Supports enqueuing individual SQL statements or executing entire transactional
    callbacks inside a BEGIN IMMEDIATE transaction block.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="SQLiteWriteQueueThread",
        )
        self._thread.start()
        self._sessions: dict[int, dict] = {}
        self._dead_sessions: set[int] = set()
        self._session_counter = itertools.count(1)
        self._sessions_lock = threading.Lock()
        self._pending_futures: set[concurrent.futures.Future] = set()
        self._pending_lock = threading.Lock()
        self._path_conns: dict[Path, sqlite3.Connection] = {}
        self._session_cmd_queues: dict[int, queue.Queue] = {}

    def _get_or_create_session_conn(self, session_id: int, db_path: Path) -> sqlite3.Connection:
        with self._sessions_lock:
            if session_id not in self._sessions:
                conn = sqlite3.connect(str(db_path), timeout=30.0)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA wal_autocheckpoint=500")
                # Run schema setup and create tenant_id function for CRDT support
                for _retry in range(3):
                    try:
                        from infra.db_migrations import run_schema_setup
                        run_schema_setup(conn)
                        break
                    except sqlite3.OperationalError as _oe:
                        if "locked" in str(_oe).lower() and _retry < 2:
                            time.sleep(0.1 * (2 ** _retry))
                        else:
                            break
                    except Exception:
                        break
                t_id = os.environ.get("MEMORY_CRON_TENANT_ID") or os.environ.get("MEMORY_TENANT_ID") or "default"
                try:
                    from infra.db import _setup_tenant_view
                    _setup_tenant_view(conn, t_id)
                except Exception:
                    pass
                self._sessions[session_id] = {"conn": conn, "db_path": db_path}
            return cast(sqlite3.Connection, self._sessions[session_id]["conn"])  # type: ignore[no-any-return]

    def _get_reusable_conn(self, db_path: Path) -> sqlite3.Connection:
        if db_path not in self._path_conns:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA wal_autocheckpoint=500")
            self._path_conns[db_path] = conn
        return self._path_conns[db_path]

    def _close_session(self, session_id: int, action: str = "rollback") -> None:
        with self._sessions_lock:
            self._dead_sessions.add(session_id)
            session = self._sessions.pop(session_id, None)
            if session is not None:
                try:
                    conn = session["conn"]
                    try:
                        if action == "commit":
                            conn.commit()
                        else:
                            conn.rollback()
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    conn.close()
                except Exception:
                    pass

    def _ensure_running(self) -> None:
        """Ensure the background worker thread is active and accepting tasks."""
        if self._shutdown.is_set() or not self._thread.is_alive():
            self.restart()

    def start_session(
        self, db_path: Union[str, Path], timeout: Optional[float] = None
    ) -> ProxyConnection:
        """Start a write session proxy that executes transactions on the write queue thread.

        ``timeout`` bounds how long the caller waits for the session to be
        established (i.e. for the db-path flock to become available).
        ``None`` (default) keeps the historical behavior of using
        ``MEMORY_WRITE_QUEUE_SESSION_TIMEOUT`` (default 15s). Best-effort
        callers (spaced-repetition reinforcement in the read/search path)
        pass a small timeout so recall can never be stalled by a contended
        writer in another process.
        """
        self._ensure_running()
        cmd_queue: queue.Queue = queue.Queue()
        resp_queue: queue.Queue = queue.Queue()
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._sessions_lock:
            session_id = next(self._session_counter)
        with self._pending_lock:
            self._pending_futures.add(future)
        self._queue.put((Path(db_path), "session", (cmd_queue, resp_queue, session_id), future))
        # Configurable timeout via environment variable (default 60s)
        session_timeout = (
            float(os.environ.get("MEMORY_WRITE_QUEUE_SESSION_TIMEOUT", "15.0"))
            if timeout is None
            else timeout
        )
        try:
            future.result(timeout=session_timeout)
        finally:
            with self._pending_lock:
                self._pending_futures.discard(future)
        return ProxyConnection(cmd_queue, resp_queue, session_id=session_id)

    def enqueue_write(
        self,
        db_path: Union[str, Path],
        query: str,
        params: tuple = (),
    ) -> concurrent.futures.Future:
        """Enqueue a single mutating SQL statement.

        Returns:
            Future resolving to (last_rowid, rowcount) tuple.
        """
        self._ensure_running()
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._pending_lock:
            self._pending_futures.add(future)
        self._queue.put((Path(db_path), "statement", (query, params), future))
        return future

    def enqueue_transaction(
        self,
        db_path: Union[str, Path],
        callback: Callable[[sqlite3.Connection], Any],
    ) -> concurrent.futures.Future:
        """Enqueue an entire transactional callback to run serially on the writer thread.

        The callback function is passed a live, open connection and runs inside
        a BEGIN IMMEDIATE block. If the callback succeeds, the transaction is committed.
        If it raises an exception, it is rolled back.

        Returns:
            Future resolving to the callback's return value.
        """
        self._ensure_running()
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._pending_lock:
            self._pending_futures.add(future)
        self._queue.put((Path(db_path), "callback", callback, future))
        return future

    def _run_loop(self) -> None:
        """Main serial processing loop running on the dedicated background thread."""
        while not self._shutdown.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                break

            db_path, task_type, payload, future = task
            try:
                from infra.db_path_flock import db_path_flock

                if task_type == "session":
                    if len(payload) == 3:
                        cmd_queue, resp_queue, session_id = payload
                    else:
                        cmd_queue, resp_queue = payload
                        session_id = id(cmd_queue)
                    self._session_cmd_queues[session_id] = cmd_queue
                    with db_path_flock(db_path):
                        conn = self._get_or_create_session_conn(session_id, db_path)
                    try:
                        future.set_result(True)
                    except Exception as e:
                        future.set_exception(e)
                        self._session_cmd_queues.pop(session_id, None)
                        continue

                    idle_timeout_env = float(os.environ.get("MEMORY_WRITE_QUEUE_IDLE_S", "300.0"))
                    idle_deadline = time.time() + idle_timeout_env
                    while True:
                        if self._shutdown.is_set():
                            with db_path_flock(db_path):
                                if conn.in_transaction:
                                    try:
                                        conn.rollback()
                                    except Exception:
                                        pass
                                self._close_session(session_id)
                            self._session_cmd_queues.pop(session_id, None)
                            break
                        remaining = idle_deadline - time.time()
                        if remaining <= 0:
                            with db_path_flock(db_path):
                                if conn.in_transaction:
                                    try:
                                        conn.rollback()
                                    except Exception:
                                        pass
                                self._close_session(session_id)
                            self._session_cmd_queues.pop(session_id, None)
                            break
                        try:
                            cmd = cmd_queue.get(timeout=min(0.5, max(remaining, 0.1)))
                        except queue.Empty:
                            continue

                        idle_deadline = time.time() + idle_timeout_env

                        if cmd is None:
                            with db_path_flock(db_path):
                                try:
                                    conn.commit()
                                except Exception:
                                    try:
                                        conn.rollback()
                                    except Exception:
                                        pass
                                self._close_session(session_id)
                            self._session_cmd_queues.pop(session_id, None)
                            try:
                                resp_queue.put(("success", None), timeout=1.0)
                            except Exception:
                                pass
                            break

                        action, act_payload = cmd
                        with db_path_flock(db_path):
                            try:
                                if action == "execute":
                                    sql, params = act_payload
                                    if not conn.in_transaction and not sql.strip().upper().startswith("BEGIN"):
                                        for _retry in range(5):
                                            try:
                                                conn.execute("BEGIN IMMEDIATE")
                                                break
                                            except sqlite3.OperationalError as _oe:
                                                if "locked" in str(_oe).lower() and _retry < 4:
                                                    time.sleep(0.05 * (2 ** _retry))
                                                else:
                                                    raise
                                    cursor = conn.execute(sql, params)
                                    res_rows = cursor.fetchall()
                                    resp_queue.put(
                                        (
                                            "success",
                                            (
                                                cursor.lastrowid,
                                                cursor.rowcount,
                                                res_rows,
                                                cursor.description,
                                            ),
                                        )
                                    )
                                elif action == "executemany":
                                    sql, params_seq = act_payload
                                    if not conn.in_transaction and not sql.strip().upper().startswith("BEGIN"):
                                        conn.execute("BEGIN IMMEDIATE")
                                    cursor = conn.executemany(sql, params_seq)
                                    resp_queue.put(
                                        (
                                            "success",
                                            (
                                                cursor.lastrowid,
                                                cursor.rowcount,
                                                [],
                                                cursor.description,
                                            ),
                                        )
                                    )
                                elif action == "executescript":
                                    sql_script = act_payload
                                    conn.executescript(sql_script)
                                    resp_queue.put(("success", (None, None, [], None)))
                                elif action == "commit":
                                    if conn.in_transaction:
                                        conn.commit()
                                    resp_queue.put(("success", None))
                                elif action == "rollback":
                                    if conn.in_transaction:
                                        conn.rollback()
                                    resp_queue.put(("success", None))
                                elif action == "close":
                                    if conn.in_transaction:
                                        try:
                                            conn.commit()
                                        except Exception:
                                            try:
                                                conn.rollback()
                                            except Exception:
                                                pass
                                    self._close_session(session_id)
                                    resp_queue.put(("success", None))
                                    break
                            except Exception as q_exc:
                                resp_queue.put(("error", q_exc))

                else:
                    lock_ctx = db_path_flock(db_path)
                    with lock_ctx:
                        conn = None
                        try:
                            conn = sqlite3.connect(str(db_path), timeout=30.0)
                            conn.execute("PRAGMA journal_mode=WAL")
                            conn.execute("PRAGMA busy_timeout=30000")
                            conn.execute("PRAGMA foreign_keys=ON")
                            conn.execute("PRAGMA wal_autocheckpoint=500")

                            if task_type == "statement":
                                query, params = payload
                                conn.execute("BEGIN IMMEDIATE")
                                cursor = conn.execute(query, params)
                                last_rowid = cursor.lastrowid
                                rowcount = cursor.rowcount
                                conn.commit()
                                future.set_result((last_rowid, rowcount))

                            elif task_type == "callback":
                                callback = payload
                                conn.execute("BEGIN IMMEDIATE")
                                try:
                                    result = callback(conn)
                                    conn.commit()
                                    future.set_result(result)
                                except Exception as cb_exc:
                                    try:
                                        conn.rollback()
                                    except Exception:
                                        pass
                                    raise cb_exc
                        except Exception as e:
                            logger.debug("SQLiteWriteQueue error in task: %s", e)
                            if not future.done():
                                future.set_exception(e)
                        finally:
                            if conn:
                                try:
                                    conn.close()
                                except Exception:
                                    pass

            except TimeoutError as e:
                # Flock timeout — another process holds the lock (e.g.
                # journal reconciler).  Retry once after a brief pause
                # instead of crashing the write queue thread.
                logger.warning("SQLiteWriteQueue flock timeout (retrying): %s", e)
                time.sleep(1.0)
                try:
                    from infra.db_path_flock import db_path_flock as _retry_flock
                    with _retry_flock(db_path):
                        pass  # just verify we can acquire
                except Exception:
                    logger.error("SQLiteWriteQueue flock retry also failed: %s", e)
                    if not future.done():
                        future.set_exception(e)
            except Exception as e:
                logger.error("Fatal error in SQLiteWriteQueue run loop: %s", e)
                if not future.done():
                    future.set_exception(e)
            finally:
                with self._pending_lock:
                    self._pending_futures.discard(future)
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    def restart(self, timeout: float = 5.0) -> None:
        """Stop the current worker thread and start a fresh one in-place.

        Preserves the same object identity so any existing references to
        this queue (e.g. from other modules that imported the singleton)
        remain valid and use the new worker thread immediately.
        """
        self.stop(timeout=timeout)
        self._shutdown.clear()
        self._queue = queue.Queue()
        self._sessions.clear()
        self._dead_sessions.clear()
        self._pending_futures.clear()
        self._path_conns.clear()
        self._session_cmd_queues.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="SQLiteWriteQueueThread",
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Gracefully stop the background worker thread.

        Sets a shutdown flag so the main loop can break out after the
        current task.  Cancels all pending futures, drains remaining tasks,
        then joins with a generous timeout for the in-flight task to
        complete and release its flock/connection.
        """
        self._shutdown.set()
        # Cancel all pending futures so callers don't hang indefinitely
        with self._pending_lock:
            pending = list(self._pending_futures)
        for f in pending:
            if not f.done():
                f.set_exception(RuntimeError("Write queue shutting down"))
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
                try:
                    self._queue.task_done()
                except ValueError:
                    pass
                drained += 1
            except queue.Empty:
                break
        try:
            self._queue.put(None)
        except Exception:
            pass
        # Forcefully interrupt any active sessions blocked on cmd_queue.get()
        with self._sessions_lock:
            for cmd_queue in list(self._session_cmd_queues.values()):
                try:
                    cmd_queue.put_nowait(None)
                except Exception:
                    pass
        self._thread.join(timeout=timeout)


# Global Singleton write queue
sqlite_write_queue = SQLiteWriteQueue()
