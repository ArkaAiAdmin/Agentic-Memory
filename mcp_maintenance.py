"""
Maintenance/system MCP tools — heartbeat, tier_stats, duplicates, merge_suggestions,
consolidate, rewrite_links, detect_contradictions, arc_stats, review_schedule,
pinned_decay, compile_skill, plus the memory_maintenance router.

Sub-modules (imported here for tool registration):
  - mcp_rebuild  — rebuild, compact, backfill_all
  - mcp_audit    — audit, audit_query, check_integrity
  - mcp_crdt     — crdt_sync, crdt_status
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import os
import sys
from pathlib import Path


import json
import re
import subprocess
from enum import Enum
from typing import Any, Optional

from infra.cache import clear_all_caches
from mcp_common import (
    _resolve_memory_dir,
    _run_subprocess_output,
    GLOBAL_SCRIPTS_DIR,
    GLOBAL_MEM_DIR,
    get_memory_paths,
    logger,
    _err,
    ErrorCode,
    with_audit,
    with_memory_connection,
    atomic_write,
    parse_frontmatter,
    _validate_slug,
)
from mcp_instance import mcp

# Sub-module imports — register @mcp.tool() functions at import time.
import mcp_rebuild  # noqa: E402, F401
import mcp_audit  # noqa: E402, F401
import mcp_crdt  # noqa: E402, F401


@mcp.tool()
@with_audit("memory_heartbeat")
@with_memory_connection
def memory_heartbeat(conn, dry_run: bool = False) -> str:
    """Run a heartbeat: re-evaluate all notes for importance, tier assignment, and archival.

    Computes importance from access patterns, success scores, and recency.
    Moves notes between hot/warm/cold tiers. Archives low-importance old notes.
    Requires MEMORY_SELF_DIRECTED=1.
    """
    from self_directed import SELF_DIRECTED_ENABLED, run_heartbeat as _heartbeat

    if not SELF_DIRECTED_ENABLED:
        return "Self-directed memory disabled. Set MEMORY_SELF_DIRECTED=1 to enable."
    try:
        result = _heartbeat(conn, dry_run=dry_run)
        prefix = "[DRY RUN] " if dry_run else ""
        return (
            f"{prefix}Heartbeat complete: {result['evaluated']} evaluated, "
            f"{result['tier_changes']} tier changes, {result['archived']} archived."
        )
    except Exception:
        logger.exception("in memory_heartbeat")
        return _err(ErrorCode.DB_ERROR, "in memory_heartbeat")


@mcp.tool()
@with_audit("memory_health_check")
@with_memory_connection
def memory_health_check(conn) -> str:
    """Unified health-check: returns a JSON dict summarising subsystem state.

    Checks DB availability, row counts, vec-index drift, FTS sync status,
    connection-pool depth, background-worker liveness, and disk space.
    """
    import shutil
    from pathlib import Path

    from mcp_common import get_memory_paths
    from infra.memory_common import connection_pool

    status: dict = {"db": {}, "vec_index": {}, "fts": {}, "pool": {}, "disk": {}}

    try:
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        row_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        status["db"] = {
            "path": str(db_path),
            "accessible": True,
            "row_count": row_count,
        }
        conn.rollback()
    except Exception as exc:
        status["db"] = {"accessible": False, "error": str(exc)[:200]}

    try:
        n_memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()[0]
        n_vec = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()[0]
        drift = n_memories - n_vec
        status["vec_index"] = {
            "memories": n_memories,
            "vec_keys": n_vec,
            "drift": max(drift, 0),
        }
    except Exception as exc:
        status["vec_index"] = {"error": str(exc)[:200]}

    try:
        fts_count = conn.execute(
            "SELECT COUNT(*) FROM memories_fts"
        ).fetchone()[0]
        status["fts"] = {"row_count": fts_count or 0}
    except Exception as exc:
        status["fts"] = {"error": str(exc)[:200]}

    try:
        pool_state = connection_pool.get_state()
        status["pool"] = {
            k: pool_state.get(k)
            for k in ("active", "idle", "max_size", "timeout")
            if k in pool_state
        }
    except Exception as exc:
        status["pool"] = {"error": str(exc)[:200]}

    try:
        _, mem_dir, _ = get_memory_paths()
        worker_log = Path(mem_dir) / "worker.log"
        worker_alive = worker_log.exists() and (
            worker_log.stat().st_mtime > __import__("time").time() - 600
        )
        status["worker"] = {"alive": worker_alive}
    except Exception as exc:
        status["worker"] = {"error": str(exc)[:200]}

    try:
        _, mem_dir, _ = get_memory_paths()
        usage = shutil.disk_usage(str(mem_dir))
        status["disk"] = {
            "total_gb": round(usage.total / 1e9, 2),
            "free_gb": round(usage.free / 1e9, 2),
            "pct_used": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        }
    except Exception as exc:
        status["disk"] = {"error": str(exc)[:200]}

    degraded = bool(
        status["db"].get("accessible") is False
        or status["vec_index"].get("drift", 0) > 50
        or status.get("disk", {}).get("pct_used", 0) > 95
    )
    status["overall"] = "degraded" if degraded else "healthy"
    return json.dumps(status)


@mcp.tool()
@with_audit("memory_incremental_update")
def memory_incremental_update(
    memory_id: str, new_content: str, old_state: Optional[list] = None
) -> str:
    """[DEPRECATED] Compute an SSM-encoder update for a memory whose content changed.

    This tool is deprecated. SSM v1 has been removed as a dead end; the new Temporal SSM v2 lives in search/scoring.py and runs automatically during search.
    """
    return "memory_incremental_update is deprecated. SSM v1 has been removed as a dead end; the new Temporal SSM v2 lives in search/scoring.py and runs automatically during search."





@mcp.tool()
@with_audit("memory_check_embedding_model")
def memory_check_embedding_model(force: bool = False, dry_run: bool = False) -> str:
    """Detect embedding model drift, auto-rebuild the vec index if the model changed.

    Compares the current embedding model config against the stored vec
    index metadata. If the model has changed (dimensions, model name, or
    api_base), triggers a full vec index rebuild.

    Args:
        force: Force a rebuild regardless of whether the model has changed.
        dry_run: Show what would be done without making changes.
    """
    try:
        from infra.embedding_recompute import check_and_rebuild

        stats = check_and_rebuild(force=force, dry_run=dry_run)
        return json.dumps(stats)
    except Exception as e:
        logger.exception("in memory_check_embedding_model")
        return _err(ErrorCode.DB_ERROR, f"in memory_check_embedding_model: {e}")


@mcp.tool()
@with_audit("memory_run_tier_migration")
def memory_run_tier_migration(dry_run: bool = False) -> str:
    """Run tier migration lifecycle: consolidate warm sessions, archive cold files.

    Hot tier:  <7 days, full-content files, indexed at full resolution.
    Warm tier: 7-90 days, session logs consolidated into lessons/ summaries.
    Cold tier: >90 days, archived to gzip bundles, replaced with stubs.

    Pinned files are protected. Superseded notes are also pruned to bundles.
    Requires MEMORY_TEMPORAL_TIERS=1 (or the feature is on by default).
    """
    import io as _io
    import contextlib as _contextlib

    script = GLOBAL_SCRIPTS_DIR / "tier_migration.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"tier_migration.py not found at {script}")
    active = _resolve_memory_dir()
    cmd = [sys.executable, str(script)]
    if dry_run:
        cmd.append("--dry-run")
    buf = _io.StringIO()
    try:
        with _contextlib.redirect_stdout(buf):
            from tier_migration import run_tier_migration, prune_superseded

            run_tier_migration(active, dry_run=dry_run)
            prune_stats = prune_superseded(active, dry_run=dry_run)
        out = buf.getvalue()
        prefix = "[DRY RUN] " if dry_run else ""
        return (
            f"{prefix}Tier migration complete.\n"
            f"{out}\n"
            f"Prune superseded: pruned={prune_stats.get('pruned', 0)} "
            f"skipped={prune_stats.get('skipped', 0)}"
        )
    except Exception as e:
        logger.exception("in memory_run_tier_migration")
        return _err(ErrorCode.DB_ERROR, f"in memory_run_tier_migration: {e}")


@mcp.tool()
@with_audit("memory_tier_stats")
@with_memory_connection
def memory_tier_stats(conn) -> str:
    """Return tier distribution and importance statistics for all memories."""
    from self_directed import SELF_DIRECTED_ENABLED, tier_stats as _tier_stats

    if not SELF_DIRECTED_ENABLED:
        return "Self-directed memory disabled. Set MEMORY_SELF_DIRECTED=1 to enable."
    try:
        stats = _tier_stats(conn)
        lines = [
            "**Tier Statistics**",
            f"  Total: {stats.get('total', 0)}",
            f"  Pinned: {stats.get('pinned', 0)}",
        ]
        for tier, info in stats.get("tiers", {}).items():
            lines.append(
                f"  {tier}: {info['count']} notes (avg importance={info['avg_importance']:.3f})"
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_tier_stats")
        return _err(ErrorCode.DB_ERROR, "in memory_tier_stats")


@mcp.tool()
@with_audit("memory_duplicates")
@with_memory_connection
def memory_duplicates(conn, threshold: float = 0.85) -> str:
    """Find near-duplicate notes using content similarity.

    Returns pairs of notes with Jaccard similarity above threshold.
    Requires MEMORY_CONSOLIDATION=1.

    G2 fix (2026-06-22): the default 0.85 is intentionally lower
    than ``memory_merge_suggestions`` (default 0.90).  A *duplicate*
    is anything that looks like it could be a copy (a wider net, so
    the operator can decide), while a *merge suggestion* is a
    recommendation to actually collapse the pair (a narrower net,
    because merging loses information).  The two thresholds are
    documented in the docstring rather than aligned to a single
    number, because the underlying consolidation logic in
    ``consolidation.py`` uses the same value for both gating steps
    in the same pipeline.
    """
    from consolidation import CONSOLIDATION_ENABLED, detect_duplicates as _detect

    if not CONSOLIDATION_ENABLED:
        return "Consolidation disabled. Set MEMORY_CONSOLIDATION=1 to enable."
    try:
        dupes = _detect(conn, threshold=threshold)
        if not dupes:
            return "No duplicates found."
        lines = [f"**Duplicates** ({len(dupes)} pairs, threshold={threshold}):"]
        for d in dupes[:20]:
            lines.append(
                f"  {d['id_a']} <-> {d['id_b']} (sim={d['similarity']:.3f}, {d['type']})"
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_duplicates")
        return _err(ErrorCode.DB_ERROR, "in memory_duplicates")


@mcp.tool()
@with_audit("memory_merge_suggestions")
@with_memory_connection
def memory_merge_suggestions(conn, threshold: float = 0.90) -> str:
    """Suggest merges for near-duplicate notes.

    Recommends which note to keep based on access count and pin status.
    Requires MEMORY_CONSOLIDATION=1.

    G2 fix (2026-06-22): see the docstring of ``memory_duplicates``
    for the rationale behind the 0.85 vs 0.90 split.
    """
    from consolidation import CONSOLIDATION_ENABLED, merge_suggestions as _suggest

    if not CONSOLIDATION_ENABLED:
        return "Consolidation disabled. Set MEMORY_CONSOLIDATION=1 to enable."
    try:
        suggestions = _suggest(conn, duplicate_threshold=threshold)
        if not suggestions:
            return "No merge suggestions."
        lines = [f"**Merge Suggestions** ({len(suggestions)}):"]
        for s in suggestions[:20]:
            lines.append(
                f"  KEEP: {s['keep']}  MERGE: {s['merge']}  (sim={s['similarity']:.3f})"
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_merge_suggestions")
        return _err(ErrorCode.DB_ERROR, "in memory_merge_suggestions")


@mcp.tool()
@with_audit("memory_consolidate")
def memory_consolidate() -> str:
    """System 2 consolidation: dedup via SHA256 + n-gram Jaccard, detect contradictions, write compaction-proposal.md."""
    script = GLOBAL_SCRIPTS_DIR / "consolidate_facts.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"consolidate_facts.py not found at {script}")
    active = _resolve_memory_dir()
    out, _ = _run_subprocess_output(
        [sys.executable, str(script)], timeout=120, cwd=str(active)
    )
    clear_all_caches()
    return out


@mcp.tool()
@with_audit("memory_rewrite_links")
def memory_rewrite_links() -> str:
    """Scan all notes for wiki-style links that don't resolve, find the closest existing note, and rewrite."""
    script = GLOBAL_SCRIPTS_DIR / "rewrite_links.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"rewrite_links.py not found at {script}")
    active = _resolve_memory_dir()
    out, _ = _run_subprocess_output(
        [sys.executable, str(script)], timeout=60, cwd=str(active)
    )
    clear_all_caches()
    return out


