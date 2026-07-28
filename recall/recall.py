from __future__ import annotations
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
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from infra.infrastructure import resolve_active_memory_dir
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)

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
            except Exception:
                pass


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


from recall.search_memory import search_memories


def session_recap(
    db_path: str | Path | None = None,
    session_id: str = "",
    query: str = "",
) -> str:
    """Lightweight session-only summary for quick continuity.

    Args:
        db_path: Path to the memory database. If None, resolves
                 from CWD via resolve_active_memory_dir().
        session_id: Optional specific session date (YYYY-MM-DD).
        query: Optional query string for contextual recall.

    Returns:
        Summary string of recent session activity.
    """
    if db_path is None:
        active_dir = resolve_active_memory_dir()
        db_path_resolved = active_dir / "memory.db"
    else:
        db_path_resolved = Path(db_path)
    if not db_path_resolved.exists():
        return "No database found."

    from infra.db import open_db

    try:
        from agent_context import get_agent
        ctx = get_agent()
        namespace = ctx.namespace
        if namespace != "default":
            tier1_scope_clause = "(id LIKE ? OR id NOT LIKE 'agents/%')"
            tier1_params = [f"agents/{namespace}/%"]
            tier4_scope_clause = "source_file LIKE ? AND source_file NOT LIKE ?"
            tier4_params = [f"agents/{namespace}/sessions/%", f"agents/{namespace}/sessions/auto-%"]
        else:
            tier1_scope_clause = "id NOT LIKE 'agents/%'"
            tier1_params = []
            tier4_scope_clause = "source_file LIKE 'sessions/%' AND source_file NOT LIKE 'sessions/auto-%'"
            tier4_params = []

        with open_db(db_path_resolved, timeout=5.0, pooled=True, write=False) as conn:
            tier1_rows = conn.execute(
                f"""SELECT id, content, source_file, created_at, pinned, importance
                   FROM memories
                   WHERE deleted_at IS NULL AND (pinned = 1 OR importance >= 4)
                     AND {tier1_scope_clause}
                   ORDER BY created_at DESC
                   LIMIT 5""",
                tier1_params,
            ).fetchall()

            tier2_rows = []
            if query:
                try:
                    search_res = search_memories(custom_db_path=str(db_path_resolved), query=query)
                    if isinstance(search_res, dict) and "raw_results" in search_res:
                        tier2_rows = search_res["raw_results"]
                except Exception:
                    pass

            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            tier4_rows = conn.execute(
                f"""SELECT id, content, source_file, created_at
                   FROM memories
                   WHERE deleted_at IS NULL AND created_at > ?
                     AND {tier4_scope_clause}
                   ORDER BY created_at DESC
                   LIMIT 10""",
                [cutoff] + tier4_params,
            ).fetchall()

            if not tier1_rows and not tier2_rows and not tier4_rows:
                return "No relevant context found"

            lines = []
            if tier1_rows:
                lines.append("**Key Context** (Tier 1)")
                lines.append("")
                for r in tier1_rows:
                    lines.append(f"- {r[1]}")
                lines.append("")

            if tier2_rows:
                lines.append("**Relevant to this session** (Tier 2)")
                lines.append("")
                for r in tier2_rows:
                    content = r[1] if len(r) > 1 else str(r)
                    lines.append(f"- {content}")
                lines.append("")

            if tier4_rows:
                if not tier1_rows and not tier2_rows:
                    lines.append("**Recent Activity** (Tier 4)")
                    lines.append("")
                lines.append("**Session Recap**")
                lines.append("")
                for r in tier4_rows:
                    content = r[1][:100] if r[1] else ""
                    created = r[3][:10] if len(r) > 3 and r[3] else ""
                    lines.append(f"- {created}: {content}")

            return "\n".join(lines)
    except Exception:
        return "No relevant context found"


# ---------------------------------------------------------------------------
# Internal fetch helpers
# ---------------------------------------------------------------------------


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
        except Exception:
            pass
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
    except Exception:
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
