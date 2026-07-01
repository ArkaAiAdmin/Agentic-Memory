#!/usr/bin/env python3
"""Tool-complete + async enqueue logic for auto-save.

Extracted from auto_save.py in Phase 3.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)


def _async_enqueue_or_fallback(
    tool: str, params: str, result_preview: str, ts: Optional[str]
) -> dict:

    from background.auto_save import _now_iso, _slugify, _is_daemon_running, _start_daemon_if_needed, _enqueue_to_inbox, _get_sessions_dir, _tool_complete_inner, _acquire_dedup_lock, _release_dedup_lock, _is_dedup_lock_stale  # noqa: E402
    """Async path: enqueue to the inbox and start the daemon if needed.

    Returns a "queued" envelope on success, or invokes the inline
    fallback (and returns its result) if the daemon can't be
    started or the inbox can't be written.

    The note_id is computed up-front so the caller can log/audit
    it before the actual save happens in the daemon.
    """
    ts = ts or _now_iso()
    ts_compact = ts.replace(":", "-").replace("T", "_").split(".")[0]
    tool_slug = _slugify(tool, max_len=40)
    note_id = f"sessions/auto-{ts_compact}-{tool_slug}"

    # Best-effort: ensure the daemon is alive.  If the spawn fails
    # we fall through to the sync path.
    if not _is_daemon_running():
        _start_daemon_if_needed()

    entry = {
        "ts": ts,
        "tool": tool,
        "params": params,
        "result_preview": result_preview,
    }
    if _enqueue_to_inbox(entry):
        return {
            "saved": "queued",
            "note_id": note_id,
            "path": str(_get_sessions_dir() / f"auto-{ts_compact}-{tool_slug}.md"),
            "timestamp": ts,
        }
    # Fallback: the inbox write failed.  Run the sync path so the
    # caller's data isn't lost.
    try:
        return _tool_complete_inner(tool, params, result_preview, ts)
    except Exception as e:
        return {
            "saved": False,
            "error": f"save failed: {e}",
            "note_id": note_id,
        }

def _upsert_memory(
    note_id: str,
    source_file: str,
    content: str,
    tags_json: list[str] | str | None,
    now_iso: str,
    pinned: int = 0,
    importance: int = 1,
    conn=None,
) -> bool:

    from background.auto_save import get_db_path  # noqa: E402
    """Insert or update a memory note in the active DB via save_pipeline.save_memory.

    Delegates to the canonical save path so the hook path benefits from:
    - Input validation (_validate_save_params)
    - Saga crash consistency (saga_save_memory)
    - Write lock (flock)
    - Post-save hooks (contradiction check, audit, skill extraction, cache invalidation)
    """
    db = get_db_path()
    if not db.exists():
        return False
    try:
        parts = note_id.split("/", 1)
        category = parts[0] if len(parts) == 2 else "sessions"
        title_slug = parts[1] if len(parts) == 2 else note_id
        if tags_json is None:
            tags_list = []
        elif isinstance(tags_json, str):
            try:
                tags_list = json.loads(tags_json)
            except json.JSONDecodeError:
                tags_list = [
                    t.strip() for t in re.split(r"[,; ]+", tags_json) if t.strip()
                ]
        elif isinstance(tags_json, list):
            tags_list = [str(t).strip() for t in tags_json if t]
        else:
            tags_list = []

        from infra._lazy_imports import save_memory as _save_memory

        result = _save_memory(
            content=content,
            category=category,
            title_slug=title_slug,
            tags=tags_list,
            pinned=bool(pinned),
            is_global=False,
            safety_wiring=False,
            _now_iso=now_iso,
            importance=importance,
            _conn=conn,
            note_id=note_id,
        )
        return isinstance(result, str) and not result.startswith("Error")
    except Exception as e:
        # CRITICAL log — this represents data loss (saga raised,
        # memory was not persisted). Unlike the soft failure path
        # (saved=False with no exception), a hard failure MUST be
        # surfaced to the caller so the agent can react.
        logger.critical("DATA LOSS: failed to upsert memory %s: %s", note_id, e)
        raise  # Re-raise so tool_complete() handles retry + error surfacing

def _scan_content_for_injection(
    tool: str, params: str, result_preview: str
) -> dict | None:
    """Run the prompt-injection scan on the tool-derived content
    that auto_save is about to write to disk.

    Returns a rejection dict if the content is high-risk (risk_score
    >= 0.5); returns ``None`` to allow the save to proceed (clean
    content OR scan failure).

    H-fix 2026-06-22: previously auto_save wrote tool content
    directly to disk without the injection scan. The scan was only
    in save_pipeline. This means a tool that bypasses the canonical
    save path (e.g. raw ``write`` tool that an agent could call) could
    persist injection-style content. The scan is pure, deterministic,
    and fast (regex over params + result_preview) so adding it here
    doesn't add a hot-path cost.

    Per the contract in save_memory._scan_for_injection_or_skip:
      * risk_score >= 0.5 → hard reject (never written)
      * risk_score > 0     → allowed but tier=untrusted (set downstream)
      * risk_score == 0    → clean, allow
    """
    from infra._lazy_imports import scan_for_injection

    content_to_scan = " ".join(filter(None, [params, result_preview]))
    if not content_to_scan.strip():
        return None
    try:
        scan = scan_for_injection(content_to_scan)
    except Exception as e:
        logger.debug("auto_save: injection scan failed (benign): %s", e)
        return None

    risk = float(scan.get("risk_score", 0.0))
    is_suspicious = bool(scan.get("is_suspicious", False))
    if risk >= 0.5:
        logger.warning(
            "auto_save: REJECTED injection-suspicious tool content "
            "(tool=%s risk=%.2f matches=%s)",
            tool,
            risk,
            scan.get("matches", []),
        )
        return {
            "saved": False,
            "skipped": True,
            "reason": "high_risk_prompt_injection",
            "tool": tool,
            "risk_score": risk,
            "matches": scan.get("matches", []),
        }
    if is_suspicious:
        logger.info(
            "auto_save: low-risk injection patterns in tool=%s "
            "(risk_score=%.2f) — allowing save with quarantine metadata",
            tool,
            risk,
        )
    return None

def _tool_complete_inner(
    tool: str, params: str, result_preview: str, ts: Optional[str], conn=None
) -> dict:

    from background.auto_save import _resolve_allowlist, _resolve_denylist, _tool_name_matches, _scan_content_for_injection, _now_iso, _slugify, _get_sessions_dir, atomic_write, _resolve_tags, _upsert_memory, _truncate, _should_skip_dedup, _record_dedup, _dedup_key, _auto_save_ttl_hours, _acquire_dedup_lock, _release_dedup_lock, _is_dedup_lock_stale  # noqa: E402
    from background.config import _params_max, _preview_max  # noqa: E402
    """The original tool_complete body, factored out so retry can wrap it.

    Performs the file write + DB upsert. Returns the save result dict,
    or raises on hard failure (which the retry wrapper converts to a
    backoff/circuit-breaker event).
    """
    if not tool:
        raise ValueError("empty tool name")
    allowlist = _resolve_allowlist()
    if allowlist is not None and not _tool_name_matches(tool, allowlist):
        return {
            "saved": False,
            "skipped": True,
            "reason": "tool not in allowlist",
            "tool": tool,
        }
    denylist = _resolve_denylist()
    if _tool_name_matches(tool, denylist):
        return {
            "saved": False,
            "skipped": True,
            "reason": "tool on denylist",
            "tool": tool,
        }
    # H-fix 2026-06-22: scan tool-derived content for prompt
    # injection. Hard-reject high-risk (>=0.5); allow but tag the
    # quarantine metadata for low-risk. Scan failure is non-fatal
    # (we never block a save on a scan error, only on a positive hit).
    injection_check = _scan_content_for_injection(tool, params, result_preview)
    if injection_check is not None:
        return injection_check
    ts = ts or _now_iso()
    ts_compact = ts.replace(":", "-").replace("T", "_").split(".")[0]
    tool_slug = _slugify(tool, max_len=40)
    note_id = f"sessions/auto-{ts_compact}-{tool_slug}"
    target_dir = _get_sessions_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"auto-{ts_compact}-{tool_slug}.md"

    dkey = _dedup_key(tool, params, result_preview)
    if _should_skip_dedup(dkey):
        return {
            "saved": False,
            "skipped": True,
            "reason": "dedup_skip",
            "note_id": note_id,
            "timestamp": ts,
        }
    lock_acquired = _acquire_dedup_lock(dkey)
    if not lock_acquired:
        lock_path = _get_dedup_lock_dir() / f"{dkey}.lock"
        if lock_path.exists() and not _is_dedup_lock_stale(lock_path):
            return {
                "saved": False,
                "skipped": True,
                "reason": "dedup_skip",
                "note_id": note_id,
                "timestamp": ts,
            }
    ttl_hours = _auto_save_ttl_hours()
    if file_path.exists() and ttl_hours > 0:
        age_s = time.time() - file_path.stat().st_mtime
        if age_s < ttl_hours * 3600:
            if lock_acquired:
                _release_dedup_lock(dkey)
            return {
                "saved": False,
                "skipped": True,
                "reason": "ttl_fresh",
                "note_id": note_id,
                "timestamp": ts,
                "age_seconds": int(age_s),
            }

    try:
        if params:
            params_obj = json.loads(params)
            params_str = json.dumps(params_obj, indent=2, ensure_ascii=False)
        else:
            params_obj = None
            params_str = "_no params_"
    except (json.JSONDecodeError, TypeError):
        params_obj = None
        params_str = _truncate(params, _params_max()) if params else "_no params_"

    if len(params_str) > _params_max():
        params_str = params_str[: _params_max()] + "..."

    result_str = _truncate(result_preview or "_no result preview_", _preview_max())

    markdown = f"""---
