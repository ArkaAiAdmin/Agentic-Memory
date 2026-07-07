"""Knowledge compilation — Pinecone Nexus-style reasoning upstream.

Three background tasks implemented here:

1. ``infer_entailment_chains`` — cross-memory entailment chains from kg_facts.
2. ``compile_concept`` — declarative knowledge compilation → concepts/ corpus.
3. ``enrich_existing_skill`` — procedural-to-declarative skill enrichment
   (principles/, ontology/).

Background handlers (``handle_*``) are also registered here so the
background worker can dispatch directly without going through the
task_queue round-trip when the call site is internal.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_CONCEPTS_DIR_NAME = "concepts"
_PRINCIPLES_DIR_NAME = "principles"
_ONTOLOGY_DIR_NAME = "ontology"

_CONFIDENCE_TRANSITIVE = 0.8
_CONFIDENCE_CONJUNCTIVE = 0.7
_CONFIDENCE_ANALOGICAL = 0.6

_ENTAILMENT_PREDICATES = frozenset({
    "is_a", "is_type_of", "subclass_of", "instance_of",
    "part_of", "has_part", "located_in",
})
_ENTAILMENT_PREDICATES_LIST = sorted(_ENTAILMENT_PREDICATES)


# ---------------------------------------------------------------------------
# 1. Cross-memory entailment chains
# ---------------------------------------------------------------------------


def infer_entailment_chains(
    conn: Any,
    db_path: str | Path,
    *,
    batch_size: int = 200,
    min_confidence: float = 0.3,
) -> dict[str, Any]:
    """Scan kg_facts and derive transitive entailment chains.

    Strategy:
        For each pair of active, non-locked facts (f1, f2) where
        f1.object == f2.subject (entity match), derive a new fact
        f3 = (f1.subject, f3.predicate, f3.object) with:
            confidence = CHAIN_CONFIDENCE_FACTOR * min(f1.confidence, f2.confidence)

    Also handles conjunctive inference: when two facts share the same
    subject and their objects differ, infer a relationship between
    those objects.

    Derived facts are inserted into kg_facts (if not already present)
    and every derivation is logged in ``entailment_chains`` so the
    provenance chain is always auditable.

    Args:
        conn:     Open database connection (not closed here).
        db_path:  Path to the memory database.
        batch_size:  Max facts to scan per invocation (keep it small
                  so the background worker doesn't time out).
        min_confidence:  Skip chains below this threshold.

    Returns:
        {"derived": int, "skipped": int, "errors": int}
    """
    db_path = Path(db_path)
    derived_count = 0
    skipped_count = 0
    error_count = 0

    # Fetch candidate active non-locked facts with entity IDs resolved.
    rows = conn.execute(
        """
        SELECT f.id, f.subject, f.predicate, f.object, f.confidence,
               f.subject_entity_id, f.object_entity_id, f.source_memory,
               e1.name AS subj_name, e2.name AS obj_name
          FROM kg_facts f
          LEFT JOIN kg_entities e1 ON e1.id = f.subject_entity_id
          LEFT JOIN kg_entities e2 ON e2.id = f.object_entity_id
         WHERE f.belief_status = 'active'
           AND f.locked = 0
           AND f.invalid_at IS NULL
            AND f.predicate IN ({})
          LIMIT ?
        """.format(",".join("?" for _ in _ENTAILMENT_PREDICATES_LIST)),
        (*_ENTAILMENT_PREDICATES_LIST, batch_size,),
    ).fetchall()

    if not rows:
        return {"derived": 0, "skipped": 0, "errors": 0}

    # Build lookup by object value for transitive inference.
    # We index on (lower(object), subject_entity_id).
    by_object: dict[tuple[str, int | None], list] = {}
    for r in rows:
        fact_id, subject, predicate, obj, conf, subj_eid, obj_eid, src_mem, subj_name, obj_name = r
        key = (obj.lower().strip() if obj else "", obj_eid)
        by_object.setdefault(key, []).append(r)

    # Build lookup by subject for conjunctive inference.
    # Index on (lower(subject), subject_entity_id) with pre-normalised object for fast dedup.
    by_subject: dict[tuple[str, int | None], list[tuple[Any, str]]] = {}
    for r in rows:
        r_subject = r[1]
        r_subj_eid = r[5]
        r_obj_norm = (r[3] or "").strip().lower()
        subj_key = (r_subject.strip().lower() if r_subject else "", r_subj_eid)
        by_subject.setdefault(subj_key, []).append((r, r_obj_norm))

    # Collect derived fact tuples before insertion to avoid SQLite
    # locking conflicts inside the loop.
    to_insert: list[dict[str, Any]] = []
    to_log: list[dict[str, Any]] = []

    for r in rows:
        fact_id, subject, predicate, obj, conf, subj_eid, obj_eid, src_mem, subj_name, obj_name = r
        if conf is None:
            conf = 1.0

        # ---- transitive inference: f1(X->Y) + f2(Y->Z) -> f3(X->Z) ----
        key = (subject.lower().strip() if subject else "", subj_eid)
        for r2 in by_object.get(key, []):
            f2_id = r2[0]
            _f2_subject = r2[1]
            f2_predicate = r2[2]
            f2_object = r2[3]
            f2_conf = r2[4] or 1.0

            raw_conf = _CONFIDENCE_TRANSITIVE * min(conf, f2_conf)
            if raw_conf < min_confidence:
                skipped_count += 1
                continue

            derived_subject = _f2_subject
            derived_predicate = predicate if f2_predicate == "is_a" else f2_predicate
            derived_object = obj
            derivation_type = "transitive"
            source_ids_json = json.dumps([fact_id, f2_id])
            to_insert.append({
                "subject": derived_subject,
                "predicate": derived_predicate,
                "object": derived_object,
                "confidence": round(raw_conf, 4),
                "subject_entity_id": r2[5],
                "object_entity_id": obj_eid,
                "source_memory": src_mem,
                "belief_status": "active",
                "epistemic_source": "inferred",
                "fact_type": "inferred",
            })
            to_log.append({
                "source_fact_ids": source_ids_json,
                "derivation_type": derivation_type,
                "confidence": round(raw_conf, 4),
            })
            derived_count += 1


        # ---- conjunctive inference: f1(X->A) + f1(X->B) -> f2(X related_to B) ----
        subj_key = (subject.strip().lower() if subject else "", subj_eid)
        same_subject_facts = by_subject.get(subj_key, [])
        for (r_other, obj_other_norm) in same_subject_facts:
            other_fid = r_other[0]
            other_obj = r_other[3]
            other_conf = r_other[4] or 1.0
            if obj_other_norm == (obj.strip().lower() if obj else ""):
                continue
            raw_conf = _CONFIDENCE_CONJUNCTIVE * min(conf, other_conf)
            if raw_conf < min_confidence:
                skipped_count += 1
                continue
            derived_predicate = "related_to"
            source_ids_json = json.dumps([fact_id, other_fid])
            to_insert.append({
                "subject": subject,
                "predicate": derived_predicate,
                "object": other_obj,
                "confidence": round(raw_conf, 4),
                "subject_entity_id": subj_eid,
                "object_entity_id": r_other[6],
                "source_memory": src_mem,
                "belief_status": "active",
                "epistemic_source": "inferred",
                "fact_type": "inferred",
            })
            to_log.append({
                "source_fact_ids": source_ids_json,
                "derivation_type": "conjunctive",
                "confidence": round(raw_conf, 4),
            })
            derived_count += 1

    if not to_insert:
        return {"derived": derived_count, "skipped": skipped_count, "errors": error_count}

    # Insert derived facts, skipping duplicates (unique on subject+predicate+object
    # where belief_status='active').
    inserted_ids: list[int] = []
    now = time.time()
    for i, row in enumerate(to_insert):
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO kg_facts
                    (subject, predicate, object, confidence, locked,
                     first_seen, last_seen, mention_count,
                     subject_entity_id, object_entity_id,
                     source_memory, belief_status, epistemic_source,
                     fact_type, is_entailed)
                VALUES (?, ?, ?, ?, 0, ?, ?, 1, ?, ?, ?, 'active', 'inferred',
                        'inferred', 1)
                """,
                (
                    row["subject"], row["predicate"], row["object"],
                    row["confidence"], now, now,
                    row["subject_entity_id"], row["object_entity_id"],
                    row["source_memory"],
                ),
            )
            fid = cur.lastrowid
            if fid:
                inserted_ids.append(fid)
            else:
                skipped_count += 1
        except Exception as exc:
            logger.debug("entailment: insert failed for %s: %s", row["subject"], exc)
            error_count += 1

    # Log records in entailment_chains.
    log_rows = []
    inserted_idx = 0
    for log_entry, row in zip(to_log, to_insert):
        if inserted_idx < len(inserted_ids):
            log_rows.append((
                log_entry["source_fact_ids"],
                inserted_ids[inserted_idx],
                log_entry["derivation_type"],
                log_entry["confidence"],
                now,
            ))
            inserted_idx += 1
        else:
            skipped_count += 1

    if log_rows:
        try:
            conn.executemany(
                """
                INSERT INTO entailment_chains
                    (source_fact_ids, derived_fact_id, derivation_type,
                     confidence, derived_at, valid)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                log_rows,
            )
        except Exception as exc:
            logger.debug("entailment: log insert failed: %s", exc)
            error_count += len(log_rows)

    # A2.4: validation pass — retract derived facts that contradict
    # an existing active fact (same subject+predicate, different object)
    # with higher confidence.  This prevents contradictory inferences
    # from surviving when a ground-truth fact already exists.
    if inserted_ids:
        try:
            for fid in inserted_ids:
                derived = conn.execute(
                    "SELECT subject, predicate, object, confidence "
                    "FROM kg_facts WHERE id = ?",
                    (fid,),
                ).fetchone()
                if not derived:
                    continue
                d_subj, d_pred, d_obj, d_conf = derived
                if d_pred in _ENTAILMENT_PREDICATES:
                    continue
                existing = conn.execute(
                    "SELECT id, confidence FROM kg_facts "
                    "WHERE subject = ? AND predicate = ? AND object != ? "
                    "AND belief_status = 'active' AND invalid_at IS NULL "
                    "AND is_entailed = 0 "
                    "ORDER BY confidence DESC LIMIT 1",
                    (d_subj, d_pred, d_obj),
                ).fetchone()
                if existing and existing[1] > d_conf:
                    conn.execute(
                        "UPDATE kg_facts SET belief_status = 'retracted', "
                        "is_entailed = 0 WHERE id = ?",
                        (fid,),
                    )
                    conn.execute(
                        "UPDATE entailment_chains SET valid = 0 "
                        "WHERE derived_fact_id = ?",
                        (fid,),
                    )
                    derived_count -= 1
        except Exception as exc:
            logger.debug("entailment: validation pass failed: %s", exc)
            error_count += 1

    return {
        "derived": derived_count,
        "skipped": skipped_count,
        "errors": error_count,
    }


def revalidate_entailment_chains(
    conn: Any,
    db_path: str | Path,
    *,
    dry_run: bool = False,
    batch_size: int = 500,
) -> dict[str, Any]:
    """A2.3: periodic revalidation of entailment chains.

    Scans ``entailment_chains`` for rows where ``valid = 1``, checks
    whether every source fact listed in ``source_fact_ids`` is still
    ``active`` and ``invalid_at IS NULL`` in ``kg_facts``, and
    invalidates any chain (and its derived fact) whose sources have
    become stale since the chain was built.

    This is the periodic sweep that complements the per-supersession
    propagation in ``fact_temporal._propagate_entailment_invalidation``.
    It catches chains whose sources were invalidated by means other
    than supersession (e.g., belief_status='retracted', hard delete).

    Uses JSON parsing of ``source_fact_ids`` rather than LIKE-based
    matching, so it is correct for multi-digit fact IDs.

    Args:
        conn:      Open database connection (not closed here).
        db_path:   Path to the memory database.
        dry_run:   If True, report without writing updates.
        batch_size: Max chains to check per invocation.

    Returns:
        {"checked": int, "invalidated": int, "errors": int, "details": list}
        ``details`` is a list of dicts with keys ``chain_id``,
        ``derived_fact_id``, and ``invalid_source_id`` for every
        chain that was (or would be) invalidated.
    """
    db_path = Path(db_path)

    rows = conn.execute(
        """
        SELECT id, source_fact_ids, derived_fact_id
          FROM entailment_chains
         WHERE valid = 1
         LIMIT ?
        """,
        (batch_size,),
    ).fetchall()

    if not rows:
        return {"checked": 0, "invalidated": 0, "errors": 0, "details": []}

    checked = 0
    invalidated = 0
    errors = 0
    details: list[dict[str, Any]] = []

    chains_to_invalidate: list[tuple[int, int, int]] = []
    derived_fids_to_demark: set[int] = set()

    for chain_id, source_ids_json, derived_fid in rows:
        checked += 1
        try:
            source_ids = json.loads(source_ids_json)
        except (json.JSONDecodeError, TypeError):
            errors += 1
            continue

        if not source_ids:
            errors += 1
            continue

        placeholders = ",".join("?" for _ in source_ids)
        try:
            source_rows = conn.execute(
                f"""
                SELECT id, belief_status, invalid_at
                  FROM kg_facts
                 WHERE id IN ({placeholders})
                """,
                list(source_ids),
            ).fetchall()
        except Exception as exc:
            logger.debug(
                "revalidate: source fact lookup failed for chain %d: %s",
                chain_id, exc,
            )
            errors += 1
            continue

        found_facts: dict[int, tuple] = {row[0]: row for row in source_rows}

        invalid_source_id: int | None = None
        for sid in source_ids:
            if sid not in found_facts:
                invalid_source_id = sid
                break
            _, belief_status, invalid_at = found_facts[sid]
            if belief_status != "active" or invalid_at is not None:
                invalid_source_id = sid
                break

        if invalid_source_id is not None:
            chains_to_invalidate.append((chain_id, derived_fid, invalid_source_id))
            derived_fids_to_demark.add(derived_fid)
            invalidated += 1

    if not dry_run and chains_to_invalidate:
        try:
            conn.executemany(
                "UPDATE entailment_chains SET valid = 0 WHERE id = ?",
                [(cid,) for cid, _, _ in chains_to_invalidate],
            )
        except Exception as exc:
            logger.debug("revalidate: chain bulk update failed: %s", exc)
            errors += len(chains_to_invalidate)

        if derived_fids_to_demark:
            valid_derived_ids: list[int] = []
            for fid in derived_fids_to_demark:
                try:
                    row = conn.execute(
                        "SELECT id FROM kg_facts WHERE id = ?",
                        (fid,),
                    ).fetchone()
                    if row:
                        valid_derived_ids.append(fid)
                except Exception:
                    pass
            if valid_derived_ids:
                try:
                    conn.executemany(
                        "UPDATE kg_facts SET is_entailed = 0 WHERE id = ?",
                        [(fid,) for fid in valid_derived_ids],
                    )
                except Exception as exc:
                    logger.debug(
                        "revalidate: derived fact bulk update failed: %s", exc
                    )
                    errors += len(valid_derived_ids)

    details = [
        {
            "chain_id": cid,
            "derived_fact_id": dfid,
            "invalid_source_id": sid,
        }
        for cid, dfid, sid in chains_to_invalidate
    ]

    return {
        "checked": checked,
        "invalidated": invalidated,
        "errors": errors,
        "details": details,
    }


# ---------------------------------------------------------------------------
# 2. Declarative knowledge compilation → concepts/
# ---------------------------------------------------------------------------


def _slugify(text: str, max_len: int = 80) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:max_len] or "unnamed"


def _gather_entities(conn: Any, memory_ids: Sequence[str]) -> list[dict]:
    """Fetch entities linked to memories via kg_edges or kg_facts."""
    placeholders = ",".join("?" for _ in memory_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT e.id, e.name, e.entity_type, e.centrality
          FROM kg_entities e
          JOIN kg_edges ed ON ed.source_id = e.id OR ed.target_id = e.id
          JOIN kg_facts f ON f.subject_entity_id = e.id OR f.object_entity_id = e.id
         WHERE f.source_memory IN ({placeholders})
        """,
        list(memory_ids),
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "type": r[2], "centrality": r[3] or 0.0}
        for r in rows
    ]


