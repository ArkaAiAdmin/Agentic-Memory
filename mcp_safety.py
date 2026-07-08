"""
Safety/security MCP tools — scan_injection, strip_provenance, check_contradictions.

G3 fix (2026-06-22): documentation about the auto-scan vs manual-scan
threshold is in the docstring of ``memory_scan_injection`` below.
G1 fix (2026-06-22): documentation about the
``check_contradictions`` vs ``detect_contradictions`` naming is in
``memory_check_contradictions`` below.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401



import json
from mcp_common import (
    _resolve_memory_dir,
    logger,
    _err,
    ErrorCode,
    with_audit,
)
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_scan_injection")
def memory_scan_injection(content: str) -> str:
    """Scan content for prompt-injection patterns. Returns risk assessment.

    Categories: imperative, roleplay, system_prompt, tool_invocation.
    Returns a JSON object with is_suspicious, risk_score, matches, category.

    G3 fix (2026-06-22): the **manual** scan always returns a result;
    it does NOT block the caller.  Compare with the **auto-scan** path
    inside ``save_pipeline.save_memory`` (save_pipeline.py:769-787)
    which **silently rejects** the save when ``risk_score >= 0.5`` and
    returns ``Error [INJECTION_DETECTED]``.  The 0.5 threshold is the
    only number that the system enforces — this tool's output is a
    report, not a gate.  Callers that want the same gate as
    ``save_memory`` should treat ``is_suspicious=True and
    risk_score >= 0.5`` as the rejection boundary.
    """
    try:
        from infra._lazy_imports import scan_for_injection

        result = scan_for_injection(content)
        return json.dumps(result, indent=2)
    except Exception:
        logger.exception("memory_scan_injection failed")
        return _err(ErrorCode.DB_ERROR, "Scan failed")


@mcp.tool()
@with_audit("memory_strip_provenance")
def memory_strip_provenance(content: str) -> str:
    """Strip a leading provenance HTML comment from content."""
    try:
        from memory_injection import strip_provenance

        clean, prov = strip_provenance(content)
        if prov:
            return f"{clean}\n\n[provenance stripped: {json.dumps(prov)}]"
        return clean
    except Exception:
        logger.exception("memory_strip_provenance failed")
        return _err(ErrorCode.DB_ERROR, "Strip failed")


@mcp.tool()
@with_audit("memory_check_contradictions")
def memory_check_contradictions(
    content: str, note_id: str = "preview", top_n: int = 20
) -> str:
    """Scan recent memories for phrase-mode contradictions against content.

    G1 fix (2026-06-22): the two contradiction tools do different
    things on purpose — naming is intentional, not a typo.

    * ``memory_check_contradictions`` (this tool) — pre-save check.
      Used by ``save_memory(safety_wiring=True)`` to refuse a save
      that would contradict existing memories.  Scans the **most
      recent N memories** for phrase-level contradictions against
      the proposed content.  Fast (<10ms), no model load, no DB
      write.

    * ``memory_detect_contradictions`` (mcp_maintenance.py) — corpus
      audit.  Spawns ``contradiction_detector.py`` as a subprocess
      over the entire memory DB.  Slow (seconds-to-minutes for
      large DBs), uses the phrase+semantic detector, writes a
      contradiction report.  Use for periodic whole-DB sweeps.

    The names are kept as-is so existing callers don't break.  If
    you want a pre-save check that ALSO uses semantic similarity,
    call ``memory_scan_injection`` (security) and
    ``memory_check_contradictions`` (phrase) on the same content —
    there is no pre-save semantic contradiction check today.
    """
    try:
        from memory_contradiction_save import check_contradictions_on_save

        try:
            active_dir = _resolve_memory_dir()
        except Exception as e:
            logger.warning("memory_check_contradictions failed: %s", e)
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        findings = check_contradictions_on_save(db_path, content, note_id, top_n=top_n)
        if not findings:
            return "No contradictions found."
        return json.dumps(findings, indent=2)
    except Exception:
        logger.exception("memory_check_contradictions failed")
        return _err(ErrorCode.DB_ERROR, "Contradiction check failed")