@mcp.tool()
@with_audit("memory_detect_contradictions")
def memory_detect_contradictions(
    min_confidence: str = "low", mode: str = "both", semantic_threshold: float = 0.65
) -> str:
    """Run the contradiction detector over the corpus."""
    if min_confidence not in ("low", "medium", "high"):
        return _err(
            ErrorCode.INVALID_PARAMS,
            f"min_confidence must be 'low', 'medium', or 'high' (got {min_confidence!r})",
        )
    if mode not in ("phrase", "semantic", "both"):
        return _err(
            ErrorCode.INVALID_PARAMS,
            f"mode must be 'phrase', 'semantic', or 'both' (got {mode!r})",
        )
    script = GLOBAL_SCRIPTS_DIR / "contradiction_detector.py"
    if not script.exists():
        return _err(
            ErrorCode.NOT_FOUND, f"contradiction_detector.py not found at {script}"
        )
    active = _resolve_memory_dir()
    out, _ = _run_subprocess_output(
        [
            sys.executable,
            str(script),
            str(active),
            f"--min-confidence={min_confidence}",
            f"--mode={mode}",
            f"--semantic-threshold={semantic_threshold}",
        ],
        timeout=60,
    )
    return out


@mcp.tool()
@with_audit("memory_arc_stats")
@with_memory_connection
def memory_arc_stats(conn) -> str:
    """Return ARC ghost-entry statistics, read live from arc_ghosts/arc_stats.

    P0 fix #4: the original implementation shelled out to arc_cache.py
    which just printed stats and never read the live DB. This version
    opens an ARCCache against the active memory dir and reports what is
    actually in the table — eviction pressure, ghost hit rate, total
    ghosts, lifetime eviction count, and the most-recent
    eviction/recent timestamps.
    """
    from infra.arc_cache import ARCCache

    try:
        db_path = Path(str(_resolve_memory_dir())) / "memory.db"
        if not db_path.exists():
            return f"No memory.db found at {db_path}; ARC stats unavailable."
        # Refresh the eviction_pressure / ghost_hit_rate / total_ghosts
        # keys so the table is consistent before we read it.
        try:
            cache = ARCCache(db_path)
            try:
                cache.compute_eviction_pressure()
                stats = cache.get_stats()
            finally:
                cache.close()
        except Exception as e:
            return _err(ErrorCode.DB_ERROR, f"ARCCache read failed: {e}")
        # Backwards-compat: also surface `arc_cache.py` CLI output so
        # anything that depended on the old text format still works.
        active = _resolve_memory_dir()
        try:
            cli_out, _ = _run_subprocess_output(
                [sys.executable, str(GLOBAL_SCRIPTS_DIR / "arc_cache.py")],
                timeout=10,
                cwd=str(active),
            )
        except (subprocess.TimeoutExpired, Exception):
            cli_out = ""
        lines = ["**ARC Cache Stats (live)**"]
        lines.append(f"  Ghost entries:      {int(stats.get('ghost_count', 0))}")
        lines.append(
            f"  Eviction pressure:  {float(stats.get('eviction_pressure', 0.5)):.2f}"
        )
        lines.append(
            f"  Ghost hit rate:     {float(stats.get('ghost_hit_rate', 0.0)):.2%}"
        )
        if "recent_total" in stats:
            lines.append(f"  Recent lookups:     {int(stats['recent_total'])}")
        if "eviction_total" in stats:
            lines.append(f"  Lifetime evictions: {int(stats['eviction_total'])}")
        if "last_eviction_at" in stats:
            import datetime as _dt

            ts = _dt.datetime.fromtimestamp(float(stats["last_eviction_at"])).isoformat(
                timespec="seconds"
            )
            lines.append(f"  Last eviction at:   {ts}")
        if "last_recent_at" in stats:
            import datetime as _dt

            ts = _dt.datetime.fromtimestamp(float(stats["last_recent_at"])).isoformat(
                timespec="seconds"
            )
            lines.append(f"  Last recent at:     {ts}")
        if cli_out:
            lines.append("")
            lines.append("--- arc_cache.py CLI output ---")
            lines.append(cli_out)
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_arc_stats")
        return _err(ErrorCode.DB_ERROR, "in memory_arc_stats")


