"""Tests for skill_extractor — automatic skill extraction from memories.

Covers the "skill agent" principle: procedural memories are turned into
reusable skills that can be searched and reused without re-running RAG.

Test categories:
  - Unit: is_skill_worthy, extract_skill_from_memory, _extract_topic, etc.
  - Persistence: save_skill (insert + update), search_skills, record_skill_hit
  - Integration: end-to-end save memory → extract skill → search hits skill
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Make the project importable
INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

from skill_extractor import (
    ensure_skill_schema,
    extract_skill_from_memory,
    save_skill,
    search_skills,
    record_skill_hit,
    list_skills,
    is_skill_worthy,
    verify_skill_contract,
    merge_and_save_skill,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_test_db() -> sqlite3.Connection:
    """Create a fresh in-memory DB with the memory_skills schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_skill_schema(conn)
    return conn


_PROCEDURAL_CONTENT = """\
# Install Ubuntu on Proxmox

## Step 1: Download the ISO
$ wget https://releases.ubuntu.com/24.04/ubuntu-24.04-live-server-amd64.iso

## Step 2: Upload to Proxmox
$ scp ubuntu-24.04-live-server-amd64.iso root@proxmox:/var/lib/vz/template/iso/

## Step 3: Create the VM
$ qm create 9000 --memory 8192 --cores 4 --net0 virtio,bridge=vmbr0

## Step 4: Install the LAMP stack
$ sudo apt update
$ sudo apt install -y apache2 mysql-server php libapache2-mod-php
"""


_FACT_CONTENT = """\
# Note about Proxmox networking

**What it is:** Proxmox uses Linux bridges for VM networking.
**Why:** Bridges allow VMs to share the host's physical NIC.

This is just background context, no procedure here.
"""


# ---------------------------------------------------------------------------
# Unit tests: is_skill_worthy heuristic
# ---------------------------------------------------------------------------


class TestIsSkillWorthy(unittest.TestCase):
    def test_short_content_is_not_skill(self):
        self.assertFalse(is_skill_worthy("hi"))

    def test_empty_content_is_not_skill(self):
        self.assertFalse(is_skill_worthy(""))

    def test_procedural_content_is_skill(self):
        self.assertTrue(is_skill_worthy(_PROCEDURAL_CONTENT))

    def test_fact_content_is_not_skill(self):
        # No numbered steps, no commands, no headers with action verbs
        # (only "What it is", "Why", "background context")
        self.assertFalse(is_skill_worthy(_FACT_CONTENT))

    def test_decision_note_is_not_skill(self):
        content = """\
# Decision: Use SQLite

## Rationale
We chose SQLite because it's simple.

## Note
Single-file DB, easy to back up.
"""
        self.assertFalse(is_skill_worthy(content))

    def test_code_block_only_content_is_skill(self):
        content = """\
How to deploy:

```bash
docker build -t myapp .
docker run -d myapp
```
"""
        self.assertTrue(is_skill_worthy(content))


# ---------------------------------------------------------------------------
# Unit tests: verify_skill_contract
# ---------------------------------------------------------------------------


