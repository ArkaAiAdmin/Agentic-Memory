"""Belief lifecycle management for agentic-memory.

Handles creation, review, retraction, deprecation, and evidence-chain
cascade for belief_assertions.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)


def ensure_belief_assertion(
    conn: AnyConnection,
    fact_id: int,
    memory_id: str | None = None,
    belief_status: str = "active",
    confidence: float = 1.0,
    epistemic_source: str = "agent",
    asserting_agent_id: str | None = None,
    evidence_chain: list[int] | None = None,
    rationale: str | None = None,
    certainty_tier: str = "likely",
    tenant_id: str = "default",
) -> int | None:
    """Create or update a belief_assertion for the given fact_id.

    Returns the belief_assertion id, or None on failure.
    """
    try:
        now = time.time()
        evidence_json = json.dumps(evidence_chain) if evidence_chain else None
        row = conn.execute(
            "SELECT id, belief_status FROM belief_assertions WHERE fact_id = ? AND tenant_id = ?",
            (fact_id, tenant_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE belief_assertions SET belief_status = ?, confidence = ?, "
                "epistemic_source = ?, asserting_agent_id = ?, evidence_chain = ?, "
                "rationale = ?, certainty_tier = ?, updated_at = ?, "
                "review_count = review_count + 1 "
                "WHERE id = ? AND tenant_id = ?",
                (
                    belief_status,
                    confidence,
                    epistemic_source,
                    asserting_agent_id,
                    evidence_json,
                    rationale,
                    certainty_tier,
                    now,
                    row[0],
                    tenant_id,
                ),
            )
            return int(row[0])
        cur = conn.execute(
            "INSERT INTO belief_assertions "
            "(fact_id, memory_id, belief_status, confidence, epistemic_source, "
            "asserting_agent_id, evidence_chain, rationale, certainty_tier, "
            "last_reviewed_at, review_count, created_at, updated_at, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                fact_id,
                memory_id,
                belief_status,
                confidence,
                epistemic_source,
                asserting_agent_id,
                evidence_json,
                rationale,
                certainty_tier,
                now,
                now,
                now,
                tenant_id,
            ),
        )
        return int(cur.lastrowid) if cur.lastrowid is not None else None
    except Exception as e:
        logger.warning("Failed to ensure belief_assertion for fact %s: %s", fact_id, e)
        return None


def get_beliefs_for_fact(
    conn: AnyConnection,
    fact_id: int,
    tenant_id: str = "default",
) -> dict | None:
    """Retrieve the belief_assertion for a given fact_id."""
    row = conn.execute(
        "SELECT id, fact_id, memory_id, belief_status, confidence, "
        "epistemic_source, asserting_agent_id, evidence_chain, rationale, "
        "certainty_tier, last_reviewed_at, review_count, created_at, updated_at "
        "FROM belief_assertions WHERE fact_id = ? AND tenant_id = ?",
        (fact_id, tenant_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "fact_id": row[1],
        "memory_id": row[2],
        "belief_status": row[3],
        "confidence": row[4],
        "epistemic_source": row[5],
        "asserting_agent_id": row[6],
        "evidence_chain": json.loads(row[7]) if row[7] else [],
        "rationale": row[8],
        "certainty_tier": row[9],
        "last_reviewed_at": row[10],
        "review_count": row[11],
        "created_at": row[12],
        "updated_at": row[13],
    }


def get_active_beliefs(
    conn: AnyConnection,
    min_confidence: float = 0.0,
    belief_status: str | None = "active",
    epistemic_source: str | None = None,
    certainty_tier: str | None = None,
    limit: int = 50,
    tenant_id: str = "default",
) -> list[dict]:
    """List belief assertions with optional filters."""
    clauses = ["ba.tenant_id = ?"]
    params: list[str | int | float] = [tenant_id]
    if belief_status is not None:
        clauses.append("ba.belief_status = ?")
        params.append(belief_status)
    if epistemic_source is not None:
        clauses.append("ba.epistemic_source = ?")
        params.append(epistemic_source)
    if certainty_tier is not None:
        clauses.append("ba.certainty_tier = ?")
        params.append(certainty_tier)
    if min_confidence > 0:
        clauses.append("ba.confidence >= ?")
        params.append(min_confidence)
    query = (
        "SELECT ba.id, ba.fact_id, ba.memory_id, ba.belief_status, ba.confidence, "
        "ba.epistemic_source, ba.asserting_agent_id, ba.evidence_chain, ba.rationale, "
        "ba.certainty_tier, ba.last_reviewed_at, ba.review_count, ba.created_at, ba.updated_at, "
        "kf.subject, kf.predicate, kf.object, kf.confidence as extraction_confidence "
        "FROM belief_assertions ba "
        "LEFT JOIN kg_facts kf ON kf.id = ba.fact_id AND kf.tenant_id = ba.tenant_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY ba.confidence DESC, ba.updated_at DESC "
        "LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    results = []
    for r in rows:
        results.append(
            {
                "id": r[0],
                "fact_id": r[1],
                "memory_id": r[2],
                "belief_status": r[3],
                "confidence": r[4],
                "epistemic_source": r[5],
                "asserting_agent_id": r[6],
                "evidence_chain": json.loads(r[7]) if r[7] else [],
                "rationale": r[8],
                "certainty_tier": r[9],
                "last_reviewed_at": r[10],
                "review_count": r[11],
                "created_at": r[12],
                "updated_at": r[13],
                "subject": r[14],
                "predicate": r[15],
                "object": r[16],
                "extraction_confidence": r[17],
            }
        )
    return results


def update_belief_status(
    conn: AnyConnection,
    fact_id: int,
    new_status: str,
    rationale: str | None = None,
    tenant_id: str = "default",
) -> bool:
    """Change a belief assertion's status (retract, deprecate, reactivate).

    Valid statuses: active, retracted, deprecated, unconfirmed.
    """
    valid = {"active", "retracted", "deprecated", "unconfirmed"}
    if new_status not in valid:
        logger.warning("Invalid belief status: %s (valid: %s)", new_status, valid)
        return False
    now = time.time()
    try:
        row = conn.execute(
            "SELECT id FROM belief_assertions WHERE fact_id = ? AND tenant_id = ?",
            (fact_id, tenant_id),
        ).fetchone()
        if row is None:
            logger.warning("No belief_assertion found for fact_id %s in tenant %s", fact_id, tenant_id)
            return False
        conn.execute(
            "UPDATE belief_assertions SET belief_status = ?, updated_at = ?, "
            "last_reviewed_at = ?, rationale = COALESCE(?, rationale) "
            "WHERE fact_id = ? AND tenant_id = ?",
            (new_status, now, now, rationale, fact_id, tenant_id),
        )
        conn.execute(
            "UPDATE kg_facts SET belief_status = ? WHERE id = ? AND tenant_id = ?",
            (new_status, fact_id, tenant_id),
        )
        return True
    except Exception as e:
        logger.warning("Failed to update belief status for fact %s: %s", fact_id, e)
        return False


def retract_dependent_beliefs(
    conn: AnyConnection,
    superseded_fact_id: int,
    tenant_id: str = "default",
) -> int:
    """When a fact is superseded, retract all beliefs that cite it in evidence_chain.

    Returns the count of retracted beliefs.
    """
    now = time.time()
    rows = conn.execute(
        "SELECT id, fact_id, evidence_chain FROM belief_assertions "
        "WHERE belief_status = 'active' AND evidence_chain IS NOT NULL AND tenant_id = ?",
        (tenant_id,),
    ).fetchall()
    retracted = 0
    for row in rows:
        ba_id, fact_id, chain_json = row
        if not chain_json:
            continue
        try:
            chain = json.loads(chain_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(chain, list):
            continue
        if superseded_fact_id in chain:
            conn.execute(
                "UPDATE belief_assertions SET belief_status = 'deprecated', "
                "updated_at = ?, rationale = 'evidence_chain_fact_superseded' "
                "WHERE id = ? AND tenant_id = ?",
                (now, ba_id, tenant_id),
            )
            conn.execute(
                "UPDATE kg_facts SET belief_status = 'deprecated' WHERE id = ? AND tenant_id = ?",
                (fact_id, tenant_id),
            )
            retracted += 1
    return retracted


def handle_evidence_chain_staleness(
    conn: AnyConnection,
    batch_size: int = 100,
    tenant_id: str = "default",
) -> dict:
    """Background task: check if any belief's evidence_chain contains superseded facts.

    If a fact in the evidence chain has been superseded (superseded_by IS NOT NULL),
    mark the dependent belief as deprecated.

    Returns summary dict with counts.
    """
    now = time.time()
    stale = conn.execute(
        "SELECT ba.id, ba.fact_id, ba.evidence_chain "
        "FROM belief_assertions ba "
        "WHERE ba.belief_status = 'active' "
        "AND ba.evidence_chain IS NOT NULL "
        "AND ba.tenant_id = ? "
        "LIMIT ?",
        (tenant_id, batch_size),
    ).fetchall()
    deprecation_count = 0
    checked_count = 0
    for row in stale:
        ba_id, fact_id, chain_json = row
        if not chain_json:
            continue
        try:
            chain = json.loads(chain_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(chain, list) or not chain:
            continue
        placeholders = ",".join("?" for _ in chain)
        superseded = conn.execute(
            f"SELECT id FROM kg_facts WHERE id IN ({placeholders}) "
            "AND superseded_by IS NOT NULL AND tenant_id = ?",
            [*chain, tenant_id],
        ).fetchall()
        checked_count += 1
        if superseded:
            conn.execute(
                "UPDATE belief_assertions SET belief_status = 'deprecated', "
                "updated_at = ?, rationale = 'evidence_chain_stale' "
                "WHERE id = ? AND tenant_id = ?",
                (now, ba_id, tenant_id),
            )
            conn.execute(
                "UPDATE kg_facts SET belief_status = 'deprecated' WHERE id = ? AND tenant_id = ?",
                (fact_id, tenant_id),
            )
            deprecation_count += 1
    return {
        "checked": checked_count,
        "deprecated": deprecation_count,
    }


def get_beliefs_due_for_review(
    conn: AnyConnection,
    staleness_days: float | None = 30.0,
    min_confidence: float | None = 1.0,
    limit: int = 50,
    tenant_id: str = "default",
    belief_status: str | None = "active",
) -> list[dict]:
    """Return beliefs that are stale or low-confidence.

    Args:
        staleness_days: Beliefs not reviewed within this many days are stale.
            None or <= 0 disables staleness filtering.
        min_confidence: Beliefs with confidence below this threshold need review.
            Default 1.0 means all confidence levels pass (only staleness filters).
            None disables confidence filtering.
        limit: Max records to return.
        tenant_id: Tenant identifier (default 'default').
        belief_status: Status filter, defaults to 'active'. None returns any status.
    """
    where_clauses = ["ba.tenant_id = ?"]
    params: list[Any] = [tenant_id]

    if belief_status is not None:
        where_clauses.append("ba.belief_status = ?")
        params.append(belief_status)

    if min_confidence is not None:
        where_clauses.append("ba.confidence < ?")
        params.append(min_confidence)

    if staleness_days is not None and staleness_days > 0:
        cutoff = time.time() - (staleness_days * 86400)
        where_clauses.append("(ba.last_reviewed_at IS NULL OR ba.last_reviewed_at < ?)")
        params.append(cutoff)

    where_sql = " AND ".join(where_clauses)
    query = (
        "SELECT ba.id, ba.fact_id, ba.memory_id, ba.belief_status, ba.confidence, "
        "ba.epistemic_source, ba.certainty_tier, ba.last_reviewed_at, ba.review_count, "
        "kf.subject, kf.predicate, kf.object, "
        "ba.evidence_chain, ba.rationale, ba.created_at "
        "FROM belief_assertions ba "
        "LEFT JOIN kg_facts kf ON kf.id = ba.fact_id AND kf.tenant_id = ba.tenant_id "
        f"WHERE {where_sql} "
        "ORDER BY (ba.last_reviewed_at IS NOT NULL) ASC, ba.last_reviewed_at ASC, ba.confidence ASC "
        "LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": r[0],
            "fact_id": r[1],
            "memory_id": r[2],
            "belief_status": r[3],
            "confidence": r[4],
            "epistemic_source": r[5],
            "certainty_tier": r[6],
            "last_reviewed_at": r[7],
            "review_count": r[8],
            "subject": r[9],
            "predicate": r[10],
            "object": r[11],
            "evidence_chain": json.loads(r[12]) if r[12] else [],
            "rationale": r[13],
            "created_at": r[14],
        }
        for r in rows
    ]


# ── V2 Lifecycle & REST RPC Helpers (SRV-16) ─────────────────────────────────


def get_belief_assertion_columns(conn: AnyConnection) -> set[str]:
    """Return the existing column names on ``belief_assertions``."""
    try:
        rows = conn.execute("PRAGMA table_info(belief_assertions)").fetchall()
        return {str(r[1]) for r in rows}
    except Exception:
        return set()


def ensure_belief_lifecycle_columns(conn: AnyConnection) -> set[str]:
    """Idempotently ensure lifecycle columns (pinned, superseded_by, content, source_sessions) exist."""
    cols = get_belief_assertion_columns(conn)
    desired = (
        ("pinned", "INTEGER DEFAULT 0"),
        ("superseded_by", "TEXT"),
        ("content", "TEXT"),
        ("source_sessions", "TEXT"),
    )
    for col_name, col_def in desired:
        if col_name not in cols:
            try:
                conn.execute(f"ALTER TABLE belief_assertions ADD COLUMN {col_name} {col_def}")
                cols.add(col_name)
            except Exception as exc:
                logger.debug("ensure_belief_lifecycle_columns(%s): %s", col_name, exc)
    return cols


def _to_epoch_seconds(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return float(s)
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return default
    return default


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _emit_belief_outbox_event(
    conn: AnyConnection,
    belief_id: str,
    payload: dict[str, Any],
) -> None:
    """Emit a ``belief.updated`` event into the SQLite ``memory_events`` outbox if present."""
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_events'"
        ).fetchone()
        if not has_table:
            return
        conn.execute(
            "INSERT INTO memory_events (event_type, note_id, payload) VALUES (?, ?, ?)",
            ("belief.updated", str(belief_id), json.dumps(payload)),
        )
    except Exception as exc:
        logger.debug("_emit_belief_outbox_event failed (non-fatal): %s", exc)


def _format_belief_assertion_record(
    conn: AnyConnection,
    ba_id: int,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Fetch and format a single ``belief_assertions`` row by primary key."""
    cols = get_belief_assertion_columns(conn)
    pinned_expr = "COALESCE(ba.pinned, 0)" if "pinned" in cols else "0"
    sup_expr = "ba.superseded_by" if "superseded_by" in cols else "NULL"
    content_expr = "ba.content" if "content" in cols else "NULL"
    sess_expr = "ba.source_sessions" if "source_sessions" in cols else "NULL"

    row = conn.execute(
        f"SELECT ba.id, ba.fact_id, ba.memory_id, ba.belief_status, ba.confidence, "
        f"ba.epistemic_source, ba.asserting_agent_id, ba.evidence_chain, ba.rationale, "
        f"ba.certainty_tier, ba.last_reviewed_at, ba.review_count, ba.created_at, ba.updated_at, "
        f"{pinned_expr}, {sup_expr}, {content_expr}, {sess_expr}, "
        f"kf.subject, kf.predicate, kf.object, kf.superseded_by, "
        f"m.pinned, m.source_file, m.metadata "
        f"FROM belief_assertions ba "
        f"LEFT JOIN kg_facts kf ON kf.id = ba.fact_id AND kf.tenant_id = ba.tenant_id "
        f"LEFT JOIN memories m ON m.id = ba.memory_id AND m.tenant_id = ba.tenant_id "
        f"WHERE ba.id = ? AND ba.tenant_id = ?",
        (ba_id, tenant_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_belief_dict(row)


def _row_to_belief_dict(r: Any) -> dict[str, Any]:
    ba_id = r[0]
    fact_id = r[1]
    memory_id = r[2]
    belief_status = r[3] or "active"
    confidence = float(r[4]) if r[4] is not None else 1.0
    evidence_chain = _parse_json_list(r[7])
    rationale = r[8]
    certainty_tier = r[9]
    last_reviewed_at = _to_epoch_seconds(r[10])
    review_count = int(r[11] or 0)
    created_at = _to_epoch_seconds(r[12], time.time())
    updated_at = _to_epoch_seconds(r[13], created_at)
    ba_pinned = bool(r[14])
    ba_superseded_by = r[15]
    ba_content = r[16]
    ba_sessions = _parse_json_list(r[17])
    subject = r[18]
    predicate = r[19]
    obj = r[20]
    kf_superseded_by = r[21]
    mem_pinned = bool(r[22]) if r[22] is not None else False
    mem_source_file = r[23]
    mem_meta = _parse_json_dict(r[24])

    if ba_content:
        content = str(ba_content)
    elif subject and predicate:
        content = f"{subject} --[{predicate}]--> {obj or ''}"
    else:
        content = str(rationale or "")

    superseded_by = (
        str(ba_superseded_by)
        if ba_superseded_by is not None and str(ba_superseded_by) != ""
        else (str(kf_superseded_by) if kf_superseded_by is not None else None)
    )
    pinned = ba_pinned or mem_pinned
    evidence_count = (len(evidence_chain) or 1) + review_count

    source_sessions: list[str] = [str(s) for s in ba_sessions if s]
    if not source_sessions and isinstance(mem_meta.get("source_sessions"), list):
        source_sessions = [str(s) for s in mem_meta["source_sessions"] if s]
    if not source_sessions and mem_source_file:
        source_sessions = [str(mem_source_file)]

    return {
        "id": str(ba_id),
        "fact_id": fact_id,
        "memory_id": memory_id,
        "content": content,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "evidence_chain": evidence_chain,
        "source_sessions": source_sessions,
        "rationale": rationale,
        "last_challenged_at": last_reviewed_at,
        "last_reviewed_at": last_reviewed_at,
        "superseded_by": superseded_by,
        "created_at": created_at,
        "updated_at": updated_at,
        "certainty_tier": certainty_tier,
        "belief_status": belief_status,
        "status": belief_status,
        "pinned": pinned,
    }


def _memory_row_to_belief_dict(r: Any) -> dict[str, Any]:
    mem_id = str(r[0])
    content = str(r[1] or "")
    pinned_col = bool(r[2])
    score = r[3]
    superseded_by = r[4]
    created_at = _to_epoch_seconds(r[5], time.time())
    updated_at = _to_epoch_seconds(r[6], created_at)
    source_file = r[7]
    access_count = r[8]
    meta = _parse_json_dict(r[9])

    raw_conf = meta.get("confidence", score if score is not None else 1.0)
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = 1.0

    raw_ev_count = meta.get("evidence_count")
    if isinstance(raw_ev_count, int) and raw_ev_count >= 1:
        evidence_count = raw_ev_count
    elif isinstance(access_count, int) and access_count >= 1:
        evidence_count = access_count
    else:
        evidence_count = 1

    sup_str = str(superseded_by) if superseded_by is not None and str(superseded_by) != "" else None
    belief_status = meta.get("belief_status")
    if not isinstance(belief_status, str) or not belief_status:
        belief_status = "deprecated" if sup_str else "active"

    pinned = pinned_col or bool(meta.get("pinned", False))
    source_sessions: list[str] = []
    if isinstance(meta.get("source_sessions"), list):
        source_sessions = [str(s) for s in meta["source_sessions"] if s]
    elif source_file:
        source_sessions = [str(source_file)]

    last_reviewed = _to_epoch_seconds(meta.get("last_reviewed_at"), updated_at)

    return {
        "id": mem_id,
        "fact_id": None,
        "memory_id": mem_id,
        "content": content,
        "subject": None,
        "predicate": None,
        "object": None,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "evidence_chain": [],
        "source_sessions": source_sessions,
        "rationale": meta.get("rationale"),
        "last_challenged_at": last_reviewed,
        "last_reviewed_at": last_reviewed,
        "superseded_by": sup_str,
        "created_at": created_at,
        "updated_at": updated_at,
        "certainty_tier": meta.get("certainty_tier", "likely"),
        "belief_status": belief_status,
        "status": belief_status,
        "pinned": pinned,
    }


def list_beliefs_unified(
    conn: AnyConnection,
    *,
    min_confidence: float | None = None,
    staleness_days: float | None = None,
    belief_status: str | None = None,
    status_explicit: bool = False,
    include_superseded: bool = True,
    limit: int = 50,
    tenant_id: str = "default",
) -> list[dict[str, Any]]:
    """List beliefs from both ``belief_assertions`` and ``memories(category='belief')``."""
    cols = get_belief_assertion_columns(conn)
    pinned_expr = "COALESCE(ba.pinned, 0)" if "pinned" in cols else "0"
    sup_expr = "ba.superseded_by" if "superseded_by" in cols else "NULL"
    content_expr = "ba.content" if "content" in cols else "NULL"
    sess_expr = "ba.source_sessions" if "source_sessions" in cols else "NULL"
    sup_cond = "(ba.superseded_by IS NOT NULL OR kf.superseded_by IS NOT NULL)" if "superseded_by" in cols else "(kf.superseded_by IS NOT NULL)"
    not_sup_cond = "(ba.superseded_by IS NULL AND kf.superseded_by IS NULL)" if "superseded_by" in cols else "(kf.superseded_by IS NULL)"

    clauses = ["ba.tenant_id = ?"]
    params: list[Any] = [tenant_id]

    if status_explicit:
        if belief_status is not None:
            clauses.append("ba.belief_status = ?")
            params.append(belief_status)
        if not include_superseded:
            clauses.append(not_sup_cond)
    else:
        if include_superseded:
            clauses.append(f"(ba.belief_status = 'active' OR {sup_cond})")
        else:
            clauses.append(f"(ba.belief_status = 'active' AND {not_sup_cond})")

    if min_confidence is not None:
        clauses.append("ba.confidence < ?")
        params.append(min_confidence)

    if staleness_days is not None and staleness_days > 0:
        cutoff = time.time() - (staleness_days * 86400)
        clauses.append("(ba.last_reviewed_at IS NULL OR ba.last_reviewed_at < ?)")
        params.append(cutoff)

    if staleness_days is not None or min_confidence is not None:
        order_by = "(ba.last_reviewed_at IS NOT NULL) ASC, ba.last_reviewed_at ASC, ba.confidence ASC"
    else:
        order_by = "ba.confidence DESC, ba.updated_at DESC"

    query = (
        f"SELECT ba.id, ba.fact_id, ba.memory_id, ba.belief_status, ba.confidence, "
        f"ba.epistemic_source, ba.asserting_agent_id, ba.evidence_chain, ba.rationale, "
        f"ba.certainty_tier, ba.last_reviewed_at, ba.review_count, ba.created_at, ba.updated_at, "
        f"{pinned_expr}, {sup_expr}, {content_expr}, {sess_expr}, "
        f"kf.subject, kf.predicate, kf.object, kf.superseded_by, "
        f"m.pinned, m.source_file, m.metadata "
        f"FROM belief_assertions ba "
        f"LEFT JOIN kg_facts kf ON kf.id = ba.fact_id AND kf.tenant_id = ba.tenant_id "
        f"LEFT JOIN memories m ON m.id = ba.memory_id AND m.tenant_id = ba.tenant_id "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY {order_by} LIMIT ?"
    )
    params.append(limit)
    ba_rows = conn.execute(query, params).fetchall()
    results: list[dict[str, Any]] = [_row_to_belief_dict(r) for r in ba_rows]
    linked_memory_ids = {str(r[2]) for r in ba_rows if r[2]}

    # Also include memory-table notes with category='belief' not already linked via belief_assertions
    try:
        mem_rows = conn.execute(
            "SELECT id, content, pinned, score, superseded_by, created_at, updated_at, "
            "source_file, access_count, metadata "
            "FROM memories "
            "WHERE category = 'belief' AND deleted_at IS NULL AND tenant_id = ? "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        for mr in mem_rows:
            if str(mr[0]) in linked_memory_ids:
                continue
            item = _memory_row_to_belief_dict(mr)
            item_status = item["belief_status"]
            item_sup = item["superseded_by"] is not None
            if status_explicit:
                if belief_status is not None and item_status != belief_status:
                    continue
                if not include_superseded and item_sup:
                    continue
            else:
                if include_superseded:
                    if item_status != "active" and not item_sup:
                        continue
                else:
                    if item_status != "active" or item_sup:
                        continue
            if min_confidence is not None and item["confidence"] >= min_confidence:
                continue
            results.append(item)
    except Exception as exc:
        logger.debug("list_beliefs_unified memories scan failed (non-fatal): %s", exc)

    return results[:limit]


def _resolve_belief_target(
    conn: AnyConnection,
    belief_id: str,
    tenant_id: str,
) -> tuple[str, Any]:
    """Resolve ``belief_id`` to either ('belief_assertion', int_id) or ('memory', str_id)."""
    if belief_id.isdigit():
        row = conn.execute(
            "SELECT id FROM belief_assertions WHERE id = ? AND tenant_id = ?",
            (int(belief_id), tenant_id),
        ).fetchone()
        if row is not None:
            return ("belief_assertion", int(row[0]))

    mem_row = conn.execute(
        "SELECT id, category FROM memories WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
        (belief_id, tenant_id),
    ).fetchone()
    if mem_row is not None:
        ba_linked = conn.execute(
            "SELECT id FROM belief_assertions WHERE memory_id = ? AND tenant_id = ?",
            (belief_id, tenant_id),
        ).fetchone()
        if ba_linked is not None:
            return ("belief_assertion", int(ba_linked[0]))
        if mem_row[1] == "belief":
            return ("memory", str(mem_row[0]))

    return ("missing", None)


def confirm_belief_record(
    conn: AnyConnection,
    belief_id: str,
    tenant_id: str = "default",
) -> dict[str, Any] | None:
    """Confirm a belief in-place: boost confidence to 1.0, increment review_count, set active."""
    ensure_belief_lifecycle_columns(conn)
    kind, target = _resolve_belief_target(conn, belief_id, tenant_id)
    if kind == "missing":
        return None

    now = time.time()
    iso_now = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    if kind == "belief_assertion":
        row = conn.execute(
            "SELECT fact_id, memory_id FROM belief_assertions WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        fact_id, memory_id = row[0], row[1]
        conn.execute(
            "UPDATE belief_assertions SET confidence = 1.0, belief_status = 'active', "
            "review_count = COALESCE(review_count, 0) + 1, last_reviewed_at = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (now, now, target, tenant_id),
        )
        if fact_id is not None:
            conn.execute(
                "UPDATE kg_facts SET confidence = 1.0, belief_status = 'active' "
                "WHERE id = ? AND tenant_id = ?",
                (fact_id, tenant_id),
            )
        if memory_id:
            mem_row = conn.execute(
                "SELECT metadata FROM memories WHERE id = ? AND tenant_id = ?",
                (memory_id, tenant_id),
            ).fetchone()
            if mem_row is not None:
                meta = _parse_json_dict(mem_row[0])
                meta["confidence"] = 1.0
                meta["belief_status"] = "active"
                meta["last_reviewed_at"] = now
                conn.execute(
                    "UPDATE memories SET score = 1.0, updated_at = ?, metadata = ? "
                    "WHERE id = ? AND tenant_id = ?",
                    (iso_now, json.dumps(meta), memory_id, tenant_id),
                )
        updated = _format_belief_assertion_record(conn, target, tenant_id)
    else:
        mem_row = conn.execute(
            "SELECT id, content, pinned, score, superseded_by, created_at, updated_at, "
            "source_file, access_count, metadata FROM memories "
            "WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (target, tenant_id),
        ).fetchone()
        meta = _parse_json_dict(mem_row[9])
        prev_count = int(meta.get("evidence_count") or mem_row[8] or 1)
        meta["confidence"] = 1.0
        meta["belief_status"] = "active"
        meta["evidence_count"] = prev_count + 1
        meta["last_reviewed_at"] = now
        conn.execute(
            "UPDATE memories SET score = 1.0, updated_at = ?, metadata = ? "
            "WHERE id = ? AND tenant_id = ?",
            (iso_now, json.dumps(meta), target, tenant_id),
        )
        refreshed = conn.execute(
            "SELECT id, content, pinned, score, superseded_by, created_at, updated_at, "
            "source_file, access_count, metadata FROM memories "
            "WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        updated = _memory_row_to_belief_dict(refreshed)

    if updated is not None:
        _emit_belief_outbox_event(
            conn,
            updated["id"],
            {
                "belief_id": updated["id"],
                "confidence": updated["confidence"],
                "belief_status": updated["belief_status"],
                "pinned": updated["pinned"],
                "action": "confirm",
                "tenant_id": tenant_id,
            },
        )
    return updated


def pin_belief_record(
    conn: AnyConnection,
    belief_id: str,
    tenant_id: str = "default",
) -> dict[str, Any] | None:
    """Pin a belief in-place without creating duplicate notes, boosting confidence to 1.0."""
    ensure_belief_lifecycle_columns(conn)
    kind, target = _resolve_belief_target(conn, belief_id, tenant_id)
    if kind == "missing":
        return None

    now = time.time()
    iso_now = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    if kind == "belief_assertion":
        row = conn.execute(
            "SELECT fact_id, memory_id FROM belief_assertions WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        fact_id, memory_id = row[0], row[1]
        conn.execute(
            "UPDATE belief_assertions SET pinned = 1, confidence = 1.0, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (now, target, tenant_id),
        )
        if fact_id is not None:
            conn.execute(
                "UPDATE kg_facts SET confidence = 1.0, locked = 1 WHERE id = ? AND tenant_id = ?",
                (fact_id, tenant_id),
            )
        if memory_id:
            mem_row = conn.execute(
                "SELECT metadata FROM memories WHERE id = ? AND tenant_id = ?",
                (memory_id, tenant_id),
            ).fetchone()
            if mem_row is not None:
                meta = _parse_json_dict(mem_row[0])
                meta["pinned"] = True
                meta["confidence"] = 1.0
                conn.execute(
                    "UPDATE memories SET pinned = 1, score = 1.0, updated_at = ?, metadata = ? "
                    "WHERE id = ? AND tenant_id = ?",
                    (iso_now, json.dumps(meta), memory_id, tenant_id),
                )
        updated = _format_belief_assertion_record(conn, target, tenant_id)
    else:
        mem_row = conn.execute(
            "SELECT metadata FROM memories WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (target, tenant_id),
        ).fetchone()
        meta = _parse_json_dict(mem_row[0])
        meta["pinned"] = True
        meta["confidence"] = 1.0
        conn.execute(
            "UPDATE memories SET pinned = 1, score = 1.0, updated_at = ?, metadata = ? "
            "WHERE id = ? AND tenant_id = ?",
            (iso_now, json.dumps(meta), target, tenant_id),
        )
        refreshed = conn.execute(
            "SELECT id, content, pinned, score, superseded_by, created_at, updated_at, "
            "source_file, access_count, metadata FROM memories "
            "WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        updated = _memory_row_to_belief_dict(refreshed)

    if updated is not None:
        _emit_belief_outbox_event(
            conn,
            updated["id"],
            {
                "belief_id": updated["id"],
                "confidence": updated["confidence"],
                "belief_status": updated["belief_status"],
                "pinned": True,
                "action": "pin",
                "tenant_id": tenant_id,
            },
        )
    return updated


def retract_belief_record(
    conn: AnyConnection,
    belief_id: str,
    reason: str | None = None,
    tenant_id: str = "default",
) -> dict[str, Any] | None:
    """Retract a belief in-place while preserving audit trail."""
    ensure_belief_lifecycle_columns(conn)
    kind, target = _resolve_belief_target(conn, belief_id, tenant_id)
    if kind == "missing":
        return None

    now = time.time()
    iso_now = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None

    if kind == "belief_assertion":
        row = conn.execute(
            "SELECT fact_id, memory_id FROM belief_assertions WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        fact_id, memory_id = row[0], row[1]
        conn.execute(
            "UPDATE belief_assertions SET belief_status = 'retracted', "
            "rationale = COALESCE(?, rationale), last_reviewed_at = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (clean_reason, now, now, target, tenant_id),
        )
        if fact_id is not None:
            conn.execute(
                "UPDATE kg_facts SET belief_status = 'retracted', "
                "invalidation_reason = COALESCE(?, invalidation_reason) "
                "WHERE id = ? AND tenant_id = ?",
                (clean_reason, fact_id, tenant_id),
            )
        if memory_id:
            mem_row = conn.execute(
                "SELECT metadata FROM memories WHERE id = ? AND tenant_id = ?",
                (memory_id, tenant_id),
            ).fetchone()
            if mem_row is not None:
                meta = _parse_json_dict(mem_row[0])
                meta["belief_status"] = "retracted"
                if clean_reason:
                    meta["retraction_reason"] = clean_reason
                conn.execute(
                    "UPDATE memories SET updated_at = ?, metadata = ? "
                    "WHERE id = ? AND tenant_id = ?",
                    (iso_now, json.dumps(meta), memory_id, tenant_id),
                )
        updated = _format_belief_assertion_record(conn, target, tenant_id)
    else:
        mem_row = conn.execute(
            "SELECT metadata FROM memories WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (target, tenant_id),
        ).fetchone()
        meta = _parse_json_dict(mem_row[0])
        meta["belief_status"] = "retracted"
        if clean_reason:
            meta["retraction_reason"] = clean_reason
            meta["rationale"] = clean_reason
        conn.execute(
            "UPDATE memories SET updated_at = ?, metadata = ? "
            "WHERE id = ? AND tenant_id = ?",
            (iso_now, json.dumps(meta), target, tenant_id),
        )
        refreshed = conn.execute(
            "SELECT id, content, pinned, score, superseded_by, created_at, updated_at, "
            "source_file, access_count, metadata FROM memories "
            "WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        updated = _memory_row_to_belief_dict(refreshed)

    if updated is not None:
        _emit_belief_outbox_event(
            conn,
            updated["id"],
            {
                "belief_id": updated["id"],
                "confidence": updated["confidence"],
                "belief_status": "retracted",
                "pinned": updated["pinned"],
                "reason": clean_reason,
                "action": "retract",
                "tenant_id": tenant_id,
            },
        )
    return updated


def supersede_belief_record(
    conn: AnyConnection,
    belief_id: str,
    replacement_content: str,
    reason: str | None = None,
    tenant_id: str = "default",
) -> dict[str, Any] | None:
    """Supersede a belief with replacement content, linking superseded_by and retracting dependents."""
    ensure_belief_lifecycle_columns(conn)
    kind, target = _resolve_belief_target(conn, belief_id, tenant_id)
    if kind == "missing":
        return None

    now = time.time()
    iso_now = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None

    if kind == "belief_assertion":
        row = conn.execute(
            "SELECT fact_id, memory_id, epistemic_source, asserting_agent_id, certainty_tier "
            "FROM belief_assertions WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        old_fact_id, old_memory_id, epistemic_source, agent_id, certainty_tier = row

        new_fact_id: int | None = None
        if old_fact_id is not None:
            kf_row = conn.execute(
                "SELECT subject, predicate, object FROM kg_facts WHERE id = ? AND tenant_id = ?",
                (old_fact_id, tenant_id),
            ).fetchone()
            subj = kf_row[0] if kf_row else f"belief:{target}"
            pred = kf_row[1] if kf_row else "asserts"
            if kf_row and kf_row[2] == replacement_content:
                raise ValueError("Cannot supersede belief with identical content")

            # Respect UNIQUE(subject, predicate, object) on kg_facts without crossing tenants
            existing_kf = conn.execute(
                "SELECT id, tenant_id FROM kg_facts WHERE subject = ? AND predicate = ? AND object = ?",
                (subj, pred, replacement_content),
            ).fetchone()
            if existing_kf is not None:
                existing_kf_id = int(existing_kf[0])
                existing_kf_tenant = existing_kf[1]
                if existing_kf_tenant == tenant_id:
                    if existing_kf_id == int(old_fact_id):
                        raise ValueError("Cannot supersede belief with identical fact")
                    new_fact_id = existing_kf_id
                    conn.execute(
                        "UPDATE kg_facts SET confidence = 1.0, belief_status = 'active', "
                        "supersedes = ?, invalid_at = NULL, superseded_by = NULL "
                        "WHERE id = ? AND tenant_id = ?",
                        (old_fact_id, new_fact_id, tenant_id),
                    )
                else:
                    # Foreign tenant owns that (subject, predicate, object) tuple; never touch foreign row
                    new_fact_id = None
            else:
                cur = conn.execute(
                    "INSERT INTO kg_facts "
                    "(subject, predicate, object, confidence, first_seen, last_seen, "
                    "valid_at, transaction_time, supersedes, belief_status, "
                    "epistemic_source, asserting_agent_id, tenant_id) "
                    "VALUES (?, ?, ?, 1.0, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        subj,
                        pred,
                        replacement_content,
                        now,
                        now,
                        now,
                        now,
                        old_fact_id,
                        epistemic_source or "agent",
                        agent_id,
                        tenant_id,
                    ),
                )
                new_fact_id = int(cur.lastrowid) if cur.lastrowid is not None else None

        new_memory_id: str | None = None
        if old_memory_id:
            old_mem = conn.execute(
                "SELECT source_file, tags, importance, repo_id, metadata FROM memories "
                "WHERE id = ? AND tenant_id = ?",
                (old_memory_id, tenant_id),
            ).fetchone()
            if old_mem is not None:
                new_memory_id = f"beliefs/superseded-{uuid.uuid4().hex[:12]}"
                new_meta = {
                    "confidence": 1.0,
                    "belief_status": "active",
                    "supersedes": old_memory_id,
                }
                if clean_reason:
                    new_meta["rationale"] = clean_reason
                conn.execute(
                    "INSERT INTO memories "
                    "(id, content, source_file, tags, category, created_at, updated_at, observed_at, "
                    "pinned, importance, score, supersedes, repo_id, metadata, tenant_id) "
                    "VALUES (?, ?, ?, ?, 'belief', ?, ?, ?, 0, ?, 1.0, ?, ?, ?, ?)",
                    (
                        new_memory_id,
                        replacement_content,
                        old_mem[0] or "belief",
                        old_mem[1] or "[]",
                        iso_now,
                        iso_now,
                        iso_now,
                        old_mem[2] if old_mem[2] is not None else 3,
                        old_memory_id,
                        old_mem[3],
                        json.dumps(new_meta),
                        tenant_id,
                    ),
                )
                old_meta = _parse_json_dict(old_mem[4])
                old_meta["belief_status"] = "deprecated"
                old_meta["superseded_by"] = new_memory_id
                if clean_reason:
                    old_meta["invalidation_reason"] = clean_reason
                conn.execute(
                    "UPDATE memories SET superseded_by = ?, updated_at = ?, metadata = ? "
                    "WHERE id = ? AND tenant_id = ?",
                    (new_memory_id, iso_now, json.dumps(old_meta), old_memory_id, tenant_id),
                )

        # Create replacement belief_assertion (avoid UNIQUE(fact_id) collision if reusing fact_id)
        existing_new_ba = None
        if new_fact_id is not None:
            existing_new_ba = conn.execute(
                "SELECT id FROM belief_assertions WHERE fact_id = ? AND tenant_id = ?",
                (new_fact_id, tenant_id),
            ).fetchone()
        if existing_new_ba is not None:
            new_ba_id = int(existing_new_ba[0])
            if new_ba_id == int(target):
                raise ValueError("Cannot supersede belief with itself")
            conn.execute(
                "UPDATE belief_assertions SET memory_id = COALESCE(?, memory_id), "
                "belief_status = 'active', confidence = 1.0, content = ?, "
                "rationale = COALESCE(?, rationale), last_reviewed_at = ?, updated_at = ? "
                "WHERE id = ? AND tenant_id = ?",
                (new_memory_id, replacement_content, clean_reason, now, now, new_ba_id, tenant_id),
            )
        else:
            cur_ba = conn.execute(
                "INSERT INTO belief_assertions "
                "(fact_id, memory_id, belief_status, confidence, epistemic_source, "
                "asserting_agent_id, rationale, certainty_tier, last_reviewed_at, "
                "review_count, created_at, updated_at, tenant_id, pinned, content) "
                "VALUES (?, ?, 'active', 1.0, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?)",
                (
                    new_fact_id,
                    new_memory_id,
                    epistemic_source or "agent",
                    agent_id,
                    clean_reason,
                    certainty_tier or "likely",
                    now,
                    now,
                    now,
                    tenant_id,
                    replacement_content,
                ),
            )
            new_ba_id = int(cur_ba.lastrowid)

        # Deprecate original belief_assertion and point superseded_by -> new_ba_id
        conn.execute(
            "UPDATE belief_assertions SET belief_status = 'deprecated', "
            "superseded_by = ?, rationale = COALESCE(?, rationale), "
            "last_reviewed_at = ?, updated_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (str(new_ba_id), clean_reason, now, now, target, tenant_id),
        )

        if old_fact_id is not None:
            conn.execute(
                "UPDATE kg_facts SET invalid_at = ?, superseded_by = ?, "
                "invalidation_reason = COALESCE(?, invalidation_reason), "
                "belief_status = 'deprecated' "
                "WHERE id = ? AND tenant_id = ?",
                (now, new_fact_id, clean_reason or "superseded", old_fact_id, tenant_id),
            )
            retract_dependent_beliefs(conn, int(old_fact_id), tenant_id=tenant_id)

        superseded_dict = _format_belief_assertion_record(conn, target, tenant_id)
        replacement_dict = _format_belief_assertion_record(conn, new_ba_id, tenant_id)
    else:
        old_mem = conn.execute(
            "SELECT id, source_file, tags, importance, repo_id, metadata "
            "FROM memories WHERE id = ? AND tenant_id = ? AND deleted_at IS NULL",
            (target, tenant_id),
        ).fetchone()
        new_memory_id = f"beliefs/{uuid.uuid4().hex[:12]}"
        new_meta = {
            "confidence": 1.0,
            "belief_status": "active",
            "supersedes": target,
            "evidence_count": 1,
        }
        if clean_reason:
            new_meta["rationale"] = clean_reason

        conn.execute(
            "INSERT INTO memories "
            "(id, content, source_file, tags, category, created_at, updated_at, observed_at, "
            "pinned, importance, score, supersedes, repo_id, metadata, tenant_id) "
            "VALUES (?, ?, ?, ?, 'belief', ?, ?, ?, 0, ?, 1.0, ?, ?, ?, ?)",
            (
                new_memory_id,
                replacement_content,
                old_mem[1] or "belief",
                old_mem[2] or "[]",
                iso_now,
                iso_now,
                iso_now,
                old_mem[3] if old_mem[3] is not None else 3,
                target,
                old_mem[4],
                json.dumps(new_meta),
                tenant_id,
            ),
        )
        old_meta = _parse_json_dict(old_mem[5])
        old_meta["belief_status"] = "deprecated"
        old_meta["superseded_by"] = new_memory_id
        if clean_reason:
            old_meta["invalidation_reason"] = clean_reason
            old_meta["rationale"] = clean_reason
        conn.execute(
            "UPDATE memories SET superseded_by = ?, updated_at = ?, metadata = ? "
            "WHERE id = ? AND tenant_id = ?",
            (new_memory_id, iso_now, json.dumps(old_meta), target, tenant_id),
        )

        old_refreshed = conn.execute(
            "SELECT id, content, pinned, score, superseded_by, created_at, updated_at, "
            "source_file, access_count, metadata FROM memories WHERE id = ? AND tenant_id = ?",
            (target, tenant_id),
        ).fetchone()
        new_refreshed = conn.execute(
            "SELECT id, content, pinned, score, superseded_by, created_at, updated_at, "
            "source_file, access_count, metadata FROM memories WHERE id = ? AND tenant_id = ?",
            (new_memory_id, tenant_id),
        ).fetchone()
        superseded_dict = _memory_row_to_belief_dict(old_refreshed)
        replacement_dict = _memory_row_to_belief_dict(new_refreshed)

    if superseded_dict is not None and replacement_dict is not None:
        _emit_belief_outbox_event(
            conn,
            superseded_dict["id"],
            {
                "belief_id": superseded_dict["id"],
                "superseded_by": replacement_dict["id"],
                "confidence": replacement_dict["confidence"],
                "belief_status": "deprecated",
                "action": "supersede",
                "tenant_id": tenant_id,
            },
        )
    return {
        "superseded": superseded_dict,
        "replacement": replacement_dict,
    }