@mcp.tool()
@with_audit("memory_arc_reset")
@with_memory_connection
def memory_arc_reset(conn) -> str:
    """Clear the ARC cache: drop all ghost entries and reset stats.

    P0 fix #4: backing tool for the new wire-up. Useful after a
    memory.db migration or a config change so the operator can start
    the eviction-pressure signal from a clean slate without dropping
    the schema.
    """
    from infra.arc_cache import ARCCache

    try:
        db_path = Path(str(_resolve_memory_dir())) / "memory.db"
        if not db_path.exists():
            return _err(ErrorCode.NOT_FOUND, f"No memory.db found at {db_path}")
        cache = ARCCache(db_path)
        try:
            result = cache.reset()
        finally:
            cache.close()
        return (
            f"ARC cache reset: {result['ghosts_deleted']} ghost entries "
            f"and {result['stats_deleted']} stat rows deleted."
        )
    except Exception:
        logger.exception("in memory_arc_reset")
        return _err(ErrorCode.DB_ERROR, "in memory_arc_reset")


@mcp.tool()
@with_audit("memory_extract_skills")
@with_memory_connection
def memory_extract_skills(
    conn,
    memory_id: str = "",
    dry_run: bool = False,
) -> str:
    """Manually trigger skill extraction.

    P0 fix #5: lets the operator re-run the lower-threshold extractor
    on a specific memory (``memory_id="lessons/foo"``) or on every
    memory when ``memory_id`` is empty. Uses the cron implementation
    so the same code path runs in both places.

    Args:
        memory_id: when non-empty, extract a skill from just this
            single memory. When empty, run the full extraction pass
            (same as cron_skill_extraction.py).
        dry_run: when True, count what would be extracted without
            writing to the DB.
    """
    try:
        if memory_id:
            row = conn.execute(
                "SELECT id, content, category FROM memories "
                "WHERE id = ? AND deleted_at IS NULL",
                (memory_id,),
            ).fetchone()
            if row is None:
                return _err(
                    ErrorCode.NOT_FOUND,
                    f"Memory '{memory_id}' not found or is deleted.",
                )
            from skill_extractor import (
                ensure_skill_schema,
                extract_skill_from_memory,
                save_skill,
                is_skill_worthy,
            )

            ensure_skill_schema(conn)
            # Access by index so the function works regardless of
            # whether the connection has row_factory=sqlite3.Row set.
            mid = row[0]
            content = row[1]
            cat = row[2] or (mid.split("/", 1)[0] if "/" in mid else "")
            if not is_skill_worthy(content, category=cat):
                return (
                    f"Memory '{memory_id}' (category={cat!r}) is not skill-worthy "
                    f"under the current threshold."
                )
            if dry_run:
                return f"[DRY RUN] Memory '{memory_id}' would be extracted as a skill."
            skill = extract_skill_from_memory(mid, content, category=cat)
            if skill is None:
                return _err(
                    ErrorCode.QUALITY_ERROR,
                    f"Extraction returned None for '{memory_id}'.",
                )
            skill_id = save_skill(conn, skill)
            conn.commit()
            return (
                f"Extracted skill #{skill_id} '{skill['name']}' from "
                f"'{memory_id}' (category={cat!r})."
            )
        # Full sweep: defer to the cron implementation.
        try:
            from cron import cron_skill_extraction
        except ImportError:
            from cron_skill_extraction import run_extraction as _run
        else:
            _run = cron_skill_extraction.run_extraction
        result = _run(conn, dry_run=dry_run)
        prefix = "[DRY RUN] " if dry_run else ""
        return (
            f"{prefix}Skill extraction complete: scanned={result['scanned']} "
            f"extracted={result['extracted']} deduplicated={result['deduplicated']} "
            f"updated={result['updated']} skipped={result['skipped']}"
        )
    except Exception:
        logger.exception("in memory_extract_skills")
        return _err(ErrorCode.DB_ERROR, "in memory_extract_skills")


