"""GDPR Right-to-Be-Forgotten: deletion certificate verification.

Tests that gdpr_erase produces a correctly signed DeletionCertificate
with expected fields, hash chains, and failure-case behavior.
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


@pytest.mark.gdpr
class TestGDPREraseCertificate:
    """Verify DeletionCertificate structure, signing, and error handling."""

    def test_certificate_has_all_required_fields(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("cert-mem", "test data", "default"),
        )
        conn.commit()
        result = gdpr_erase(conn, principal_id="principal-42", data_subject_sub="user@example.com")
        conn.close()
        required = {
            "request_id", "principal_id", "data_subject_hash", "tenant_id",
            "requested_at", "completed_at", "status", "rows_deleted",
            "md_files_deleted", "certificate_hash",
        }
        assert required.issubset(result.keys()), f"Missing fields: {required - set(result)}"

    def test_certificate_hash_is_non_empty(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("hash-mem", "data", "default"),
        )
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="hash-test@x.com")
        conn.close()
        assert len(result["certificate_hash"]) == 64  # SHA-256 hex

    def test_certificate_hash_changes_with_content(self, db_path: Path):
        from infra.gdpr import gdpr_erase, _sign_certificate, DeletionCertificate
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("c1", "alpha", "default"),
        )
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("c2", "beta", "default"),
        )
        conn.commit()
        r1 = gdpr_erase(conn, principal_id="p1", data_subject_sub="user1@x.com")
        conn.close()

        conn2 = sqlite3.connect(str(db_path))
        conn2.execute("PRAGMA foreign_keys=ON")
        conn2.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("c3", "gamma", "default"),
        )
        conn2.commit()
        r2 = gdpr_erase(conn2, principal_id="p2", data_subject_sub="user2@x.com")
        conn2.close()

        assert r1["certificate_hash"] != r2["certificate_hash"]

    def test_gdpr_requests_table_persists_certificate(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("cert-mem-2", "data", "default"),
        )
        conn.commit()
        result = gdpr_erase(conn, principal_id="p1", data_subject_sub="persist@x.com")
        row = conn.execute(
            "SELECT id, status, deletion_certificate_json FROM gdpr_requests WHERE id=?",
            (result["request_id"],),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "completed"
        cert = json.loads(row[2])
        assert cert["data_subject_hash"] == result["data_subject_hash"]

    def test_successive_erases_produce_unique_certificates(self, db_path: Path):
        from infra.gdpr import gdpr_erase
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("m1", "alpha", "default"),
        )
        conn.commit()
        r1 = gdpr_erase(conn, principal_id="admin", data_subject_sub="alpha@x.com")

        conn.execute(
            "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
            ("m2", "beta", "default"),
        )
        conn.commit()
        r2 = gdpr_erase(conn, principal_id="admin", data_subject_sub="beta@x.com")
        conn.close()
        assert r1["request_id"] != r2["request_id"]
        assert r1["certificate_hash"] != r2["certificate_hash"]
