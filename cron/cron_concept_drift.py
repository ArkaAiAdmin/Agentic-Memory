#!/usr/bin/env python3
"""Cron wrapper: concept drift detection (2026-06-19, 2026-06-22 refactor).

Runs the memory_check_concept_drift logic on a schedule. Before this
script existed, the ``concept_drift`` table was empty — the drift
check was only invokable manually via the MCP tool.

Mirrors the pattern in cron_detect_vec_drift.py:
- Default DB path from GLOBAL_MEM_DIR
- Reads threshold from config (memory.toml ``[search]`` or env)
- Writes a row to ``concept_drift`` only when drift >= threshold
- Default output is a single minimal line on stdout
  (``concept_drift: scanned=N, drifted=M``) — alerts go to the
  standard cron log via the ``logging`` module; pass ``--verbose``
  to get the full per-event detail.

Run from crontab:
    7 5 * * 0 .../venv/bin/python .../cron_concept_drift.py >> .../memory/concept-drift.log 2>&1

The drift computation is a cosine distance between the current
embedding centroid and the previously stored one (or 0 if there
is no prior). Cosine distance > threshold → write a row.
"""

from _flock import acquire_lock_or_exit
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

import numpy as np
import numpy.typing as npt

os.chdir(os.path.dirname(os.path.abspath(__file__)))
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from infra.memory_common import GLOBAL_MEM_DIR

DEFAULT_DB_PATH = str(GLOBAL_MEM_DIR / "memory.db")
DEFAULT_THRESHOLD = 0.15

# Module logger — cron captures the stderr/stdout of the script,
# so all alerts (DRIFT, BASELINE, ERROR) flow through here. Verbose
# per-event detail only prints when --verbose is set, keeping the
# default cron log readable.
from infra.log import setup_logging

logger = setup_logging("cron_concept_drift")


def _get_threshold() -> float:
    """Read concept-drift threshold from config (env/memory.toml)."""
    try:
        from infra._lazy_imports import get_config

        cfg = get_config()
        return float(cfg.concept_drift_threshold)
    except Exception:
        try:
            return float(
                os.environ.get("MEMORY_CONCEPT_DRIFT_THRESHOLD", str(DEFAULT_THRESHOLD))
            )
        except Exception:
            return DEFAULT_THRESHOLD


