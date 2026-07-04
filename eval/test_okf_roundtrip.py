"""Round-trip and conformance tests for OKF export/import.

Tests are behavioral: they verify the spec's guarantees, not just
that the functions return without error.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from okf_export import okf_export
from okf_import import okf_import
from okf_conformance import validate_bundle, OKF_VERSION


def _make_db(path: Path) -> sqlite3.Connection:
    from _fixtures import bootstrap_temp_db_clean

    bootstrap_temp_db_clean(path)
    conn = sqlite3.connect(str(path))
    return conn


def _seed_memory(conn: sqlite3.Connection, note_id: str, **overrides) -> None:
    defaults = {
        "content": "body text",
        "source_file": f"memory/{note_id}.md",
        "tags": '["a", "b"]',
        "pinned": 0,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-02T00:00:00",
        "observed_at": "2026-01-01T12:00:00",
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "superseded_by": "",
        "metadata": json.dumps({
            "type": "decision",
            "resource": "https://example.com/x",
            "description": "desc",
            "custom_key": "custom_value",
        }),
    }
    defaults.update(overrides)
    conn.execute(
        """INSERT INTO memories (id, content, source_file, tags, pinned,
           created_at, updated_at, observed_at, valid_from, valid_to,
           superseded_by, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            note_id,
            defaults["content"],
            defaults["source_file"],
            defaults["tags"],
            defaults["pinned"],
            defaults["created_at"],
            defaults["updated_at"],
            defaults["observed_at"],
            defaults["valid_from"],
            defaults["valid_to"],
            defaults["superseded_by"],
            defaults["metadata"],
        ),
    )
    conn.commit()


class TestOKFRoundTrip(unittest.TestCase):
    """Export -> import must preserve behaviorally important fields."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        self.conn = _make_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_roundtrip_preserves_type_and_resource(self):
        _seed_memory(self.conn, "decisions/use-okf")
        self.conn.close()

        out = self.tmpdir / "out"
        okf_export(self.db_path, out)
        result = okf_import(out)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["errors"], 0)

        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", ("decisions/use-okf",)
        ).fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(meta.get("type"), "decision")
        self.assertEqual(meta.get("resource"), "https://example.com/x")

    def test_roundtrip_preserves_unknown_metadata_keys(self):
        _seed_memory(self.conn, "lessons/custom")
        self.conn.close()

        out = self.tmpdir / "out2"
        okf_export(self.db_path, out)
        result = okf_import(out)
        self.assertEqual(result["imported"], 1)

        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", ("lessons/custom",)
        ).fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(meta.get("custom_key"), "custom_value")

    def test_roundtrip_preserves_tags_and_pinned(self):
        _seed_memory(
            self.conn, "preferences/roundtrip",
            tags='["x", "y"]', pinned=1,
        )
        self.conn.close()

        out = self.tmpdir / "out3"
        okf_export(self.db_path, out)
        okf_import(out)

        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT tags, pinned FROM memories WHERE id = ?",
            ("preferences/roundtrip",),
        ).fetchone()
        self.assertIsNotNone(row)
        tags = json.loads(row[0])
        self.assertEqual(sorted(tags), ["x", "y"])
        self.assertEqual(row[1], 1)

    def test_export_skips_reserved_filenames(self):
        _seed_memory(self.conn, "lessons/index")
        _seed_memory(self.conn, "lessons/log")
        self.conn.close()

        out = self.tmpdir / "out4"
        result = okf_export(self.db_path, out)
        self.assertEqual(result["exported"], 2)
        self.assertIn("renamed to avoid reserved filename collision",
                      "\n".join(result.get("warnings", [])))

        # files exist with underscore prefix
        assert (out / "lessons" / "_index.md").exists()
        assert (out / "lessons" / "_log.md").exists()

    def test_export_writes_okf_version_in_index(self):
        _seed_memory(self.conn, "lessons/vcheck")
        self.conn.close()

        out = self.tmpdir / "out5"
        okf_export(self.db_path, out)
        index = (out / "index.md").read_text()
        assert f"okf_version: {OKF_VERSION}" in index

    def test_conformance_validator_reports_missing_type(self):
        bad = self.tmpdir / "bad_bundle"
        bad.mkdir()
        (bad / "concept.md").write_text("---\ntitle: no type here\n---\nbody\n")

        violations = validate_bundle(bad)
        self.assertTrue(
            any("missing required `type`" in v for v in violations),
            f"Expected missing-type violation, got: {violations}",
        )

    def test_frontmatter_roundtrip_has_all_spec_fields(self):
        _seed_memory(self.conn, "decisions/frontmatter-check")
        self.conn.close()

        out = self.tmpdir / "out6"
        okf_export(self.db_path, out)
        md = (out / "decisions" / "frontmatter-check.md").read_text()

        for key in ["type", "title", "description", "resource", "tags",
                    "pinned", "timestamp", "related"]:
            assert f"{key}:" in md, f"Missing OKF field {key} in frontmatter"


if __name__ == "__main__":
    unittest.main(verbosity=2)
