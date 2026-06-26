"""SessionManager — pure orchestration layer for the Session Memory System.

Critical design rule (C-1 fix): SessionManager NEVER writes directly to
the session / thread / compaction tables.  Every persistent write routes
through ``save_memory`` via the internal ``_save_system_record`` helper.
This ensures the saga, FTS update, cache invalidation, and contradiction
check all fire exactly once.

Reads (lookups, queries) use direct SQL — they're read-only and don't
need saga participation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports (avoid circular deps at module load time)
# ---------------------------------------------------------------------------
_session_models_imported = False
_session = None
_thread = None
_event = None
_compaction = None
_context = None


def _import_models():
    global _session_models_imported, _session, _thread, _event, _compaction, _context
    if not _session_models_imported:
        from session_models import (
            CompactionLog,
            DecisionThread,
            Session,
            SessionContext,
            ThreadEvent,
        )

        _session = Session
        _thread = DecisionThread
        _event = ThreadEvent
        _compaction = CompactionLog
        _context = SessionContext
        _session_models_imported = True


# ---------------------------------------------------------------------------
# Feature-flag guard
# ---------------------------------------------------------------------------

def _is_enabled() -> bool:
    try:
        from config import get_config
        return bool(get_config().session_memory)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# PII scrub helper (Sprint 2, C-8 fix)
# ---------------------------------------------------------------------------

_PII_KEY_RE = re.compile(
    r"^(password|token|secret|api_key|apikey|auth|credential|"
    r"private_key|access_key|refresh_token)$",
    re.IGNORECASE,
)


def _scrub_metadata(d: Optional[dict]) -> dict:
    if not d:
        return {}
    out: dict[str, Any] = {}
    for k, v in d.items():
        if _PII_KEY_RE.match(k):
            continue
        if isinstance(v, dict):
            out[k] = _scrub_metadata(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Internal write-through helper (the ONLY DB write path from SessionManager)
# ---------------------------------------------------------------------------

def _save_system_record(
    table: str,
    row: dict[str, Any],
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Persist a system table row via ``save_memory``.

    Constructs a structured JSON content payload and routes it through the
    canonical save pipeline.  The resulting ``note_id`` (if the open-code
    save_memory is active) mirrors the row; if the feature flag is off or
    save_memory is unavailable, returns ``None`` (silent no-op — the plan
    mandates feature-flag gating).

    Args:
        table: One of ``"sessions"``, ``"decision_threads"``,
            ``"thread_events"``, ``"session_compaction_log"``.
        row: Column values for the row.  Must include all NOT NULL columns
            except auto-generated ones (id, created_at).
        db_path: Override DB path.  Defaults to the active memory DB.

    Returns:
        The ``note_id`` (``"system/<slug>"``) on success, ``None`` if
        session memory is disabled or save_memory is unavailable.
    """
    if not _is_enabled():
        return None
    row_id = row.get("id") or f"{table[:4]}_{uuid.uuid4().hex[:12]}"
    payload = {
        "_table": table,
        "_row_id": row_id,
        **{k: v for k, v in row.items() if k != "id"},
    }
    try:
        from save_pipeline import save_memory
    except ImportError:
        return None
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        slug = row_id.replace("/", "_").replace(" ", "_")[:64]
        note_id = save_memory(
            content=json.dumps(payload, default=str),
            category="system",
            title_slug=f"{table[:4]}_{slug}",
            tags=["session", "internal", table],
            pinned=False,
            db_path=str(db_path) if db_path else None,
            _now_iso=now_iso,
            context="generic",
        )
        if isinstance(note_id, str) and not note_id.startswith("Error"):
            return note_id
        logger.warning("_save_system_record: save_memory returned error for %s: %s", table, note_id)
        return None
    except Exception as exc:
        logger.warning("_save_system_record: save_memory raised for %s: %s", table, exc)
        return None


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Orchestration layer for sessions, decision threads, and compaction.

    All persistent writes route through ``_save_system_record`` → ``save_memory``.
    Reads from the v22 tables use direct SQL — no save_memory call on
    the read path.

    Usage::

        mgr = SessionManager(db_path=Path("memory/memory.db"))
        ctx = mgr.start_session(project_root, agent_id)
        mgr.record_event(ctx.session.id, thread_id, "decision", "chose option A")
        mgr.end_session(ctx.session.id, summary="...")
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path
        self._seq_lock: dict[str, threading.Lock] = {}
        self._seq_lock_global = threading.Lock()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _conn(self):
        """Acquire a pooled connection for read/write operations."""
        from db import connection_pool
        path = str(self._db_path) if self._db_path else str(Path.cwd() / "memory" / "memory.db")
        return connection_pool.get(path, timeout=30.0)

    def _pool_path(self) -> str:
        if self._db_path:
            return str(self._db_path)
        return str(Path.cwd() / "memory" / "memory.db")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _seq_lock_for(self, thread_id: str) -> threading.Lock:
        with self._seq_lock_global:
            if thread_id not in self._seq_lock:
                self._seq_lock[thread_id] = threading.Lock()
            return self._seq_lock[thread_id]

    # ------------------------------------------------------------------
    # start_session (Sprint 2, Task 2.3)
    # ------------------------------------------------------------------

    def start_session(
        self,
        project_root: str,
        agent_id: str = "",
        parent_session_id: Optional[str] = None,
    ) -> Optional["SessionContext"]:
        """Start a new session or resume an active one (crash recovery).

        1. Look for an existing ``active`` session in the same project.
        2. If found, return it (resume path).
        3. Otherwise insert a new session row (via ``_save_system_record``).
        4. Load any open decision threads for the project.
        5. Return ``SessionContext``.

        Returns:
            SessionContext on success, None if session memory is disabled.
        """
        if not _is_enabled():
            return None
        _import_models()

        db_path_str = self._pool_path()
        # --- Crash recovery: look for active session in this project ---
        existing: Optional["_session"] = None
        try:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT id, started_at, ended_at, project_root, agent_id, "
                    "parent_session_id, summary_note_id, status, version_vector, metadata "
                    "FROM sessions WHERE status='active' AND project_root=? ORDER BY started_at DESC LIMIT 1",
                    (project_root,),
                ).fetchone()
                if row:
                    existing = _session(
                        id=row[0], started_at=row[1], ended_at=row[2],
                        project_root=row[3], agent_id=row[4],
                        parent_session_id=row[5], summary_note_id=row[6],
                        status=row[7], version_vector=row[8] or "{}",
                        metadata=json.loads(row[9]) if row[9] else {},
                    )
            finally:
                from db import safe_close_db
                safe_close_db(conn)
        except Exception as exc:
            logger.warning("start_session: crash-recovery query failed: %s", exc)

        if existing:
            logger.info("start_session: resuming active session %s", existing.id)
            threads = self._load_open_threads(existing.id)
            recent = self._load_recent_events(existing.id)
            return _context(session=existing, active_threads=threads, recent_events=recent)

        # --- Create new session ---
        sess_id = f"sess_{uuid.uuid4().hex[:12]}"
        now = self._now()
        new_session = _session(
            id=sess_id, started_at=now, project_root=project_root,
            agent_id=agent_id, parent_session_id=parent_session_id,
            status="active", version_vector="{}",
        )
        _save_system_record(
            "sessions",
            {
                "id": sess_id,
                "started_at": now,
                "project_root": project_root,
                "agent_id": agent_id,
                "parent_session_id": parent_session_id,
                "status": "active",
                "version_vector": "{}",
                "metadata": _scrub_metadata({}),
            },
            db_path=Path(self._db_path) if self._db_path else None,
        )
        logger.info("start_session: created session %s for %s", sess_id, project_root)
        return _context(session=new_session)

    # ------------------------------------------------------------------
    # record_event (Sprint 2, Task 2.4)
    # ------------------------------------------------------------------

    def record_event(
        self,
        session_id: str,
        thread_id: str,
        event_type: str,
        content: str,
        memory_id: Optional[str] = None,
        confidence: float = 0.5,
    ) -> Optional["ThreadEvent"]:
        """Append an event to a decision thread.

        Args:
            session_id: Parent session ID.
            thread_id: Decision thread ID.
            event_type: One of ``claim``, ``evidence``, ``decision``,
                ``question``, ``pivot``.
            content: Full event text.
            memory_id: Optional FK to a saved memory note.
            confidence: 0.0–1.0 confidence score.

        Returns:
            ThreadEvent on success, None if disabled or error.
        """
        if not _is_enabled():
            return None
        _import_models()
        if event_type not in ("claim", "evidence", "decision", "question", "pivot"):
            logger.error("record_event: invalid event_type %r", event_type)
            return None

        summary = content[:300] if len(content) <= 300 else content[:297] + "..."
        lock = self._seq_lock_for(thread_id)
        with lock:
            seq = 1
            try:
                conn = self._conn()
                try:
                    max_seq = conn.execute(
                        "SELECT MAX(seq) FROM thread_events WHERE thread_id=?",
                        (thread_id,),
                    ).fetchone()[0]
                    seq = (max_seq or 0) + 1
                finally:
                    from db import safe_close_db
                    safe_close_db(conn)
            except Exception as exc:
                logger.warning("record_event: seq lookup failed: %s", exc)

        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now = self._now()
        event = _event(
            id=event_id, thread_id=thread_id, session_id=session_id,
            seq=seq, event_type=event_type, content=content,
            content_summary=summary, memory_id=memory_id,
            confidence=confidence, created_at=now, version_vector="{}",
        )
        _save_system_record(
            "thread_events",
            {
                "id": event_id,
                "thread_id": thread_id,
                "session_id": session_id,
                "seq": seq,
                "event_type": event_type,
                "content": content,
                "content_summary": summary,
                "memory_id": memory_id,
                "confidence": confidence,
                "created_at": now,
                "version_vector": "{}",
            },
            db_path=Path(self._db_path) if self._db_path else None,
        )
        return event

    # ------------------------------------------------------------------
    # resolve_thread (Sprint 2, Task 2.5)
    # ------------------------------------------------------------------

    def resolve_thread(
        self,
        thread_id: str,
        resolution: str = "",
        superseded_by: Optional[str] = None,
    ) -> bool:
        """Mark a decision thread as resolved or superseded.

        Updates the thread status via ``_save_system_record`` (Rule #1).
        """
        if not _is_enabled():
            return False
        _import_models()
        now = self._now()
        new_status = "superseded" if superseded_by else "resolved"
        _save_system_record(
            "decision_threads",
            {
                "id": thread_id,
                "status": new_status,
                "resolved_at": now,
                "superseded_by": superseded_by,
                "version_vector": "{}",
                "metadata": _scrub_metadata(
                    {"resolution": resolution} if resolution else {}
                ),
            },
            db_path=Path(self._db_path) if self._db_path else None,
        )
        return True

    # ------------------------------------------------------------------
    # end_session (Sprint 2, Task 2.6)
    # ------------------------------------------------------------------

    def end_session(self, session_id: str, summary: str = "") -> bool:
        """Close a session, save summary as a pinned memory, defer open threads.

        Summary is saved via ``save_memory(category="sessions", pinned=True)``
        so it appears in recall results.  Thread status updated via
        ``_save_system_record``.
        """
        if not _is_enabled():
            return False
        _import_models()
        now = self._now()

        # Save summary as a live memory note (via canonical save path).
        summary_note_id: Optional[str] = None
        try:
            from save_pipeline import save_memory
            note_id = save_memory(
                content=summary or f"Session {session_id} ended.",
                category="sessions",
                title_slug=f"session_{session_id[-8:]}",
                tags=["session", "summary"],
                pinned=True,
                db_path=str(self._db_path) if self._db_path else None,
                _now_iso=now,
                context="generic",
            )
            if isinstance(note_id, str) and not note_id.startswith("Error"):
                summary_note_id = note_id
        except Exception as exc:
            logger.warning("end_session: summary save failed: %s", exc)

        # Update session row via system record path.
        _save_system_record(
            "sessions",
            {
                "id": session_id,
                "ended_at": now,
                "status": "ended",
                "summary_note_id": summary_note_id,
                "version_vector": "{}",
            },
            db_path=Path(self._db_path) if self._db_path else None,
        )

        # Defer all open threads for this session.
        try:
            conn = self._conn()
            try:
                open_threads = conn.execute(
                    "SELECT id FROM decision_threads WHERE session_id=? AND status='open'",
                    (session_id,),
                ).fetchall()
            finally:
                from db import safe_close_db
                safe_close_db(conn)
            for (tid,) in open_threads:
                _save_system_record(
                    "decision_threads",
                    {"id": tid, "status": "deferred", "version_vector": "{}"},
                    db_path=Path(self._db_path) if self._db_path else None,
                )
        except Exception as exc:
            logger.warning("end_session: thread deferral failed: %s", exc)

        return True

    # ------------------------------------------------------------------
    # compact_session (Sprint 2, Task 2.7)
    # ------------------------------------------------------------------

    def compact_session(
        self,
        session_id: str,
        tokens_before: Optional[int] = None,
        tokens_after: Optional[int] = None,
        summary_note_id: Optional[str] = None,
        recovered_note_ids: Optional[list[str]] = None,
    ) -> bool:
        """Log a compaction event and update the session status.

        Args:
            session_id: Session being compacted.
            tokens_before: Estimated token count pre-compaction.
            tokens_after: Estimated token count post-compaction.
            summary_note_id: The pinned note that holds the compacted summary.
            recovered_note_ids: Auto-save note IDs recovered during compaction.
        """
        if not _is_enabled():
            return False
        _import_models()
        comp_id = f"comp_{uuid.uuid4().hex[:12]}"
        now = self._now()
        _save_system_record(
            "session_compaction_log",
            {
                "id": comp_id,
                "session_id": session_id,
                "compacted_at": now,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "summary_note_id": summary_note_id,
                "recovered_note_ids": json.dumps(recovered_note_ids or []),
                "metadata": _scrub_metadata({}),
                "version_vector": "{}",
            },
            db_path=Path(self._db_path) if self._db_path else None,
        )
        _save_system_record(
            "sessions",
            {"id": session_id, "status": "compacted", "version_vector": "{}"},
            db_path=Path(self._db_path) if self._db_path else None,
        )
        return True

    # ------------------------------------------------------------------
    # Read helpers (Sprint 2)
    # ------------------------------------------------------------------

    def _load_open_threads(self, session_id: str) -> list["DecisionThread"]:
        if not _is_enabled():
            return []
        _import_models()
        threads: list[_thread] = []
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT id, session_id, title, status, created_at, resolved_at, "
                    "superseded_by, version_vector, metadata "
                    "FROM decision_threads WHERE session_id=? AND status='open'",
                    (session_id,),
                ).fetchall()
            finally:
                from db import safe_close_db
                safe_close_db(conn)
            for r in rows:
                threads.append(
                    _thread(
                        id=r[0], session_id=r[1], title=r[2], status=r[3],
                        created_at=r[4], resolved_at=r[5], superseded_by=r[6],
                        version_vector=r[7] or "{}",
                        metadata=json.loads(r[8]) if r[8] else {},
                    )
                )
        except Exception as exc:
            logger.warning("_load_open_threads: %s", exc)
        return threads

    def _load_recent_events(self, session_id: str, limit: int = 10) -> dict[str, list["ThreadEvent"]]:
        if not _is_enabled():
            return {}
        _import_models()
        result: dict[str, list["_event"]] = {}
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT te.id, te.thread_id, te.session_id, te.seq, te.event_type, "
                    "te.content, te.content_summary, te.memory_id, te.confidence, "
                    "te.created_at, te.version_vector "
                    "FROM thread_events te "
                    "JOIN decision_threads dt ON te.thread_id = dt.id "
                    "WHERE dt.session_id=? "
                    "ORDER BY te.thread_id, te.seq DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            finally:
                from db import safe_close_db
                safe_close_db(conn)
            for r in rows:
                ev = _event(
                    id=r[0], thread_id=r[1], session_id=r[2], seq=r[3],
                    event_type=r[4], content=r[5], content_summary=r[6],
                    memory_id=r[7], confidence=r[8], created_at=r[9],
                    version_vector=r[10] or "{}",
                )
                result.setdefault(ev.thread_id, []).append(ev)
        except Exception as exc:
            logger.warning("_load_recent_events: %s", exc)
        return result
