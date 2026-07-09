"""Process execution scope — drives config-drift policy application.

Scopes (in priority order):
  1. MEMORY_SCOPE env var          (explicit operator intent)
  2. memory.toml [scope] section    (deployment default)
  3. Auto-detection heuristics:
     - "pytest" in sys.modules     → test
     - MEMORY_DB_PATH outside install_root → production
     - otherwise → development

Scopes mean different things — see policy resolution rules.
"""

from __future__ import annotations

import logging
import os
import sys
from enum import Enum

logger = logging.getLogger(__name__)


class Scope(str, Enum):
    PRODUCTION  = "production"   # paying customers. strict failsafe.
    STAGING     = "staging"      # pre-prod deployment mirroring. strict failsafe.
    DEVELOPMENT = "development"  # local dev. warn-only failsafe.
    TEST        = "test"         # in-process test harness. permissive.


def resolve_scope() -> Scope:
    """Determine the current execution scope.

    Order of precedence:
      1. MEMORY_SCOPE env var (if a valid Scope value)
      2. memory.toml [scope].name
      3. heuristics-based auto-detection
    """
    # In-process test harness always wins — prevents enforcement from
    # triggering during import-time side effects in pytest runs.
    if "pytest" in sys.modules:
        return Scope.TEST

    env_val = os.environ.get("MEMORY_SCOPE", "").strip().lower()
    if env_val:
        for s in Scope:
            if s.value == env_val:
                return s

    try:
        from infra.config import _read_toml, _TOML_PATH
        toml_data = _read_toml(_TOML_PATH) if _TOML_PATH.exists() else {}
        toml_scope = (toml_data.get("scope") or {}).get("name", "").lower()
        if toml_scope:
            for s in Scope:
                if s.value == toml_scope:
                    return s
    except Exception as e:
        logger.debug("scope: failed to read TOML scope: %s", e)

    # Heuristic: in-process test harness
    if "pytest" in sys.modules:
        return Scope.TEST
    # Heuristic: a configured DB path inside INSTALL_ROOT → production-ish
    try:
        from infra.config import resolve_db_path
        db_path = str(resolve_db_path("memory/memory.db"))
        install_root = os.environ.get("MEMORY_INSTALL_ROOT", "")
        if install_root and install_root in db_path:
            return Scope.PRODUCTION
        return Scope.DEVELOPMENT
    except Exception:
        return Scope.DEVELOPMENT


def is_test_scope() -> bool:
    """True iff current process is in TEST scope."""
    return resolve_scope() == Scope.TEST