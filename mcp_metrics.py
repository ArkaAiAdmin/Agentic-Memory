"""MCP tools for the Prometheus metrics server.

Allows starting, stopping, and checking the status of the
standalone metrics HTTP server (metrics_server.py).
"""

import logging
logger = logging.getLogger(__name__)

import json
import signal
import subprocess
import sys
from pathlib import Path

from mcp_instance import mcp
from mcp_common import _err, ErrorCode, with_audit

_METRICS_PROCESS: subprocess.Popen | None = None
_METRICS_SCRIPT: Path | None = None


@mcp.tool()
@with_audit("memory_metrics_server")
def memory_metrics_server(action: str = "status", port: int = 9464) -> str:
    """Start, stop, or check the Prometheus metrics exporter server.

    The metrics server exposes memory system metrics at
    http://localhost:<port>/metrics in Prometheus format.

    Args:
        action: "start" to launch the server, "stop" to shut it down,
                "status" to check if running (default).
        port: HTTP port (default: 9464).
    """
    global _METRICS_PROCESS, _METRICS_SCRIPT

    if action == "start":
        if _METRICS_PROCESS is not None and _METRICS_PROCESS.poll() is None:
            return json.dumps(
                {
                    "ok": True,
                    "status": "already_running",
                    "pid": _METRICS_PROCESS.pid,
                    "port": port,
                }
            )
        script = Path(__file__).parent / "metrics_server.py"
        if not script.exists():
            return _err(ErrorCode.NOT_FOUND, f"metrics_server.py not found at {script}")
        try:
            _METRICS_PROCESS = subprocess.Popen(
                [sys.executable, str(script), "--port", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _METRICS_SCRIPT = script
            return json.dumps(
                {
                    "ok": True,
                    "status": "started",
                    "pid": _METRICS_PROCESS.pid,
                    "port": port,
                    "metrics_url": f"http://localhost:{port}/metrics",
                }
            )
        except Exception as e:
            logger.warning("memory_metrics_server failed: %s", e)
            return _err(ErrorCode.DB_ERROR, f"metrics server start failed: {e}")

    elif action == "stop":
        if _METRICS_PROCESS is None or _METRICS_PROCESS.poll() is not None:
            _METRICS_PROCESS = None
            return json.dumps({"ok": True, "status": "not_running"})
        try:
            _METRICS_PROCESS.send_signal(signal.SIGINT)
            _METRICS_PROCESS.wait(timeout=5)
            _METRICS_PROCESS = None
            return json.dumps({"ok": True, "status": "stopped"})
        except subprocess.TimeoutExpired:
            if _METRICS_PROCESS is not None:
                _METRICS_PROCESS.kill()
                _METRICS_PROCESS.wait()
            _METRICS_PROCESS = None
            return json.dumps({"ok": True, "status": "killed"})
        except Exception as e:
            logger.warning("memory_metrics_server failed: %s", e)
            return _err(ErrorCode.DB_ERROR, f"metrics server stop failed: {e}")

    else:
        if _METRICS_PROCESS is not None and _METRICS_PROCESS.poll() is None:
            return json.dumps(
                {
                    "ok": True,
                    "status": "running",
                    "pid": _METRICS_PROCESS.pid,
                    "port": port,
                }
            )
        else:
            _METRICS_PROCESS = None
            return json.dumps({"ok": True, "status": "not_running"})
