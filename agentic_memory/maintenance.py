"""Maintenance SDK — rebuild, compact, audit, heartbeat, etc."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_memory.exceptions import MaintenanceError
from agentic_memory.models import IntegrityReport, MaintenanceResult
from agentic_memory.utils import (
    resolve_db_path,
    safe_close_db,
    get_db_connection,
)
from infra.db_write_queue import sqlite_write_queue


def _safe_json_parse(raw: str) -> Any:
    """Try to parse JSON from script output.

    Handles leading/trailing non-JSON text by finding the first
    ``[`` or ``{`` character.
    """
    if not raw or not isinstance(raw, str):
        return raw
    for i, ch in enumerate(raw):
        if ch in ("[", "{"):
            try:
                return json.loads(raw[i:])
            except (json.JSONDecodeError, ValueError):
                break
    return raw


class Maintenance:
    """High-level maintenance operations.

    Wraps the admin/maintenance MCP tool functions into a typed Python API.
    All methods handle errors gracefully and return typed dataclasses or dicts.

    Args:
        db_path: Path to the memory database. If None, resolved from
            environment or config.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = resolve_db_path(db_path)
        self._memory_dir = self.db_path.parent

    # ------------------------------------------------------------------
    # Rebuild
    # ------------------------------------------------------------------

    def rebuild(self, scope: str = "active") -> MaintenanceResult:
        """Rebuild the FTS5 index.

        Args:
            scope: One of ``"active"``, ``"local"``, or ``"global"``.
        """
        from config import GLOBAL_SCRIPTS_DIR

        try:
            rebuild_script = GLOBAL_SCRIPTS_DIR / "rebuild_index.py"
            if not rebuild_script.exists():
                return MaintenanceResult(
                    operation="rebuild",
                    success=False,
                    message=f"rebuild_index.py not found at {rebuild_script}",
                )
            result = subprocess.run(
                [
                    sys.executable,
                    str(rebuild_script),
                    str(self._memory_dir),
                    str(self.db_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr
            if result.returncode != 0:
                return MaintenanceResult(
                    operation="rebuild",
                    success=False,
                    message=f"Rebuild script exited {result.returncode}:\n{output}",
                )
            from infra.cache import clear_all_caches

            clear_all_caches()
            return MaintenanceResult(
                operation="rebuild",
                success=True,
                message=f"Memory index rebuilt successfully ({scope} scope).",
                details={"scope": scope, "output": output},
            )
        except subprocess.TimeoutExpired:
            return MaintenanceResult(
                operation="rebuild",
                success=False,
                message="Rebuild timed out after 120s",
            )
        except Exception as e:
            return MaintenanceResult(
                operation="rebuild",
                success=False,
                message=str(e),
            )

    # ------------------------------------------------------------------
    # Compact
    # ------------------------------------------------------------------

    def compact(self, dry_run: bool = False) -> MaintenanceResult:
        """Run tier migration + consolidation + rebuild + session archival."""
        from config import GLOBAL_SCRIPTS_DIR

        parts: list[str] = []
        try:
            tier_script = GLOBAL_SCRIPTS_DIR / "tier_migration.py"
            if tier_script.exists():
                cmd = [sys.executable, str(tier_script)]
                if dry_run:
                    cmd.append("--dry-run")
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(self._memory_dir),
                )
                out = r.stdout or ""
                if r.stderr:
                    out += "\n" + r.stderr
                parts.append(f"Tier Migration (dry_run={dry_run}):\n{out}")

            consolidate_script = GLOBAL_SCRIPTS_DIR / "consolidate_facts.py"
            if consolidate_script.exists():
                r = subprocess.run(
                    [sys.executable, str(consolidate_script)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(self._memory_dir),
                )
                out = r.stdout or ""
                if r.stderr:
                    out += "\n" + r.stderr
                parts.append(f"Fact Consolidation:\n{out[:500]}")

            rebuild_script = GLOBAL_SCRIPTS_DIR / "rebuild_index.py"
            if rebuild_script.exists():
                r = subprocess.run(
                    [
                        sys.executable,
                        str(rebuild_script),
                        str(self._memory_dir),
                        str(self.db_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                out = r.stdout or ""
                if r.stderr:
                    out += "\n" + r.stderr
                parts.append(f"Index Rebuild:\n{out}")

            sessions_dir = self._memory_dir / "sessions"
            archive_dir = self._memory_dir / "archive" / "sessions"
            if sessions_dir.exists():
                if not dry_run:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    archived = 0
                    for f in sessions_dir.glob("*.md"):
                        if f.stat().st_mtime < (time.time() - 14 * 86400):
                            dst = archive_dir / f.name
                            try:
                                if f.stat().st_dev == dst.parent.stat().st_dev:
                                    os.replace(str(f), str(dst))
                                else:
                                    shutil.move(str(f), str(dst))
                            except OSError as exc:
                                import logging
                                logging.getLogger("maintenance").warning("maintenance: cannot archive session %s: %s", f, exc)
                            archived += 1
                    if archived:
                        parts.append(f"Archived {archived} sessions.")
                else:
                    count = sum(
                        1
                        for f in sessions_dir.glob("*.md")
                        if f.stat().st_mtime < (time.time() - 14 * 86400)
                    )
                    if count:
                        parts.append(
                            f"[DRY RUN] Would archive {count} sessions older than 14 days."
                        )

            if self.db_path.exists():
                try:
                    from infra.memory_common import wal_checkpoint_idle

                    ckpt = wal_checkpoint_idle(self.db_path, wal_size_threshold_mb=1.0)
                    if ckpt.get("status") != "skipped":
                        parts.append(f"WAL Checkpoint:\n{json.dumps(ckpt, indent=2)}")
                except Exception as e:
                    parts.append(f"WAL Checkpoint (error, non-fatal): {e}")

            return MaintenanceResult(
                operation="compact",
                success=True,
                message="\n\n".join(parts),
            )
        except Exception as e:
            return MaintenanceResult(
                operation="compact",
                success=False,
                message=str(e),
            )

    # ------------------------------------------------------------------
    # Check integrity
    # ------------------------------------------------------------------

    def check_integrity(self, deep: bool = False) -> IntegrityReport:
        """Run a health check on the memory DB.

        Args:
            deep: When True, run a more thorough (slower) check.
        """
        try:
            from memory_integrity import check_index_integrity

            report = check_index_integrity(self.db_path, deep=deep)
            ok = bool(report.get("ok", False))
            findings = report.get("findings", [])
            errors = [f["message"] for f in findings if f.get("severity") == "error"]
            warnings = [
                f["message"]
                for f in findings
                if f.get("severity") in ("warn", "warning")
            ]
            if not ok:
                errors.append("Integrity check failed overall.")
            return IntegrityReport(
                passed=ok,
                errors=errors,
                warnings=warnings,
                stats=dict(report),
            )
        except Exception as e:
            return IntegrityReport(
                passed=False,
                errors=[f"Integrity check raised: {e}"],
            )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit(self) -> dict:
        """Audit memory system health using SRMA-inspired metrics.

        Returns a dict with total count, health score, and per-memory metrics.
        """
        conn = get_db_connection(self.db_path)
        try:
            from mcp_common import run_db_migrations

            run_db_migrations(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, content, created_at, updated_at, access_count, pinned FROM memories"
            ).fetchall()
            if not rows:
                return {"total_memories": 0, "metrics": []}

            now = datetime.now(timezone.utc)
            metrics: list[dict[str, Any]] = []
            corrupted_dates = 0
            for row in rows:
                content = row["content"] or ""
                access_count = row["access_count"] or 0
                try:
                    created = datetime.fromisoformat(str(row["created_at"]))
                    updated = datetime.fromisoformat(str(row["updated_at"]))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    corrupted_dates += 1
                    continue
                days_since_creation = max(1.0, (now - created).total_seconds() / 86400)
                days_since_updated = max(0.0, (now - updated).total_seconds() / 86400)
                rho = access_count / days_since_creation
                psi = days_since_updated / max(1, access_count)
                omega = len(content) / max(1, access_count)
                metrics.append(
                    {
                        "id": row["id"],
                        "pinned": bool(row["pinned"]),
                        "access_count": access_count,
                        "rho": rho,
                        "psi": psi,
                        "omega": omega,
                        "content_preview": content[:80]
                        .replace("\n", " ")
                        .replace("\r", ""),
                    }
                )

            n = len(metrics)
            if n == 0:
                return {
                    "total_memories": 0,
                    "metrics": [],
                    "corrupted_dates": corrupted_dates,
                }

            max_rho = max(m["rho"] for m in metrics) or 1.0
            max_psi = max(m["psi"] for m in metrics) or 1.0
            max_omega = max(m["omega"] for m in metrics) or 1.0
            health_scores = []
            for m in metrics:
                n_rho = m["rho"] / max_rho
                n_psi = m["psi"] / max_psi
                n_omega = m["omega"] / max_omega
                health_scores.append((n_rho + (1 - n_psi) + (1 - n_omega)) / 3)
            overall_health = sum(health_scores) / n if n else 0.0

            drifted = sorted(metrics, key=lambda m: m["psi"], reverse=True)[:5]
            efficient = sorted(metrics, key=lambda m: m["omega"])[:5]
            never_accessed = [m for m in metrics if m["access_count"] == 0]

            return {
                "total_memories": n,
                "overall_health": round(overall_health, 3),
                "corrupted_dates": corrupted_dates,
                "drifted": drifted,
                "efficient": efficient,
                "never_accessed": never_accessed,
                "metrics": metrics,
            }
        finally:
            safe_close_db(conn)

    # ------------------------------------------------------------------
    # Generic maintenance operation
    # ------------------------------------------------------------------

    def run(self, operation: str, **kwargs: Any) -> str:
        """Run an admin maintenance operation by name.

        Delegates to the ``memory_maintenance`` MCP dispatch function.
        See ``mcp_maintenance.MaintenanceOp`` for the full list of
        supported operation names.

        Args:
            operation: The operation name (e.g. ``"heartbeat"``,
                ``"duplicates"``, ``"arc_stats"``).
            **kwargs: Per-operation keyword arguments.

        Returns:
            The raw string result from the maintenance handler.
        """
        from mcp_maintenance import memory_maintenance

        try:
            result = memory_maintenance(operation=operation, **kwargs)
            if isinstance(result, str):
                return result
            return str(result)
        except Exception as e:
            raise MaintenanceError(
                f"Maintenance operation '{operation}' failed: {e}"
            ) from e

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self) -> dict:
        """Re-evaluate all notes for importance, tier assignment, and archival.

        Requires MEMORY_SELF_DIRECTED=1.
        """
        from self_directed import SELF_DIRECTED_ENABLED, run_heartbeat

        if not SELF_DIRECTED_ENABLED:
            return {
                "enabled": False,
                "message": "Self-directed memory disabled. Set MEMORY_SELF_DIRECTED=1 to enable.",
            }
        conn = sqlite_write_queue.start_session(Path(self.db_path))
        try:
            result = run_heartbeat(conn, dry_run=False)
            return dict(result)
        except Exception as e:
            raise MaintenanceError(f"Heartbeat failed: {e}") from e
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Tier statistics
    # ------------------------------------------------------------------

    def tier_stats(self) -> dict:
        """Return tier distribution and importance statistics.

        Requires MEMORY_SELF_DIRECTED=1.
        """
        from self_directed import SELF_DIRECTED_ENABLED, tier_stats

        if not SELF_DIRECTED_ENABLED:
            return {"enabled": False}
        conn = get_db_connection(self.db_path)
        try:
            stats = tier_stats(conn)
            return dict(stats)
        except Exception as e:
            raise MaintenanceError(f"Tier stats failed: {e}") from e
        finally:
            safe_close_db(conn)

    # ------------------------------------------------------------------
    # Tier migration
    # ------------------------------------------------------------------

    def run_tier_migration(self) -> str:
        """Run tier migration lifecycle.

        Consolidates warm sessions, archives cold files, prunes superseded notes.
        """
        from tier_migration import run_tier_migration, prune_superseded

        try:
            run_tier_migration(self._memory_dir, dry_run=False)
            prune_stats = prune_superseded(self._memory_dir, dry_run=False)
            return (
                f"Tier migration complete. "
                f"Prune superseded: pruned={prune_stats.get('pruned', 0)} "
                f"skipped={prune_stats.get('skipped', 0)}"
            )
        except Exception as e:
            raise MaintenanceError(f"Tier migration failed: {e}") from e

    # ------------------------------------------------------------------
    # Consolidate
    # ------------------------------------------------------------------

    def consolidate(self) -> MaintenanceResult:
        """Run System 2 consolidation: dedup, detect contradictions, write proposal."""
        from config import GLOBAL_SCRIPTS_DIR

        try:
            script = GLOBAL_SCRIPTS_DIR / "consolidate_facts.py"
            if not script.exists():
                return MaintenanceResult(
                    operation="consolidate",
                    success=False,
                    message=f"consolidate_facts.py not found at {script}",
                )
            r = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self._memory_dir),
            )
            output = r.stdout or ""
            if r.stderr and r.stderr.strip():
                output += "\n[stderr]\n" + r.stderr
            from infra.cache import clear_all_caches

            clear_all_caches()
            return MaintenanceResult(
                operation="consolidate",
                success=r.returncode == 0,
                message=output.strip() or "Consolidation complete.",
                details={"returncode": r.returncode},
            )
        except subprocess.TimeoutExpired:
            return MaintenanceResult(
                operation="consolidate",
                success=False,
                message="Timed out after 120s",
            )
        except Exception as e:
            return MaintenanceResult(
                operation="consolidate", success=False, message=str(e)
            )

    # ------------------------------------------------------------------
    # Rewrite links
    # ------------------------------------------------------------------

    def rewrite_links(self) -> MaintenanceResult:
        """Scan and rewrite broken wiki-style links to the closest existing note."""
        from config import GLOBAL_SCRIPTS_DIR

        try:
            script = GLOBAL_SCRIPTS_DIR / "rewrite_links.py"
            if not script.exists():
                return MaintenanceResult(
                    operation="rewrite_links",
                    success=False,
                    message=f"rewrite_links.py not found at {script}",
                )
            r = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self._memory_dir),
            )
            output = r.stdout or ""
            if r.stderr and r.stderr.strip():
                output += "\n[stderr]\n" + r.stderr
            from infra.cache import clear_all_caches

            clear_all_caches()
            return MaintenanceResult(
                operation="rewrite_links",
                success=r.returncode == 0,
                message=output.strip() or "Link rewrite complete.",
                details={"returncode": r.returncode},
            )
        except subprocess.TimeoutExpired:
            return MaintenanceResult(
                operation="rewrite_links",
                success=False,
                message="Timed out after 60s",
            )
        except Exception as e:
            return MaintenanceResult(
                operation="rewrite_links", success=False, message=str(e)
            )

    # ------------------------------------------------------------------
    # Detect contradictions
    # ------------------------------------------------------------------

    def detect_contradictions(
        self,
        min_confidence: str = "low",
        mode: str = "both",
        semantic_threshold: float = 0.65,
    ) -> list[dict]:
        """Run the contradiction detector over the corpus.

        Args:
            min_confidence: ``"low"``, ``"medium"``, or ``"high"``.
            mode: ``"phrase"``, ``"semantic"``, or ``"both"``.
            semantic_threshold: Similarity threshold for semantic mode.

        Returns:
            List of contradiction dicts parsed from the detector output.
        """
        from config import GLOBAL_SCRIPTS_DIR

        try:
            script = GLOBAL_SCRIPTS_DIR / "contradiction_detector.py"
            if not script.exists():
                raise MaintenanceError(
                    f"contradiction_detector.py not found at {script}"
                )
            r = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    str(self._memory_dir),
                    f"--min-confidence={min_confidence}",
                    f"--mode={mode}",
                    f"--semantic-threshold={semantic_threshold}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = r.stdout or ""
            if r.stderr and r.stderr.strip():
                output += "\n[stderr]\n" + r.stderr
            parsed = _safe_json_parse(output)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and "contradictions" in parsed:
                raw = parsed["contradictions"]
                if isinstance(raw, list):
                    return raw
                return list(raw) if raw is not None else []
            return []
        except Exception as e:
            raise MaintenanceError(f"Contradiction detection failed: {e}") from e
