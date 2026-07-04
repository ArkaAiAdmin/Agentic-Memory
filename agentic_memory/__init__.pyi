"""Type stubs for the agentic_memory package.

Provides rich type information for IDE autocomplete and static type
checking without requiring the runtime to be installed.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any, Iterator, Optional

__version__: str

# ── Core SDK ────────────────────────────────────────────────────────────────

class MemoryClient:
    """Core SDK client for the agentic-memory system."""

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        user_id: str = "default",
    ) -> None: ...
    def save(
        self,
        content: str,
        category: str = "sdk",
        tags: Optional[list[str]] = None,
        pinned: bool = False,
        is_global: bool = False,
        importance: int = 3,
        title_slug: str = "",
    ) -> str: ...
    def search(
        self,
        query: str,
        limit: int = 5,
        rerank: bool = True,
        boost_pinned: bool = True,
        recency_weight: float = 0.1,
        include_global: bool = True,
        include_facts: bool = True,
        fact_limit: int = 5,
        synthesize: bool = False,
        max_synthesis_sentences: int = 5,
    ) -> SearchResults: ...
    def delete(self, note_id: str, hard: bool = False) -> bool: ...
    def restore(self, note_id: str) -> bool: ...
    def get(self, note_id: str) -> Optional[MemoryResult]: ...
    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        category: str = "",
    ) -> list[MemoryResult]: ...
    def clear(self) -> int: ...
    def stats(self) -> Stats: ...
    def scan_injection(self, content: str) -> dict[str, Any]: ...
    def check_contradictions(
        self, content: str, top_n: int = 20
    ) -> builtins.list[dict[str, Any]]: ...
    def get_user_profile(self) -> dict[str, Any]: ...
    def record_access(
        self, note_id: str, source: str = "search", category: str = "", tags: str = ""
    ) -> None: ...
    def check_integrity(self, deep: bool = False) -> IntegrityReport: ...
    def audit(self) -> dict[str, Any]: ...
    def search_facts(self, query: str, limit: int = 10) -> builtins.list[Fact]: ...
    def list_facts(self, limit: int = 50, offset: int = 0) -> builtins.list[Fact]: ...
    def rebuild(self, scope: str = "active") -> str: ...
    def quality_filter(self, query: str, limit: int = 50) -> builtins.list[dict[str, Any]]: ...
    def quality_stats(self) -> dict[str, Any]: ...
    def summarize(self, note_id: str) -> str: ...
    def adaptive_retention(self, dry_run: bool = False) -> str: ...
    @property
    def kg(self) -> KnowledgeGraph: ...
    @property
    def temporal(self) -> TemporalKG: ...
    @property
    def admin(self) -> Admin: ...

class Memory:
    """Mem0-compatible memory store (legacy backward-compat)."""

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        user_id: str = "default",
        config: Optional[dict[str, Any]] = None,
    ) -> None: ...
    def add(self, content: str, tags: Optional[list[str]] = None) -> str: ...
    def search(
        self, query: str, limit: int = 10, rerank: bool = True
    ) -> list[dict[str, Any]]: ...
    def delete(self, note_id: str) -> bool: ...
    def list(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]: ...
    def clear(self) -> int: ...
    def stats(self) -> dict[str, int]: ...

# ── Domain SDK classes ──────────────────────────────────────────────────────

class KnowledgeGraph:
    """Knowledge Graph operations."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None: ...
    def search(
        self, query: str, limit: int = 10, max_hops: int = 2
    ) -> list[Entity]: ...
    def search_facts(self, query: str, limit: int = 10) -> list[Fact]: ...
    def shortest_path(
        self, source: str, target: str, max_hops: int = 5
    ) -> list[dict[str, Any]]: ...
    def traverse(
        self, start: str, max_hops: int = 3
    ) -> tuple[list[Entity], list[Relation]]: ...
    def stats(self) -> dict[str, Any]: ...
    def list_facts(self, limit: int = 50, offset: int = 0) -> list[Fact]: ...