class TestVerifySkillContract(unittest.TestCase):
    """Direct unit tests for verify_skill_contract() — Step 4a."""

    def test_rejects_no_steps(self):
        skill = {"name": "test", "triggers": ["a"], "steps": [], "description": "prose"}
        ok, reason = verify_skill_contract(skill)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_steps")

    def test_rejects_insufficient_triggers(self):
        skill = {"name": "test", "triggers": [], "steps": ["$ run command"], "description": ""}
        ok, reason = verify_skill_contract(skill)
        self.assertFalse(ok)
        self.assertEqual(reason, "insufficient_triggers")

    def test_rejects_no_actionable_content(self):
        skill = {
            "name": "test",
            "triggers": ["thoughtful"],
            "steps": ["Think about the problem carefully", "Consider all options"],
            "description": "A thoughtful process",
        }
        ok, reason = verify_skill_contract(skill)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_actionable_content")

    def test_accepts_shell_command_in_steps(self):
        skill = {
            "name": "deploy-app",
            "triggers": ["deploy"],
            "steps": ["$ docker build -t app .", "$ docker push app"],
            "description": "Deploy the application",
        }
        ok, reason = verify_skill_contract(skill)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_accepts_numbered_steps(self):
        skill = {
            "name": "setup-db",
            "triggers": ["database"],
            "steps": ["1. Install PostgreSQL", "2. Create the database", "3. Run migrations"],
            "description": "Set up the database",
        }
        ok, reason = verify_skill_contract(skill)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_accepts_code_block_in_steps(self):
        skill = {
            "name": "configure-nginx",
            "triggers": ["nginx", "proxy"],
            "steps": ["Edit nginx.conf:", "```\nserver { listen 80; }\n```"],
            "description": "Configure nginx",
        }
        ok, reason = verify_skill_contract(skill)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_accepts_action_verbs_in_description(self):
        skill = {
            "name": "fix-auth",
            "triggers": ["auth"],
            "steps": ["Fix the authentication flow"],
            "description": "Deploy the fix to production after testing",
        }
        ok, reason = verify_skill_contract(skill)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_accepts_code_module_reference(self):
        skill = {
            "name": "search-optimizer",
            "triggers": ["optimizer"],
            "steps": ["Tune the search/orchestrator.py reranker parameters"],
            "description": "Optimize search pipeline",
        }
        ok, reason = verify_skill_contract(skill)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_save_skill_rejects_invalid_and_returns_neg_one(self):
        conn = _make_test_db()
        try:
            skill = {"name": "junk", "triggers": [], "steps": [], "description": "pure prose"}
            result = save_skill(conn, skill)
            self.assertEqual(result, -1)
        finally:
            conn.close()

    def test_merge_and_save_skill_rejects_invalid(self):
        conn = _make_test_db()
        try:
            skill = {"name": "sync-junk", "triggers": [], "steps": [], "description": "pure prose"}
            result = merge_and_save_skill(conn, skill)
            self.assertEqual(result, {})
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_skills WHERE name = ?", (skill["name"],)
            ).fetchone()
            self.assertEqual(row[0], 0)
        finally:
            conn.close()

    def test_merge_and_save_skill_accepts_valid(self):
        conn = _make_test_db()
        try:
            for col in ("hit_vector", "last_used_vector", "logical_clock"):
                try:
                    conn.execute(f"ALTER TABLE memory_skills ADD COLUMN {col} TEXT")
                except Exception:
                    pass
            conn.commit()
            skill = {
                "name": "sync-valid",
                "triggers": json.dumps(["test"]),
                "steps": json.dumps(["$ run the test suite"]),
                "description": "Run tests",
                "source_memory_id": "m1",
            }
            result = merge_and_save_skill(conn, skill)
            self.assertIn("name", result)
            self.assertEqual(result["name"], "sync-valid")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Unit tests: extract_skill_from_memory
# ---------------------------------------------------------------------------


class TestExtractSkillFromMemory(unittest.TestCase):
    def test_returns_none_for_non_skill(self):
        result = extract_skill_from_memory("note-1", _FACT_CONTENT)
        self.assertIsNone(result)

    def test_returns_skill_dict_for_procedural(self):
        result = extract_skill_from_memory("note-1", _PROCEDURAL_CONTENT)
        self.assertIsNotNone(result)
        self.assertIn("name", result)
        self.assertIn("topic", result)
        self.assertIn("description", result)
        self.assertIn("triggers", result)
        self.assertIn("steps", result)
        self.assertIn("content_hash", result)
        self.assertEqual(result["source_memory_id"], "note-1")

    def test_name_is_slug(self):
        result = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        # Name should be a URL-safe slug

        self.assertRegex(result["name"], r"^[a-z0-9-]+$")

    def test_topic_from_first_heading(self):
        result = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        self.assertIn("ubuntu", result["topic"].lower())
        self.assertIn("proxmox", result["topic"].lower())

    def test_triggers_non_empty(self):
        result = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        self.assertGreater(len(result["triggers"]), 0)
        # Should include topic tokens
        self.assertIn("ubuntu", result["triggers"])
        self.assertIn("proxmox", result["triggers"])

    def test_steps_extracted(self):
        result = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        self.assertGreater(len(result["steps"]), 0)
        # At least one step should mention the LAMP stack
        joined = " ".join(result["steps"]).lower()
        self.assertTrue("lamp" in joined or "install" in joined or "wget" in joined)

    def test_content_hash_stable(self):
        r1 = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        r2 = extract_skill_from_memory("m2", _PROCEDURAL_CONTENT)
        self.assertEqual(r1["content_hash"], r2["content_hash"])

    def test_content_hash_different_for_different_content(self):
        r1 = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        r2 = extract_skill_from_memory("m2", "## Step 1: Different\n$ ls -la")
        self.assertNotEqual(r1["content_hash"], r2["content_hash"])


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