def _compute_centroid(conn: sqlite3.Connection) -> Optional[npt.NDArray[np.float32]]:
    """Stack all embeddings into a numpy array and return the centroid.

    Returns None if there are no embeddings or numpy is missing.
    """
    rows = conn.execute("SELECT embedding FROM memory_embeddings").fetchall()
    if not rows:
        return None
    vectors: list[npt.NDArray[np.float32]] = []
    for (blob,) in rows:
        if not blob:
            continue
        try:
            vec: npt.NDArray[np.float32] = np.frombuffer(blob, dtype=np.float32).copy()
            vectors.append(vec)
        except Exception:
            continue
    if not vectors:
        return None
    centroid_arr: npt.NDArray[np.float32] = np.stack(vectors).mean(axis=0)
    return centroid_arr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect concept drift in the embedding space."
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"Path to the memory SQLite DB (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Cosine-distance threshold above which to record a drift event "
        "(default: from config or 0.15)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full per-event detail (DRIFT/BASELINE/OK lines). "
        "Default: single minimal line on stdout.",
    )
    args = parser.parse_args(argv)
    threshold = args.threshold if args.threshold is not None else _get_threshold()
    acquire_lock_or_exit('cron_concept_drift')

    # Default cron log level is WARNING (errors and drift alerts only).
    # --verbose promotes to INFO so the OK/DRIFT/BASELINE lines print.
    setup_logging(
        "cron_concept_drift",
        level="INFO" if args.verbose else "WARNING",
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not Path(args.db_path).exists():
        logger.error("no memory.db at %s", args.db_path)
        sys.exit(1)

    try:
        t0 = time.time()
        from infra.db_write_queue import sqlite_write_queue
        conn = sqlite_write_queue.start_session(Path(args.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            centroid = _compute_centroid(conn)
            if centroid is None:
                print("concept_drift: scanned=0, drifted=0")
                sys.exit(0)

            n_embedded = int(
                conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
            )
            centroid_dim = int(centroid.shape[0])

            prev = conn.execute(
                "SELECT drifted_dimensions FROM concept_drift ORDER BY triggered_at DESC LIMIT 1"
            ).fetchone()
            prev_centroid: Optional[npt.NDArray[np.float32]] = None
            if prev and prev[0]:
                try:
                    prev_centroid = np.asarray(json.loads(prev[0]), dtype=np.float32)
                except (ValueError, TypeError):
                    prev_centroid = None

            # First-run baseline: if no prior centroid exists, write a
            # baseline row at drift=0.0 so the table is non-empty and
            # subsequent runs have something to compare against. This
            # also surfaces "no data" as visible state instead of an
            # empty table that looks like a bug.
            is_baseline = prev_centroid is None

            if prev_centroid is not None and len(prev_centroid) == len(centroid):
                assert prev_centroid is not None  # narrow for type checker
                cos_sim = float(
                    np.dot(centroid, prev_centroid)
                    / (np.linalg.norm(centroid) * np.linalg.norm(prev_centroid) + 1e-10)
                )
                drift: float = 1.0 - cos_sim
            else:
                drift = 0.0

            diff = centroid - (
                prev_centroid if prev_centroid is not None else np.zeros_like(centroid)
            )

            # E2 / G8 fix (2026-06-22): delegate the actual write to
            # ``_record_drift_event`` so the cron + MCP tool paths share
            # one implementation.  ``_record_drift_event`` also enforces
            # a 60-second dedupe window so back-to-back invocations
            # don't write duplicate rows to ``concept_drift``.
            from search.orchestrator import _record_drift_event

            alarm_id, n_alarms_written, was_written = _record_drift_event(
                conn,
                centroid,
                diff,
                drift,
                threshold,
                is_baseline=is_baseline,
                time_mod=time,
            )
            if was_written:
                conn.commit()
                if is_baseline:
                    logger.info(
                        "BASELINE drift_metric=%.4f (no prior) threshold=%s "
                        "alarm_id=%s n_embedded=%d centroid_dim=%d "
                        "n_alarms=%d elapsed=%.2fs",
                        drift,
                        threshold,
                        alarm_id,
                        n_embedded,
                        centroid_dim,
                        n_alarms_written,
                        time.time() - t0,
                    )
                else:
                    alarm_level = (
                        "critical"
                        if drift >= 2.0 * threshold
                        else "warning"
                        if drift >= 1.5 * threshold
                        else "info"
                    )
                    logger.info(
                        "DRIFT drift_metric=%.4f threshold=%s alarm_id=%s "
                        "n_embedded=%d centroid_dim=%d alarm_level=%s "
                        "n_alarms=%d elapsed=%.2fs",
                        drift,
                        threshold,
                        alarm_id,
                        n_embedded,
                        centroid_dim,
                        alarm_level,
                        n_alarms_written,
                        time.time() - t0,
                    )
            else:
                logger.info(
                    "OK drift_metric=%.4f below threshold=%s n_embedded=%d "
                    "centroid_dim=%d elapsed=%.2fs",
                    drift,
                    threshold,
                    n_embedded,
                    centroid_dim,
                    time.time() - t0,
                )
        finally:
            conn.close()
        # Minimal one-line summary on stdout (always — this is the
        # cron-friendly single line the user wants to see in the log).
        n_drifted = 1 if (drift >= threshold and not is_baseline) else 0
        print(f"concept_drift: scanned={n_embedded}, drifted={n_drifted}")
        sys.exit(0)
    except Exception:
        logger.error("Script failed with exception:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