@mcp.tool()
@with_audit("memory_list_skills")
@with_memory_connection
def memory_list_skills(conn, limit: int = 50) -> str:
    """List extracted skills, ordered by hit_count desc.

    P0 fix #5: gives the operator a way to inspect what the
    lower-threshold extractor actually pulled in. The list includes
    the topic, hit count, last-used timestamp, and a preview of the
    description so it's easy to spot good vs bad extractions.
    """
    try:
        from skill_extractor import ensure_skill_schema, list_skills

        ensure_skill_schema(conn)
        skills = list_skills(conn, limit=max(1, min(int(limit or 50), 500)))
        if not skills:
            return "No skills extracted yet. Run `memory_extract_skills` to populate."
        lines = [f"**Extracted Skills** ({len(skills)})"]
        for s in skills:
            ts = ""
            if s.get("last_used_at"):
                import datetime as _dt

                ts = _dt.datetime.fromtimestamp(float(s["last_used_at"])).isoformat(
                    timespec="seconds"
                )
            lines.append(
                f"  [{s['id']}] {s['name']}  hits={s['hit_count']}  "
                f"last_used={ts or 'never'}"
            )
            if s.get("description"):
                lines.append(f"      {s['description'][:120]}")
        return "\n".join(lines)
    except Exception:
        logger.exception("in memory_list_skills")
        return _err(ErrorCode.DB_ERROR, "in memory_list_skills")


