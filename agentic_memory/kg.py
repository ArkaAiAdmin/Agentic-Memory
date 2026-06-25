"""Knowledge Graph SDK — typed wrapper for KG operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_memory.models import Entity, Relation, Fact
from agentic_memory.utils import resolve_db_path, get_db_connection, safe_close_db


def _as_list(data: Any) -> list[dict[str, Any]]:
    """Normalise JSON responses that may be str, dict, or list.

    Handles the three shapes returned by the underlying pipeline:
    ``None`` / empty → ``[]``, ``str`` (JSON-encoded list or dict) →
    parsed, ``dict`` → ``.get("results", .get("data", []))``,
    ``list`` → identity.
    """
    if data is None:
        return []
    if isinstance(data, str):
        data = data.strip()
        if not data:
            return []
        data = json.loads(data)
    if isinstance(data, dict):
        return data.get("results", data.get("data", []))
    if isinstance(data, list):
        return data
    return []


class KnowledgeGraph:
    """Typed SDK wrapper for Knowledge Graph (KG) operations.

    Provides typed access to entity search, fact search, shortest-path
    computation, and graph traversal backed by the same underlying
    pipeline as the MCP server.

    Usage::

        kg = KnowledgeGraph()
        entities = kg.search("python")
        facts = kg.search_facts("user prefers")
        path = kg.shortest_path("python", "typing")
        ents, rels = kg.traverse("python", max_hops=2)
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = resolve_db_path(db_path)

    # ── Entity search ───────────────────────────────────────────────

    def search(self, query: str, limit: int = 10, max_hops: int = 2) -> list[Entity]:
        """Search the knowledge graph for entities matching *query*.

        Args:
            query: Free-text search against entity names and types.
            limit: Maximum number of entities to return.
            max_hops: How many hops of edges to include (1 or 2).

        Returns:
            Entities ranked by relevance (mention count).
        """
        from knowledge_graph import graph_search_db

        raw = graph_search_db(self._db_path, query, limit=limit, max_hops=max_hops)
        result = json.loads(raw) if isinstance(raw, str) else raw
        entities = result.get("entities", []) if isinstance(result, dict) else []
        return [
            Entity(
                id=str(e["id"]),
                name=e["name"],
                entity_type=e["entity_type"],
                description=e.get("description", ""),
                metadata={
                    k: v
                    for k, v in e.items()
                    if k not in ("id", "name", "entity_type", "description")
                },
            )
            for e in entities
        ]

    # ── Fact search ─────────────────────────────────────────────────

    def search_facts(self, query: str, limit: int = 10) -> list[Fact]:
        """Search extracted facts (SPO triples) matching *query*.

        Args:
            query: Free-text search against subject, predicate, and
                object fields.
            limit: Maximum number of facts to return.

        Returns:
            Matching facts with confidence scores and temporal metadata.
        """
        from fact_extraction import facts_search_db

        raw = facts_search_db(self._db_path, query, limit=limit)
        items = _as_list(raw)
        return [
            Fact(
                id=str(f.get("id", "")),
                subject=f.get("subject", ""),
                predicate=f.get("predicate", ""),
                obj=f.get("object", f.get("obj", "")),
                confidence=float(
                    f.get("confidence", f.get("effective_confidence", 1.0))
                ),
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
            for f in items
        ]

    # ── Shortest path ───────────────────────────────────────────────

    def shortest_path(
        self, source: str, target: str, max_hops: int = 5
    ) -> list[Relation]:
        """Compute the shortest path between two KG entities.

        Args:
            source: Name of the start entity.
            target: Name of the destination entity.
            max_hops: Maximum traversal depth (default 5).

        Returns:
            A list of ``Relation`` objects describing each hop in the
            path, ordered from source toward target.  Empty list when
            no path exists.
        """
        from db import open_db
        from kg_traversal import find_shortest_path

        with open_db(self._db_path, timeout=5.0, write=False) as conn:
            path = find_shortest_path(conn, source, target, max_depth=max_hops)

        if not path:
            return []

        relations: list[Relation] = []
        idx = 0
        while idx + 2 < len(path):
            src = path[idx]
            rel = path[idx + 1]
            tgt = path[idx + 2]
            relations.append(
                Relation(
                    id=str(rel.get("id", "")),
                    source=src["name"],
                    target=tgt["name"],
                    relation_type=rel["relation"],
                )
            )
            idx += 2
        return relations

    # ── Graph traversal ─────────────────────────────────────────────

    def traverse(
        self, start: str, max_hops: int = 3
    ) -> tuple[list[Entity], list[Relation]]:
        """Traverse the KG starting from *start*.

        Follows all outgoing edges up to *max_hops* deep.

        Args:
            start: Name of the starting entity.
            max_hops: Maximum traversal depth (default 3).

        Returns:
            A ``(entities, relations)`` tuple with deduplicated entities
            and all discovered relations.
        """
        from db import open_db
        from kg_traversal import find_neighbors

        with open_db(self._db_path, timeout=5.0, write=False) as conn:
            edges = find_neighbors(conn, start, direction="out", max_depth=max_hops)

        seen: dict[str, Entity] = {}
        relations: list[Relation] = []

        for edge in edges:
            src = edge["source"]
            tgt = edge["target"]

            for data in (src, tgt):
                name = data["name"]
                if name not in seen:
                    seen[name] = Entity(
                        id="",
                        name=name,
                        entity_type=data.get("entity_type", ""),
                    )

            relations.append(
                Relation(
                    id=str(edge.get("id", "")),
                    source=src["name"],
                    target=tgt["name"],
                    relation_type=edge["relation"],
                    weight=float(edge.get("weight", 1.0)),
                )
            )

        return list(seen.values()), relations

    # ── Direct DB helpers ───────────────────────────────────────────

    def list_facts(self, limit: int = 50, offset: int = 0) -> list[Fact]:
        """List all facts with pagination (newest first).

        Args:
            limit: Maximum number of facts to return.
            offset: Number of facts to skip for pagination.

        Returns:
            Facts ordered by descending ID.
        """
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
                    locked=bool(r[14]),
                )
                for r in rows
            ]
        finally:
            safe_close_db(conn)

    def stats(self) -> dict[str, Any]:
        """Return KG statistics.

        Returns:
            A dict with keys ``enabled``, ``entity_count``,
            ``edge_count``, ``type_distribution``,
            ``relation_distribution``, and ``most_connected``.
        """
        from knowledge_graph import graph_stats_db

        raw = graph_stats_db(self._db_path)
        result = json.loads(raw) if isinstance(raw, str) else raw
        return result if isinstance(result, dict) else {}
