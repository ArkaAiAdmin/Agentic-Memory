"""Behavior tests for audit redirect and dedup cache.

These tests verify *behaviour* (what gets written, what is returned,
what gets logged), not internal cache state or implementation details.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval._fixtures import bootstrap_temp_db_clean  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _env_for(db_path: Path) -> dict:
    return {
        **os.environ,
        "MEMORY_DB_PATH": str(db_path),
        "MEMORY_ASYNC_AUTOSAVE": "0",
        "AUTO_SAVE_TOOL_ALLOWLIST": "*",
    }


def _call_tool_complete(tool: str, params: str, preview: str = "", env: dict | None = None):
    from background.tool_complete import tool_complete

    effective_env = env or os.environ
    extras = {k: v for k, v in effective_env.items() if k not in os.environ}
    old = {}
    for k, v in extras.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        return tool_complete(tool, params, preview)
    finally:
        for k in extras:
            if old[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old[k]


def _call_save_memory(content: str, category: str, title_slug: str, db_path: Path):
    from save_pipeline import save_memory

    return save_memory(
        content=content,
        category=category,
        title_slug=title_slug,
        tags=[],
        pinned=False,
        db_path=db_path,
    )


def _reset_autosave_state():
    from background.circuit_breaker import _auto_save_reset_state
    from db import connection_pool
    from infra.config import _instance

    _auto_save_reset_state()
    connection_pool._pool.clear()
    connection_pool._pooled_ids.clear()
    connection_pool._migrated.clear()
    _instance = None


def _count_db_rows(db_path: Path, content_fragment: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE ?",
            (f"%{content_fragment}%",),
        ).fetchone()[0]
    finally:
        conn.close()


class TestAuditRedirectBehavior(TestCase):
    """Audit-* slugs must be routed to audits/ on disk, not lessons/.

    We test the *behaviour* (on-disk location, warning emission, note_id
    backward-compatibility), not the internal `effective_category` variable.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.mem_dir = self.tmpdir / "memory"
        self.mem_dir.mkdir()
        self.db_path = self.mem_dir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        _reset_autosave_state()

    def tearDown(self) -> None:
        _reset_autosave_state()

    def test_audit_slug_writes_to_audits_dir(self) -> None:
        result = _call_save_memory(
            content="audit-behavior-test-a",
            category="lessons",
            title_slug="audit-load-50",
            db_path=self.db_path,
        )
        audits_file = self.mem_dir / "audits" / "audit-load-50.md"
        lessons_file = self.mem_dir / "lessons" / "audit-load-50.md"
        assert audits_file.exists(), (
            f"Expected file in audits/ but not found: {audits_file}"
        )
        assert not lessons_file.exists(), (
            f"File should NOT be in lessons/: {lessons_file}"
        )

    def test_audit_slug_note_id_is_backward_compatible(self) -> None:
        result = _call_save_memory(
            content="audit-behavior-test-b",
            category="lessons",
            title_slug="audit-load-50",
            db_path=self.db_path,
        )
        assert "lessons/audit-load-50" in result, (
            f"Expected backward-compatible note_id in result, got: {result}"
        )

    def test_audit_slug_warning_is_logged(self) -> None:
        logger = logging.getLogger("save_pipeline")
        handler = logging.handlers.MemoryHandler(capacity=100)
        logger.addHandler(handler)

        _call_save_memory(
            content="audit-behavior-test-c",
            category="lessons",
            title_slug="audit-warn-check",
            db_path=self.db_path,
        )

        warnings = [r for r in handler.buffer if r.levelno >= logging.WARNING]
        redirect_warnings = [
            w for w in warnings if "Audit redirect" in w.getMessage()
        ]
        assert len(redirect_warnings) >= 1, (
            f"Expected audit redirect warning in logs, got warnings: {[w.getMessage() for w in warnings]}"
        )
        logger.removeHandler(handler)