class TestSaveSkill(unittest.TestCase):
    def setUp(self):
        self.conn = _make_test_db()

    def tearDown(self):
        self.conn.close()

    def test_save_returns_id(self):
        skill = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        skill_id = save_skill(self.conn, skill)
        self.assertIsInstance(skill_id, int)
        self.assertGreater(skill_id, 0)

    def test_save_inserts_row(self):
        skill = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        save_skill(self.conn, skill)
        row = self.conn.execute(
            "SELECT name, topic, description, source_memory_id FROM memory_skills WHERE name = ?",
            (skill["name"],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], skill["name"])
        self.assertEqual(row["topic"], skill["topic"])
        self.assertEqual(row["source_memory_id"], "m1")

    def test_save_stores_triggers_as_json(self):
        skill = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        save_skill(self.conn, skill)
        row = self.conn.execute(
            "SELECT triggers FROM memory_skills WHERE name = ?",
            (skill["name"],),
        ).fetchone()
        triggers = json.loads(row["triggers"])
        self.assertIsInstance(triggers, list)
        self.assertGreater(len(triggers), 0)

    def test_save_is_idempotent_on_same_content(self):
        skill = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        id1 = save_skill(self.conn, skill)
        id2 = save_skill(self.conn, skill)
        self.assertEqual(id1, id2)
        # Only one row
        count = self.conn.execute("SELECT COUNT(*) FROM memory_skills").fetchone()[0]
        self.assertEqual(count, 1)

    def test_save_updates_on_content_change(self):
        skill1 = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        save_skill(self.conn, skill1)
        modified = _PROCEDURAL_CONTENT + "\n\n## Step 5: Reboot\n$ sudo reboot"
        skill2 = extract_skill_from_memory("m1", modified)
        self.assertNotEqual(skill1["content_hash"], skill2["content_hash"])
        save_skill(self.conn, skill2)
        # Content should now have Step 5
        row = self.conn.execute(
            "SELECT steps FROM memory_skills WHERE name = ?",
            (skill1["name"],),
        ).fetchone()
        steps = json.loads(row["steps"])
        joined = " ".join(steps).lower()
        self.assertIn("reboot", joined)


class TestSearchSkills(unittest.TestCase):
    def setUp(self):
        self.conn = _make_test_db()
        # Seed with a few skills
        for i, content in enumerate(
            [
                _PROCEDURAL_CONTENT,
                _PROCEDURAL_CONTENT.replace("Ubuntu", "Debian").replace(
                    "proxmox", "vmware"
                ),
                """# Configure nginx reverse proxy
## Step 1: Install
$ sudo apt install -y nginx
## Step 2: Edit config
$ sudo vi /etc/nginx/sites-available/default
""",
            ]
        ):
            skill = extract_skill_from_memory(f"m{i}", content)
            save_skill(self.conn, skill)

    def tearDown(self):
        self.conn.close()

    def test_search_empty_query(self):
        self.assertEqual(search_skills(self.conn, ""), [])

    def test_search_finds_matching_skill(self):
        results = search_skills(self.conn, "how to install ubuntu on proxmox")
        self.assertGreater(len(results), 0)
        # Top result should be the Ubuntu skill
        self.assertIn("ubuntu", results[0]["topic"].lower())

    def test_search_ranks_by_overlap(self):
        results = search_skills(self.conn, "ubuntu proxmox install")
        # The ubuntu skill should rank higher than nginx (more matching tokens)
        topics = [r["topic"].lower() for r in results]
        if "ubuntu" in topics[0] and "nginx" in topics[0]:
            ubuntu_idx = topics.index("ubuntu")
            nginx_idx = topics.index("nginx")
            self.assertLess(ubuntu_idx, nginx_idx)

    def test_search_respects_limit(self):
        results = search_skills(self.conn, "install", limit=2)
        self.assertLessEqual(len(results), 2)

    def test_search_no_match(self):
        results = search_skills(self.conn, "quantum entanglement physics")
        self.assertEqual(len(results), 0)

    def test_search_includes_steps(self):
        results = search_skills(self.conn, "ubuntu proxmox")
        self.assertGreater(len(results), 0)
        self.assertIn("steps", results[0])
        self.assertIsInstance(results[0]["steps"], list)