def _gather_facts(conn: Any, memory_ids: Sequence[str]) -> list[dict]:
    placeholders = ",".join("?" for _ in memory_ids)
    rows = conn.execute(
        f"""
        SELECT id, subject, predicate, object, confidence,
               subject_entity_id, object_entity_id, source_memory
          FROM kg_facts
         WHERE source_memory IN ({placeholders})
           AND belief_status = 'active'
           AND invalid_at IS NULL
        """,
        list(memory_ids),
    ).fetchall()
    return [
        {
            "id": r[0], "subject": r[1], "predicate": r[2],
            "object": r[3], "confidence": r[4],
            "subject_entity_id": r[5], "object_entity_id": r[6],
            "source_memory": r[7],
        }
        for r in rows
    ]


def compile_concept(
    conn: Any,
    db_path: str | Path,
    memory_ids: Sequence[str],
    *,
    concept_name: str | None = None,
    min_confidence: float = 0.4,
) -> dict[str, Any] | None:
    """Compile a concept note from a batch of memories.

    Scans the facts and entities linked to ``memory_ids``, deduplicates
    by predicate/object, then writes a structured markdown file to
    ``concepts/<slug>.md`` and inserts a row in the ``memories`` table
    with ``category='concepts'``.

    The markdown looks like::

        ---
        type: concept
        entities: [entity_id, ...]
        derived_from: [memory_id, ...]
        confidence: 0.85
        ---

        # Concept Name

        ## Definition
        LLM-synthesized definition from source facts.

        ## Properties
        - (subject, predicate, object) - confidence

        ## Evidence
        | Claim | Supporting Fact | Confidence |
        | ...   | fact_42        | 0.92       |

    Args:
        conn:          Open database connection.
        db_path:       Path to the memory database.
        memory_ids:    Sequence of note IDs to compile from.
        concept_name:  Override for the concept title (auto-derived otherwise).
        min_confidence: Ignore facts below this threshold.

    Returns:
        {"concept_id": str, "slug": str, "file": str} or None if
        there are no facts to compile.
    """
    db_path = Path(db_path)
    if not memory_ids:
        return None

    facts = [f for f in _gather_facts(conn, memory_ids) if (f["confidence"] or 0) >= min_confidence]
    entities = _gather_entities(conn, memory_ids)

    if not facts:
        return None

    # Derive concept name from most common (predicate, object) pattern if not supplied.
    if concept_name is None:
        pred_obj_counts: dict[str, int] = {}
        for f in facts:
            key = f"{f['predicate']}:{f['object']}"
            pred_obj_counts[key] = pred_obj_counts.get(key, 0) + 1
        if not pred_obj_counts:
            return None
        best = max(pred_obj_counts.items(), key=lambda x: x[1])[0]
        concept_name = best.split(":", 1)[1].replace("_", " ").title()

    slug = _slugify(concept_name)
    concepts_dir = db_path.parent / _CONCEPTS_DIR_NAME
    concepts_dir.mkdir(parents=True, exist_ok=True)
    concept_file = concepts_dir / f"{slug}.md"

    entity_ids = sorted({e["id"] for e in entities if e["id"] is not None})
    avg_conf = round(sum(f["confidence"] or 0.5 for f in facts) / max(len(facts), 1), 2)

    # Build property bullets grouped by predicate.
    prop_groups: dict[str, list] = {}
    for f in facts:
        prop_groups.setdefault(f["predicate"], []).append(f)

    # Build evidence table.
    evidence_rows = "\n".join(
        f"| ({f['subject']}, {f['predicate']}, {f['object']}) | fact_{f['id']} | {f['confidence']:.2f} |"
        for f in sorted(facts, key=lambda x: -(x["confidence"] or 0))[:20]
    )

    properties_section = "\n".join(
        f"- ({f['subject']}, {pred}, {f['object']})"
        for pred in sorted(prop_groups)
        for f in sorted(prop_groups[pred], key=lambda x: -(x["confidence"] or 0))[:10]
    )

    derived_from_json = json.dumps(sorted(set(memory_ids)))

    md = (
        "---\n"
        f"type: concept\n"
        f"entities: [{', '.join(str(e) for e in entity_ids)}]\n"
        f"derived_from: {derived_from_json}\n"
        f"confidence: {avg_conf}\n"
        "---\n"
        f"\n# {concept_name}\n"
        "\n## Definition\n"
        f"{concept_name} is a concept synthesized from "
        f"{len(memory_ids)} source memories, capturing "
        f"{len(facts)} facts across {len(entity_ids)} entities.\n"
        "\n## Properties\n"
        f"{properties_section}\n"
        "\n## Evidence\n"
        "| Claim | Supporting Fact | Confidence |\n"
        "|-------|----------------|------------|\n"
        f"{evidence_rows}\n"
    )

    try:
        concept_file.write_text(md, encoding="utf-8")
    except Exception as exc:
        logger.warning("compile: failed to write concept file %s: %s", concept_file, exc)
        return None

    concept_id = f"concepts/{slug}"

    # Persist a row in memories so the concept is searchable.
    try:
        existing = conn.execute(
            "SELECT id FROM memories WHERE id = ?",
            (concept_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (md, time.time(), concept_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO memories
                    (id, source_file, category, content, tags, pinned,
                     created_at, updated_at, observed_at, importance, metadata)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 3, ?)
                """,
                (
                    concept_id,
                    f"concepts/{slug}.md",
                    "concepts",
                    md,
                    "[]",
                    time.time(),
                    time.time(),
                    time.time(),
                    json.dumps({
                        "derived_from": sorted(set(memory_ids)),
                        "entities": entity_ids,
                        "fact_ids": [f["id"] for f in facts],
                        "avg_confidence": avg_conf,
                    }),
                ),
            )
    except Exception as exc:
        logger.debug("compile: failed to persist concept row: %s", exc)

    return {
        "concept_id": concept_id,
        "slug": slug,
        "file": str(concept_file),
        "facts_used": len(facts),
        "entities": len(entity_ids),
        "avg_confidence": avg_conf,
    }


# ---------------------------------------------------------------------------
# 3. Procedural-to-declarative skill enrichment
# ---------------------------------------------------------------------------


def _detect_kind(content: str) -> str | None:
    """Heuristic: decide whether this memory is declarative knowledge
    ('concept'), a decision principle ('principle'), or an ontology
    mapping ('ontology').

    Rules (first match wins):
        concept   — content contains 'definition' or 'is a' patterns
        principle — content contains 'should' / 'must' / 'rule'
        ontology  — content contains relations between types/entities
    """
    lowered = content.lower()
    if re.search(r"\b(should|must|rule|principle|guideline)\b", lowered):
        return "principle"
    if re.search(r"\b(is a|is an|is type of|part of|subclass)\b", lowered):
        return "ontology"
    if re.search(r"\b(definition|concept|means|refers to|defined as)\b", lowered):
        return "concept"
    return None


def enrich_existing_skill(
    conn: Any,
    db_path: str | Path,
    memory_id: str,
    content: str,
) -> dict[str, Any] | None:
    """Enrich a procedural memory with a declarative companion note.

    Reads ``content``, classifies it, and writes one of:
        concepts/<slug>.md   — if classified as 'concept'
        principles/<slug>.md — if classified as 'principle'
        ontology/<slug>.md   — if classified as 'ontology'

    The companion note is lightweight: a short definition/statement
    derived from the source content. A row is inserted into ``memories``
    with ``category`` matching the target directory.

    Returns dict on success, None if no classification matched.
    """
    kind = _detect_kind(content)
    if kind is None:
        return None

    subdir_map = {
        "concept": _CONCEPTS_DIR_NAME,
        "principle": _PRINCIPLES_DIR_NAME,
        "ontology": _ONTOLOGY_DIR_NAME,
    }
    subdir = subdir_map[kind]
    # Derive slug from the first sentence.
    first_line = content.strip().split("\n")[0]
    slug = _slugify(first_line, max_len=60)
    out_dir = Path(db_path).parent / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{slug}.md"

    md = (
        "---\n"
        f"type: {kind}\n"
        f"derived_from: [{memory_id}]\n"
        "---\n"
        f"\n# {first_line[:80]}\n"
        "\n## Source\n"
        f"Derived from `{memory_id}`.\n"
        "\n## Statement\n"
        f"{content[:500]}\n"
    )

    try:
        out_file.write_text(md, encoding="utf-8")
    except Exception as exc:
        logger.warning("enrich: failed to write %s: %s", out_file, exc)
        return None

    note_id = f"{subdir}/{slug}"
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO memories
                (id, source_file, category, content, tags, pinned,
                 created_at, updated_at, observed_at, importance, metadata)
            VALUES (?, ?, ?, '', 0, ?, ?, ?, 3, ?)
            """,
            (
                note_id, str(out_file), subdir, md,
                time.time(), time.time(), time.time(),
                json.dumps({"derived_from": [memory_id], "kind": kind}),
            ),
        )
    except Exception as exc:
        logger.debug("enrich: failed to persist row: %s", exc)

    return {"type": kind, "slug": slug, "file": str(out_file), "note_id": note_id}


# ---------------------------------------------------------------------------
# Background handlers (called from background_worker.py)
# ---------------------------------------------------------------------------


def handle_entailment_chains(
    payload: dict,
    conn: Any,
    db_path: str | Path,
) -> str:
    """Background handler: infer entailment chains for a memory batch."""
    memory_ids = payload.get("memory_ids", [])
    batch_size = int(payload.get("batch_size", 200))
    if not memory_ids:
        # Wildcard: scan all pending facts.
        result = infer_entailment_chains(
            conn, db_path, batch_size=batch_size, min_confidence=0.3
        )
        return (
            f"entailment_chains: derived={result['derived']} "
            f"skipped={result['skipped']} errors={result['errors']}"
        )
    result = infer_entailment_chains(
        conn, db_path, batch_size=batch_size, min_confidence=0.3
    )
    return (
        f"entailment_chains: derived={result['derived']} "
        f"skipped={result['skipped']} errors={result['errors']}"
    )


def handle_concept_compilation(
    payload: dict,
    conn: Any,
    db_path: str | Path,
) -> str:
    """Background handler: compile a concept note from memories."""
    memory_ids = payload.get("memory_ids", [])
    concept_name = payload.get("concept_name")
    if not memory_ids:
        return "concept_compilation: skipped (no memory_ids)"
    result = compile_concept(
        conn, db_path, memory_ids, concept_name=concept_name, min_confidence=0.4
    )
    if result is None:
        return "concept_compilation: no facts to compile"
    return (
        f"concept_compilation: wrote {result['file']} "
        f"(facts={result['facts_used']}, entities={result['entities']}, "
        f"confidence={result['avg_confidence']})"
    )


def handle_skill_enrichment(
    payload: dict,
    conn: Any,
    db_path: str | Path,
) -> str:
    """Background handler: enrich a memory with a declarative companion note."""
    memory_id = payload.get("memory_id", "")
    content = payload.get("content", "")
    if not memory_id or not content:
        return "skill_enrichment: skipped (missing memory_id or content)"
    result = enrich_existing_skill(conn, db_path, memory_id, content)
    if result is None:
        return "skill_enrichment: no classification matched"
    return f"skill_enrichment: wrote {result['file']} ({result['type']})"
