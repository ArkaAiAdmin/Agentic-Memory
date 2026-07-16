#!/usr/bin/env python3
"""Cron script: Recompute temporal priors per category.

Fits decay = exp(-age_days * ln(2) / half_life_days) per category against
access_count history. Writes fitted half-lives to memory_temporal_priors.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
try:
    from infra.tenant_query import install_tenant_context
except Exception:  # pragma: no cover
    def install_tenant_context(conn, tenant_id=None):
        import os
        tid = tenant_id or os.environ.get("MEMORY_CRON_TENANT_ID") or "default"
        conn.create_function("tenant_id", 0, lambda: tid)
        conn.execute('CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS SELECT * FROM memories WHERE tenant_id = tenant_id()')
        return tid

import sqlite3
import sys
import time
import traceback
from pathlib import Path

# Bootstrap path
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from _flock import acquire_lock_or_exit
from infra.memory_common import GLOBAL_MEM_DIR
from infra.log import setup_logging

logger = setup_logging("cron_recompute_temporal_priors")

DEFAULT_DB_PATH = str(GLOBAL_MEM_DIR / "memory.db")

DEFAULT_TEMPORAL_PRIORS = {
    "lessons": 180.0,
    "concepts": 730.0,
    "sessions": 14.0,
    "preferences": 90.0,
    "projects": 365.0,
    "decisions": 365.0,
    "facts": 90.0,
}


def fit_half_life(category: str, rows: list[tuple[str, int]]) -> float:
    """Fit decay = exp(-age_days * ln(2) / half_life_days) via linear regression.

    Y_i = ln(access_count_i)
    X_i = age_days_i
    y = A * exp(-B * x) => ln(y) = ln(A) - B * x
    where B = ln(2) / half_life_days => half_life_days = ln(2) / B.

    If B <= 1e-5 (decay doesn't decrease, or grows), we return the default.
    """
    default_hl = DEFAULT_TEMPORAL_PRIORS.get(category, 180.0)
    if len(rows) < 10:
        logger.debug(
            "Category '%s' has insufficient data (%d samples). Falling back to default: %.1f",
            category,
            len(rows),
            default_hl,
        )
        return default_hl

    now = time.time()
    x = []
    y = []
    for created_at, access_count in rows:
        if not created_at:
            continue
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(created_at)
            ts = dt.timestamp()
            age_days = max(0.01, (now - ts) / 86400.0)
            x.append(age_days)
            y.append(math.log(max(1.0, float(access_count))))
        except Exception:
            continue

    if len(x) < 10:
        return default_hl

    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)

    num = 0.0
    den = 0.0
    for xi, yi in zip(x, y):
        num += (xi - mean_x) * (yi - mean_y)
        den += (xi - mean_x) ** 2

    if den < 1e-5:
        logger.debug(
            "Category '%s' has zero or near-zero variance in age. Falling back to default: %.1f",
            category,
            default_hl,
        )
        return default_hl

    beta_1 = num / den

    # We expect beta_1 to be negative (older notes have fewer accesses / decay).
    # If beta_1 is positive or near zero, decay is not working or accessed more.
    if beta_1 >= -1e-5:
        logger.debug(
            "Category '%s' fitted slope is non-negative (beta_1=%.6f). Falling back to default: %.1f",
            category,
            beta_1,
            default_hl,
        )
        return default_hl

    b = -beta_1
    hl = math.log(2.0) / b

    # Bound half-life to [1.0, 3650.0] days to prevent extreme values
    hl_bounded = max(1.0, min(3650.0, hl))
    logger.info(
        "Fitted half-life for category '%s': %.1f days (raw=%.1f, beta_1=%.6f, n=%d)",
        category,
        hl_bounded,
        hl,
        beta_1,
        len(x),
    )
    return hl_bounded


def main():
    parser = argparse.ArgumentParser(description="Recompute temporal priors per category.")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to database")
    args = parser.parse_args()

    # acquire flock to prevent concurrent runs
    lock_file = Path(args.db).parent / "cron_recompute_temporal_priors.lock"
    acquire_lock_or_exit(str(lock_file))

    t0 = time.time()
    try:
        conn = sqlite3.connect(args.db)
    install_tenant_context(conn, os.environ.get("MEMORY_CRON_TENANT_ID"))

        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            # 1. Ensure memory_temporal_priors table exists
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_temporal_priors (
                    category       TEXT PRIMARY KEY,
                    half_life_days REAL NOT NULL,
                    updated_at     REAL NOT NULL DEFAULT (strftime('%s', 'now'))
                )
                """
            )

            # 2. Get all non-deleted memories category, created_at, access_count
            memories = conn.execute(
                "SELECT category, created_at, access_count FROM memories WHERE deleted_at IS NULL"
            ).fetchall()

            # Group memories by category
            by_category: dict[str, list[tuple[str, int]]] = {}
            for cat, created, count in memories:
                if cat:
                    by_category.setdefault(cat, []).append((created, count or 1))

            # 3. Fit half-life for each category, fallback to default otherwise
            updated = 0
            for category in DEFAULT_TEMPORAL_PRIORS:
                rows = by_category.get(category, [])
                hl = fit_half_life(category, rows)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memory_temporal_priors (category, half_life_days, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (category, hl, time.time()),
                )
                updated += 1

            conn.commit()
            logger.info("Successfully updated %d temporal priors in %.2fs", updated, time.time() - t0)
            print(f"temporal_priors: updated={updated}")
        finally:
            conn.close()
    except Exception:
        logger.error("Script failed with exception:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
