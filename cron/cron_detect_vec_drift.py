#!/usr/bin/env python3
"""Detect drift between vec_keys and embeddings in a memory DB."""

from _flock import acquire_lock_or_exit
import argparse
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
import sys
import os
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from memory_common import GLOBAL_MEM_DIR

DEFAULT_DB_PATH = str(GLOBAL_MEM_DIR / "memory.db")

# Test-accessible thresholds: WARN when drift > WARN_THRESHOLD,
# INFO when drift > INFO_THRESHOLD. Defaults assume 5-drift rebuild threshold.
WARN_THRESHOLD = 50
INFO_THRESHOLD = 10


def _get_rebuild_threshold() -> int:
    """Read vec_rebuild_threshold from config (env/memory.toml)."""
    try:
        from _lazy_imports import get_config
        val = get_config().vec_rebuild_threshold
        return int(val) if val is not None else 5
    except Exception:
        return int(os.environ.get("MEMORY_VEC_REBUILD_THRESHOLD", "5"))


def _derive_thresholds() -> tuple[int, int]:
    """Derive WARN and INFO thresholds from the rebuild threshold.

    WARN = 10x rebuild threshold (e.g. rebuild=5 → WARN at 50)
    INFO = 2x rebuild threshold (e.g. rebuild=5 → INFO at 10)
    """
    base = _get_rebuild_threshold()
    return max(base * 10, 10), max(base * 2, 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect drift between vec_keys and vec_idx tables."
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"Path to the memory SQLite DB (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)
    acquire_lock_or_exit('cron_detect_vec_drift')

    try:
        conn = sqlite3.connect(args.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        cursor = conn.cursor()

        n_mem = cursor.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()[0]
        n_vec = cursor.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()[0]
        n_emb = cursor.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]

        warn_threshold, info_threshold = _derive_thresholds()

        vec_drift = n_mem - n_vec
        emb_drift = n_mem - n_emb

        if vec_drift > warn_threshold or emb_drift > warn_threshold:
            print(
                f"WARN: memories={n_mem}, vec_keys={n_vec}, embeddings={n_emb}, "
                f"vec_drift={vec_drift}, emb_drift={emb_drift} "
                f"(thresholds: INFO>{info_threshold}, WARN>{warn_threshold})"
            )
        elif vec_drift > info_threshold or emb_drift > info_threshold:
            print(
                f"INFO: memories={n_mem}, vec_keys={n_vec}, embeddings={n_emb}, "
                f"vec_drift={vec_drift}, emb_drift={emb_drift}"
            )
        else:
            print(
                f"OK: memories={n_mem}, vec_keys={n_vec}, embeddings={n_emb}, "
                f"vec_drift={vec_drift}, emb_drift={emb_drift}"
            )

        conn.close()
        sys.exit(0)

    except Exception:
        print("ERROR: Script failed with exception:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
