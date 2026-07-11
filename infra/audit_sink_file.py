"""Rolling JSONL file audit sink — always available.

Writes one JSON object per line to a local file. When the file reaches
``max_bytes`` it is rotated (``path.1``, ``path.2``, ... up to ``backups``
generations), matching the rotation behavior of the existing config-drift
audit log (infra/config_drift_audit.py).
"""

from __future__ import annotations

import json
import logging
import os
import threading

from infra.audit_sink import AuditSink

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB rotation threshold
DEFAULT_BACKUPS = 5


class FileAuditSink:
    """Append-only rolling JSONL sink."""

    def __init__(
        self,
        path: str | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
    ) -> None:
        if path is None:
            from infra.infrastructure import resolve_active_memory_dir

            path = str(resolve_active_memory_dir() / "audit_sink.jsonl")
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def emit(self, event: dict) -> None:
        try:
            line = json.dumps(event, default=str)
        except (TypeError, ValueError):
            line = json.dumps({"_unserializable": True, "tool": event.get("tool")}, default=str)
        with self._lock:
            try:
                if (
                    self.backups
                    and os.path.exists(self.path)
                    and os.path.getsize(self.path) >= self.max_bytes
                ):
                    self._rotate()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception as exc:
                logger.warning("file audit sink write failed: %s", exc)

    def _rotate(self) -> None:
        for i in range(self.backups - 1, 0, -1):
            src = f"{self.path}.{i}"
            dst = f"{self.path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        if os.path.exists(self.path):
            os.replace(self.path, f"{self.path}.1")

    def flush(self) -> None:
        # Line-buffered append; nothing buffered to flush.
        return

    def read_events(self) -> list[dict]:
        """Test/dev helper: read all emitted JSONL events back as dicts."""
        out: list[dict] = []
        if not os.path.exists(self.path):
            return out
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except (TypeError, ValueError):
                        continue
        return out
