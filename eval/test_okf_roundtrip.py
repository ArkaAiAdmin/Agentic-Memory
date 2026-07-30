"""Round-trip and conformance tests for OKF v0.2 export/import.

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
        okf_import(out, db_path=self.db_path)

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
        _seed_memory(
            self.conn, "decisions/frontmatter-check",
            metadata=json.dumps({
                "type": "decision",
                "resource": "https://example.com/x",
                "description": "desc",
                "custom_key": "custom_value",
                "related": [{"id": "lessons/api-pitfall", "type": "lesson"}],
            }),
        )
        self.conn.close()

        out = self.tmpdir / "out6"
        okf_export(self.db_path, out)
        md = (out / "decisions" / "frontmatter-check.md").read_text()

        for key in ["type", "title", "description", "resource", "tags",
                    "pinned", "generated", "related"]:
            assert f"{key}:" in md, f"Missing OKF field {key} in frontmatter"


class TestOKFV02Fields(unittest.TestCase):
    """v0.2-specific round-trip and conformance tests."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        self.conn = _make_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_v02_generated_field_roundtrips(self):
        _seed_memory(self.conn, "decisions/generated-check")
        self.conn.close()

        out = self.tmpdir / "out_v02_gen"
        okf_export(self.db_path, out)
        md = (out / "decisions" / "generated-check.md").read_text()
        assert "generated:" in md
        assert "by: process:agentic-memory-export" in md
        assert "at: 2026-01-02T00:00:00" in md

        okf_import(out, db_path=self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", ("decisions/generated-check",)
        ).fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertIn("generated", meta)

    def test_v02_sources_roundtrips(self):
        meta = {
            "type": "reference",
            "resource": "https://example.com/doc",
            "sources": [
                {
                    "id": "src-1",
                    "resource": "https://example.com/source",
                    "title": "Source document",
                    "author": "team:docs",
                    "usage_count": 5000,
                    "last_modified": "2026-05-30",
                }
            ],
            "usage_window": {"from": "2026-06-01", "to": "2026-06-30"},
        }
        _seed_memory(self.conn, "references/src-roundtrip", metadata=json.dumps(meta))
        self.conn.close()

        out = self.tmpdir / "out_v02_src"
        okf_export(self.db_path, out)
        md = (out / "references" / "src-roundtrip.md").read_text()
        assert "sources:" in md
        assert "usage_count: 5000" in md

        okf_import(out, db_path=self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", ("references/src-roundtrip",)
        ).fetchone()
        self.assertIsNotNone(row)
        meta_back = json.loads(row[0])
        self.assertIn("sources", meta_back)
        self.assertEqual(meta_back["sources"][0]["usage_count"], 5000)

    def test_v02_verified_roundtrips(self):
        meta = {
            "type": "decision",
            "verified": {"by": "human:ahormati", "at": "2026-06-25T09:00:00Z"},
        }
        _seed_memory(self.conn, "decisions/verified-check", metadata=json.dumps(meta))
        self.conn.close()

        out = self.tmpdir / "out_v02_ver"
        okf_export(self.db_path, out)
        md = (out / "decisions" / "verified-check.md").read_text()
        assert "verified:" in md
        assert "human:ahormati" in md

        okf_import(out, db_path=self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", ("decisions/verified-check",)
        ).fetchone()
        self.assertIsNotNone(row)
        meta_back = json.loads(row[0])
        self.assertIn("verified", meta_back)
        self.assertEqual(meta_back["verified"]["by"], "human:ahormati")

    def test_v02_status_and_stale_after(self):
        meta = {
            "type": "playbook",
            "status": "stable",
            "stale_after": "2026-12-31",
        }
        _seed_memory(self.conn, "playbooks/status-check", metadata=json.dumps(meta))
        self.conn.close()

        out = self.tmpdir / "out_v02_status"
        okf_export(self.db_path, out)
        md = (out / "playbooks" / "status-check.md").read_text()
        assert "status: stable" in md
        assert "stale_after: 2026-12-31" in md

        okf_import(out, db_path=self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", ("playbooks/status-check",)
        ).fetchone()
        self.assertIsNotNone(row)
        meta_back = json.loads(row[0])
        self.assertEqual(meta_back.get("status"), "stable")
        self.assertEqual(meta_back.get("stale_after"), "2026-12-31")

    def test_v02_attested_computation_fields(self):
        meta = {
            "type": "Attested Computation",
            "title": "Revenue",
            "description": "Recognized revenue",
            "runtime": "bigquery",
            "parameters": [{"name": "year", "type": "integer", "required": True}],
            "computation": "references/computations/revenue.sql",
            "executor": {
                "resource": "references/skills/run-on-bq.md",
                "receipt": ["job_id", "executed_sql", "result"],
            },
            "attester": {"resource": "references/attesters/sql-equality.py"},
            "verified": {"by": "human:ahormati", "at": "2026-06-25T09:00:00Z"},
            "stale_after": "2026-12-31",
        }
        _seed_memory(self.conn, "computations/revenue", metadata=json.dumps(meta))
        self.conn.close()

        out = self.tmpdir / "out_v02_attested"
        okf_export(self.db_path, out)
        md = (out / "computations" / "revenue.md").read_text()
        assert "type: Attested Computation" in md
        assert "runtime: bigquery" in md
        assert "parameters:" in md
        assert "executor:" in md
        assert "attester:" in md

        okf_import(out, db_path=self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", ("computations/revenue",)
        ).fetchone()
        self.assertIsNotNone(row)
        meta_back = json.loads(row[0])
        self.assertEqual(meta_back.get("runtime"), "bigquery")
        self.assertEqual(meta_back.get("type"), "Attested Computation")

    def test_v02_conformance_validator(self):
        bundle = self.tmpdir / "v02_bundle"
        bundle.mkdir()

        # Valid v0.2 concept
        (bundle / "metrics" / "revenue.md").parent.mkdir(parents=True)
        (bundle / "metrics" / "revenue.md").write_text(
            "---\ntype: Metric\ntitle: Revenue\ngenerated: { by: agent/1.0, at: 2026-06-20T22:53:05Z }\nverified: { by: human:ahormati, at: 2026-06-25T09:00:00Z }\nstatus: stable\nstale_after: 2026-12-31\nsources:\n  - id: src-1\n    resource: https://example.com/src\n---\n# Revenue\nBody\n"
        )

        # Valid Attested Computation
        (bundle / "computations" / "calc.md").parent.mkdir(parents=True)
        (bundle / "computations" / "calc.md").write_text(
            "---\ntype: Attested Computation\ntitle: Calc\nruntime: python\nparameters:\n  - { name: x, type: integer, required: true }\n---\n# Computation\ncode\n"
        )

        violations = validate_bundle(bundle)
        self.assertEqual(violations, [], f"Expected no violations, got: {violations}")

    def test_v02_conformance_rejects_invalid_status(self):
        bad = self.tmpdir / "bad_status"
        bad.mkdir()
        (bad / "concept.md").write_text(
            "---\ntype: Note\ntitle: Bad\nstatus: invalid_status\n---\nbody\n"
        )

        violations = validate_bundle(bad)
        self.assertTrue(
            any("status" in v and "must be one of" in v for v in violations),
            f"Expected invalid-status violation, got: {violations}",
        )

    def test_v02_conformance_requires_runtime_for_attested_computation(self):
        bad = self.tmpdir / "bad_attested"
        bad.mkdir()
        (bad / "calc.md").write_text(
            "---\ntype: Attested Computation\ntitle: Calc\n---\nbody\n"
        )

        violations = validate_bundle(bad)
        self.assertTrue(
            any("requires `runtime`" in v for v in violations),
            f"Expected missing-runtime violation, got: {violations}",
        )

    def test_v02_import_raw_v02_content(self):
        """Import a hand-crafted v0.2 markdown file not generated by export."""
        (self.tmpdir / "concepts").mkdir()
        (self.tmpdir / "concepts" / "raw-note.md").write_text(
            "---\n"
            "type: Concept\n"
            "title: Raw Import\n"
            "generated:\n"
            "  by: agent:test\n"
            "  at: 2026-07-30T00:00:00Z\n"
            "verified:\n"
            "  by: human:tester\n"
            "  at: 2026-07-30T01:00:00Z\n"
            "status: draft\n"
            "stale_after: 2027-01-01\n"
            "sources:\n"
            "  - id: src-raw\n"
            "    resource: https://example.com/raw\n"
            "    usage_count: 10\n"
            "---\n"
            "# Raw Import\n"
            "Hand-crafted v0.2 content.\n"
        )

        okf_import(self.tmpdir, db_path=self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?",
            ("concepts/raw-note",),
        ).fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(meta.get("type"), "Concept")
        self.assertEqual(meta.get("generated"), {"by": "agent:test", "at": "2026-07-30T00:00:00Z"})
        self.assertEqual(meta.get("verified"), {"by": "human:tester", "at": "2026-07-30T01:00:00Z"})
        self.assertEqual(meta.get("status"), "draft")
        self.assertEqual(meta.get("stale_after"), "2027-01-01")
        self.assertIsInstance(meta.get("sources"), list)
        self.assertEqual(meta["sources"][0]["usage_count"], 10)

    def test_v02_related_field_roundtrip(self):
        """Verify the `related` field round-trips through export→import."""
        meta = {
            "type": "decision",
            "related": [
                {"id": "lessons/api-pitfall", "type": "lesson"},
                {"id": "decisions/auth-redesign", "type": "decision"},
            ],
        }
        _seed_memory(self.conn, "decisions/related-check", metadata=json.dumps(meta))
        self.conn.close()

        out = self.tmpdir / "out_v02_related"
        okf_export(self.db_path, out)
        md = (out / "decisions" / "related-check.md").read_text()
        assert "related:" in md

        okf_import(out, db_path=self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        row = self.conn.execute(
            "SELECT metadata FROM memories WHERE id = ?",
            ("decisions/related-check",),
        ).fetchone()
        self.assertIsNotNone(row)
        meta_back = json.loads(row[0])
        self.assertIn("related", meta_back)
        self.assertIsInstance(meta_back["related"], list)
        self.assertEqual(len(meta_back["related"]), 2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
