"""Test skill-first search integration in search_pipeline.py (Phase 2)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

from search_pipeline import search_memories
from skill_extractor import ensure_skill_schema, extract_skill_from_memory, save_skill


_PROC = """\
# Install nginx
## Step 1: Install
$ sudo apt install -y nginx
## Step 2: Configure
$ sudo vi /etc/nginx/sites-available/default
"""


def _bootstrap_db(db_path: Path, memories: list[tuple[str, str]]) -> None:
    """Create a minimal DB with the memories table + skill schema."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            deleted_at TEXT,
            fitness_score REAL,
            importance REAL,
            pinned INTEGER DEFAULT 0,
            last_accessed TEXT,
            metadata TEXT,
            access_count INTEGER DEFAULT 1
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, tags, source_file,
            content_rowid=rowid,
            tokenize='porter unicode61'
        );
    """)
    for i, (mid, content) in enumerate(memories):
        cur = conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, '[]', datetime('now'), datetime('now'), datetime('now'))",
            (mid, content, f"lessons/{mid}.md"),
        )
        rowid = cur.lastrowid
        conn.execute(
            "INSERT INTO memories_fts (rowid, content, tags, source_file) VALUES (?, ?, '[]', ?)",
            (rowid, content, f"lessons/{mid}.md")
        )
    conn.commit()
    ensure_skill_schema(conn)
    conn.close()


class TestSkillFirstSearch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="skill_search_"))
        self.db_path = self.tmpdir / "memory.db"
        _bootstrap_db(self.db_path, [("lessons/nginx", _PROC)])
        # Extract and save a skill
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        ensure_skill_schema(conn)
        skill = extract_skill_from_memory("lessons/nginx", _PROC)
        save_skill(conn, skill)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_skill_first_short_circuits_when_skill_matches(self):
        """skill_first=True returns skill match results with is_skill flag."""
        result = search_memories(self.db_path, "how to install nginx", skill_first=True)
        self.assertGreater(result["count"], 0)
        self.assertTrue(any(item.get("is_skill") for item in result["results"]))
        self.assertIn("Skill match", result.get("output", ""))

    def test_skill_first_falls_back_to_rag_when_no_skill(self):
        result = search_memories(
            self.db_path, "quantum entanglement physics", skill_first=True
        )
        # No skill matches, should fall back to RAG and find nothing
        self.assertEqual(result["count"], 0)
        self.assertNotIn("Skill match", result["output"])

    def test_skill_first_false_runs_normal_rag(self):
        result = search_memories(
            self.db_path, "how to install nginx", skill_first=False
        )
        # Should find the memory via RAG, not the skill
        self.assertGreater(result["count"], 0)
        self.assertFalse(any(item.get("is_skill") for item in result["results"]))

    def test_skill_first_records_hit_count(self):
        """skill_first search increments hit_count on matched skills."""
        from infra.cache import _search_cache

        _search_cache.clear()
        search_memories(self.db_path, "install nginx", skill_first=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        hit1 = conn.execute("SELECT hit_count FROM memory_skills").fetchone()[
            "hit_count"
        ]
        _search_cache.clear()
        search_memories(self.db_path, "configure nginx", skill_first=True)
        hit2 = conn.execute("SELECT hit_count FROM memory_skills").fetchone()[
            "hit_count"
        ]
        conn.close()
        self.assertGreater(hit2, hit1)

    def test_skill_first_uses_cache(self):
        """Skill-first search uses result cache for duplicate queries."""
        r1 = search_memories(self.db_path, "install nginx", skill_first=True)
        r2 = search_memories(self.db_path, "install nginx", skill_first=True)
        self.assertEqual(r1["count"], r2["count"])
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        hit = conn.execute("SELECT hit_count FROM memory_skills").fetchone()[
            "hit_count"
        ]
        conn.close()
        self.assertEqual(hit, 1)

    def test_skill_first_no_skills_table(self):
        """If the skills table doesn't exist, skill_first still runs RAG gracefully."""
        # Create a DB without memory_skills
        tmpdir2 = Path(tempfile.mkdtemp(prefix="skill_search_no_table_"))
        db_path2 = tmpdir2 / "memory.db"
        _bootstrap_db(db_path2, [("lessons/nginx", _PROC)])
        try:
            result = search_memories(db_path2, "install nginx", skill_first=True)
            # Falls back to RAG
            self.assertGreater(result["count"], 0)
            self.assertFalse(any(item.get("is_skill") for item in result["results"]))
        finally:
            import shutil

            shutil.rmtree(tmpdir2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
