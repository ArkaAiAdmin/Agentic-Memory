"""Dataclasses for the Session Memory System (schema v22).

No DB logic lives here — these are pure typed containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Session:
    id: str
    started_at: str
    ended_at: Optional[str] = None
    project_root: Optional[str] = None
    agent_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    summary_note_id: Optional[str] = None
    status: str = "active"
    version_vector: str = "{}"
    metadata: dict = field(default_factory=dict)


@dataclass
class DecisionThread:
    id: str
    session_id: str
    title: str
    status: str = "open"
    created_at: str = ""
    resolved_at: Optional[str] = None
    superseded_by: Optional[str] = None
    version_vector: str = "{}"
    metadata: dict = field(default_factory=dict)


@dataclass
class ThreadEvent:
    id: str
    thread_id: str
    session_id: str
    seq: int
    event_type: str
    content: str
    content_summary: str = ""
    memory_id: Optional[str] = None
    confidence: float = 0.5
    created_at: str = ""
    version_vector: str = "{}"


@dataclass
class CompactionLog:
    id: str
    session_id: str
    compacted_at: str
    tokens_before: Optional[int] = None
    tokens_after: Optional[int] = None
    summary_note_id: Optional[str] = None
    recovered_note_ids: str = "[]"
    metadata: dict = field(default_factory=dict)
    version_vector: str = "{}"


@dataclass
class SessionContext:
    """Returned by start_session — carries everything the caller needs."""
    session: Session
    active_threads: list[DecisionThread] = field(default_factory=list)
    recent_events: dict[str, list[ThreadEvent]] = field(default_factory=dict)