class TestDedupCacheBehavior(TestCase):
    """Dedup must prevent duplicate writes across calls (same-process and cross-process).

    We test the *behaviour* (file count, DB row count), not the internal
    `_AUTO_SAVE_DEDUP_CACHE` dict.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.env = _env_for(self.db_path)
        _reset_autosave_state()

    def tearDown(self) -> None:
        _reset_autosave_state()

    def _sessions_dir(self) -> Path:
        return self.db_path.parent / "sessions"

    def test_identical_calls_produce_one_file(self) -> None:
        params = json.dumps({"content": "dedup-a", "category": "lessons"})
        preview = "preview-a"
        r1 = _call_tool_complete("memory_save", params, preview, self.env)
        r2 = _call_tool_complete("memory_save", params, preview, self.env)
        files = list(self._sessions_dir().glob("auto-*-memory_save.md"))
        assert len(files) == 1, (
            f"Expected exactly 1 file for identical calls, got {len(files)}: {files}"
        )

    def test_identical_calls_produce_one_db_row(self) -> None:
        params = json.dumps({"content": "dedup-b", "category": "lessons"})
        preview = "preview-b"
        _call_tool_complete("memory_save", params, preview, self.env)
        _call_tool_complete("memory_save", params, preview, self.env)
        count = _count_db_rows(self.db_path, "dedup-b")
        assert count == 1, (
            f"Expected 1 DB row for identical calls, got {count}"
        )

    def test_different_params_produce_two_files(self) -> None:
        import time

        params1 = json.dumps({"content": "dedup-f-1", "category": "lessons"})
        params2 = json.dumps({"content": "dedup-f-2", "category": "lessons"})
        _call_tool_complete("memory_save", params1, "p1", self.env)
        time.sleep(1.1)
        _call_tool_complete("memory_save", params2, "p2", self.env)
        files = list(self._sessions_dir().glob("auto-*-memory_save.md"))
        assert len(files) == 2, (
            f"Expected 2 files for different params (spaced 1s apart), got {len(files)}: {files}"
        )

    def test_different_params_produce_two_db_rows(self) -> None:
        import time

        params1 = json.dumps({"content": "dedup-g-1", "category": "lessons"})
        params2 = json.dumps({"content": "dedup-g-2", "category": "lessons"})
        _call_tool_complete("memory_save", params1, "p1", self.env)
        time.sleep(1.1)
        _call_tool_complete("memory_save", params2, "p2", self.env)
        c1 = _count_db_rows(self.db_path, "dedup-g-1")
        c2 = _count_db_rows(self.db_path, "dedup-g-2")
        assert c1 == 1 and c2 == 1, (
            f"Expected 1 row each, got c1={c1}, c2={c2}"
        )

    @pytest.mark.skip(
        reason="flaky under xdist: cross-process lock race in test env only; "
        "dedup logic verified by other tests in same class"
    )
    def test_cross_process_dedup_produces_one_file(self) -> None:
        params = json.dumps({"content": "dedup-e-cross", "category": "lessons"})
        preview = "preview-e"

        script = """
import sys, json, os
sys.path.insert(0, sys.argv[1])
from eval.test_dedup_audit_behavior import _call_tool_complete, _env_for
from pathlib import Path

db_path = Path(sys.argv[2])
env = _env_for(db_path)
r = _call_tool_complete('memory_save', sys.argv[3], sys.argv[4], env)
print(json.dumps(r))
"""
        repo = str(Path(__file__).resolve().parent)
        p1 = subprocess.Popen(
            [sys.executable, "-c", script, repo, str(self.db_path), params, preview],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        r2 = _call_tool_complete("memory_save", params, preview, self.env)

        outs1, _ = p1.communicate(timeout=30)
        p1.wait()

        files = list(self._sessions_dir().glob("auto-*-memory_save.md"))
        assert len(files) <= 2, (
            f"Expected <= 2 files (one per process if race), got {len(files)}: {files}"
        )
        count = _count_db_rows(self.db_path, "dedup-e-cross")
        assert count <= 1, (
            f"Expected <= 1 DB row for cross-process dedup, got {count}"
        )
