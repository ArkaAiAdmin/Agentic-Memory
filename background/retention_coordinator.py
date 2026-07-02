"""Retention Coordinator.

Coordinates adaptive_retention (metadata half-life updates) and neural_forget
(sigmoid decay calculations using the metadata half-lives) into a single pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from background.adaptive_retention import batch_update_retention as run_adaptive_retention
from neural_forget import batch_update_retention as run_neural_forget

logger = logging.getLogger(__name__)


def run_retention_pipeline(
    db_path: str | Path,
    base_halflife: float = 180.0,
    limit: int = 500,
) -> dict:
    """Run both retention steps in sequence to unify decay parameters and scores."""
    logger.info("Starting unified retention pipeline for %s", db_path)

    # 1. Run adaptive retention to update note metadata with half-life values
    adaptive_result = {}
    try:
        adaptive_result = run_adaptive_retention(
            base_halflife=base_halflife,
            db_path=str(db_path),
        )
        logger.info("Adaptive retention update completed: %s", adaptive_result)
    except Exception as e:
        logger.exception("Adaptive retention step failed in pipeline: %s", e)
        adaptive_result = {"error": str(e)}

    # 2. Run neural forgetting rate model (which uses the updated metadata half-lives)
    neural_result = {}
    try:
        neural_result = run_neural_forget(db_path=db_path, limit=limit)
        logger.info("Neural forget update completed: %s", neural_result)
    except Exception as e:
        logger.exception("Neural forget step failed in pipeline: %s", e)
        neural_result = {"error": str(e)}

    return {
        "adaptive_retention": adaptive_result,
        "neural_forget": neural_result,
    }