class TemporalKG:
    """Temporal Knowledge Graph operations."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None: ...
    def search(self, query: str, limit: int = 10) -> list[Fact]: ...
    def contradictions(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        reason: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    def query_facts_at_time(
        self, timestamp: float, query: Optional[str] = None, limit: int = 50
    ) -> list[Fact]: ...
    def query_changed_since(self, timestamp: float, limit: int = 100) -> list[Fact]: ...
    def query_supersession_chain(self, fact_id: int) -> list[Fact]: ...
    def invalidate_fact(self, fact_id: int, reason: str = "manual") -> bool: ...

class Maintenance:
    """High-level maintenance operations."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None: ...
    def rebuild(self, scope: str = "active") -> MaintenanceResult: ...
    def compact(self, dry_run: bool = False) -> MaintenanceResult: ...
    def check_integrity(self, deep: bool = False) -> IntegrityReport: ...
    def audit(self) -> dict: ...
    def heartbeat(self) -> dict: ...
    def tier_stats(self) -> dict: ...
    def run_tier_migration(self) -> str: ...
    def consolidate(self) -> MaintenanceResult: ...
    def rewrite_links(self) -> MaintenanceResult: ...
    def detect_contradictions(
        self,
        min_confidence: str = "low",
        mode: str = "both",
        semantic_threshold: float = 0.65,
    ) -> list[dict]: ...
    def run(self, operation: str, **kwargs: Any) -> str: ...

class Admin:
    """System administration operations — health, circuit breaker."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None: ...
    def health(self, db_path: Optional[str | Path] = None) -> dict[str, Any]: ...
    def circuit_breaker_status(
        self, limit: int = 20, since_ts: Optional[float] = None
    ) -> dict[str, Any]: ...

class AgentMemory:
    """Agent-scoped memory with namespace isolation."""

    def __init__(
        self,
        agent_id: str,
        display_name: str = "",
        parent_agent: Optional[str] = None,
        db_path: Optional[str | Path] = None,
    ) -> None: ...
    def save(
        self,
        content: str,
        category: str = "agents",
        tags: Optional[list[str]] = None,
        pinned: bool = False,
        importance: int = 3,
    ) -> str: ...
    def search(self, query: str, limit: int = 10) -> SearchResults: ...
    def list(self, limit: int = 50) -> list[MemoryResult]: ...
    def clear(self) -> int: ...
    @property
    def info(self) -> AgentInfo: ...
    @property
    def client(self) -> MemoryClient: ...
    @staticmethod
    def list_agents() -> builtins.list[AgentInfo]: ...
    @staticmethod
    def reset(agent_id: str) -> bool: ...

class SyncManager:
    """CRDT sync and sharing operations."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None: ...
    def sync(
        self,
        peer_url: str,
        peer_name: str = "",
        peer_agent_id: str = "",
        limit: int = 200,
    ) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...
    def share(self, note_id: str, agent_id: str) -> bool: ...
    def list_shared(
        self, agent_id: str = "", category: str = "", limit: int = 50
    ) -> list[dict[str, Any]]: ...
    def import_shared(self, shared_id: str, target_agent_id: str) -> bool: ...
    def auto_share(
        self, agent_id: str = "", min_importance: int = 0, dry_run: bool = False
    ) -> dict[str, Any]: ...

# ── Models ──────────────────────────────────────────────────────────────────