@mcp.tool()
@with_audit("memory_review_schedule")
def memory_review_schedule() -> str:
    """Return SM-2 spaced-repetition stats."""
    script = GLOBAL_SCRIPTS_DIR / "spaced_repetition.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, "spaced_repetition.py not found.")
    active = _resolve_memory_dir()
    try:
        out, _ = _run_subprocess_output(
            [sys.executable, str(script)], timeout=10, cwd=str(active)
        )
        return out
    except subprocess.TimeoutExpired:
        return _err(ErrorCode.TIMEOUT, "spaced_repetition.py timed out after 10s.")


import sys as _sys


@mcp.tool()
@with_audit("memory_pinned_decay_check")
def memory_pinned_decay_check(dry_run: bool = True) -> str:
    """Check pinned notes for drift and auto-unpin stale ones."""
    script = GLOBAL_SCRIPTS_DIR / "pinned_decay.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"pinned_decay.py not found at {script}.")
    try:
        cmd = [_sys.executable, str(script)]
        if not dry_run:
            cmd.append("--auto-apply")
        out, _ = _run_subprocess_output(cmd, timeout=10, cwd=str(GLOBAL_SCRIPTS_DIR))
        if out.startswith("[stderr]") or "[stderr]" in out[:50]:
            return f"pinned_decay stderr: {out[:300]}"
        if not dry_run:
            clear_all_caches()
        return out
    except subprocess.TimeoutExpired:
        return _err(ErrorCode.TIMEOUT, "pinned_decay.py timed out after 10s.")


