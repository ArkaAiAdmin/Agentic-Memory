from __future__ import annotations

import logging
"""Memory recall mechanism for agent cold-start and session continuity.

Assembles a structured briefing from multiple memory sources so that
an agent picking up a conversation has the context it needs without
loading the entire database.

Three entry points:
  recall_context()  — full structured recall (used by MCP tools)
  format_briefing() — human-readable text for agent injection
  session_recap()   — lightweight session-only summary

Design principles (from research, 2026-06-10):
  - Cold-start brief should be 800-1500 tokens (3-5KB)
  - Multi-signal ranking: recency + relevance + importance
  - Structure by type: pinned → recent → important → relevant
  - Max 15 items total across all sections
  - Provenance tracked for every item
"""

__all__ = ["recall_context", "format_briefing", "session_recap"]

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from infra.infrastructure import resolve_active_memory_dir
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)

# Module-level search_memories reference — tests patch this attribute on this
# module (`recall.recall.search_memories`) to intercept calls without needing
# to know the lazy-import plumbing. First real call boots the lazy import.
search_memories = None





# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_ITEMS_TOTAL = 15
MAX_PINNED = 5
MAX_RECENT = 5
MAX_IMPORTANT = 3
MAX_RELEVANT = 5
RECENT_DAYS = 7
TOKEN_BUDGET = 1500  # approximate target tokens
HIGH_IMPORTANCE_THRESHOLD = 4


