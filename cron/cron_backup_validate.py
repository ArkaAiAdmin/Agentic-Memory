#!/usr/bin/env python3
"""Cron wrapper: backup validation — test-restore + integrity check.

Verifies that the most recent (or a specified) .db.gz backup is
actually restorable. This is the difference between "we have backups"
and "we have recoverable backups."

Usage:
    # Validate latest backup
    venv/bin/python cron_backup_validate.py

    # Validate a specific backup
    venv/bin/python cron_backup_validate.py /path/to/memory-2026-06-28.db.gz

    # Dry-run (report only, don't write)
    venv/bin/python cron_backup_validate.py --dry-run

    # Install daily cron job (runs at 3am, after backup at 2am)
    venv/bin/python cron_backup_validate.py --install-cron

    # Uninstall cron job
    venv/bin/python cron_backup_validate.py --uninstall-cron

What it checks:
    1. File is valid gzip (decompresses without error)
    2. SQLite integrity_check passes (returns exactly ["ok"])
    3. Key tables exist (memories, schema_version, memories_fts, etc.)
    4. schema_version matches current expected version
    5. Row count sanity: memories > 0, schema_version has entries
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# H9 fix (2026-06-22): serialize against concurrent backup / integrity runs.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _flock import acquire_lock_or_exit  # noqa: E402

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from infra.memory_common import GLOBAL_MEM_DIR, safe_close_db

_mem_dir_env = os.environ.get("MEMORY_DB_PATH")
_MEM_DIR = Path(_mem_dir_env).parent if _mem_dir_env else GLOBAL_MEM_DIR
from infra.log import setup_logging
from infra.migration_runner import SCHEMA_VERSION as CURRENT_SCHEMA_VERSION

logger = setup_logging(__name__)

BACKUP_DIR_NAME = "backups"
CRON_MARKER = "# agentic-memory-backup-validate"
CRON_SCHEDULE = "0 3 * * *"

EXPECTED_TABLES = frozenset([
    "memories",
    "schema_version",
    "memories_fts",
    "memories_fts_config",
    "memories_fts_data",
    "memories_fts_docsize",
    "memories_fts_idx",
    "memory_embeddings",
    "memory_audit_log",
    "memory_vec_idx",
    "memory_vec_keys",
    "memory_chunks",
    "memory_chunk_embeddings",
    "memory_chunk_vec_idx",
    "memory_chunk_vec_keys",
    "kg_entities",
    "kg_edges",
    "kg_facts",
    "kg_facts_fts",
    "shared_memories",
    "backlinks",
    "memory_skills",
    "sync_log",
    "kg_extraction_stats",
    "concept_drift",
    "drift_alarms",
    "arc_ghosts",
    "arc_stats",
    "memory_field_crdt",
    "kg_entity_crdt",
    "kg_edge_crdt",
    "sessions",
    "decision_threads",
    "thread_events",
    "session_compaction_log",
    "belief_assertions",
    "memory_revision_log",
    "entailment_chains",
    "graph_snapshots",
    "memory_events",
    "memory_ctr_feedback",
    "user_profile_access_log",
    "search_phase_stats",
])


def find_latest_backup(backup_dir: Path) -> Path | None:
    """Return the most recent .db.gz file in backup_dir, or None."""
    gz_files = sorted(backup_dir.glob("memory-*.db.gz"), key=os.path.getmtime, reverse=True)
    return gz_files[0] if gz_files else None


def validate_backup(backup_path: Path, dry_run: bool = False) -> dict:
    """Validate a single .db.gz backup file.

    Returns a dict with 'valid' (bool), 'checks' (list of per-check results),
    and 'error' (str) if something failed early.
    """
    checks: list[dict] = []
    result: dict = {"valid": False, "checks": checks, "backup_path": str(backup_path)}

    if not backup_path.exists():
        return {**result, "error": f"backup file not found: {backup_path}"}

    # --- Check 1: valid gzip (decompress succeeds) --------------------------
    try:
        with gzip.open(backup_path, "rb") as fh:
            _ = fh.read(4)
        checks.append({"check": "gzip_decompress", "pass": True, "detail": "decompressed OK"})
    except Exception as exc:
        logger.warning("validate_backup failed: %s", exc)
        checks.append({"check": "gzip_decompress", "pass": False, "detail": str(exc)})
        return {**result, "error": f"gzip decompress failed: {exc}"}

    # --- Check 2: decompress + PRAGMA integrity_check -----------------------
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="backup_validate_")
    os.close(tmp_fd)
    try:
        shutil.copy(backup_path, tmp_path + ".gz")
        with gzip.open(tmp_path + ".gz", "rb") as gz_in, open(tmp_path, "wb") as f_out:
            shutil.copyfileobj(gz_in, f_out)

        conn = sqlite3.connect(tmp_path, timeout=10.0)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            integrity_ok = integrity == ["ok"]
            checks.append({
                "check": "integrity_check",
                "pass": integrity_ok,
                "detail": f"{len(integrity)} rows returned",
            })
            if not integrity_ok:
                return {**result, "error": f"integrity_check failed: {integrity[:5]}"}
        finally:
            safe_close_db(conn)

        # --- Check 3: expected tables exist ---------------------------------
        conn = sqlite3.connect(tmp_path, timeout=10.0)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            existing = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            missing = sorted(EXPECTED_TABLES - existing)
            tables_ok = len(missing) == 0
            checks.append({
                "check": "tables_exist",
                "pass": tables_ok,
                "detail": f"missing={missing}" if missing else "all present",
            })
        finally:
            safe_close_db(conn)

        # --- Check 4: schema version -----------------------------------------
        conn = sqlite3.connect(tmp_path, timeout=10.0)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            sv = row[0] if row else None
            sv_ok = sv == CURRENT_SCHEMA_VERSION
            checks.append({
                "check": "schema_version",
                "pass": sv_ok,
                "detail": f"version={sv} (expected {CURRENT_SCHEMA_VERSION})",
            })
        finally:
            safe_close_db(conn)

        # --- Check 5: row count sanity ---------------------------------------
        conn = sqlite3.connect(tmp_path, timeout=10.0)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            mem_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            chunk_count = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
            has_data = mem_count > 0
            checks.append({
                "check": "row_counts",
                "pass": has_data,
                "detail": f"memories={mem_count}, chunks={chunk_count}",
            })
        finally:
            safe_close_db(conn)

    finally:
        for stale in (tmp_path, tmp_path + ".gz"):
            try:
                if os.path.exists(stale):
                    os.unlink(stale)
            except OSError:
                pass

    all_pass = all(c["pass"] for c in checks)
    result["valid"] = all_pass
    return result


RPO_HOURS = 24


def check_rpo(latest: Path | None) -> dict:
    """Check the Recovery Point Objective: the newest backup must be recent.

    Returns a dict with 'rpo_met' (bool) and, when the backup is too old,
    an 'rpo_breach' entry describing the breach.
    """
    if latest is None:
        return {"rpo_met": False, "rpo_breach": {"breached": True, "latest_backup_age_hours": None, "rpo_hours": RPO_HOURS}}

    age_seconds = time.time() - os.path.getmtime(latest)
    age_hours = age_seconds / 3600.0
    rpo_met = age_hours <= RPO_HOURS
    if not rpo_met:
        logger.warning(
            "RPO breach: newest backup %s is %.1fh old (max %dh)",
            latest, age_hours, RPO_HOURS,
        )
        return {
            "rpo_met": False,
            "rpo_breach": {
                "breached": True,
                "latest_backup_age_hours": round(age_hours, 2),
                "rpo_hours": RPO_HOURS,
            },
        }
    return {"rpo_met": True, "latest_backup_age_hours": round(age_hours, 2)}


def find_and_validate_latest(dry_run: bool = False) -> dict:
    backup_dir = _MEM_DIR / BACKUP_DIR_NAME
    if not backup_dir.exists():
        return {"error": f"backup dir not found: {backup_dir}"}

    latest = find_latest_backup(backup_dir)
    if latest is None:
        rpo = check_rpo(None)
        return {"error": f"no .db.gz backups found in {backup_dir}", **rpo}

    logger.info("Validating latest backup: %s", latest)
    result = validate_backup(latest, dry_run=dry_run)
    result.update(check_rpo(latest))
    return result


def _get_python_path() -> str:
    venv_python = Path(__file__).resolve().parent.parent / "venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def _get_cron_line() -> str:
    python = _get_python_path()
    script = str(Path(__file__).resolve())
    log = str(_MEM_DIR / "backups" / "validate.log")
    return f"{CRON_SCHEDULE} {python} {script} >> {log} 2>&1 {CRON_MARKER}"


def install_cron() -> dict:
    """Install daily cron job for backup validation (after backup at 2am)."""
    import subprocess
    cron_line = _get_cron_line()
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        crontab = existing.stdout if existing.returncode == 0 else ""
        if CRON_MARKER in crontab:
            return {"installed": False, "reason": "already installed"}
        new_crontab = crontab + f"\n{cron_line}\n" if crontab.strip() else cron_line + "\n"
        proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True, timeout=5)
        if proc.returncode == 0:
            return {"installed": True, "cron_line": cron_line}
        return {"installed": False, "error": f"crontab - failed: {proc.stderr}"}
    except Exception as exc:
        logger.warning("install_cron failed: %s", exc)
        return {"installed": False, "error": str(exc)}


def uninstall_cron() -> dict:
    """Remove the backup-validation cron job."""
    import subprocess
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if existing.returncode != 0:
            return {"removed": False, "reason": "no crontab"}
        lines = [
            line for line in existing.stdout.splitlines()
            if CRON_MARKER not in line
        ]
        proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, timeout=5)
        return {"removed": proc.returncode == 0}
    except Exception as exc:
        logger.warning("uninstall_cron failed: %s", exc)
        return {"removed": False, "error": str(exc)}


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print((__doc__ or "").strip(), file=sys.stderr)
        return 0

    setup_logging(__name__, level="INFO", fmt="%(message)s")

    if "--install-cron" in sys.argv:
        res = install_cron()
        print(f"install_cron: {res}")
        return 0 if res.get("installed") else 1

    if "--uninstall-cron" in sys.argv:
        res = uninstall_cron()
        print(f"uninstall_cron: {res}")
        return 0 if res.get("removed") else 1

    dry_run = "--dry-run" in sys.argv
    acquire_lock_or_exit("cron_backup_validate")

    backup_path = None
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        backup_path = Path(arg)
        break

    if backup_path is None:
        res = find_and_validate_latest(dry_run=dry_run)
    else:
        res = validate_backup(backup_path, dry_run=dry_run)

    if res.get("error"):
        print(f"FAIL: {res['error']}")
        return 2

    for check in res.get("checks", []):
        status = "PASS" if check["pass"] else "FAIL"
        print(f"  [{status}] {check['check']}: {check['detail']}")

    # RPO check section
    rpo_met = res.get("rpo_met", True)
    rpo_age = res.get("latest_backup_age_hours")
    rpo_status = "OK" if rpo_met else "BREACHED"
    print(f"  RPO check: {{'status': '{rpo_status}', 'latest_backup_age_hours': {rpo_age}, 'max_rpo_hours': {RPO_HOURS}}}")
    if not rpo_met:
        breach = res.get("rpo_breach", {})
        print(f"  RPO BREACH: newest backup is {breach.get('latest_backup_age_hours')}h old (max {RPO_HOURS}h)")

    if res.get("valid") and rpo_met:
        print(f"OK: backup is valid — {res['backup_path']}")
        return 0

    if not rpo_met:
        print("FAIL: RPO breach — newest backup is too old")
        from infra.alert import alert

        alert(
            "error",
            "Backup validation RPO breach",
            f"path={res.get('backup_path')}, latest_backup_age_hours={res.get('latest_backup_age_hours')}, max_rpo_hours={RPO_HOURS}",
        )
        return 1

    print(f"FAIL: backup validation failed — {res['backup_path']}")
    from infra.alert import alert

    alert(
        "error",
        "Backup validation failed",
        f"path={res['backup_path']}, error={res.get('error', 'unknown')}",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