@mcp.tool()
@with_audit("memory_compile_skill")
def memory_compile_skill(
    lesson_slug: str,
    skill_name: str,
    primary_triggers: list,
    secondary_triggers: Optional[list] = None,
) -> str:
    """Compile a lesson note into a validated executable agent skill rule file in ~/.agents/skills/."""
    for val, lbl in ((lesson_slug, "lesson_slug"), (skill_name, "skill_name")):
        err = _validate_slug(val, lbl)
        if err:
            return _err(ErrorCode.INVALID_PARAMS, err)
    active_dir = _resolve_memory_dir()
    if os.environ.get("MEMORY_DB_PATH"):
        local_mem = active_dir
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        cwd, local_mem, global_mem = get_memory_paths()
    lesson_file = local_mem / "lessons" / f"{lesson_slug}.md"
    if not lesson_file.exists():
        lesson_file = global_mem / "lessons" / f"{lesson_slug}.md"
    if not lesson_file.exists():
        return _err(
            ErrorCode.NOT_FOUND, f"Lesson memory note '{lesson_slug}' does not exist."
        )
    try:
        content = lesson_file.read_text(encoding="utf-8")
        metadata, body = parse_frontmatter(content)
        code_blocks = re.findall(r"```(\w+)\r?\n(.*?)\r?\n```", body, re.DOTALL)
        for lang, code in code_blocks:
            if lang.lower() in ("python", "py"):
                try:
                    compile(code, "<string>", "exec")
                except SyntaxError as se:
                    return _err(
                        ErrorCode.INVALID_PARAMS,
                        f"Python syntax error in lesson code blocks: {se}",
                    )
        import yaml

        skill_metadata = {
            "name": skill_name,
            "description": metadata.get(
                "description", f"Executable skill compiled from lesson: {lesson_slug}"
            ),
            "when_to_use": f"Use when working with topics related to: {', '.join(primary_triggers)}",
            "disable-model-invocation": True,
            "triggers": {
                "keywords": {
                    "primary": primary_triggers,
                    "secondary": secondary_triggers if secondary_triggers else [],
                }
            },
        }
        yaml_header = yaml.dump(skill_metadata, sort_keys=False)
        skill_content = f"""---
{yaml_header.strip()}
---

# Skill: {skill_name.replace("-", " ").title()}

{body.strip()}
"""
        skills_dir = Path.home() / ".agents" / "skills" / skill_name
        skills_dir.mkdir(parents=True, exist_ok=True)
        skill_file_path = skills_dir / "SKILL.md"
        atomic_write(skill_file_path, skill_content, encoding="utf-8")
        from mcp_common import recompile_skills_catalog

        recompile_skills_catalog()
        return f"Successfully compiled and validated skill: {skill_name} at ~/.agents/skills/{skill_name}/SKILL.md (Skills index updated)."
    except Exception:
        logger.exception("compiling skill")
        return _err(ErrorCode.DB_ERROR, "compiling skill")


# ---------------------------------------------------------------------------
# Maintenance operation dispatch
# ---------------------------------------------------------------------------
# The ``memory_maintenance`` tool is a single entry point for ~40 admin
# operations. To keep the router type-safe and avoid a 200-line if/elif
# chain, operations are declared in a single ``MaintenanceOp`` enum and
# dispatched via a handler table. Each handler is a small function that
# accepts the router's flat params as kwargs and returns the result.
#
# To add a new operation:
#   1. Add the value to ``MaintenanceOp``
#   2. Add the corresponding ``_op_<name>`` handler below
#   3. Register it in ``_MAINTENANCE_HANDLERS``


