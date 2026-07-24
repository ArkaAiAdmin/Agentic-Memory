"""Concept drift detection and alarm recording."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)


def _record_drift_event(
    conn: AnyConnection,
    centroid: Any,
    diff: Any,
    drift: float,
    threshold: float,
    *,
    is_baseline: bool = False,
    min_seconds_between_writes: float = 60.0,
    time_mod=None,
) -> tuple[str, int, bool]:
    """Write a single concept-drift event to ``concept_drift`` + ``drift_alarms``.

    Shared between ``check_concept_drift_db`` (the MCP tool path) and
    ``cron/cron_concept_drift.py`` (the scheduled path).  E2 / G8 fix
    (2026-06-22): the write logic used to be duplicated in both
    call sites and could write duplicate rows in the same time
    window.  The dedupe check (``min_seconds_between_writes``) makes
    a re-run within ``min_seconds_between_writes`` of the previous
    write a no-op — the cron + MCP tool can run back-to-back without
    polluting the table.

    Args:
        conn: Open ``sqlite3.Connection``.  Caller manages commit.
        centroid: numpy array of the current embedding centroid.
        diff: numpy array of ``centroid - prev_centroid`` (or
            ``centroid`` if no prior).  Used to compute per-memory
            alarm contributions on the top-5 dimensions.
        drift: Cosine distance between current and previous centroid.
            When ``drift < threshold`` and ``is_baseline`` is False,
            this function is a no-op.
        threshold: Cosine-distance threshold (used both for the gate
            and to record the threshold snapshot in the alarm row).
        is_baseline: When True, force a write even if ``drift == 0``
            (first run after a fresh DB).  The alarm_level for a
            baseline event is forced to ``info``.
        min_seconds_between_writes: Skip the write if the most recent
            ``concept_drift`` row is younger than this.  G8 fix.
        time_mod: Optional module with ``time()`` / ``gmtime()`` /
            ``strftime``.  Defaults to the standard library ``time``.
            Cron / tests can pass a fake.

    Returns:
        ``(alarm_id, n_alarms_written, was_written)``.  ``was_written``
        is False when the gate (``drift >= threshold`` or
        ``is_baseline``) is not met, or when the dedupe window
        suppresses the write.  The caller should still commit on a
        no-op (no harm; the read path doesn't care).
    """
    import time as _time

    if time_mod is None:
        time_mod = _time

    if drift < threshold and not is_baseline:
        return "", 0, False

    # G8 fix: skip if a row was written very recently. Without this
    # gate, the MCP tool + cron running back-to-back would write
    # duplicate rows with the same drift_metric.
    try:
        recent = conn.execute(
            "SELECT triggered_at FROM concept_drift ORDER BY triggered_at DESC LIMIT 1"
        ).fetchone()
        if recent and recent[0] is not None:
            try:
                age = float(time_mod.time()) - float(recent[0])
                if age < min_seconds_between_writes:
                    return "", 0, False
            except (TypeError, ValueError):
                pass
    except sqlite3.OperationalError:
        # concept_drift table doesn't exist yet — first run; allow write.
        pass

    import numpy as _np

    # M29 fix: use uuid4 to avoid collision when multiple drifts detected in the same second
    alarm_id = f"drift_{uuid.uuid4().hex[:12]}"
    detected_at_iso = time_mod.strftime("%Y-%m-%dT%H:%M:%SZ", time_mod.gmtime())
    # The `drifted_dimensions` column stores the centroid (not a list
    # of dim deltas).  Downstream read code parses it as a numpy
    # centroid; storing the dim delta list there breaks the read+write
    # pair.  The dim-delta summary lives in the per-memory alarm
    # `concept` field instead.
    centroid_json = json.dumps(centroid.tolist())
    conn.execute(
        "INSERT INTO concept_drift "
        "(id, drift_metric, drifted_dimensions, triggered_at) "
        "VALUES (?, ?, ?, ?)",
        (alarm_id, round(drift, 4), centroid_json, time_mod.time()),
    )

    # L22 fix: unify alarm_level with return dict logic (same thresholds)
    if is_baseline:
        alarm_level = "info"
    elif drift >= 2.0 * threshold:
        alarm_level = "critical"
    elif drift >= 1.5 * threshold:
        alarm_level = "warning"
    elif drift >= threshold:
        alarm_level = "info"
    else:
        alarm_level = ""

    # Top-5 dimensions that drifted the most.
    top_idxs = sorted(
        range(len(diff)),
        key=lambda i: -abs(float(diff[i])),
    )[:5]
    n_alarms_written = 0
    try:
        top_memory_rows = conn.execute(
            "SELECT memory_id, embedding FROM memory_embeddings"
        ).fetchall()
        scored = []
        for mem_id, blob in top_memory_rows:
            if not blob:
                continue
            try:
                vec = _np.frombuffer(blob, dtype=_np.float32).copy()
            except (ValueError, BufferError):
                continue
            contrib = float(
                sum(abs(float(vec[i])) * abs(float(diff[i])) for i in top_idxs)
            )
            scored.append((mem_id, contrib))
        scored.sort(key=lambda x: -x[1])
        # Cap per-event fan-out: 10 alarms per drift event. This
        # keeps the table queryable even during severe drift bursts;
        # the operator can re-run for more if needed.
        for mem_id, _ in scored[:10]:
            try:
                conn.execute(
                    "INSERT INTO drift_alarms "
                    "(memory_id, concept, drift_score, threshold, "
                    " alarm_level, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        mem_id,
                        f"embedding_dim_top{','.join(str(i) for i in top_idxs)}",
                        round(drift, 4),
                        threshold,
                        alarm_level,
                        detected_at_iso,
                    ),
                )
                n_alarms_written += 1
            except sqlite3.IntegrityError:
                # FK violation (memory hard-deleted between read and
                # write) is non-fatal; skip.
                continue
    except sqlite3.OperationalError:
        # drift_alarms table doesn't exist yet (pre-v15 DB); silently
        # skip per-memory alarms.
        pass

    return alarm_id, n_alarms_written, True


def check_concept_drift_db(db_path: str | Path, threshold: float = 0.15, tenant_id: str = "default") -> dict:
    """Check concept drift with connection lifecycle managed.

    Writes a row to the ``concept_drift`` table when drift exceeds the
    threshold. Also writes a per-memory alarm to ``drift_alarms`` (added
    in v15) for every memory whose top-drifted-dimension index
    corresponds to a high-contribution row, so operators have a
    per-memory view of which notes triggered the alarm.

    E2 / G8 fix (2026-06-22): the actual write logic now lives in
    ``_record_drift_event`` so the cron and MCP paths share one
    implementation.  ``_record_drift_event`` also enforces a 60-second
    dedupe window so back-to-back invocations don't write duplicate
    rows to the ``concept_drift`` table.
    """
    import numpy as _np
    from infra._lazy_imports import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
    try:
        rows = conn.execute("SELECT embedding FROM memory_embeddings").fetchall()
        if not rows:
            return {
                "drift_metric": 0.0,
                "drifted_dimensions": [],
                "alarm_id": "",
                "n_embedded": 0,
                "note": "no embeddings found",
            }
        vectors = []
        for (blob,) in rows:
            vec = _np.frombuffer(blob, dtype=_np.float32).copy()
            vectors.append(vec)
        if not vectors:
            return {
                "drift_metric": 0.0,
                "drifted_dimensions": [],
                "alarm_id": "",
                "n_embedded": 0,
                "note": "no embeddings found",
            }
        from collections import Counter
        dims = [len(v) for v in vectors]
        most_common_dim = Counter(dims).most_common(1)[0][0]
        vectors = [v for v in vectors if len(v) == most_common_dim]
        embeddings = _np.stack(vectors)
        centroid = embeddings.mean(axis=0)
        prev = conn.execute(
            "SELECT drifted_dimensions FROM concept_drift ORDER BY triggered_at DESC LIMIT 1"
        ).fetchone()
        prev_centroid = None
        if prev and prev[0]:
            try:
                prev_centroid = _np.array(json.loads(prev[0]))
            except json.JSONDecodeError as _de:
                logger.warning("concept_drift: failed to parse drifted_dimensions: %s", _de)
        if prev_centroid is not None and len(prev_centroid) == len(centroid):
            cos_sim = float(
                _np.dot(centroid, prev_centroid)
                / (_np.linalg.norm(centroid) * _np.linalg.norm(prev_centroid) + 1e-10)
            )
            drift = 1.0 - cos_sim
            diff = centroid - prev_centroid
        else:
            drift = 0.0
            diff = centroid
        top_dims = sorted(
            enumerate(abs(diff).tolist()),
            key=lambda x: -x[1],
        )[:5]
        drifted = [
            {"index": idx, "delta": round(float(diff[idx]), 4)} for idx, _ in top_dims
        ]
        # E2 fix (2026-06-22): detect "first run / no prior centroid" and
        # pass ``is_baseline=True`` so the shared writer records a
        # baseline row + per-memory alarm.  Without this, the orchestrator
        # would never write on the first run (drift is always 0 when
        # prev_centroid is None), making ``concept_drift`` look empty
        # until something actually drifts.  The cron path already
        # detected this case; this brings the MCP path into parity.
        is_baseline = prev_centroid is None
        alarm_id, n_alarms_written, was_written = _record_drift_event(
            conn,
            centroid,
            diff,
            drift,
            threshold,
            is_baseline=is_baseline,
        )
        if was_written:
            conn.commit()
        # Derive alarm_level from drift magnitude
        if drift >= threshold * 2:
            computed_level = "critical"
        elif drift >= threshold:
            computed_level = "warning"
        else:
            computed_level = "info"
        return {
            "drift_metric": round(drift, 4),
            "drifted_dimensions": drifted,
            "alarm_id": alarm_id,
            "n_embedded": len(vectors),
            "n_alarms_written": n_alarms_written,
            "alarm_level": computed_level,
        }
    finally:
        safe_close_db(conn)
