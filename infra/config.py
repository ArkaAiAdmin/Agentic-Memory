"""Thin backward-compatibility shim — re-exports everything from config.py.

All logic lives in config.py (the canonical central configuration module).
This file exists solely so that any lingering ``from infra.config import …``
references continue to work during the migration window. It should be
removed once all call sites have been updated.
"""
from __future__ import annotations

from config import (  # noqa: F401
    DECISION_CATEGORIES,
    AGENTS_SKILLS_DIR,
    GLOBAL_SCRIPTS_DIR,
    INSTALL_ROOT,
    MemoryConfig,
    OPENCODE_SKILLS_DIR,
    SCRIPTS_SUBDIR,
    _instance,
    get_config,
    get_feature_flags,
    log_feature_flags_at_startup,
    reset_config,
    resolve_db_path,
)

__all__ = [
    "MemoryConfig",
    "get_config",
    "reset_config",
    "get_feature_flags",
    "log_feature_flags_at_startup",
    "INSTALL_ROOT",
    "GLOBAL_SCRIPTS_DIR",
    "SCRIPTS_SUBDIR",
    "AGENTS_SKILLS_DIR",
    "OPENCODE_SKILLS_DIR",
    "resolve_db_path",
    "DECISION_CATEGORIES",
    "_instance",
]
