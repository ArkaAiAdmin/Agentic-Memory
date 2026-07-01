#!/usr/bin/env python3
"""Corpus budget guard for agentic-memory.

Runs inside the background worker loop. Checks the current corpus size
against the configured budget multiple and triggers a compaction job
if the corpus has grown too large.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CORPUS_BUDGET_MULTIPLE = 5
_BUDGET_TOKENS = 50_000
_HEALTH_FILE_NAME = ".health_status.json"


def _get_memory_dir() -> Path:
    from background.auto_save import get_db_path
    return get_db_path().parent


def _corpus_budget_multiple() -> int:
    env_val = os.environ.get("MEMORY_CORPUS_BUDGET_MULTIPLE")
    if env_val is not None:
        try:
            return max(1, int(env_val))
        except ValueError:
            pass
    try:
        from infra._lazy_imports import get_config
        cfg = get_config()
        return max(1, int(getattr(cfg, "corpus_budget_multiple", _DEFAULT_CORPUS_BUDGET_MULTIPLE)))
    except Exception:
        return _DEFAULT_CORPUS_BUDGET_MULTIPLE


def _estimate_corpus_tokens(db_path: Path) -> int:
    try:
        from infra.memory_common import connection_pool, safe_close_db
        conn = connection_pool.get(str(db_path), timeout=10.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*), AVG(LENGTH(content)) FROM memories WHERE deleted_at IS NULL"
            ).fetchone()
            if not row or not row[0]:
                return 0
            count = int(row[0])
            avg_len = float(row[1] or 0)
            return int(count * avg_len / 4.0)
        finally:
            safe_close_db(conn)
    except Exception as exc:
        logger.debug("corpus_budget_guard: estimate failed: %s", exc)
        return 0


def _read_health_status(memory_dir: Path) -> dict:
    health_path = memory_dir / _HEALTH_FILE_NAME
    if not health_path.exists():
        return {}
    try:
        return json.loads(health_path.read_text())
    except Exception:
        return {}


def _write_health_status(memory_dir: Path, status: dict) -> None:
    health_path = memory_dir / _HEALTH_FILE_NAME
    try:
        health_path.write_text(json.dumps(status, indent=2, default=str))
    except Exception as exc:
        logger.debug("corpus_budget_guard: failed to write health file: %s", exc)


def check_corpus_budget(db_path: Path, conn=None) -> dict:
    memory_dir = _get_memory_dir()
    now = time.time()

    tokens = _estimate_corpus_tokens(db_path)
    budget = _BUDGET_TOKENS * _corpus_budget_multiple()
    over_budget = tokens > budget if budget > 0 else False

    health = _read_health_status(memory_dir)
    health.update({
        "last_budget_check_ts": now,
        "corpus_tokens_est": tokens,
        "corpus_budget_tokens": budget,
        "corpus_over_budget": over_budget,
    })
    _write_health_status(memory_dir, health)

    if over_budget:
        logger.warning(
            "corpus_budget_guard: corpus ~%d tokens exceeds budget %d",
            tokens,
            budget,
        )
        return {
            "action": "compact",
            "tokens": tokens,
            "budget": budget,
            "multiple": _corpus_budget_multiple(),
            "memory_dir": str(memory_dir),
        }

    return {
        "action": None,
        "tokens": tokens,
        "budget": budget,
        "multiple": _corpus_budget_multiple(),
        "memory_dir": str(memory_dir),
    }


def _enqueue_compaction(db_path: Path, conn) -> bool:
    try:
        import json as _json
        payload = _json.dumps({
            "type": "compact",
            "reason": "corpus_budget_exceeded",
            "tokens": _estimate_corpus_tokens(db_path),
        })
        conn.execute(
            "INSERT INTO task_queue (task_type, payload, status, created_at) "
            "VALUES ('compact', ?, 'pending', ?)",
            (payload, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        conn.commit()
        logger.info("corpus_budget_guard: enqueued compaction task")
        return True
    except Exception as exc:
        logger.error("corpus_budget_guard: failed to enqueue compaction: %s", exc)
        return False


def run_corpus_budget_guard(db_path: Path, conn=None) -> dict:
    status = check_corpus_budget(db_path, conn=conn)
    if status.get("action") == "compact" and conn is not None:
        status["compaction_enqueued"] = _enqueue_compaction(db_path, conn)
    return status
