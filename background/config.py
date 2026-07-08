#!/usr/bin/env python3
"""Configuration helpers and tool-policy constants for auto-save.

Extracted from background/auto_save.py in Phase 4.

Owns:
- Default config constants
- Allowlist / denylist resolution
- _batch_interval, _daemon_idle_seconds, _preview_max, _params_max, _health_check_minutes
- _tool_name_matches helper
"""
from __future__ import annotations

import logging

import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default config values (duplicated here to keep config.py standalone)
# ---------------------------------------------------------------------------
_DEFAULT_BATCH_INTERVAL_S = 0.5
_DEFAULT_BATCH_SIZE = 50
_DEFAULT_DAEMON_IDLE_S = 300
_DEFAULT_PREVIEW_MAX = 200
_DEFAULT_PARAMS_MAX = 2000
_DEFAULT_HEALTH_CHECK_MINUTES = 5
_DEFAULT_ASYNC_AUTOSAVE = True

# ---------------------------------------------------------------------------
# Tool-policy constants (duplicated here to keep config.py standalone)
# ---------------------------------------------------------------------------
DEFAULT_TOOL_ALLOWLIST: frozenset = frozenset(
    {
        "memory_save",
        "memory_supersede",
        "memory_delete",
        "memory_reinforce",
        "todowrite",
        "task",
        "question",
        "read",
        "filesystem_read_file",
        "filesystem_read_text_file",
        "filesystem_read_multiple_files",
        "write",
        "edit",
        "glob",
        "grep",
        "search_files",
        "filesystem_search_files",
        "bash",
        "run_command",
        "memory_search",
        "memory_read_graph",
        "memory_graph_search",
        "memory_graph_stats",
        "memory_graph_shortest_path",
        "memory_graph_traverse",
        "memory_temporal_query",
        "memory_temporal_contradictions",
        "memory_create_entities",
        "memory_add_observations",
        "memory_search_nodes",
        "memory_open_nodes",
        "memory_create_relations",
        "memory_delete_relations",
    }
)

DEFAULT_TOOL_DENYLIST: frozenset = frozenset(
    {
        "filesystem_list_allowed_directories",
        "filesystem_list_directory",
        "filesystem_directory_tree",
        "filesystem_read_multiple_files",
        "filesystem_search_files",
        "filesystem_get_file_info",
        "filesystem_list_directory_with_sizes",
        "memory_session_start",
        "memory_user_profile",
        "memory_recall_context",
        "memory_profile_access",
        "memory_record_ctr_feedback",
        "memory_check_concept_drift",
        "todo",
        "process",
        "read_terminal",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_name_matches(name: str, candidates: frozenset) -> bool:
    """Check if ``name`` matches any entry in ``candidates``.

    MCP tools are namespaced as ``<server>_<tool>`` (e.g.
    ``agentic-memory_memory_save``) while the allowlist/denylist uses
    bare tool names. This helper matches both forms."""
    if name in candidates:
        return True
    if "_" in name:
        unprefixed = name.split("_", 1)[1]
        if unprefixed in candidates:
            return True
    return False


def _resolve_denylist() -> frozenset:
    """Return the active tool denylist, honoring env/TOML override."""
    override = os.environ.get("AUTO_SAVE_TOOL_DENYLIST", "").strip()
    if override == "":
        if "AUTO_SAVE_TOOL_DENYLIST" in os.environ:
            return frozenset()
        try:
            from infra._lazy_imports import get_config
            cfg = get_config()
            toml_denylist = getattr(cfg, "auto_save_denylist", "")
            if toml_denylist:
                return frozenset(t.strip() for t in toml_denylist.split(",") if t.strip())
        except Exception as e:
            logger.warning("_resolve_denylist failed: %s", e)
        return DEFAULT_TOOL_DENYLIST
    if override.lower() in {"0", "false", "off", "disable", "disabled"}:
        return frozenset()
    return frozenset(t.strip() for t in override.split(",") if t.strip())


def _resolve_allowlist() -> frozenset | None:
    """Return the active tool allow-list, or None if all tools are allowed.

    Priority: env var > TOML config > default.
    Set to ``"*"`` to allow all tools (fall back to denylist-only)."""
    override = os.environ.get("AUTO_SAVE_TOOL_ALLOWLIST", "").strip()
    if override == "*":
        return None
    if override:
        return frozenset(t.strip() for t in override.split(",") if t.strip())
    try:
        from infra._lazy_imports import get_config
        cfg = get_config()
        toml_allowlist = getattr(cfg, "auto_save_allowlist", "")
        if toml_allowlist:
            return frozenset(t.strip() for t in toml_allowlist.split(",") if t.strip())
    except Exception as e:
        logger.warning("_resolve_allowlist failed: %s", e)
    return DEFAULT_TOOL_ALLOWLIST


def _batch_interval() -> float:
    from infra._lazy_imports import get_config
    return getattr(get_config(), "auto_save_batch_interval_seconds", _DEFAULT_BATCH_INTERVAL_S)


def _daemon_idle_seconds() -> int:
    from infra._lazy_imports import get_config
    return getattr(get_config(), "auto_save_daemon_idle_seconds", _DEFAULT_DAEMON_IDLE_S)


def _preview_max() -> int:
    from infra._lazy_imports import get_config
    return getattr(get_config(), "auto_save_preview_max", _DEFAULT_PREVIEW_MAX)


def _params_max() -> int:
    from infra._lazy_imports import get_config
    return getattr(get_config(), "auto_save_params_max", _DEFAULT_PARAMS_MAX)


def _health_check_minutes() -> int:
    from infra._lazy_imports import get_config
    return getattr(
        get_config(), "auto_save_health_check_minutes", _DEFAULT_HEALTH_CHECK_MINUTES
    )
