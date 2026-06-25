"""
Summarization MCP tools — memory_summarize, memory_auto_summarize, memory_summarization_stats.
"""

import _bootstrap_path  # noqa: E402
import os
import sys
from pathlib import Path


import json
from mcp_common import logger, _err, ErrorCode, with_audit
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_summarize")
def memory_summarize(note_id: str) -> str:
    """Summarize a specific note using extractive TF-IDF summarization.

    Opt-in via MEMORY_SUMMARIZATION=1. Stores summary in metadata.
    """
    import summarization as sm

    if not sm.SUMMARIZATION_ENABLED:
        return json.dumps(
            {"enabled": False, "message": "Set MEMORY_SUMMARIZATION=1 to enable."}
        )
    try:
        summary = sm.summarize_note(note_id)
        if not summary:
            return _err(
                ErrorCode.NOT_FOUND,
                f"Note {note_id} not found or too short to summarize.",
            )
        return json.dumps({"note_id": note_id, "summary": summary}, indent=2)
    except Exception as e:
        logger.exception("Summarization failed")
        return _err(ErrorCode.SUMMARY_ERROR, "Summarization failed")


@mcp.tool()
@with_audit("memory_auto_summarize")
def memory_auto_summarize(min_length: int = 500, dry_run: bool = False) -> str:
    """Auto-summarize all long notes. Opt-in via MEMORY_SUMMARIZATION=1."""
    import summarization as sm

    if not sm.SUMMARIZATION_ENABLED:
        return json.dumps({"enabled": False})
    try:
        result = sm.auto_summarize_long(min_length=min_length, dry_run=dry_run)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("Auto-summarize failed")
        return _err(ErrorCode.SUMMARY_ERROR, "Auto-summarize failed")


@mcp.tool()
@with_audit("memory_summarization_stats")
def memory_summarization_stats() -> str:
    """Return summarization statistics."""
    import summarization as sm

    if not sm.SUMMARIZATION_ENABLED:
        return json.dumps({"enabled": False})
    try:
        return json.dumps(sm.summarization_stats(), indent=2)
    except Exception as e:
        logger.exception("Stats failed")
        return _err(ErrorCode.SUMMARY_ERROR, "Stats failed")