class MemoryResult:
    id: str
    content: str
    score: float
    tags: list[str]
    category: str
    created_at: str
    pinned: bool
    importance: int
    metadata: dict[str, Any]

    def __init__(
        self,
        id: str = "",
        content: str = "",
        score: float = 0.0,
        tags: Optional[list[str]] = None,
        category: str = "",
        created_at: str = "",
        pinned: bool = False,
        importance: int = 3,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

class SearchResults:
    results: list[MemoryResult]
    total: int
    synthesis: str
    query: str

    def __init__(
        self,
        results: Optional[list[MemoryResult]] = None,
        total: int = 0,
        synthesis: str = "",
        query: str = "",
    ) -> None: ...
    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[MemoryResult]: ...

class Stats:
    memories: int
    vector_keys: int
    chunks: int
    facts: int
    entities: int
    relations: int

    def __init__(
        self,
        memories: int = 0,
        vector_keys: int = 0,
        chunks: int = 0,
        facts: int = 0,
        entities: int = 0,
        relations: int = 0,
    ) -> None: ...

class Entity:
    name: str
    type: str
    observations: list[str]

    def __init__(
        self, name: str = "", type: str = "", observations: Optional[list[str]] = None
    ) -> None: ...

class Relation:
    source: str
    target: str
    type: str

    def __init__(self, source: str = "", target: str = "", type: str = "") -> None: ...

class Fact:
    id: int
    subject: str
    predicate: str
    obj: str
    confidence: float
    category: str
    source_note_id: str
    event_time: str
    event_time_granularity: str
    valid_at: str
    invalid_at: str
    superseded_by: str
    supersedes: str
    contradiction_score: float
    locked: bool

    def __init__(
        self,
        id: int = 0,
        subject: str = "",
        predicate: str = "",
        obj: str = "",
        confidence: float = 1.0,
        category: str = "",
        source_note_id: str = "",
        event_time: str = "",
        event_time_granularity: str = "",
        valid_at: str = "",
        invalid_at: str = "",
        superseded_by: str = "",
        supersedes: str = "",
        contradiction_score: float = 0.0,
        locked: bool = False,
    ) -> None: ...

class IntegrityReport:
    passed: bool
    errors: list[str]
    warnings: list[str]
    stats: dict[str, Any]

    def __init__(
        self,
        passed: bool = True,
        errors: Optional[list[str]] = None,
        warnings: Optional[list[str]] = None,
        stats: Optional[dict[str, Any]] = None,
    ) -> None: ...

class MaintenanceResult:
    operation: str
    success: bool
    message: str
    details: Optional[dict[str, Any]]

    def __init__(
        self,
        operation: str = "",
        success: bool = True,
        message: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> None: ...

class AgentInfo:
    agent_id: str
    display_name: str
    memory_count: int
    parent_agent: Optional[str]

    def __init__(
        self,
        agent_id: str = "",
        display_name: str = "",
        memory_count: int = 0,
        parent_agent: Optional[str] = None,
    ) -> None: ...

# ── Exceptions ──────────────────────────────────────────────────────────────

class AgenticMemoryError(Exception): ...
class ConnectionError(AgenticMemoryError): ...
class NotFoundError(AgenticMemoryError): ...
class ValidationError(AgenticMemoryError): ...
class IntegrityError(AgenticMemoryError): ...
class MaintenanceError(AgenticMemoryError): ...
class SyncError(AgenticMemoryError): ...
class PermissionError(AgenticMemoryError): ...
class CircuitBreakerOpen(AgenticMemoryError): ...
class ConfigError(AgenticMemoryError): ...

# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for the agentic-memory package.

    Subcommands:
        - ``add <text> [tags...]``
        - ``search <query> [--limit N]``
        - ``list [--limit N]``
        - ``stats``
        - ``clear``
        - ``demo [--query Q]``
        - ``kg search/facts/path/traverse/stats/list-facts``
        - ``temporal search/contradictions/at-time/changed-since/chain/invalidate``
        - ``maintenance rebuild/compact/check/audit/heartbeat/...``
        - ``admin health/circuit-breaker``
        - ``agent list/info/save/search/list-memories/clear``
        - ``sync status/share/list-shared/import/auto-share``

    Returns:
        The process exit code (0 on success).
    """
    ...
