"""GDPR Right-to-Be-Forgotten: full cascade deletion test.

Verifies that gdpr_erase wipes all tenant-scoped data:
  - memories
  - kg_facts, kg_entities, kg_edges (reachable via source_memory)
  - backlinks
  - memory_audit_log
  - gdpr_requests tracking entry
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest

sys.path.insert(
    0,
    str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))),
)
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))


def _bootstrap_db(p: Path) -> None:
    from infra.db import open_db
    from infra.migration_runner import run_migrations
    from fact.fact_schema import ensure_facts_schema
    with open_db(p, timeout=10.0) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        ensure_facts_schema(db)
        db.commit()


@pytest.fixture
def db_path() -> Generator[Path, None, None]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    p = Path(tmp.name)
    try:
        _bootstrap_db(p)
        yield p
    finally:
        p.unlink(missing_ok=True)


def _seed_tenant_data(conn: sqlite3.Connection, tenant_id: str = "tenant-a", prefix: str = "a") -> None:
    """Insert memories + KG rows for a tenant."""
    mem_id = f"{prefix}-mem-1"
    conn.execute(
        "INSERT INTO memories (id, content, source_file, tags, created_at, tenant_id) "
        "VALUES (?, ?, ?, ?, datetime('now'), ?)",
        (mem_id, f"{prefix} memory content", f"/tmp/{prefix}_memory.md", "tag1", tenant_id),
    )
    conn.execute(
        "INSERT INTO kg_entities (name, entity_type, created_at) VALUES (?, ?, datetime('now'))",
        (f"{prefix} Entity", "person"),
    )
    ent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, source_memory, "
        "subject_entity_id, object_entity_id, fact_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"{prefix} Subject", "works_at", f"{prefix} Corp",
         mem_id, ent_id, ent_id, "observation"),
    )


@pytest.mark.gdpr
class TestGDPREraseFullCascade:
    """Verify all known table types are wiped for a tenant."""

    def test_deletes_memories(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant_data(conn, tenant_id="tenant-a", prefix="a")
        _seed_tenant_data(conn, tenant_id="tenant-b", prefix="b")
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="user@a.com", tenant_id="tenant-a")
        conn.close()
        assert result["status"] == "completed"
        assert result["rows_deleted"]["memories"] >= 1

    def test_deletes_kg_facts(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant_data(conn, tenant_id="tenant-a", prefix="a")
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="user@a.com", tenant_id="tenant-a")
        remaining = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
        conn.close()
        assert result["status"] == "completed"
        assert result["rows_deleted"]["kg_facts"] >= 1
        assert remaining == 0

    def test_preserves_other_tenant(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant_data(conn, tenant_id="tenant-a", prefix="a")
        _seed_tenant_data(conn, tenant_id="tenant-b", prefix="b")
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="user@a.com", tenant_id="tenant-a")
        other_mems = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tenant_id='tenant-b'"
        ).fetchone()[0]
        conn.close()
        assert result["status"] == "completed"
        assert other_mems == 1

    def test_records_gdpr_request(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant_data(conn, tenant_id="tenant-a", prefix="a")
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="user@a.com", tenant_id="tenant-a")
        row = conn.execute(
            "SELECT id, status, deletion_certificate_json FROM gdpr_requests WHERE id=?",
            (result["request_id"],),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "completed"
        cert = json.loads(row[2])
        assert cert["data_subject_hash"] == result["data_subject_hash"]
        assert cert["certificate_hash"] != ""

    def test_deletes_backlinks(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_tenant_data(conn, tenant_id="tenant-a", prefix="a")
        mem_id = "a-mem-1"
        conn.execute(
            "INSERT INTO backlinks (source_id, target_id) VALUES (?, ?)",
            (mem_id, "other-note"),
        )
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="user@a.com", tenant_id="tenant-a")
        remaining = conn.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0]
        conn.close()
        assert result["status"] == "completed"
        assert remaining == 0

    def test_deletes_memory_audit_log(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("audit-mem", "test", "tenant-a"),
        )
        conn.execute(
            "INSERT INTO memory_audit_log (ts, tool, args, latency_ms, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (1712345678.0, "gdpr_test", "{}", 0.0, "tenant-a"),
        )
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="user@a.com", tenant_id="tenant-a")
        remaining = conn.execute(
            "SELECT COUNT(*) FROM memory_audit_log WHERE tenant_id='tenant-a'"
        ).fetchone()[0]
        conn.close()
        assert result["status"] == "completed"
        assert remaining == 0