class MaintenanceOp(str, Enum):
    HEARTBEAT = "heartbeat"
    TIER_STATS = "tier_stats"
    TIER_MIGRATION = "tier_migration"
    EMBEDDING_MODEL_CHECK = "embedding_model_check"
    INCREMENTAL_UPDATE = "incremental_update"
    DUPLICATES = "duplicates"
    MERGE_SUGGESTIONS = "merge_suggestions"
    REBUILD = "rebuild"
    AUDIT = "audit"
    AUDIT_QUERY = "audit_query"
    CONSOLIDATE = "consolidate"
    REWRITE_LINKS = "rewrite_links"
    DETECT_CONTRADICTIONS = "detect_contradictions"
    COMPACT = "compact"
    ARC_STATS = "arc_stats"
    ARC_RESET = "arc_reset"
    REVIEW_SCHEDULE = "review_schedule"
    PINNED_DECAY = "pinned_decay"
    CHECK_INTEGRITY = "check_integrity"
    COMPILE_SKILL = "compile_skill"
    BACKFILL_ALL = "backfill_all"
    CRDT_SYNC = "crdt_sync"
    CRDT_STATUS = "crdt_status"
    OKF_EXPORT = "okf_export"
    OKF_IMPORT = "okf_import"
    REINFORCE = "reinforce"
    TRASH = "trash"
    PURGE_EXPIRED = "purge_expired"
    AUTO_SAVE_HOOK = "auto_save_hook"
    AUTO_SAVE_STATUS = "auto_save_status"
    AUTO_SAVE_DAEMON_METRICS = "auto_save_daemon_metrics"
    PURGE_AUTO_SAVES = "purge_auto_saves"
    DAILY_DIGEST = "daily_digest"
    SHARE = "share"
    SHARED_LIST = "shared_list"
    SHARED_IMPORT = "shared_import"
    SHARED_STATS = "shared_stats"
    RECORD_CTR_FEEDBACK = "record_ctr_feedback"
    CHECK_CONCEPT_DRIFT = "check_concept_drift"
    LIST_DRIFT_ALARMS = "list_drift_alarms"  # 2026-06-22: v15
    QUALITY_FILTER = "quality_filter"
    QUALITY_STATS = "quality_stats"
    SUMMARIZE = "summarize"
    AUTO_SUMMARIZE = "auto_summarize"
    SUMMARIZATION_STATS = "summarization_stats"
    FACTS_LIST = "facts_list"
    FACTS_STATS = "facts_stats"
    GRAPH_STATS = "graph_stats"
    PROFILE_STATS = "profile_stats"
    LLM_UNLOAD = "llm_unload"
    ADAPTIVE_RETENTION = "adaptive_retention"
    RETENTION_STATS = "retention_stats"
    INGEST_FILE = "ingest_file"
    INGEST_URL = "ingest_url"
    DASHBOARD = "dashboard"
    METRICS_SERVER = "metrics_server"
    CIRCUIT_BREAKER_STATUS = "circuit_breaker_status"  # 2026-06-22 follow-up
    TEMPORAL_CONTRADICTIONS = (
        "temporal_contradictions"  # T3.6: list fact-level supersession events
    )
    TEMPORAL_QUERY = "temporal_query"  # T4.5: at_time / chain / changed_since
    COMPLIANCE_CHECK = "compliance_check"  # P1: AGENTS.md rule compliance audit
    SESSION_STATS = "session_stats"  # Sprint 7
    THREAD_STATS = "thread_stats"  # Sprint 7
    COMPACTION_STATS = "compaction_stats"  # Sprint 7
    LIST_ACTIVE_THREADS = "list_active_threads"  # Sprint 7
    RECOVER_SESSION = "recover_session"  # Sprint 7
    AGENT_INIT = "agent_init"
    AGENT_CLEAR = "agent_clear"
    AGENT_LIST = "agent_list"
    EXTRACT_SKILLS = "extract_skills"
    LIST_SKILLS = "list_skills"
    AUTO_SHARE = "auto_share"
    GRAPH_SHORTEST_PATH = "graph_shortest_path"
    GRAPH_TRAVERSE = "graph_traverse"
    RECONCILE_AUDIT = "reconcile_audit"
    TRAIN_FORGET_MODEL = "train_forget_model"
    SEMANTIC_SEARCH = "semantic_search"
    FACTS_SEARCH = "facts_search"
    GRAPH_SEARCH = "graph_search"
    RECALL_CONTEXT = "recall_context"
    THREAD_CONTEXT = "thread_context"
    LIST_THREADS = "list_threads"
    RESOLVE_THREAD = "resolve_thread"
    USER_PROFILE = "user_profile"
    CHECK_CONTRADICTIONS = "check_contradictions"
    SCAN_INJECTION = "scan_injection"
    PROFILE_ACCESS = "profile_access"
    FLAGS_STATUS = "flags_status"
    PHASE_ERRORS = "phase_errors"

    @classmethod
    def all_values(cls) -> list[str]:
        return [m.value for m in cls]


# Per-operation dispatchers (the _op_* functions and MAINTENANCE_HANDLERS
# dict) live in mcp_maintenance_ops.py to keep this file focused on the
# high-level @mcp.tool() definitions and the router below.
from mcp_maintenance_ops import MAINTENANCE_HANDLERS  # noqa: E402


