#!/usr/bin/env python3
"""Unit tests for sarcasm-aware contradiction detection.
"""
import sys
import tempfile
import unittest
from pathlib import Path

# Make the agentic-memory package importable.
INSTALL_DIR = Path.resolve(Path(__file__).parents[2])
sys.path.insert(0, str(INSTALL_DIR))

from kg.contradiction_detector import _claim_polarity, detect_contradictions_semantic


def _bootstrap_test_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            deleted_at TEXT,
            deleted_by TEXT,
            valid_from TEXT,
            valid_to TEXT,
            superseded_by TEXT,
            last_accessed TEXT,
            context_prefix TEXT,
            category TEXT,
            tier TEXT,
            importance_score REAL,
            metadata TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_note(
    db_path: Path,
    note_id: str,
    content: str = "hello world",
    source_file: str = "lessons/test.md",
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO memories
            (id, content, source_file, tags, created_at, updated_at,
             observed_at, deleted_at, deleted_by)
        VALUES (?, ?, ?, '[]', ?, ?, ?, NULL, NULL)
        """,
        (note_id, content, source_file, now, now, now),
    )
    conn.commit()
    conn.close()


class TestSarcasmContradiction(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sarcasm_test_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_test_db(self.db_path)

    def tearDown(self):
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def test_claim_polarity_sarcasm(self):
        # Normal positive claim containing an affirmation cue ("works", "fine")
        neg1, aff1 = _claim_polarity("The system works fine.")
        self.assertEqual(neg1, 0)
        self.assertGreater(aff1, 0)

        # Sarcastic negative claim: "The system works fine but crashed."
        # Sarcasm heuristic inverts polarity by incrementing negation count
        neg2, aff2 = _claim_polarity("The system works fine but crashed.")
        self.assertGreater(neg2, 0)

    def test_sarcasm_contradiction_semantic(self):
        from infra.embedding_search import get_embedding_search
        if get_embedding_search().model is None:
            raise unittest.SkipTest("model2vec not available")

        # Insert a note claiming the script works fine.
        _insert_note(
            self.db_path,
            "lessons/compilation-existing",
            "The compilation script works perfectly.",
        )

        # Insert a sarcastic note claiming the script works perfectly but fails.
        _insert_note(
            self.db_path,
            "lessons/compilation-sarcastic",
            "The compilation script works perfectly but fails immediately.",
        )

        # Detect contradictions using semantic detector
        result = detect_contradictions_semantic(self.tmpdir)
        self.assertGreaterEqual(len(result), 1)
        top = result[0]
        # The contradiction should be between the existing note and the sarcastic note
        self.assertIn("lessons/compilation-existing", (top["source"], top["target"]))
        self.assertIn("lessons/compilation-sarcastic", (top["source"], top["target"]))


if __name__ == "__main__":
    unittest.main()
