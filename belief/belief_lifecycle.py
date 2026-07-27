"""Belief lifecycle management for agentic-memory.

Handles creation, review, retraction, deprecation, and evidence-chain
cascade for belief_assertions.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

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
) -> int | None:
    """Create or update a belief_assertion for the given fact_id.

    Returns the belief_assertion id, or None on failure.
    """
    try:
        now = time.time()
        evidence_json = json.dumps(evidence_chain) if evidence_chain else None
        row = conn.execute(
            "SELECT id, belief_status FROM belief_assertions WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE belief_assertions SET belief_status = ?, confidence = ?, "
                "epistemic_source = ?, asserting_agent_id = ?, evidence_chain = ?, "
                "rationale = ?, certainty_tier = ?, updated_at = ?, "
                "review_count = review_count + 1 "
                "WHERE id = ?",
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
                ),
            )
            return int(row[0])
        cur = conn.execute(
            "INSERT INTO belief_assertions "
            "(fact_id, memory_id, belief_status, confidence, epistemic_source, "
            "asserting_agent_id, evidence_chain, rationale, certainty_tier, "
            "last_reviewed_at, review_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
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
            ),
        )
        return int(cur.lastrowid) if cur.lastrowid is not None else None
    except Exception as e:
        logger.warning("Failed to ensure belief_assertion for fact %s: %s", fact_id, e)
        return None


def get_beliefs_for_fact(
    conn: AnyConnection, fact_id: int
) -> dict | None:
    """Retrieve the belief_assertion for a given fact_id."""
    row = conn.execute(
        "SELECT id, fact_id, memory_id, belief_status, confidence, "
        "epistemic_source, asserting_agent_id, evidence_chain, rationale, "
        "certainty_tier, last_reviewed_at, review_count, created_at, updated_at "
        "FROM belief_assertions WHERE fact_id = ?",
        (fact_id,),
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
) -> list[dict]:
    """List belief assertions with optional filters."""
    clauses = ["1=1"]
    params: list[str | int | float] = []
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
        "LEFT JOIN kg_facts kf ON kf.id = ba.fact_id "
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
            "SELECT id FROM belief_assertions WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if row is None:
            logger.warning("No belief_assertion found for fact_id %s", fact_id)
            return False
        conn.execute(
            "UPDATE belief_assertions SET belief_status = ?, updated_at = ?, "
            "last_reviewed_at = ?, rationale = COALESCE(?, rationale) "
            "WHERE fact_id = ?",
            (new_status, now, now, rationale, fact_id),
        )
        conn.execute(
            "UPDATE kg_facts SET belief_status = ? WHERE id = ?",
            (new_status, fact_id),
        )
        return True
    except Exception as e:
        logger.warning("Failed to update belief status for fact %s: %s", fact_id, e)
        return False


def retract_dependent_beliefs(
    conn: AnyConnection, superseded_fact_id: int
) -> int:
    """When a fact is superseded, retract all beliefs that cite it in evidence_chain.

    Returns the count of retracted beliefs.
    """
    now = time.time()
    rows = conn.execute(
        "SELECT id, fact_id, evidence_chain FROM belief_assertions "
        "WHERE belief_status = 'active' AND evidence_chain IS NOT NULL"
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
                "WHERE id = ?",
                (now, ba_id),
            )
            conn.execute(
                "UPDATE kg_facts SET belief_status = 'deprecated' WHERE id = ?",
                (fact_id,),
            )
            retracted += 1
    return retracted


def handle_evidence_chain_staleness(
    conn: AnyConnection, batch_size: int = 100
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
        "LIMIT ?",
        (batch_size,),
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
            "AND superseded_by IS NOT NULL",
            chain,
        ).fetchall()
        checked_count += 1
        if superseded:
            conn.execute(
                "UPDATE belief_assertions SET belief_status = 'deprecated', "
                "updated_at = ?, rationale = 'evidence_chain_stale' "
                "WHERE id = ?",
                (now, ba_id),
            )
            conn.execute(
                "UPDATE kg_facts SET belief_status = 'deprecated' WHERE id = ?",
                (fact_id,),
            )
            deprecation_count += 1
    return {
        "checked": checked_count,
        "deprecated": deprecation_count,
    }


def get_beliefs_due_for_review(
    conn: AnyConnection, staleness_days: float = 30.0,
    min_confidence: float = 1.0, limit: int = 50,
) -> list[dict]:
    """Return active beliefs that are stale or low-confidence.

    Args:
        staleness_days: Beliefs not reviewed within this many days are stale.
        min_confidence: Beliefs with confidence below this threshold need review.
            Default 1.0 means all confidence levels pass (only staleness filters).
    """
    import time as _time
    cutoff = _time.time() - (staleness_days * 86400)
    rows = conn.execute(
        "SELECT ba.id, ba.fact_id, ba.memory_id, ba.belief_status, ba.confidence, "
        "ba.epistemic_source, ba.certainty_tier, ba.last_reviewed_at, ba.review_count, "
        "kf.subject, kf.predicate, kf.object "
        "FROM belief_assertions ba LEFT JOIN kg_facts kf ON kf.id = ba.fact_id "
        "WHERE ba.belief_status = 'active' AND ba.confidence <= ? "
        "AND (ba.last_reviewed_at IS NULL OR ba.last_reviewed_at < ?) "
        "ORDER BY ba.confidence ASC, ba.last_reviewed_at ASC LIMIT ?",
        (min_confidence, cutoff, limit),
    ).fetchall()
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
        }
        for r in rows
    ]
