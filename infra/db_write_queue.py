"""SQLite Single-Writer thread queue for Agentic Memory.

Channels all mutating SQL queries (INSERT, UPDATE, DELETE) and transactional
write operations to a single background worker thread to completely eliminate
lock contention and database locks in concurrent multithreaded workflows.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Union

logger = logging.getLogger(__name__)


class ProxyCursor:
    description: Optional[tuple]
    _data: list
    lastrowid: Optional[int]
    rowcount: int
    _idx: int

    def __init__(
        self,
        description: Optional[tuple],
        fetchall_data: list,
        lastrowid: Optional[int],
        rowcount: int,
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
    rowcount: int
    _data: list
    _idx: int

    def __init__(self, connection: ProxyConnection) -> None:
        self.connection = connection
        self.description = None
        self.lastrowid = None
        self.rowcount = -1
        self._data = []
        self._idx = 0

    def execute(self, sql: str, params: tuple = ()) -> ProxyCursorObject:
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
    def __init__(self, cmd_queue: queue.Queue, resp_queue: queue.Queue) -> None:
        self._cmd_queue = cmd_queue
        self._resp_queue = resp_queue
        self.row_factory: Optional[Any] = None
        self._closed = False

    def cursor(self) -> ProxyCursorObject:
        return ProxyCursorObject(self)

    @property
    def in_transaction(self) -> bool:
        return True

    def execute(self, sql: str, params: tuple = ()) -> ProxyCursor:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed connection.")
        if sql.strip().upper().startswith("BEGIN"):
            # The write queue session is already in a transaction (started with BEGIN IMMEDIATE)
            return ProxyCursor(None, [], None, None)
        self._cmd_queue.put(("execute", (sql, params)))
        status, res = self._resp_queue.get()
        if status == "error":
            raise res
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
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed connection.")
        self._cmd_queue.put(("executemany", (sql, list(params_seq))))
        status, res = self._resp_queue.get()
        if status == "error":
            raise res
        lastrowid, rowcount, _, description = res
        return ProxyCursor(description, [], lastrowid, rowcount)

    def executescript(self, sql_script: str) -> ProxyConnection:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed connection.")
        self._cmd_queue.put(("executescript", sql_script))
        status, res = self._resp_queue.get()
        if status == "error":
            raise res
        return self

    def commit(self) -> None:
        if self._closed:
            return
        self._cmd_queue.put(("commit", None))
        status, res = self._resp_queue.get()
        if status == "error":
            raise res

    def rollback(self) -> None:
        if self._closed:
            return
        self._cmd_queue.put(("rollback", None))
        status, res = self._resp_queue.get()
        if status == "error":
            raise res

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cmd_queue.put(("close", None))
        try:
            self._resp_queue.get()
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

    def start_session(self, db_path: Union[str, Path]) -> ProxyConnection:
        """Start a write session proxy that executes transactions on the write queue thread."""
        cmd_queue: queue.Queue = queue.Queue()
        resp_queue: queue.Queue = queue.Queue()
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((Path(db_path), "session", (cmd_queue, resp_queue), future))
        future.result(timeout=30.0)
        return ProxyConnection(cmd_queue, resp_queue)

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
        future: concurrent.futures.Future = concurrent.futures.Future()
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
        future: concurrent.futures.Future = concurrent.futures.Future()
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

                lock_ctx = db_path_flock(db_path)
                with lock_ctx:
                    conn = None
                    try:
                        conn = sqlite3.connect(str(db_path), timeout=30.0)
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA busy_timeout=30000")
                        conn.execute("PRAGMA foreign_keys=ON")

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

                        elif task_type == "session":
                            cmd_queue, resp_queue = payload
                            conn.execute("BEGIN IMMEDIATE")
                            try:
                                future.set_result(True)
                                while True:
                                    try:
                                        cmd = cmd_queue.get(timeout=10.0)
                                    except queue.Empty:
                                        try:
                                            conn.rollback()
                                        except Exception:
                                            pass
                                        break
                                    if cmd is None:
                                        conn.commit()
                                        break
                                    action, act_payload = cmd
                                    try:
                                        if action == "execute":
                                            sql, params = act_payload
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
                                            resp_queue.put(
                                                ("success", (None, None, [], None))
                                            )
                                        elif action == "commit":
                                            conn.commit()
                                            conn.execute("BEGIN IMMEDIATE")
                                            resp_queue.put(("success", None))
                                        elif action == "rollback":
                                            conn.rollback()
                                            conn.execute("BEGIN IMMEDIATE")
                                            resp_queue.put(("success", None))
                                        elif action == "close":
                                            conn.commit()
                                            resp_queue.put(("success", None))
                                            break
                                    except Exception as q_exc:
                                        resp_queue.put(("error", q_exc))
                            except Exception as sess_exc:
                                try:
                                    conn.rollback()
                                except Exception:
                                    pass
                                if not future.done():
                                    future.set_exception(sess_exc)

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

            except Exception as e:
                logger.error("Fatal error in SQLiteWriteQueue run loop: %s", e)
                if not future.done():
                    future.set_exception(e)
            finally:
                self._queue.task_done()

    def stop(self, timeout: float = 15.0) -> None:
        """Gracefully stop the background worker thread.

        Sets a shutdown flag so the main loop can break out even if it's
        currently blocked on a session's inner cmd_queue (10s timeout).
        Then drains any remaining tasks before joining.  Pending writes
        resolve normally; a timed-out join means the daemon thread will
        be reaped by the OS at process exit.
        """
        self._shutdown.set()
        try:
            self._queue.put(None)
        except Exception:
            pass
        self._thread.join(timeout=timeout)


# Global Singleton write queue
sqlite_write_queue = SQLiteWriteQueue()
