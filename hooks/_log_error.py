#!/usr/bin/env python3
"""Shared error logger for lifecycle hooks.

Hooks must never block agent operation (AGENTS.md hard rule #8), so they
catch all exceptions. But "never block" doesn't mean "never tell anyone
when I fail" — silent failures are invisible.

This helper writes a single-line error to ``memory/hooks.log`` (a known
location that can be tailed or monitored). STDOUT contract is preserved
so the agent's context is not polluted.

Usage:
    from _log_error import log_error

    try:
        do_something()
    except Exception as e:
        log_error(e, context="memory-proactive-context.main()")
"""

import logging
logger = logging.getLogger(__name__)

import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from infra.memory_config import GLOBAL_MEM_DIR

# Log file lives next to memory.db so it ships with the memory directory.
_HOOK_LOG = Path(os.environ.get("MEMORY_HOOK_LOG") or GLOBAL_MEM_DIR / "hooks.log")


def log_error(exc: BaseException, context: str = "") -> None:
    """Append a single-line error to the hook log. Never raises.

    Best-effort: any failure inside this function is itself swallowed
    so the caller's "never block" guarantee is preserved.
    """
    try:
        _HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        ctx = f" [{context}]" if context else ""
        line = f"{ts}{ctx} {type(exc).__name__}: {exc}\n"
        with _HOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
            # Append a one-line traceback hint (first frame only) for triage
            tb = traceback.extract_tb(exc.__traceback__)
            if tb:
                last = tb[-1]
                f.write(f"  at {last.filename}:{last.lineno} in {last.name}\n")
    except Exception as e:
        # We are the last line of defense — do not raise
        logger.warning("log_error failed: %s", e)
