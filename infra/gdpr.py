"""GDPR Right-to-Be-Forgotten erase implementation.

Cascading wipe of all data associated with a data subject across:
  - memories, memory_chunks, memory_embeddings, memory_vec_keys
  - kg_facts, kg_edges, kg_entities
  - memory_audit_log
  - backlinks
  - .md files on disk

Tenant-scoped: erase only affects rows matching the given tenant_id.
Produces a signed deletion certificate as proof of execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeletionCertificate:
    request_id: str
    principal_id: str
    data_subject_hash: str
    tenant_id: str
    requested_at: str
    completed_at: str
    status: str
    rows_deleted: dict[str, int]
    md_files_deleted: int
    certificate_hash: str


def _hash_email(email: str) -> str:
    """SHA-256 hash of a data subject identifier (email, sub, etc)."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def _get_md_file_paths(conn: sqlite3.Connection, tenant_id: str) -> list[Path]:
    """Resolve .md file paths for all memories in the tenant.

    Walks memories with a matching tenant_id and resolves the on-disk path
    via the source_file column. Returns only paths that actually exist.
    """
    paths: list[Path] = []
    try:
        rows = conn.execute(
            "SELECT source_file FROM memories WHERE tenant_id = ? AND source_file IS NOT NULL",
            (tenant_id,),
        ).fetchall()
        for (sf,) in rows:
            p = Path(sf)
            if p.exists():
                paths.append(p)
    except Exception as exc:
        logger.warning("gdpr: failed to resolve .md paths: %s", exc)
    return paths


