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


def _get_md_file_paths(
    conn: sqlite3.Connection,
    tenant_id: str,
    data_subject_sub: str | None = None,
) -> list[Path]:
    """Resolve .md file paths for memories belonging to a data subject.

    Walks memories with a matching tenant_id (and optionally a matching
    data_subject_sub) and resolves the on-disk path via the source_file
    column. Returns only paths that actually exist.
    """
    paths: list[Path] = []
    try:
        if data_subject_sub:
            rows = conn.execute(
                "SELECT source_file FROM memories "
                "WHERE tenant_id = ? AND data_subject_sub = ? "
                "AND source_file IS NOT NULL",
                (tenant_id, data_subject_sub),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_file FROM memories "
                "WHERE tenant_id = ? AND source_file IS NOT NULL",
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
    md_paths = _get_md_file_paths(conn, tenant_id, data_subject_sub)

    try:
        # Step 3: cascading delete — dependency-safe order
        #
        # NOTE: kg_facts, kg_entities, kg_edges, and backlinks do NOT
        # have a tenant_id column. We resolve the tenant's memory IDs
        # first, then walk the KG graph to find the affected rows.

        # Collect memory IDs for the target tenant + subject.
        # When data_subject_sub is set, only erase that subject's memories.
        # When NULL (backward compat), erase the entire tenant.
        if data_subject_sub:
            memory_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM memories "
                    "WHERE tenant_id = ? AND data_subject_sub = ?",
                    (tenant_id, data_subject_sub),
                ).fetchall()
            ]
        else:
            memory_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM memories WHERE tenant_id = ?", (tenant_id,)
                ).fetchall()
            ]

        # backlinks reference memory IDs; no tenant_id column on the table
        if memory_ids:
            placeholders = ",".join("?" * len(memory_ids))
            try:
                cur = conn.execute(
                    f"DELETE FROM backlinks WHERE source_id IN ({placeholders}) "
                    f"OR target_id IN ({placeholders})",
                    memory_ids + memory_ids,
                )
                rows_deleted["backlinks"] = cur.rowcount
            except sqlite3.OperationalError:
                rows_deleted["backlinks"] = 0
        else:
            rows_deleted["backlinks"] = 0

        # kg_facts reference tenant memories via source_memory
        if memory_ids:
            placeholders = ",".join("?" * len(memory_ids))
            cur = conn.execute(
                f"DELETE FROM kg_facts WHERE source_memory IN ({placeholders})",
                memory_ids,
            )
            rows_deleted["kg_facts"] = cur.rowcount

            # Collect orphan KG entity IDs for cleanup.  We re-query for
            # any remaining kg_facts referencing these entities (facts
            # from other tenants might still reference them) and only
            # delete entities that are now fully orphaned.
            entity_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT e.id FROM kg_entities e "
                    "WHERE e.id IN ("
                    "  SELECT subject_entity_id FROM kg_facts WHERE source_memory IN ({0})"
                    "  UNION "
                    "  SELECT object_entity_id FROM kg_facts WHERE source_memory IN ({0})"
                    ")".format(placeholders),
                    memory_ids + memory_ids,
                ).fetchall()
            ]
            if entity_ids:
                e_placeholders = ",".join("?" * len(entity_ids))
                # kg_edges referencing these entities
                cur = conn.execute(
                    f"DELETE FROM kg_edges WHERE source_id IN ({e_placeholders}) "
                    f"OR target_id IN ({e_placeholders})",
                    entity_ids + entity_ids,
                )
                rows_deleted["kg_edges"] = cur.rowcount
                # Only delete entities that no longer have any facts
                cur = conn.execute(
                    f"DELETE FROM kg_entities WHERE id IN ({e_placeholders}) "
                    f"AND id NOT IN (SELECT subject_entity_id FROM kg_facts) "
                    f"AND id NOT IN (SELECT object_entity_id FROM kg_facts)",
                    entity_ids,
                )
                rows_deleted["kg_entities"] = cur.rowcount
            else:
                rows_deleted["kg_edges"] = 0
                rows_deleted["kg_entities"] = 0
        else:
            rows_deleted["kg_facts"] = 0
            rows_deleted["kg_edges"] = 0
            rows_deleted["kg_entities"] = 0

        # memory_chunks (parent_id = memories.id), chunk_vec_keys,
        # chunk_embeddings, then top-level memory_embeddings and
        # memory_vec_keys.
        # memory_chunk_vec_keys — FK to memory_chunk_embeddings
        try:
            if data_subject_sub:
                cur = conn.execute(
                    "DELETE FROM memory_chunk_vec_keys WHERE chunk_id IN "
                    "(SELECT id FROM memory_chunks WHERE parent_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ? "
                    "AND data_subject_sub = ?))",
                    (tenant_id, data_subject_sub),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM memory_chunk_vec_keys WHERE chunk_id IN "
                    "(SELECT id FROM memory_chunks WHERE parent_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ?))",
                    (tenant_id,),
                )
            rows_deleted["memory_chunk_vec_keys"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_chunk_vec_keys"] = 0

        # memory_chunk_embeddings — FK to memories via parent_id
        try:
            if data_subject_sub:
                cur = conn.execute(
                    "DELETE FROM memory_chunk_embeddings WHERE chunk_id IN "
                    "(SELECT id FROM memory_chunks WHERE parent_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ? "
                    "AND data_subject_sub = ?))",
                    (tenant_id, data_subject_sub),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM memory_chunk_embeddings WHERE chunk_id IN "
                    "(SELECT id FROM memory_chunks WHERE parent_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ?))",
                    (tenant_id,),
                )
            rows_deleted["memory_chunk_embeddings"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_chunk_embeddings"] = 0

        # memory_chunks — parent_id = memories.id
        if data_subject_sub:
            cur = conn.execute(
                "DELETE FROM memory_chunks WHERE parent_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ? "
                "AND data_subject_sub = ?)",
                (tenant_id, data_subject_sub),
            )
        else:
            cur = conn.execute(
                "DELETE FROM memory_chunks WHERE parent_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?)",
                (tenant_id,),
            )
        rows_deleted["memory_chunks"] = cur.rowcount

        # memory_embeddings — memory_id = memories.id
        try:
            if data_subject_sub:
                cur = conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ? "
                    "AND data_subject_sub = ?)",
                    (tenant_id, data_subject_sub),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ?)",
                    (tenant_id,),
                )
            rows_deleted["memory_embeddings"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_embeddings"] = 0

        # memory_vec_keys — memory_id = memories.id
        try:
            if data_subject_sub:
                cur = conn.execute(
                    "DELETE FROM memory_vec_keys WHERE memory_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ? "
                    "AND data_subject_sub = ?)",
                    (tenant_id, data_subject_sub),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM memory_vec_keys WHERE memory_id IN "
                    "(SELECT id FROM memories WHERE tenant_id = ?)",
                    (tenant_id,),
                )
            rows_deleted["memory_vec_keys"] = cur.rowcount
        except sqlite3.OperationalError:
            rows_deleted["memory_vec_keys"] = 0

        # memories
        if data_subject_sub:
            cur = conn.execute(
                "DELETE FROM memories WHERE tenant_id = ? "
                "AND data_subject_sub = ?",
                (tenant_id, data_subject_sub),
            )
        else:
            cur = conn.execute(
                "DELETE FROM memories WHERE tenant_id = ?",
                (tenant_id,),
            )
        rows_deleted["memories"] = cur.rowcount

        # memory_audit_log — ANONYMIZE, never bulk-delete (GAP 3).
        # Audit evidence must be retained for SOC2 (>=1yr) / HIPAA (>=6yr).
        # We tombstone the principal and redact the args payload, preserving
        # the structural row so the audit trail survives a data-subject erase.
        # When data_subject_sub is provided, only anonymize rows matching
        # that subject's principal_id (subject-scoped erase).
        tombstone = f"erased-subject-{data_subject_hash}"
        try:
            if data_subject_sub:
                cur = conn.execute(
                    "UPDATE memory_audit_log "
                    "SET principal_id = ?, args = ? "
                    "WHERE tenant_id = ? AND principal_id = ?",
                    (tombstone, '{"redacted": true}', tenant_id, data_subject_sub),
                )
            else:
                cur = conn.execute(
                    "UPDATE memory_audit_log "
                    "SET principal_id = ?, args = ? "
                    "WHERE tenant_id = ?",
                    (tombstone, '{"redacted": true}', tenant_id),
                )
            rows_deleted["memory_audit_log_anonymized"] = cur.rowcount
        except sqlite3.OperationalError:
            # Pre-V44 schema (no tenant_id column): never bulk-delete audit
            # evidence. Anonymize matching rows instead of erasing them.
            try:
                if data_subject_sub:
                    cur = conn.execute(
                        "UPDATE memory_audit_log "
                        "SET principal_id = ?, args = ? "
                        "WHERE principal_id = ?",
                        (tombstone, '{"redacted": true}', data_subject_sub),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE memory_audit_log SET principal_id = ?, args = ?",
                        (tombstone, '{"redacted": true}'),
                    )
                rows_deleted["memory_audit_log_anonymized"] = cur.rowcount
            except sqlite3.OperationalError:
                rows_deleted["memory_audit_log_anonymized"] = 0

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
