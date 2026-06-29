"""MemoryClient — core SDK class wrapping the agentic-memory system."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agentic_memory.exceptions import (
    ValidationError,
)
from agentic_memory.models import (
    MemoryResult,
    SearchResults,
    Stats,
    IntegrityReport,
    Fact,
)
from agentic_memory.utils import (
    resolve_db_path,
    get_db_connection,
    safe_close_db,
    parse_search_results,
)


class MemoryClient:
    """Core SDK client for the agentic-memory system.

    Wraps the full 79-tool MCP surface as a typed Python API. Every
    operation goes through the same save/search pipeline used by the
    MCP server.

    Examples::

        mc = MemoryClient()
        note_id = mc.save("User prefers dark mode")
        results = mc.search("What does the user prefer?")
        mc.delete(note_id)
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        user_id: str = "default",
    ) -> None:
        self._db_path = resolve_db_path(db_path)
        self._user_id = user_id
        self._kg: object = None
        self._temporal: object = None
        self._admin: object = None

    # ── CRUD ──────────────────────────────────────────────────────────

    def save(
        self,
        content: str,
        category: str = "sdk",
        tags: list[str] | None = None,
        pinned: bool = False,
        is_global: bool = False,
        importance: int = 3,
        title_slug: str = "",
    ) -> str:
        """Save a memory and return its note ID.

        Args:
            content: Text content to store.
            category: Subdirectory under ``memory/`` (lessons, projects,
                decisions, preferences, sessions, sdk).
            tags: Optional list of tag strings.
            pinned: If True, the note is boosted in recall.
            is_global: If True, stores at the global config level.
            importance: 1-5 ranking weight (default 3).
            title_slug: Optional explicit slug; auto-generated if empty.
        """
        if not content or not content.strip():
            raise ValidationError("Content must be non-empty")
        if importance < 1 or importance > 5:
            raise ValidationError("Importance must be between 1 and 5")

        from _lazy_imports import save_memory

        slug = title_slug or _auto_slug(content)
        note_id = save_memory(
            content=content,
            category=category,
            title_slug=slug,
            tags=tags or [],
            pinned=pinned,
            is_global=is_global,
            importance=importance,
        )
        return str(note_id)

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
    ) -> SearchResults:
        """Search memories by semantic relevance.

        Returns a ``SearchResults`` container with typed ``MemoryResult``
        objects and optional synthesis.
        """
        from _lazy_imports import search_memories

        raw = search_memories(
            db_path=self._db_path,
            query=query,
            limit=limit,
            include_global=include_global,
            rerank=rerank,
            boost_pinned=boost_pinned,
            recency_weight=recency_weight,
            include_facts=include_facts,
            fact_limit=fact_limit,
            synthesize=synthesize,
            max_synthesis_sentences=max_synthesis_sentences,
        )

        items = parse_search_results(raw)
        results = [
            MemoryResult(
                id=r.get("id", ""),
                content=r.get("content", ""),
                score=float(r.get("final_score", r.get("rank", 0))),
                tags=r.get("tags", []),
                category=r.get("category", ""),
                created_at=r.get("created_at", ""),
                pinned=bool(r.get("pinned", False)),
                importance=int(r.get("importance", 3)),
                metadata={
                    k: v
                    for k, v in r.items()
                    if k
                    not in (
                        "id",
                        "content",
                        "final_score",
                        "rank",
                        "tags",
                        "category",
                        "created_at",
                        "pinned",
                        "importance",
                    )
                },
            )
            for r in items
        ]

        synthesis = ""
        raw_dict: dict = {}
        if isinstance(raw, str):
            try:
                raw_dict = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                raw_dict = {}
        elif isinstance(raw, dict):
            raw_dict = raw
        if raw_dict:
            s = raw_dict.get("synthesis", "")
            if isinstance(s, dict):
                synthesis = s.get("answer", str(s))
            elif isinstance(s, str):
                synthesis = s

        return SearchResults(
            results=results,
            total=len(results),
            synthesis=synthesis,
            query=query,
        )

    def delete(self, note_id: str, hard: bool = False) -> bool:
        """Soft-delete (or hard-purge) a memory by note ID."""
        from memory_delete import soft_delete_note, hard_delete_note

        if hard:
            return hard_delete_note(str(self._db_path), note_id)
        return soft_delete_note(str(self._db_path), note_id)

    def restore(self, note_id: str) -> bool:
        """Restore a soft-deleted memory."""
        from memory_delete import restore_note

        return restore_note(str(self._db_path), note_id)

    def get(self, note_id: str) -> MemoryResult | None:
        """Retrieve a single memory by note ID."""
        conn = get_db_connection(self._db_path)
        try:
            row = conn.execute(
                "SELECT id, content, tags, category, created_at, "
                "       pinned, importance "
                "FROM memories WHERE id = ? AND deleted_at IS NULL",
                (note_id,),
            ).fetchone()
            if not row:
                return None
            return MemoryResult(
                id=row[0],
                content=row[1],
                tags=row[2] or [],
                category=row[3] or "",
                created_at=row[4] or "",
                pinned=bool(row[5]),
                importance=int(row[6] or 3),
            )
        finally:
            safe_close_db(conn)

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        category: str = "",
    ) -> list[MemoryResult]:
        """List recent memories (newest first), optionally filtered by category."""
        conn = get_db_connection(self._db_path)
        try:
            if category:
                rows = conn.execute(
                    "SELECT id, content, tags, category, created_at, "
                    "       pinned, importance "
                    "FROM memories WHERE deleted_at IS NULL AND category = ? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (category, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, content, tags, category, created_at, "
                    "       pinned, importance "
                    "FROM memories WHERE deleted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [
                MemoryResult(
                    id=r[0],
                    content=r[1],
                    tags=r[2] or [],
                    category=r[3] or "",
                    created_at=r[4] or "",
                    pinned=bool(r[5]),
                    importance=int(r[6] or 3),
                )
                for r in rows
            ]
        finally:
            safe_close_db(conn)

    def clear(self) -> int:
        """Clear all SDK-created memories. Returns count cleared."""
        conn = get_db_connection(self._db_path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            n = conn.execute(
                "DELETE FROM memories WHERE source_file LIKE 'sdk-%'"
            ).rowcount
            conn.commit()
            return int(n)
        finally:
            safe_close_db(conn)

    def stats(self) -> Stats:
        """Return memory system statistics."""
        conn = get_db_connection(self._db_path)
        try:
            memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
            ).fetchone()[0]
            vec_keys = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()[
                0
            ]
            chunks = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
            facts = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
            entities = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
            relations = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
            return Stats(
                memories=int(memories),
                vector_keys=int(vec_keys),
                chunks=int(chunks),
                facts=int(facts),
                entities=int(entities),
                relations=int(relations),
            )
        finally:
            safe_close_db(conn)

    # ── Safety ────────────────────────────────────────────────────────

    def scan_injection(self, content: str) -> dict[str, Any]:
        """Scan content for prompt-injection patterns."""
        from mcp_safety import memory_scan_injection

        raw = memory_scan_injection(content=content)
        return json.loads(raw) if isinstance(raw, str) else raw

    def check_contradictions(
        self, content: str, top_n: int = 20
    ) -> list[dict[str, Any]]:
        """Check content for phrase-level contradictions."""
        from mcp_safety import memory_check_contradictions

        raw = memory_check_contradictions(content=content, top_n=top_n)
        items = json.loads(raw) if isinstance(raw, str) else raw
        return items if isinstance(items, list) else []

    # ── User profile ──────────────────────────────────────────────────

    def get_user_profile(self) -> dict[str, Any]:
        """Get the user preference profile."""
        from mcp_profile import memory_user_profile

        raw = memory_user_profile()
        return json.loads(raw) if isinstance(raw, str) else raw

    def record_access(
        self,
        note_id: str,
        source: str = "search",
        category: str = "",
        tags: str = "",
    ) -> None:
        """Record that a note was accessed (opt-in via MEMORY_USER_PROFILE=1)."""
        from mcp_profile import memory_profile_access

        memory_profile_access(
            note_id=note_id,
            source=source,
            category=category,
            tags=tags,
        )

    # ── Integrity ──────────────────────────────────────────────────────

    def check_integrity(self, deep: bool = False) -> IntegrityReport:
        """Run a health check on the memory DB."""
        from mcp_audit import memory_check_integrity

        raw = memory_check_integrity(deep=deep)
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            return IntegrityReport(
                passed=data.get("passed", data.get("healthy", True)),
                errors=data.get("errors", data.get("issues", [])),
                warnings=data.get("warnings", []),
                stats=data.get("stats", {}),
            )
        return IntegrityReport(
            passed=bool(data),
            errors=[str(data)] if not data else [],
        )

    def audit(self) -> dict[str, Any]:
        """Audit memory system health (SRMA metrics)."""
        from mcp_audit import memory_audit

        raw = memory_audit()
        return json.loads(raw) if isinstance(raw, str) else raw

    # ── Facts ─────────────────────────────────────────────────────────

    def search_facts(self, query: str, limit: int = 10) -> list[Fact]:
        """Search extracted facts (SPO triples)."""
        from mcp_kg import memory_facts_search

        raw = memory_facts_search(query=query, limit=limit)
        items = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(items, dict):
            items = items.get("results", items.get("data", []))
        return [
            Fact(
                id=f.get("id", ""),
                subject=f.get("subject", ""),
                predicate=f.get("predicate", ""),
                obj=f.get("object", f.get("obj", "")),
                confidence=float(f.get("confidence", 1.0)),
                category=f.get("category", ""),
                source_note_id=f.get("source_note_id", ""),
                event_time=f.get("event_time", ""),
                event_time_granularity=f.get("event_time_granularity", ""),
                valid_at=f.get("valid_at", ""),
                invalid_at=f.get("invalid_at", ""),
                superseded_by=f.get("superseded_by", ""),
                supersedes=f.get("supersedes", ""),
                contradiction_score=float(f.get("contradiction_score", 0.0)),
                locked=bool(f.get("locked", False)),
            )
            for f in (items if isinstance(items, list) else [])
        ]

    def list_facts(self, limit: int = 50, offset: int = 0) -> list[Fact]:
        """List recent facts."""
        conn = get_db_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id, subject, predicate, object, confidence, category, "
                "       source_note_id, event_time, event_time_granularity, "
                "       valid_at, invalid_at, superseded_by, supersedes, "
                "       contradiction_score, locked "
                "FROM kg_facts ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [
                Fact(
                    id=r[0],
                    subject=r[1],
                    predicate=r[2],
                    obj=r[3],
                    confidence=float(r[4] or 1.0),
                    category=r[5] or "",
                    source_note_id=r[6] or "",
                    event_time=r[7] or "",
                    event_time_granularity=r[8] or "",
                    valid_at=r[9] or "",
                    invalid_at=r[10] or "",
                    superseded_by=r[11] or "",
                    supersedes=r[12] or "",
                    contradiction_score=float(r[13] or 0.0),
                    locked=bool(r[14] or False),
                )
                for r in rows
            ]
        finally:
            safe_close_db(conn)

    # ── Rebuild ────────────────────────────────────────────────────────

    def rebuild(self, scope: str = "active") -> str:
        """Rebuild the FTS5 index."""
        from mcp_rebuild import memory_rebuild

        return str(memory_rebuild(scope=scope))

    # ── Quality ────────────────────────────────────────────────────────

    def quality_filter(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search with quality gates (validation + deduplication)."""
        from mcp_quality import memory_quality_filter

        raw = memory_quality_filter(query=query, limit=limit)
        return json.loads(raw) if isinstance(raw, str) else raw

    def quality_stats(self) -> dict[str, Any]:
        """Return quality gate statistics."""
        from mcp_quality import memory_quality_stats

        raw = memory_quality_stats()
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return {"raw": raw}
        return raw

    # ── Summarization ─────────────────────────────────────────────────

    def summarize(self, note_id: str) -> str:
        """Summarize a specific note using extractive TF-IDF."""
        from mcp_summarization import memory_summarize

        return str(memory_summarize(note_id=note_id))

    # ── Adaptive retention ────────────────────────────────────────────

    def adaptive_retention(self, dry_run: bool = False) -> str:
        """Compute adaptive half-lives and neural forget curve scores."""
        from mcp_retention import memory_adaptive_retention

        return str(memory_adaptive_retention(dry_run=dry_run))

    # ── Domain sub-clients (lazy) ─────────────────────────────────────

    @property
    def kg(self) -> object:
        """Access the Knowledge Graph API.

        Returns a lazily-instantiated :class:`~agentic_memory.kg.KnowledgeGraph`
        bound to the same database as this client.

        Usage::

            mc = MemoryClient()
            ents = mc.kg.search("python")
            facts = mc.kg.search_facts("database")
        """
        if self._kg is None:
            from agentic_memory.kg import KnowledgeGraph

            self._kg = KnowledgeGraph(db_path=self._db_path)
        return self._kg

    @property
    def temporal(self) -> object:
        """Access the Temporal KG API.

        Returns a lazily-instantiated :class:`~agentic_memory.temporal.TemporalKG`
        bound to the same database as this client.

        Usage::

            mc = MemoryClient()
            facts = mc.temporal.search("python")
            contradictions = mc.temporal.contradictions()
        """
        if self._temporal is None:
            from agentic_memory.temporal import TemporalKG

            self._temporal = TemporalKG(db_path=self._db_path)
        return self._temporal

    @property
    def admin(self) -> object:
        """Access the Admin (health, circuit breaker) API.

        Returns a lazily-instantiated :class:`~agentic_memory.admin.Admin`
        bound to the same database as this client.

        Usage::

            mc = MemoryClient()
            h = mc.admin.health()
            cb = mc.admin.circuit_breaker_status()
        """
        if self._admin is None:
            from agentic_memory.admin import Admin

            self._admin = Admin(db_path=self._db_path)
        return self._admin

    # ── Context manager ───────────────────────────────────────────────

    def __enter__(self) -> MemoryClient:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _auto_slug(content: str) -> str:
    """Generate a short slug from content for auto-naming."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    h = hash(content) & 0xFFFF
    return f"sdk-auto-{ts}-{h:04x}"