@mcp.tool()
@with_audit("memory_maintenance")
def memory_maintenance(
    operation: str,
    **kwargs: Any,
) -> str:
    """Run an administrative or maintenance operation on the memory system.

    USE THIS TOOL WHEN:
    - You need to perform system administration, diagnostics, index compacting, sync, or custom statistics checks.
    - This is the single entry point for all administrative, routing, and specialized metadata operations.

    ARGUMENTS:
    - operation: The name of the operation to run (case-insensitive).
    - kwargs: Key-value arguments specific to the chosen operation.

    **List of supported operations:**

    **Common operations (no extra params):**
      ``tier_stats``, ``audit``, ``consolidate``, ``rewrite_links``,
      ``arc_stats``, ``arc_reset``, ``review_schedule``, ``quality_stats``, ``facts_stats``,
      ``graph_stats``, ``profile_stats``, ``retention_stats``,
      ``summarization_stats``, ``auto_save_status``, ``shared_stats``,
      ``flags_status``, ``phase_errors``

    **Operations with params:**
      ``heartbeat``         dry_run
      ``duplicates``        threshold
      ``merge_suggestions`` threshold
      ``rebuild``           scope
      ``compact``           dry_run
      ``check_integrity``   deep
      ``pinned_decay``      dry_run
      ``backfill_all``      backfill_mode, source
      ``detect_contradictions``  min_confidence, contradiction_mode, semantic_threshold
      ``audit_query``       tool_name, since_ts, until_ts, only_errors, limit, offset
      ``compile_skill``     lesson_slug, skill_name, primary_triggers, secondary_triggers
      ``crdt_sync``         agent_id, remote_notes_json
      ``crdt_status``       (no extra params)
      ``reinforce``         memory_ids, success
      ``adaptive_retention`` dry_run
      ``auto_summarize``    min_length, dry_run
      ``daily_digest``      date
      ``trash``             include_expired
      ``purge_expired``     (none)
      ``share``             share_note_id, share_agent_id
      ``shared_list``       share_agent_id (reused), shared_category, shared_limit
      ``shared_import``     shared_id, target_agent_id
      ``quality_filter``    query, quality_limit
      ``summarize``         note_id
      ``facts_list``        facts_limit, facts_min_confidence
       ``record_ctr_feedback`` ctr_id, query_id, ctr_action, ctr_source
       ``check_concept_drift`` threshold
        ``list_drift_alarms`` acknowledged, alarm_level, limit, acknowledge_ids, acknowledged_by, notes
        ``auto_save_hook``    auto_save_tool, auto_save_params_json, auto_save_result_preview
        ``okf_export``        output_dir, include_deleted, overwrite
        ``okf_import``        input_dir, is_global, dry_run, overwrite
        ``ingest_file``       file_path, category, tags
        ``ingest_url``        url, category, tags
        ``dashboard``         (no extra params)
        ``metrics_server``    action, port
        ``tier_migration``    dry_run
        ``embedding_model_check``  force, dry_run
        ``incremental_update`` memory_id, new_content, old_state
        ``llm_unload``         (no extra params)
        ``circuit_breaker_status``  limit, since_ts
        ``temporal_contradictions``  since_ts, until_ts, reason, limit, offset
        ``temporal_query``     as_of, fact_id, since_ts, query, limit
        ``compliance_check``   session_id
        ``session_stats``      (no extra params)
        ``thread_stats``       (no extra params)
        ``compaction_stats``   (no extra params)
        ``list_active_threads``  project_root, status, limit
        ``recover_session``    session_id
        ``agent_init``         agent_id, display_name, parent_agent, namespace
        ``agent_clear``        (no extra params)
        ``agent_list``         (no extra params)
        ``extract_skills``     memory_id, dry_run
        ``list_skills``        limit
        ``auto_share``         agent_id, min_importance, min_fitness, limit, dry_run
        ``graph_shortest_path``  source, target, max_depth
        ``graph_traverse``     start, edge_patterns

    Per-operation validation is delegated to the handler — each
    operation extracts only the kwargs it needs. Unknown kwargs are
    silently ignored.
    """
    unknown = {
        k: f"<{type(v).__name__}>" for k, v in kwargs.items() if not k.startswith("_")
    }
    op = operation.lower().replace("-", "_")
    try:
        op_enum = MaintenanceOp(op)
    except ValueError:
        hint = f" (got unexpected keys: {unknown})" if unknown else ""
        return _err(
            ErrorCode.INVALID_PARAMS,
            f"Unknown maintenance operation '{op}'. Known: {', '.join(sorted(MaintenanceOp.all_values()))}{hint}",
        )
    handler = MAINTENANCE_HANDLERS[op_enum]
    raw = handler(**kwargs)
    return str(raw) if not isinstance(raw, str) else raw


@mcp.tool()
@with_audit("memory_llm_unload")
def memory_llm_unload() -> str:
    """Unload the LLM extraction model from memory, freeing GPU/MPS resources.

    The model will auto-reload on the next save that triggers fact/entity
    extraction. Use to free memory or cool down your machine.
    """
    try:
        from llm_extraction import _extractor

        if _extractor is None:
            return json.dumps({"unloaded": False, "message": "Model was not loaded."})

        was_loaded = _extractor.is_loaded
        _extractor.unload()
        return json.dumps(
            {
                "unloaded": was_loaded,
                "message": (
                    "LLM extraction model unloaded from memory."
                    if was_loaded
                    else "Model was not loaded."
                ),
            }
        )
    except Exception as e:
        return json.dumps({"unloaded": False, "error": str(e)})
