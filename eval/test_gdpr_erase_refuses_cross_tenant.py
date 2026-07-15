"""GDPR Right-to-Be-Forgotten: cross-tenant isolation tests.

Verifies that gdpr_erase refuses or limits operations when:
  - Erasing a tenant only affects that tenant's data
  - The function does not leak data across tenants
"""

from __future__ import annotations

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


def _seed_two_tenants(conn: sqlite3.Connection) -> int:
    """Insert memories for two tenants with overlapping KG data.

    Returns the shared entity ID.
    """
    conn.execute(
        "INSERT INTO memories (id, content, tenant_id, data_subject_sub) VALUES (?, ?, ?, ?)",
        ("mem-tenant-a", "A data", "tenant-a", "a@co.com"),
    )
    conn.execute(
        "INSERT INTO memories (id, content, tenant_id, data_subject_sub) VALUES (?, ?, ?, ?)",
        ("mem-tenant-b", "B data", "tenant-b", "b@co.com"),
    )
    # Shared entity referenced by both tenants' facts (INTEGER PK)
    conn.execute(
        "INSERT INTO kg_entities (name, entity_type) VALUES (?, ?)",
        ("Shared", "concept"),
    )
    ent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, source_memory, "
        "subject_entity_id, object_entity_id, fact_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("A", "has", "shared", "mem-tenant-a", ent_id, ent_id, "observation"),
    )
    conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, source_memory, "
        "subject_entity_id, object_entity_id, fact_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("B", "has", "shared", "mem-tenant-b", ent_id, ent_id, "observation"),
    )
    return ent_id


@pytest.mark.gdpr
class TestGDPREraseRefusesCrossTenant:
    """Verify that GDPR erase is properly scoped to one tenant."""

    def test_erase_tenant_a_does_not_touch_tenant_b_memories(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_two_tenants(conn)
        conn.commit()
        result = gdpr_erase(conn, principal_id="admin", data_subject_sub="a@co.com", tenant_id="tenant-a")
        remaining_a = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tenant_id='tenant-a'"
        ).fetchone()[0]
        remaining_b = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tenant_id='tenant-b'"
        ).fetchone()[0]
        conn.close()
        assert result["status"] == "completed"
        assert remaining_a == 0, "tenant-a memories should all be deleted"
        assert remaining_b == 1, "tenant-b memories should be preserved"

    def test_erase_tenant_a_only_deletes_its_facts(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        _seed_two_tenants(conn)
        conn.commit()
        result = gdpr_erase(conn, principal_id="admin", data_subject_sub="a@co.com", tenant_id="tenant-a")
        remaining_sources = [
            r[0]
            for r in conn.execute("SELECT source_memory FROM kg_facts").fetchall()
        ]
        conn.close()
        assert result["status"] == "completed"
        assert "mem-tenant-a" not in remaining_sources, "tenant-a facts should be deleted"
        assert "mem-tenant-b" in remaining_sources, "tenant-b facts should survive"

    def test_shared_entity_preserved_when_one_tenant_erased(self, db_path: Path):
        """A shared entity referenced by both tenants is not deleted when only one is erased."""
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        shared_id = _seed_two_tenants(conn)
        conn.commit()
        result = gdpr_erase(conn, principal_id="admin", data_subject_sub="a@co.com", tenant_id="tenant-a")
        remaining_ents = {r[0] for r in conn.execute("SELECT id FROM kg_entities").fetchall()}
        conn.close()
        assert result["status"] == "completed"
        assert shared_id in remaining_ents, "shared entity should survive"

    def test_default_tenant_does_not_leak_to_named_tenant(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id, data_subject_sub) VALUES (?, ?, ?, ?)",
            ("default-mem", "default data", "default", "default@x.com"),
        )
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id, data_subject_sub) VALUES (?, ?, ?, ?)",
            ("named-mem", "named data", "tenant-x", "named@x.com"),
        )
        conn.commit()
        result = gdpr_erase(conn, principal_id="admin", data_subject_sub="default@x.com", tenant_id="default")
        default_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tenant_id='default'"
        ).fetchone()[0]
        named_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tenant_id='tenant-x'"
        ).fetchone()[0]
        conn.close()
        assert result["status"] == "completed"
        assert default_count == 0
        assert named_count == 1
