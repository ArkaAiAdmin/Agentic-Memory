
import logging
import json
import os
import sqlite3
import subprocess
import sys
import signal
from pathlib import Path
from contextlib import closing

from mcp_common import _bootstrap_path  # noqa: E402,F401
from mcp_instance import mcp
from mcp_common import _err, ErrorCode, with_audit, _resolve_memory_dir

logger = logging.getLogger(__name__)

_DASHBOARD_PROCESS: subprocess.Popen | None = None
_DASHBOARD_SCRIPT: Path | None = None


@mcp.tool()
@with_audit("memory_dashboard")
def memory_dashboard(action: str = "status", port: int = 8501) -> str:
    """Start, stop, or check the Streamlit dashboard server.

    Args:
        action: "start" to launch the server, "stop" to shut it down,
                "status" to check if running (default).
        port: HTTP port (default: 8501).
    """
    global _DASHBOARD_PROCESS, _DASHBOARD_SCRIPT
    stats = {}
    mem_dir = _resolve_memory_dir()
    db_path = mem_dir / "memory.db"
    if db_path.exists():
        try:
            with closing(sqlite3.connect(str(db_path), timeout=5)) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                row = conn.execute("""
                    SELECT
                      (SELECT COUNT(*) FROM memories),
                      (SELECT COUNT(*) FROM memories WHERE pinned=1),
                      (SELECT COUNT(*) FROM memory_embeddings),
                      (SELECT COUNT(*) FROM kg_entities),
                      (SELECT COUNT(*) FROM kg_facts),
                      (SELECT COUNT(*) FROM memory_chunks),
                      (SELECT COUNT(*) FROM memory_ctr_feedback),
                      (SELECT COUNT(*) FROM concept_drift),
                      (SELECT COUNT(*) FROM memory_audit_log),
                      (SELECT COUNT(*) FROM memory_audit_log WHERE error IS NOT NULL)
                """).fetchone()
                stats = {
                    "notes_total": row[0],
                    "pinned_notes": row[1],
                    "embeddings": row[2],
                    "entities": row[3],
                    "facts": row[4],
                    "chunks": row[5],
                    "ctr_events": row[6],
                    "drift_events": row[7],
                    "audit_calls": row[8],
                    "audit_errors": row[9],
                    "db_size_bytes": db_path.stat().st_size,
                    "db_path": str(db_path),
                }
        except (sqlite3.DatabaseError, OSError) as e:
            # Dashboard status is best-effort telemetry — a corrupt
            # or locked DB must not break the action path. Surface
            # the failure in the log so operators can investigate.
            logger.warning("memory_dashboard: stats unavailable: %s", e)
            stats = {"error": str(e), "ok": False}

    if action == "start":
        if _DASHBOARD_PROCESS is not None and _DASHBOARD_PROCESS.poll() is None:
            return json.dumps(
                {
                    "ok": True,
                    "status": "already_running",
                    "pid": _DASHBOARD_PROCESS.pid,
                    "port": port,
                    "stats": stats,
                }
            )
        script = Path(__file__).parent / "dashboard.py"
        if not script.exists():
            return _err(ErrorCode.NOT_FOUND, f"dashboard.py not found at {script}")
        try:
            streamlit_bin = Path(sys.executable).parent / "streamlit"
            if not streamlit_bin.exists():
                streamlit_bin = Path(sys.exec_prefix) / "bin" / "streamlit"
            if not streamlit_bin.exists():
                streamlit_bin = Path("streamlit")

            _bind_host = os.environ.get("MEMORY_DASHBOARD_HOST", "127.0.0.1")
            if _bind_host not in ("127.0.0.1", "localhost"):
                logger.warning(
                    "Dashboard bound to %s — set MEMORY_DASHBOARD_TLS_CERT / "
                    "MEMORY_DASHBOARD_TLS_KEY for remote use.",
                    _bind_host,
                )
            _DASHBOARD_PROCESS = subprocess.Popen(
                [
                    str(streamlit_bin),
                    "run",
                    str(script),
                    "--server.port",
                    str(port),
                    "--server.address",
                    _bind_host,
                    "--server.headless",
                    "true",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _DASHBOARD_SCRIPT = script
            return json.dumps(
                {
                    "ok": True,
                    "status": "started",
                    "pid": _DASHBOARD_PROCESS.pid,
                    "port": port,
                    "dashboard_url": f"http://localhost:{port}",
                    "stats": stats,
                }
            )
        except Exception as e:
            logger.warning("memory_dashboard failed: %s", e)
            return _err(ErrorCode.DB_ERROR, f"dashboard start failed: {e}")

    elif action == "stop":
        if _DASHBOARD_PROCESS is None or _DASHBOARD_PROCESS.poll() is not None:
            _DASHBOARD_PROCESS = None
            return json.dumps({"ok": True, "status": "not_running", "stats": stats})
        try:
            _DASHBOARD_PROCESS.send_signal(signal.SIGINT)
            _DASHBOARD_PROCESS.wait(timeout=5)
            _DASHBOARD_PROCESS = None
            return json.dumps({"ok": True, "status": "stopped", "stats": stats})
        except subprocess.TimeoutExpired:
            if _DASHBOARD_PROCESS is not None:
                _DASHBOARD_PROCESS.kill()
                _DASHBOARD_PROCESS.wait()
            _DASHBOARD_PROCESS = None
            return json.dumps({"ok": True, "status": "killed", "stats": stats})
        except Exception as e:
            logger.warning("memory_dashboard failed: %s", e)
            return _err(ErrorCode.DB_ERROR, f"dashboard stop failed: {e}")

    else:
        if _DASHBOARD_PROCESS is not None and _DASHBOARD_PROCESS.poll() is None:
            return json.dumps(
                {
                    "ok": True,
                    "status": "running",
                    "pid": _DASHBOARD_PROCESS.pid,
                    "port": port,
                    "dashboard_url": f"http://localhost:{port}",
                    "stats": stats,
                }
            )
        else:
            _DASHBOARD_PROCESS = None
            return json.dumps({"ok": True, "status": "not_running", "stats": stats})