created: {ts}
updated: {ts}
observed_at: {ts}
tags: [auto-save, hook, tool-log, {tool_slug}]
pinned: false
related: []
valid_from: {ts}
valid_to: null
superseded_by: null
---

# Auto-save: {tool} @ {ts_compact}

**Tool**: `{tool}`
**Timestamp**: {ts}

## Params
```json
{params_str}
```

## Result (preview)
{result_str}

---
*Auto-generated by auto_save.py. Will be rolled into `sessions/{ts[:10]}.md` by the daily digest.*
"""
    _record_dedup(dkey)
    try:
        atomic_write(file_path, markdown, encoding="utf-8")

        tags = _resolve_tags("sessions", None, context="auto-save", tool_slug=tool_slug)
        saved = _upsert_memory(
            note_id,
            f"sessions/{file_path.name}",
            markdown,
            tags,
            ts,
            pinned=0,
            importance=1,
            conn=conn,
        )
    finally:
        if lock_acquired:
            _release_dedup_lock(dkey)
    return {
        "saved": saved,
        "note_id": note_id,
        "path": str(file_path),
        "timestamp": ts,
    }

def tool_complete(
    tool: str, params: str, result_preview: str = "", ts: Optional[str] = None
) -> dict:

    from background.auto_save import _check_circuit_timeout_expiry, _auto_save_circuit_open, _AUTO_SAVE_STATE, _async_autosave_enabled, _fast_path_enqueue, _tool_complete_inner, _auto_save_record_failure_and_maybe_trip, _auto_save_record_success  # noqa: E402
    """Save one tool invocation as a session note, with backoff + circuit
    breaker on failure.

    Returns a dict with 'saved' (bool), 'note_id' (str), 'path' (str), and
    on failure 'error', 'backoff_seconds', and possibly 'circuit_open'.

    Behavior on failure:
      - First failure: return immediately with error + backoff_seconds
        so the next attempt waits
      - After max_retries failures within failure_window_seconds: open
        the circuit breaker; subsequent calls return
        ``{"saved": False, "circuit_open": True}`` for
        circuit_breaker_seconds.

    A "soft" failure (saved=False with no exception) is treated as a
    caller-visible error: the result dict is returned as-is and the
    backoff state is NOT updated. A "hard" failure (raised exception)
    triggers the backoff + circuit breaker.
    """
    # P0-11 fix (2026-06-23): check for circuit timeout expiry before
    # checking the open state. This ensures the close event is
    # persisted when the circuit transitions from open to closed.
    _check_circuit_timeout_expiry()
    if _auto_save_circuit_open():
        return {
            "saved": False,
            "skipped": True,
            "reason": "circuit_breaker_open",
            "circuit_open_until": _AUTO_SAVE_STATE["circuit_open_until"],
        }
    # Async/background-batch path (2026-06-22).  When enabled, the
    # actual save runs in a long-running daemon.  The hook returns
    # immediately with a "queued" envelope, so per-call latency drops
    # from ~100-200ms (Python subprocess + sync work) to ~2-5ms
    # (just the inbox append + daemon liveness check).
    #
    # The fast path handles allowlist/denylist/injection at enqueue
    # time, so the daemon doesn't have to re-filter — the entry
    # already passed the gates.  A failure to enqueue (or to start
    # the daemon) falls through to the inline sync path so no
    # data is lost.
    if _async_autosave_enabled():
        async_envelope = _fast_path_enqueue(tool, params, result_preview, ts)
        if async_envelope is not None:
            return async_envelope
        # Fall through to the sync path on enqueue/daemon failure.
    try:
        result = _tool_complete_inner(tool, params, result_preview, ts)
    except Exception as e:
        cb = _auto_save_record_failure_and_maybe_trip()
        tb = logging.getLogger(__name__).getEffectiveLevel() <= logging.DEBUG and __import__("traceback").format_exc() or str(e)
        logger.warning(
            "auto-save %s failed: %s (failure %d/%d within window, backoff=%.1fs)",
            tool,
            e,
            cb["n_failures"],
            cb["max_retries"] + 1,
            cb["next_backoff"],
        )
        try:
            import os, json as _json, datetime as _dt
            from pathlib import Path
            _am_dir = Path(__file__).resolve().parent.parent
            _err_path = _am_dir / "memory" / "hook-errors.jsonl"
            _err_path.parent.mkdir(parents=True, exist_ok=True)
            _entry = {
                "ts": int(_dt.datetime.now().timestamp() * 1000),
                "label": "auto-save",
                "error": str(e),
                "traceback": tb if "traceback" in dir() else "",
                "failureCount": cb["n_failures"],
                "code": 1,
            }
            with open(_err_path, "a") as _ef:
                _ef.write(_json.dumps(_entry) + "\n")
        except Exception:
            pass
        return {
            "saved": False,
            "error": f"save failed: {e}",
            "backoff_seconds": cb["next_backoff"],
            "n_failures": cb["n_failures"],
            "circuit_open": _auto_save_circuit_open(),
        }
    if result.get("saved"):
        _auto_save_record_success()
    return result

def _fast_path_enqueue(
    tool: str, params: str, result_preview: str, ts: Optional[str]
) -> Optional[dict]:

    from background.auto_save import _resolve_allowlist, _tool_name_matches, _resolve_denylist, _scan_content_for_injection, _async_enqueue_or_fallback  # noqa: E402
    """Apply the gates (allowlist/denylist/injection) and enqueue if they pass.

    Returns the "queued" envelope on success, or ``None`` if any
    step fails (caller falls through to the sync path).  The gate
    checks are intentionally duplicated here so the daemon can be
    a pure writer — it never has to re-validate.
    """
    try:
        if not tool:
            return None
        allowlist = _resolve_allowlist()
        if allowlist is not None and not _tool_name_matches(tool, allowlist):
            return {
                "saved": False,
                "skipped": True,
                "reason": "tool not in allowlist",
                "tool": tool,
            }
        denylist = _resolve_denylist()
        if _tool_name_matches(tool, denylist):
            return {
                "saved": False,
                "skipped": True,
                "reason": "tool on denylist",
                "tool": tool,
            }
        # Run the injection scan on the fast path too — the daemon
        # trusts the entry and does not re-scan.  A high-risk hit
        # must block the save at the hook.
        injection_check = _scan_content_for_injection(tool, params, result_preview)
        if injection_check is not None:
            return injection_check
        return _async_enqueue_or_fallback(tool, params, result_preview, ts)
    except Exception as e:
        # Any unexpected failure on the fast path must not block the
        # save — fall through to the sync path.
        logger.debug("auto-save: fast-path enqueue failed, falling back: %s", e)
        return None
