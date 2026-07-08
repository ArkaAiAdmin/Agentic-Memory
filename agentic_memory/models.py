"""Typed dataclasses for the agentic-memory SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryResult:
    id: str
    content: str
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    category: str = ""
    created_at: str = ""
    pinned: bool = False
    importance: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResults:
    results: list[MemoryResult] = field(default_factory=list)
    total: int = 0
    synthesis: str = ""
    query: str = ""

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)


@dataclass
class Entity:
    id: str
    name: str
    entity_type: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    id: str
    source: str
    target: str
    relation_type: str
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Fact:
    id: str
    subject: str
    predicate: str
    obj: str
    confidence: float = 1.0
    category: str = ""
    source_note_id: str = ""
    event_time: str = ""
    event_time_granularity: str = ""
    valid_at: str = ""
    invalid_at: str = ""
    superseded_by: str = ""
    supersedes: str = ""
    contradiction_score: float = 0.0
    locked: bool = False


@dataclass
class Stats:
    memories: int = 0
    vector_keys: int = 0
    chunks: int = 0
    facts: int = 0
    entities: int = 0
    relations: int = 0


@dataclass
class AgentInfo:
    agent_id: str
    display_name: str = ""
    parent_agent: str = ""
    namespace: str = ""


@dataclass
class IntegrityReport:
    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class MaintenanceResult:
    operation: str
    success: bool = True
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