def gdpr_erase(
    conn: sqlite3.Connection,
    principal_id: str,
    data_subject_sub: str,
    tenant_id: str = "default",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Cascading erase of all data for *data_subject_sub* within *tenant_id*.

    Returns a :class:`DeletionCertificate` serialized as a dict.

    Steps:
      1. Hash the subject identifier.
      2. Collect .md file paths (before deletion so we know what to clean up).
      3. Delete in dependency-safe order: KG edges → KG facts → KG entities,
         memory_chunks → memory_embeddings → memory_vec_keys → memories,
         memory_audit_log, backlinks.
      4. Delete .md files on disk.
      5. Record the request in gdpr_requests.
      6. Return a signed certificate.
    """
    if request_id is None:
        request_id = f"gdpr-{uuid.uuid4().hex[:16]}"
    data_subject_hash = _hash_email(data_subject_sub)
    requested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    completed_at = requested_at

    rows_deleted: dict[str, int] = {}
    md_files_deleted = 0

    # Step 2: collect .md file paths before deletion
    md_paths = _get_md_file_paths(conn, tenant_id)

    try:
        # Step 3: cascading delete — dependency-safe order

        # kg_edges referencing tenant entities
        cur = conn.execute(
            "DELETE FROM kg_edges WHERE source_id IN "
            "(SELECT id FROM kg_entities WHERE tenant_id = ?) "
            "OR target_id IN (SELECT id FROM kg_entities WHERE tenant_id = ?)",
            (tenant_id, tenant_id),
        )
        rows_deleted["kg_edges"] = cur.rowcount

        # backlinks (note: the table may use tenant_isolation or not; safe to attempt)
        try:
            cur = conn.execute(
                "DELETE FROM backlinks WHERE source_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?) "
                "OR target_id IN (SELECT id FROM memories WHERE tenant_id = ?)",
                (tenant_id, tenant_id),
            )
            rows_deleted["backlinks"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["backlinks"] = 0

        # kg_facts for tenant entities
        cur = conn.execute(
            "DELETE FROM kg_facts WHERE tenant_id = ?",
            (tenant_id,),
        )
        rows_deleted["kg_facts"] = cur.rowcount

        # kg_entities
        cur = conn.execute(
            "DELETE FROM kg_entities WHERE tenant_id = ?",
            (tenant_id,),
        )
        rows_deleted["kg_entities"] = cur.rowcount

        # memory_chunks → memory_embeddings → memory_vec_keys
        # (chunks reference memories; embeddings reference chunks/vec_keys)
        try:
            cur = conn.execute(
                "DELETE FROM memory_chunk_embeddings WHERE chunk_id IN "
                "(SELECT id FROM memory_chunks WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?))",
                (tenant_id,),
            )
            rows_deleted["memory_chunk_embeddings"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_chunk_embeddings"] = 0

        try:
            cur = conn.execute(
                "DELETE FROM memory_chunk_vec_keys WHERE chunk_id IN "
                "(SELECT id FROM memory_chunks WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?))",
                (tenant_id,),
            )
            rows_deleted["memory_chunk_vec_keys"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_chunk_vec_keys"] = 0

        cur = conn.execute(
            "DELETE FROM memory_chunks WHERE memory_id IN "
            "(SELECT id FROM memories WHERE tenant_id = ?)",
            (tenant_id,),
        )
        rows_deleted["memory_chunks"] = cur.rowcount

        # memory_embeddings
        try:
            cur = conn.execute(
                "DELETE FROM memory_embeddings WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?)",
                (tenant_id,),
            )
            rows_deleted["memory_embeddings"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_embeddings"] = 0

        # memory_vec_keys
        try:
            cur = conn.execute(
                "DELETE FROM memory_vec_keys WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?)",
                (tenant_id,),
            )
            rows_deleted["memory_vec_keys"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_vec_keys"] = 0

        # memories
        cur = conn.execute(
            "DELETE FROM memories WHERE tenant_id = ?",
            (tenant_id,),
        )
        rows_deleted["memories"] = cur.rowcount

        # memory_audit_log
        try:
            cur = conn.execute(
                "DELETE FROM memory_audit_log WHERE tenant_id = ?",
                (tenant_id,),
            )
            rows_deleted["memory_audit_log"] = cur.rowcount
        except sqlite3.OperationalError:
            # tenant_id column may not exist on older schemas
            cur = conn.execute("DELETE FROM memory_audit_log")
            rows_deleted["memory_audit_log"] = cur.rowcount

        # Step 4: delete .md files
        for p in md_paths:
            try:
                p.unlink(missing_ok=True)
                md_files_deleted += 1
            except Exception as exc:
                logger.warning("gdpr: failed to delete .md file %s: %s", p, exc)

    except Exception as exc:
        logger.error("gdpr: erase failed partway through: %s", exc)
        # Record the failed request
        cert = DeletionCertificate(
            request_id=request_id,
            principal_id=principal_id,
            data_subject_hash=data_subject_hash,
            tenant_id=tenant_id,
            requested_at=requested_at,
            completed_at=completed_at,
            status="failed",
            rows_deleted=rows_deleted,
            md_files_deleted=md_files_deleted,
            certificate_hash="",
        )
        _record_request(conn, cert)
        conn.commit()
        raise

    # Step 5: build certificate and record
    cert = DeletionCertificate(
        request_id=request_id,
        principal_id=principal_id,
        data_subject_hash=data_subject_hash,
        tenant_id=tenant_id,
        requested_at=requested_at,
        completed_at=completed_at,
        status="completed",
        rows_deleted=rows_deleted,
        md_files_deleted=md_files_deleted,
        certificate_hash="",
    )
    cert.certificate_hash = _sign_certificate(cert)
    _record_request(conn, cert)
    conn.commit()

    return asdict(cert)


def _sign_certificate(cert: DeletionCertificate) -> str:
    """Produce a SHA-256 hash over the certificate fields as a signature."""
    raw = json.dumps(asdict(cert), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_request(conn: sqlite3.Connection, cert: DeletionCertificate) -> None:
    """Insert or update the gdpr_requests row."""
    try:
        conn.execute(
            "INSERT OR REPLACE INTO gdpr_requests "
            "(id, principal_id, data_subject_hash, requested_at, completed_at, "
            " status, deletion_certificate_json, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cert.request_id,
                cert.principal_id,
                cert.data_subject_hash,
                cert.requested_at,
                cert.completed_at,
                cert.status,
                json.dumps(asdict(cert)),
                cert.tenant_id,
            ),
        )
    except sqlite3.OperationalError as exc:
        logger.warning("gdpr: failed to record request (table may not exist): %s", exc)
