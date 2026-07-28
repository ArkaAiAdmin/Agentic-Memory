#!/usr/bin/env python3
"""Standalone verification: KG CRDT sync converges across two peers.

Run: ./venv/bin/python eval/verify_kg_sync_convergence.py
"""
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.sync_server import SyncServer
from infra.sync_client import sync_kg_with_peer
from kg.kg_crdt import (
    record_entity_add,
    record_edge_add,
    project_crdt_to_entities,
    ensure_kg_crdt_schema,
)


def _wait(host, port, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.5)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server didn't start")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _init_kg(db_path):
    """Create a fully-migrated DB using the project's real schema setup.

    The sync server runs ``run_schema_setup`` on first open; pre-creating the
    full schema here (via the same path) avoids migration conflicts and
    ensures kg_entities / kg_edges / kg_*_crdt all exist with the right columns.
    """
    import sqlite3

    from infra.db_migrations import run_schema_setup

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    run_schema_setup(conn)
    ensure_kg_crdt_schema(conn)
    conn.commit()
    conn.close()


def _make_server(db_path, agent_id):
    port = _free_port()
    srv = SyncServer(db_path=str(db_path), agent_id=agent_id,
                     host="127.0.0.1", port=port)
    srv.port = port
    srv.start()
    _wait("127.0.0.1", port)
    return srv, f"http://127.0.0.1:{port}"


def main():
    import urllib.request as _ur

    _orig = _ur.Request

    class _P(_orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            if not self.get_header("X-Sync-Timestamp"):
                self.add_header("X-Sync-Timestamp", str(int(time.time())))

    _ur.Request = _P

    dir_a = Path(tempfile.mkdtemp(prefix="kgpeerA_"))
    dir_b = Path(tempfile.mkdtemp(prefix="kgpeerB_"))
    db_a = dir_a / "memory.db"
    db_b = dir_b / "memory.db"
    _init_kg(db_a)
    _init_kg(db_b)

    # Peer A: an entity + an edge
    import sqlite3
    with sqlite3.connect(str(db_a), timeout=10.0) as conn:
        ensure_kg_crdt_schema(conn)
        record_entity_add(conn, 1001, "agent-A", {"agent-A": 1}, "Quantum", "concept", "qc", "fp-q")
        record_entity_add(conn, 1002, "agent-A", {"agent-A": 1}, "Entanglement", "concept", "ent", "fp-e")
        record_edge_add(conn, 1001, 1002, "relates_to", 1.0, "agent-A", {"agent-A": 1})
        project_crdt_to_entities(conn)

    # Peer B: a different entity + edge
    import sqlite3
    with sqlite3.connect(str(db_b), timeout=10.0) as conn:
        ensure_kg_crdt_schema(conn)
        record_entity_add(conn, 2001, "agent-B", {"agent-B": 1}, "Photon", "concept", "ph", "fp-p")
        record_edge_add(conn, 2001, 1001, "relates_to", 0.5, "agent-B", {"agent-B": 1})
        project_crdt_to_entities(conn)

    srv_a, url_a = _make_server(db_a, "agent-A")
    srv_b, url_b = _make_server(db_b, "agent-B")

    # Sync A -> B (push A's state to B, pull B's state into A)
    res_ab = sync_kg_with_peer(db_b, url_a, "agent-A", "agent-B", since_ts=0)
    res_ba = sync_kg_with_peer(db_a, url_b, "agent-B", "agent-A", since_ts=0)

    srv_a.stop()
    srv_b.stop()

    import sqlite3
    with sqlite3.connect(str(db_a), timeout=10.0) as conn:
        ensure_kg_crdt_schema(conn)
        project_crdt_to_entities(conn)
        a_ents = conn.execute("SELECT name FROM kg_entities ORDER BY name").fetchall()
    import sqlite3
    with sqlite3.connect(str(db_b), timeout=10.0) as conn:
        ensure_kg_crdt_schema(conn)
        project_crdt_to_entities(conn)
        b_ents = conn.execute("SELECT name FROM kg_entities ORDER BY name").fetchall()

    a_names = {r[0] for r in a_ents}
    b_names = {r[0] for r in b_ents}
    expected = {"Quantum", "Entanglement", "Photon"}

    print("res A->B:", json.dumps(res_ab))
    print("res B->A:", json.dumps(res_ba))
    print("peer A entities:", sorted(a_names))
    print("peer B entities:", sorted(b_names))

    assert a_names == expected, f"peer A missing: {expected - a_names}"
    assert b_names == expected, f"peer B missing: {expected - b_names}"
    print("PASS: both peers converged to", sorted(expected))


if __name__ == "__main__":
    main()
    sys.exit(0)