def recall_context(
    query: str = "",
    limit: int = MAX_ITEMS_TOTAL,
    include_pinned: bool = True,
    include_recent_digests: bool = True,
    include_high_importance: bool = bool(True),
    include_user_profile: bool = True,
    days_recent: int = RECENT_DAYS,
    deep_rerank: bool = False,
    db_path: str | None = None,
) -> dict:
    """Assemble a structured recall briefing for agent context.

    Pulls from multiple memory sources and returns both a structured dict
    and a human-readable formatted string suitable for agent injection.

    Args:
        query: Optional query for contextual recall (uses search_pipeline).
        limit: Max total items across all sections.
        include_pinned: Include pinned notes.
        include_recent_digests: Include recent session digests.
        include_high_importance: Include high-importance memories.
        include_user_profile: Include user preference profile.
        days_recent: How many days back for recent activity.
        deep_rerank: When True, run the Qwen3-0.6B / BGE-m3 deep reranker
                     on the relevant-memories section. Adds 1-5s of CPU
                     latency and can hang on Apple Silicon MPS (M-series
                     kernel bug — see reranker.py). Default False so the
                     recall briefing is bounded to <100ms. Set True for
                     highest-quality ranking when a model hang is acceptable.
        db_path: Path to the memory database (string). If None, resolves
                 from CWD via resolve_active_memory_dir().

    Returns:
        dict with keys: query, timestamp, sections, total_memories,
        formatted, token_estimate.
    """
    if db_path is None:
        active_dir = resolve_active_memory_dir()
        db_path_resolved = active_dir / "memory.db"
    else:
        db_path_resolved = Path(db_path)
    if not db_path_resolved.exists():
        return _empty_result(query, "Database not found")

    from infra.db import connection_pool, safe_close_db

    try:
        conn = connection_pool.get(str(db_path_resolved), timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000;")
    except Exception as e:
        logger.warning("recall_context failed: %s", e)
        return _empty_result(query, f"Connection failed: {e}")

    sections: dict = {}
    try:
        if include_pinned:
            sections["pinned"] = _fetch_pinned(conn, min(MAX_PINNED, limit))

        if include_recent_digests:
            sections["recent_activity"] = _fetch_recent_digests(
                conn, days_recent, min(MAX_RECENT, limit)
            )

        if include_high_importance:
            sections["important"] = _fetch_high_importance(
                conn, min(MAX_IMPORTANT, limit)
            )

        if query:
            sections["relevant"] = _fetch_relevant(
                db_path_resolved,
                query,
                min(MAX_RELEVANT, limit),
                deep_rerank=deep_rerank,
            )

        if include_user_profile:
            profile = _fetch_user_profile()
            if profile:
                sections["profile"] = profile

        # Count total memories
        total = _count_memories(conn)

        # Build result
        result = {
            "query": query,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sections": sections,
            "total_memories": total,
        }

        # Format for agent injection
        formatted = format_briefing(result)
        result["formatted"] = formatted
        result["token_estimate"] = _estimate_tokens(formatted)

        return result

    except Exception as e:
        logger.warning("recall_context error: %s", e)
        return _empty_result(query, str(e))
    finally:
        if conn is not None:
            try:
                safe_close_db(conn, should_commit=False)
            except Exception as e:
                logger.warning("recall_context failed: %s", e)


def format_briefing(data: dict) -> str:
    """Format a recall context dict into human-readable text.

    Args:
        data: Output from recall_context().

    Returns:
        Structured text suitable for agent context injection.
    """
    lines = [
        "## Memory Recall Briefing",
        f"Generated: {data.get('timestamp', 'unknown')}",
        "",
    ]

    sections = data.get("sections", {})

    # Pinned notes
    pinned = sections.get("pinned", [])
    if pinned:
        lines.append(f"### Pinned Notes ({len(pinned)})")
        for i, item in enumerate(pinned, 1):
            meta = _item_meta(item)
            lines.append(
                f"{i}. [{item.get('id', '?')}] {item.get('content', '')[:120]}{meta}"
            )
        lines.append("")

    # Recent activity
    recent = sections.get("recent_activity", [])
    if recent:
        lines.append(f"### Recent Activity (last {RECENT_DAYS} days)")
        for item in recent:
            created = item.get("created_at", "")[:10]
            content = item.get("content", "")[:100]
            lines.append(f"- {created}: {content}")
        lines.append("")

    # Important memories
    important = sections.get("important", [])
    if important:
        lines.append(f"### Important Notes ({len(important)})")
        for i, item in enumerate(important, 1):
            meta = _item_meta(item)
            lines.append(
                f"{i}. [{item.get('id', '?')}] {item.get('content', '')[:120]}{meta}"
            )
        lines.append("")

    # Relevant (query-based)
    relevant = sections.get("relevant", [])
    if relevant:
        lines.append(f'### Relevant to "{data.get("query", "")}"')
        for i, item in enumerate(relevant, 1):
            meta = _item_meta(item)
            lines.append(
                f"{i}. [{item.get('source', item.get('id', '?'))}] {item.get('content', '')[:120]}{meta}"
            )
        lines.append("")

    # User profile
    profile = sections.get("profile")
    if profile and profile.get("enabled", False):
        lines.append("### User Preferences")
        for key, value in profile.items():
            if key != "enabled":
                lines.append(f"- {key}: {value}")
        lines.append("")

    # Footer
    total = data.get("total_memories", 0)
    lines.append(f"---\nTotal memories in database: {total}")

    return "\n".join(lines)


_RECALL_DEFAULTS: dict[str, int | bool] = {
    "max_tokens": 800,
    "tier1_hot_days": 7,
    "tier_fallback_threshold": 5,
}


def _get_recall_cfg() -> dict[str, int | bool]:
    """Load recall tuning values from MemoryConfig, falling back to defaults."""
    try:
        from infra._lazy_imports import get_config

        cfg = get_config()
        return {
            "max_tokens": int(getattr(cfg, "recall_max_tokens", _RECALL_DEFAULTS["max_tokens"])),
            "tier1_hot_days": int(getattr(cfg, "recall_tier1_hot_days", _RECALL_DEFAULTS["tier1_hot_days"])),
            "tier_fallback_threshold": int(getattr(cfg, "recall_tier_fallback_threshold", _RECALL_DEFAULTS["tier_fallback_threshold"])),
        }
    except Exception as e:
        logger.warning("_get_recall_cfg failed: %s", e)
        return dict(_RECALL_DEFAULTS)


def _trace_event(
    event: str,
    *,
    query: str = "",
    tier_counts: dict[str, int] | None = None,
    token_estimate: int = 0,
    truncated: bool = False,
    db_path: Path | None = None,
) -> None:
    """Append one trace event to ``memory/recall_trace.jsonl``.

    Each line is a JSON object with the event name, timestamp, query
    snippet, per-tier item counts, and the final token estimate.  The
    file is append-only and self-pruning — capped at 50 000 lines to
    prevent unbounded growth on a long-running system.

    Failures are silently swallowed: tracing must never break recall.
    """
    try:
        if db_path is None:
            active_dir = resolve_active_memory_dir()
            trace_path = active_dir / "recall_trace.jsonl"
        else:
            trace_path = db_path.parent / "recall_trace.jsonl"
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "query": (query or "")[:200],
                "tier_counts": tier_counts or {},
                "token_estimate": int(token_estimate),
                "truncated": bool(truncated),
            },
            ensure_ascii=False,
        )
        with open(trace_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        _maybe_prune_trace(trace_path, max_lines=50_000)
    except Exception as e:
        logger.warning("_trace_event failed: %s", e)


def _maybe_prune_trace(trace_path: Path, max_lines: int = 50_000) -> None:
    """If trace file exceeds max_lines, keep only the most recent half."""
    try:
        if not trace_path.exists():
            return
        with open(trace_path, "rb") as fh:
            total = sum(1 for _ in fh)
        if total <= max_lines:
            return
        keep = max_lines // 2
        lines: list[bytes] = []
        with open(trace_path, "rb") as fh:
            for i, line in enumerate(fh):
                if i >= total - keep:
                    lines.append(line)
        tmp = trace_path.with_suffix(".jsonl.tmp")
        with open(tmp, "wb") as fh:
            fh.writelines(lines)
        tmp.replace(trace_path)
    except Exception as e:
        logger.warning("_maybe_prune_trace failed: %s", e)




def session_recap(
    db_path: str | Path | None = None,
    session_id: str = "",
    query: str = "",
) -> str:
    """4-tier recall policy for session-start context injection.

    Tier 1 — Hot/curated (max 5):
        Pinned or high-importance notes created in the last 7 days.
        Section header: "## Key Context"

    Tier 2 — Semantic search (max 5):
        search_memories(query, light=True) for project-relevant content.
        Only fires when a non-empty query is provided.
        Section header: "## Relevant to this session"

    Tier 3 — KG facts (max 3):
        Known facts from the knowledge graph for the current namespace.
        Section header: "## Known Facts"

    Tier 4 — Recent sessions (max 3, fallback only):
        Only if tiers 1-3 returned fewer than RECALL_FALLBACK_THRESHOLD items.
        Excludes auto-saved tool logs.
        Section header: "## Recent Activity"

    Total token budget: SESSION_RECAP_MAX_TOKENS (~800 tokens).
    Falls back gracefully at every tier.

    Args:
        db_path: Path to the memory database (string). If None, resolves
                 from CWD via resolve_active_memory_dir().
        session_id: Optional specific session identifier (reserved for
                    future per-session scoping; currently unused).
        query: Optional natural-language query for contextual recall.
               Extracted from session-start hook data (prompt, task, cwd).

    Returns:
        Formatted markdown string suitable for agent context injection.
    """
    if db_path is None:
        active_dir = resolve_active_memory_dir()
        db_path_resolved = active_dir / "memory.db"
    else:
        db_path_resolved = Path(db_path)
    if not db_path_resolved.exists():
        return "No database found."

    try:
        conn = sqlite3.connect(str(db_path_resolved), timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout = 10000;")
    except Exception as e:
        logger.warning("session_recap failed: %s", e)
        return "Database connection failed."

    try:
        from agent_context import get_agent
        ctx = get_agent()
        namespace = ctx.namespace
        _rcfg = _get_recall_cfg()
        _max_tokens = int(_rcfg.get("max_tokens", 800))
        _tier1_days = int(_rcfg.get("tier1_hot_days", 7))
        _fallback_threshold = int(_rcfg.get("tier_fallback_threshold", 5))

        _trace_event(
            "recall_start",
            query=query,
            db_path=db_path_resolved,
        )

        tier1 = _fetch_curated(conn, namespace, limit=5)
        tier2 = _fetch_relevant_light(db_path_resolved, namespace, query, limit=5) if query else []
        tier3 = _fetch_kg_facts(conn, namespace, limit=3)
        tier4_total = len(tier1) + len(tier2) + len(tier3)
        tier4 = _fetch_recent_sessions(conn, namespace, limit=3) if tier4_total < _fallback_threshold else []

        sections = []
        if tier1:
            sections.append(("## Key Context", tier1))
        if tier2:
            sections.append(("## Relevant to this session", tier2))
        if tier3:
            sections.append(("## Known Facts", tier3))
        if tier4:
            sections.append(("## Recent Activity", tier4))

        if not sections:
            _trace_event(
                "recall_complete",
                query=query,
                tier_counts={"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0},
                token_estimate=0,
                db_path=db_path_resolved,
            )
            return "No relevant context found for this session."

        lines = ["**Session Recap**", ""]
        for header, items in sections:
            lines.append(header)
            lines.append("")
            for item in items:
                content = item.get("content", "")[:120]
                created = item.get("created_at", "")[:10]
                meta = _item_meta(item)
                lines.append(f"- {created}: {content}{meta}")
            lines.append("")

        text = "\n".join(lines).rstrip()
        token_est = _estimate_tokens(text)
        truncated = False
        if token_est > _max_tokens:
            truncated = True
            _trace_event(
                "recall_truncated",
                query=query,
                tier_counts={
                    "tier1": len(tier1),
                    "tier2": len(tier2),
                    "tier3": len(tier3),
                    "tier4": len(tier4),
                },
                token_estimate=token_est,
                db_path=db_path_resolved,
            )
            lines_out = ["**Session Recap**", ""]
            budget_per_section = _max_tokens // max(len(sections), 1)
            for header, items in sections:
                lines_out.append(header)
                lines_out.append("")
                chars_left = budget_per_section * 4
                used = 0
                for item in items:
                    entry = f"- {item.get('created_at', '')[:10]}: {item.get('content', '')[:80]}{_item_meta(item)}"
                    if used + len(entry) > chars_left:
                        break
                    lines_out.append(entry)
                    used += len(entry)
                lines_out.append("")
            text = "\n".join(lines_out).rstrip()

        _trace_event(
            "recall_complete",
            query=query,
            tier_counts={
                "tier1": len(tier1),
                "tier2": len(tier2),
                "tier3": len(tier3),
                "tier4": len(tier4),
            },
            token_estimate=token_est,
            truncated=truncated,
            db_path=db_path_resolved,
        )
        return text
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal fetch helpers
# ---------------------------------------------------------------------------


def _fetch_curated(conn: AnyConnection, namespace: str, limit: int) -> list[dict]:
    """Tier 1: Hot/curated notes — pinned or high-importance, last 7 days."""
    tier1_days = _get_recall_cfg().get("tier1_hot_days", 7)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(tier1_days))).isoformat()
    ns_filter = f"agents/{namespace}/%" if namespace != "default" else None
    if namespace != "default":
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE deleted_at IS NULL
                 AND (pinned = 1 OR importance >= ?)
                 AND created_at > ?
                 AND source_file NOT LIKE ?
                 AND id NOT LIKE 'agents/%'
                 AND (id LIKE ? OR id NOT LIKE 'agents/%')
               ORDER BY pinned DESC, importance DESC, fitness_score DESC
               LIMIT ?""",
            (HIGH_IMPORTANCE_THRESHOLD, cutoff, "sessions/auto-%", ns_filter, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE deleted_at IS NULL
                 AND (pinned = 1 OR importance >= ?)
                 AND created_at > ?
                 AND source_file NOT LIKE 'sessions/auto-%'
                 AND id NOT LIKE 'agents/%'
               ORDER BY pinned DESC, importance DESC, fitness_score DESC
               LIMIT ?""",
            (HIGH_IMPORTANCE_THRESHOLD, cutoff, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _fetch_relevant_light(
    db_path: Path, namespace: str, query: str, limit: int
) -> list[dict]:
    """Tier 2: Lightweight semantic search for session context."""
    try:
        sm = search_memories
        if sm is None:
            from infra._lazy_imports import search_memories as _sm

            globals()["search_memories"] = _sm
            sm = _sm
        tenant_id = namespace if namespace != "default" else "default"
        result = sm(
            db_path=db_path,
            query=query,
            limit=limit,
            rerank=True,
            boost_pinned=True,
            recency_weight=0.1,
            include_global=True,
            safety_wiring=True,
            deep_rerank=False,
            light=True,
            tenant_id=tenant_id,
        )
        raw_results = result.get("raw_results", result.get("results", []))
        converted = []
        for row in raw_results:
            if isinstance(row, dict):
                converted.append(row)
            elif isinstance(row, tuple) and len(row) >= 8:
                converted.append(
                    {
                        "id": row[0],
                        "content": row[1] or "",
                        "source_file": row[2] or "",
                        "tags": _parse_tags(row[3]),
                        "created_at": row[4] or "",
                        "fitness_score": float(row[5] or 0.0),
                        "importance": row[8] if len(row) > 8 else 0,
                        "pinned": bool(row[9]) if len(row) > 9 else False,
                    }
                )
        return [i for i in converted if not _is_auto_save(i)][:limit]
    except Exception as e:
        logger.debug("search_memories light failed for session_recap: %s", e)
        return []


def _fetch_kg_facts(conn: AnyConnection, namespace: str, limit: int) -> list[dict]:
    """Tier 3: Known facts from the knowledge graph for the current namespace."""
    ns_filter = f"agents/{namespace}/%" if namespace != "default" else None
    try:
        if namespace != "default":
            rows = conn.execute(
                """SELECT id, entity, predicate, obj, confidence, valid_at
                   FROM kg_facts
                   WHERE deleted_at IS NULL
                     AND (memory_id LIKE ? OR memory_id NOT LIKE 'agents/%')
                   ORDER BY confidence DESC, valid_at DESC
                   LIMIT ?""",
                (ns_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, entity, predicate, obj, confidence, valid_at
                   FROM kg_facts
                   WHERE deleted_at IS NULL
                     AND memory_id NOT LIKE 'agents/%'
                   ORDER BY confidence DESC, valid_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "content": f"{r[1]} {r[2]} {r[3]}" if r[3] else f"{r[1]} {r[2]}",
                "source_file": "kg_facts",
                "tags": [],
                "created_at": (r[5] or "")[:10],
                "fitness_score": float(r[4] or 0.0),
                "importance": int(r[4] * 5) if r[4] else 0,
                "pinned": False,
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug("kg_facts fetch failed for session_recap: %s", e)
        return []


def _fetch_recent_sessions(conn: AnyConnection, namespace: str, limit: int) -> list[dict]:
    """Tier 4 (fallback): Recent non-auto session notes, last 3 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    if namespace != "default":
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE deleted_at IS NULL
                 AND source_file LIKE ?
                 AND source_file NOT LIKE ?
                 AND created_at > ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (f"agents/{namespace}/sessions/%", f"agents/{namespace}/sessions/auto-%", cutoff, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE deleted_at IS NULL
                 AND source_file LIKE 'sessions/%'
                 AND source_file NOT LIKE 'sessions/auto-%'
                 AND source_file NOT LIKE 'agents/%'
                 AND created_at > ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _fetch_pinned(conn: AnyConnection, limit: int) -> list[dict]:
    """Fetch pinned notes."""
    from agent_context import get_agent
    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE pinned = 1
                 AND deleted_at IS NULL
                 AND (id LIKE ? OR id NOT LIKE 'agents/%')
               ORDER BY importance DESC, fitness_score DESC
               LIMIT ?""",
            (f"agents/{namespace}/%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE pinned = 1
                 AND deleted_at IS NULL
                 AND id NOT LIKE 'agents/%'
               ORDER BY importance DESC, fitness_score DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _fetch_recent_digests(
    conn: AnyConnection, days: int, limit: int
) -> list[dict]:
    """Fetch recent session digest notes (excluding auto-saved tool logs)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    from agent_context import get_agent
    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE deleted_at IS NULL
                 AND source_file LIKE ?
                 AND source_file NOT LIKE ?
                 AND created_at > ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (f"agents/{namespace}/sessions/%", f"agents/{namespace}/sessions/auto-%", cutoff, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE deleted_at IS NULL
                 AND source_file LIKE 'sessions/%'
                 AND source_file NOT LIKE 'sessions/auto-%'
                 AND source_file NOT LIKE 'agents/%'
                 AND created_at > ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _fetch_high_importance(conn: AnyConnection, limit: int) -> list[dict]:
    """Fetch high-importance memories (importance >= 4)."""
    from agent_context import get_agent
    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE importance >= ?
                 AND deleted_at IS NULL
                 AND (id LIKE ? OR id NOT LIKE 'agents/%')
               ORDER BY importance DESC, fitness_score DESC
               LIMIT ?""",
            (HIGH_IMPORTANCE_THRESHOLD, f"agents/{namespace}/%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, content, source_file, tags, created_at,
                      fitness_score, importance, pinned
               FROM memories
               WHERE importance >= ?
                 AND deleted_at IS NULL
                 AND id NOT LIKE 'agents/%'
               ORDER BY importance DESC, fitness_score DESC
               LIMIT ?""",
            (HIGH_IMPORTANCE_THRESHOLD, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _fetch_relevant(
    db_path: Path, query: str, limit: int, deep_rerank: bool = False
) -> list[dict]:
    """Fetch contextually relevant memories using search_pipeline.

    Args:
        deep_rerank: When True, run the Qwen3-0.6B / BGE-m3 deep reranker.
                     Default False (the lightweight weak-CE path is used) so
                     recall is bounded to <100ms. Pass True for the
                     highest-quality ranking — at the cost of 1-5s extra
                     CPU latency and a known risk of MPS kernel hang on
                     Apple Silicon (see reranker.py).
    """
    try:
        from infra._lazy_imports import search_memories

        result = search_memories(
            db_path=db_path,
            query=query,
            limit=limit,
            rerank=True,
            boost_pinned=True,
            recency_weight=0.1,
            include_global=True,
            safety_wiring=True,
            deep_rerank=deep_rerank,
        )
        # search_memories returns {'results': [...], 'count': N, ...}
        # raw_results are tuples: (id, content, source_file, tags, created_at,
        #                          embedding_score, semantic_score, hybrid_score,
        #                          importance, pinned)
        raw_results = result.get("raw_results", result.get("results", []))
        converted = []
        for row in raw_results:
            if isinstance(row, dict):
                converted.append(row)
            elif isinstance(row, tuple) and len(row) >= 8:
                converted.append(
                    {
                        "id": row[0],
                        "content": row[1] or "",
                        "source_file": row[2] or "",
                        "tags": _parse_tags(row[3]),
                        "created_at": row[4] or "",
                        "fitness_score": float(row[5] or 0.0),
                        "importance": row[8] if len(row) > 8 else 0,
                        "pinned": bool(row[9]) if len(row) > 9 else False,
                    }
                )
        items = [i for i in converted if not _is_auto_save(i)][:limit]
        # Spaced repetition: best-effort, never breaks recall
        try:
            from spaced_repetition import SpacedRepetition

            sr = SpacedRepetition(db_path)
            try:
                if items:
                    for item in items:
                        nid = item.get("id")
                        if nid:
                            sr.record_success(str(nid))
            finally:
                sr.close()
        except Exception as e:
            logger.warning("_fetch_relevant failed: %s", e)
        return items
    except Exception as e:
        logger.debug("search_memories failed for recall: %s", e)
        return []


def _fetch_user_profile(db_path: str | None = None) -> Optional[dict]:
    """Fetch user preference profile if enabled.

    Args:
        db_path: Optional db path string, passed through for consistency.
    """
    try:
        import user_profile as up

        if not up.PROFILE_ENABLED:
            return None
        profile = up.get_user_profile()
        if profile and profile.get("enabled", False):
            return profile
        return None
    except Exception as e:
        logger.warning("_fetch_user_profile failed: %s", e)
        return None


def _count_memories(conn: AnyConnection) -> int:
    """Count total active memories (excluding auto-saved tool logs)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND source_file NOT LIKE 'sessions/auto-%'"
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _is_auto_save(item: dict) -> bool:
    """Check if a memory item is an auto-saved tool log."""
    sf = item.get("source_file", "") or ""
    return sf.startswith("sessions/auto-")


def _parse_tags(raw) -> list:
    """Parse tags from DB — handle both JSON arrays and comma-separated strings."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else [raw]
        except (json.JSONDecodeError, ValueError):
            return [raw]
    return [t.strip() for t in raw.split(",") if t.strip()]


def _row_to_dict(row) -> dict:
    """Convert a DB row to a memory item dict."""
    return {
        "id": row[0],
        "content": row[1] or "",
        "source_file": row[2] or "",
        "tags": _parse_tags(row[3]),
        "created_at": row[4] or "",
        "fitness_score": row[5] or 0.0,
        "importance": row[6] or 0,
        "pinned": bool(row[7]),
    }


def _item_meta(item: dict) -> str:
    """Build a metadata suffix string for an item."""
    parts = []
    if item.get("pinned"):
        parts.append("pinned")
    imp = item.get("importance", 0)
    if imp:
        parts.append(f"importance: {imp}")
    tags = item.get("tags", [])
    if tags and isinstance(tags, list) and tags[0]:
        parts.append(f"tag: {tags[0]}")
    elif tags and isinstance(tags, str) and tags.strip():
        parts.append(f"tag: {tags.split(',')[0].strip()}")
    if parts:
        return f" ({', '.join(parts)})"
    return ""


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token for English)."""
    return len(text) // 4


def _empty_result(query: str, reason: str = "") -> dict:
    """Return an empty recall result."""
    return {
        "query": query,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sections": {},
        "total_memories": 0,
        "formatted": f"No recall available: {reason}"
        if reason
        else "No recall available.",
        "token_estimate": 0,
    }