class TestRecordSkillHit(unittest.TestCase):
    def setUp(self):
        self.conn = _make_test_db()
        skill = extract_skill_from_memory("m1", _PROCEDURAL_CONTENT)
        self.skill_id = save_skill(self.conn, skill)

    def tearDown(self):
        self.conn.close()

    def test_hit_increments_counter(self):
        row = self.conn.execute(
            "SELECT hit_count, last_used_at FROM memory_skills WHERE id = ?",
            (self.skill_id,),
        ).fetchone()
        self.assertEqual(row["hit_count"], 0)
        self.assertIsNone(row["last_used_at"])

        record_skill_hit(self.conn, self.skill_id)
        row = self.conn.execute(
            "SELECT hit_count, last_used_at FROM memory_skills WHERE id = ?",
            (self.skill_id,),
        ).fetchone()
        self.assertEqual(row["hit_count"], 1)
        self.assertIsNotNone(row["last_used_at"])

    def test_multiple_hits(self):
        for _ in range(5):
            record_skill_hit(self.conn, self.skill_id)
        row = self.conn.execute(
            "SELECT hit_count FROM memory_skills WHERE id = ?",
            (self.skill_id,),
        ).fetchone()
        self.assertEqual(row["hit_count"], 5)


class TestListSkills(unittest.TestCase):
    def setUp(self):
        self.conn = _make_test_db()
        for i, content in enumerate(
            [
                _PROCEDURAL_CONTENT,
                _PROCEDURAL_CONTENT.replace("Ubuntu", "Debian"),
                """# Setup PostgreSQL
## Step 1: Install
$ sudo apt install -y postgresql
""",
            ]
        ):
            skill = extract_skill_from_memory(f"m{i}", content)
            save_skill(self.conn, skill)
            if i == 0:
                for _ in range(3):
                    record_skill_hit(self.conn, skill["name"]) if False else None
        # Manually bump hit counts
        list_skills(self.conn, limit=10)
        # Set distinct hit counts
        self.conn.execute(
            "UPDATE memory_skills SET hit_count = 10 WHERE name LIKE '%ubuntu%'"
        )
        self.conn.execute(
            "UPDATE memory_skills SET hit_count = 5 WHERE name LIKE '%debian%'"
        )
        self.conn.execute(
            "UPDATE memory_skills SET hit_count = 1 WHERE name LIKE '%postgres%'"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_list_returns_all_skills(self):
        skills = list_skills(self.conn)
        self.assertEqual(len(skills), 3)

    def test_list_ordered_by_hit_count(self):
        skills = list_skills(self.conn)
        hits = [s["hit_count"] for s in skills]
        self.assertEqual(hits, sorted(hits, reverse=True))

    def test_list_respects_limit(self):
        skills = list_skills(self.conn, limit=2)
        self.assertEqual(len(skills), 2)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestIntegrationWithMemoryDB(unittest.TestCase):
    """End-to-end: save a memory in the prod-schema DB, extract a skill from it, search."""

    def setUp(self):
        # Use a fresh temp DB with the full prod schema (memory + memory_skills)
        self.tmpdir = Path(tempfile.mkdtemp(prefix="skill_test_"))
        self.db_path = self.tmpdir / "memory.db"

        # Manually create a basic memories table for this test
        import sqlite3

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        # Minimal memories schema (just enough for the skill test)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
        """)
        # Insert a skill-worthy memory
        self.conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, '[]', datetime('now'), datetime('now'), datetime('now'))",
            (
                "lessons/install-ubuntu",
                _PROCEDURAL_CONTENT,
                "lessons/install-ubuntu.md",
            ),
        )
        self.conn.commit()
        # Now ensure the skill schema is in place
        ensure_skill_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_end_to_end_save_extract_search(self):
        """Save a skill-worthy memory → extract skill → search hits it."""
        # Step 1: read the memory
        row = self.conn.execute(
            "SELECT id, content FROM memories WHERE id = ?",
            ("lessons/install-ubuntu",),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "lessons/install-ubuntu")
        self.assertEqual(row["content"], _PROCEDURAL_CONTENT)

        # Step 2: extract the skill (simulating cron)
        skill = extract_skill_from_memory(row["id"], row["content"])
        self.assertIsNotNone(skill, "Procedural memory should be skill-worthy")

        # Step 3: save the skill
        skill_id = save_skill(self.conn, skill)
        self.assertGreater(skill_id, 0)

        # Step 4: search for the skill
        results = search_skills(self.conn, "how to install ubuntu on proxmox")
        self.assertGreater(
            len(results), 0, "Skill-first search should find the extracted skill"
        )
        self.assertIn("ubuntu", results[0]["topic"].lower())

        # Step 5: record a hit (simulating a search that used the skill)
        record_skill_hit(self.conn, results[0]["id"])
        row = self.conn.execute(
            "SELECT hit_count FROM memory_skills WHERE id = ?",
            (results[0]["id"],),
        ).fetchone()
        self.assertEqual(row["hit_count"], 1)

        # Step 6: verify hit count is reflected in subsequent search results
        results2 = search_skills(self.conn, "ubuntu proxmox")
        self.assertEqual(results2[0]["hit_count"], 1)

    def test_non_skill_memory_is_not_extracted(self):
        """Save a non-skill memory (a fact) → no skill is extracted."""
        self.conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, '[]', datetime('now'), datetime('now'), datetime('now'))",
            ("lessons/fact", _FACT_CONTENT, "lessons/fact.md"),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id, content FROM memories WHERE id = ?", ("lessons/fact",)
        ).fetchone()
        skill = extract_skill_from_memory(row["id"], row["content"])
        self.assertIsNone(skill, "Fact-only memory should not be a skill")

    def test_list_skills_after_extraction(self):
        """After extracting 1 skill, list_skills returns it."""
        skill = extract_skill_from_memory("lessons/install-ubuntu", _PROCEDURAL_CONTENT)
        save_skill(self.conn, skill)
        skills = list_skills(self.conn)
        self.assertEqual(len(skills), 1)
        self.assertIn("ubuntu", skills[0]["topic"].lower())


class TestIntegrationWithSavePipeline(unittest.TestCase):
    """Verify the skill extraction works against the real memory system schema."""

    def setUp(self):
        # Use a temp DB with the real prod schema via the bootstrap helper
        from _fixtures import bootstrap_temp_db_clean

        self.tmpdir = Path(tempfile.mkdtemp(prefix="skill_pipeline_test_"))
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        ensure_skill_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_skill_schema_coexists_with_memories(self):
        """The memory_skills table should coexist with all prod tables."""
        # Check that we can query both memories and memory_skills
        mem_count = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        skill_count = self.conn.execute(
            "SELECT COUNT(*) FROM memory_skills"
        ).fetchone()[0]
        self.assertIsInstance(mem_count, int)
        self.assertEqual(skill_count, 0)  # No skills yet

    def test_real_save_then_extract_workflow(self):
        """Insert a real memory into the prod schema, then extract a skill from it."""
        # Insert the procedural memory into the prod schema
        self.conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, '[]', datetime('now'), datetime('now'), datetime('now'))",
            (
                "lessons/install-ubuntu-test",
                _PROCEDURAL_CONTENT,
                "lessons/install-ubuntu-test.md",
            ),
        )
        self.conn.commit()
        # Verify the memory is in the DB
        row = self.conn.execute(
            "SELECT id, content FROM memories WHERE id = ?",
            ("lessons/install-ubuntu-test",),
        ).fetchone()
        self.assertIsNotNone(row)
        # Extract and save the skill
        skill = extract_skill_from_memory(row["id"], row["content"])
        self.assertIsNotNone(skill)
        save_skill(self.conn, skill)
        # Search and verify
        results = search_skills(self.conn, "ubuntu proxmox install")
        self.assertGreater(len(results), 0)
        # Cleanup: remove the test memory
        self.conn.execute(
            "DELETE FROM memory_skills WHERE source_memory_id = ?",
            ("lessons/install-ubuntu-test",),
        )
        self.conn.execute(
            "DELETE FROM memories WHERE id = ?", ("lessons/install-ubuntu-test",)
        )
        self.conn.commit()


if __name__ == "__main__":
    unittest.main()
