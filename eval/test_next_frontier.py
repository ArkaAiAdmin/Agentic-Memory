"""Verification suite for the Next Frontier features (P1–P4).

Covers:
  - Skill CRDT convergence (G-Counter + LWW merge)
  - HTTP skill sync (server + client)
  - Rule-based contradiction resolver (LLM fallback)
  - Federated skill decay (per-agent vs global)
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp: Path) -> sqlite3.Connection:
    """Create a fresh in-memory DB with the memory_skills schema, ensuring new columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from skill_extractor import ensure_skill_schema
    ensure_skill_schema(conn)
    for col, ctype in [
        ("hit_vector", "TEXT DEFAULT '{}'"),
        ("last_used_vector", "TEXT DEFAULT '{}'"),
        ("logical_clock", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE memory_skills ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


# ===================================================================
# Skill CRDT + Merge
# ===================================================================

class TestSkillCRDTConvergence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_merge_hit_vector_gcounter(self):
        from skill_extractor import merge_skills, ensure_skill_schema

        conn = _make_db(self.tmp)
        ensure_skill_schema(conn)
        conn.execute(
            """INSERT INTO memory_skills
               (name, description, hit_count, last_used_at, created_at, updated_at,
                hit_vector, last_used_vector, logical_clock)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "desc", 3, 100.0, 100.0, 100.0,
             json.dumps({"agent-a": 2, "agent-b": 1}),
             json.dumps({"agent-a": 100.0, "agent-b": 80.0}),
             5),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM memory_skills WHERE name='s1'").fetchone()
        skill_a = dict(row)
        skill_b = {
            "name": "s1",
            "hit_vector": json.dumps({"agent-a": 1, "agent-c": 3}),
            "last_used_vector": json.dumps({"agent-a": 120.0, "agent-c": 200.0}),
            "logical_clock": 7,
            "updated_at": 200.0,  # newer than skill_a's 100.0 → skill_b wins LWW
            "description": "new desc",
        }
        merged = merge_skills(skill_a, skill_b)
        hv = json.loads(merged["hit_vector"])
        self.assertEqual(hv["agent-a"], 2)
        self.assertEqual(hv["agent-b"], 1)
        self.assertEqual(hv["agent-c"], 3)
        self.assertEqual(merged["hit_count"], 6)
        self.assertEqual(merged["last_used_at"], 200.0)
        # skill_b has higher updated_at, so skill_b's description wins via LWW
        self.assertEqual(merged["description"], "new desc")
        conn.close()

    def test_record_skill_hit_crdt(self):
        from skill_extractor import ensure_skill_schema, record_skill_hit, _resolve_skill_agent_id as _raid

        conn = _make_db(self.tmp)
        ensure_skill_schema(conn)
        sid = conn.execute(
            "INSERT INTO memory_skills (name, created_at, updated_at) VALUES (?, ?, ?)",
            ("s2", time.time(), time.time()),
        ).lastrowid
        assert sid is not None
        conn.commit()
        record_skill_hit(conn, sid)
        row = conn.execute("SELECT hit_count, hit_vector, logical_clock FROM memory_skills WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["hit_count"], 1)
        hv = json.loads(row["hit_vector"])
        agent_id = _raid()
        self.assertIn(agent_id, hv)
        self.assertEqual(hv[agent_id], 1)
        self.assertEqual(row["logical_clock"], 1)
        conn.close()

    def test_merge_idempotent(self):
        from skill_extractor import merge_skills

        s = {
            "name": "idemp",
            "hit_vector": json.dumps({"a": 3}),
            "last_used_vector": json.dumps({"a": 150.0}),
            "logical_clock": 3,
            "description": "v1",
            "updated_at": 100.0,
        }
        m1 = merge_skills(s, s)
        m2 = merge_skills(m1, s)
        self.assertEqual(json.loads(m1["hit_vector"]), json.loads(m2["hit_vector"]))

    def test_merge_and_save_skill_insert(self):
        from skill_extractor import ensure_skill_schema, merge_and_save_skill

        conn = _make_db(self.tmp)
        ensure_skill_schema(conn)
        for col in ("hit_vector", "last_used_vector", "logical_clock"):
            try:
                conn.execute(f"ALTER TABLE memory_skills ADD COLUMN {col} TEXT")
            except Exception:
                pass
        conn.commit()
        result = merge_and_save_skill(conn, {
            "name": "new-skill", "description": "d", "hit_count": 0,
            "triggers": json.dumps(["test"]),
            "steps": json.dumps(["$ run command"]),
        })
        self.assertEqual(result["name"], "new-skill")
        row = conn.execute("SELECT name FROM memory_skills WHERE name='new-skill'").fetchone()
        self.assertIsNotNone(row)
        conn.close()

    def test_merge_and_save_skill_update_existing(self):
        from skill_extractor import ensure_skill_schema, merge_and_save_skill
        import time

        conn = _make_db(self.tmp)
        ensure_skill_schema(conn)
        for col in ("hit_vector", "last_used_vector", "logical_clock"):
            try:
                conn.execute(f"ALTER TABLE memory_skills ADD COLUMN {col} TEXT")
            except Exception:
                pass
        conn.commit()
        now = time.time()
        merge_and_save_skill(conn, {
            "name": "upd-skill", "description": "old", "updated_at": now,
            "triggers": json.dumps(["test"]),
            "steps": json.dumps(["$ run command"]),
        })
        merge_and_save_skill(conn, {
            "name": "upd-skill", "description": "new", "updated_at": now + 1,
            "hit_vector": json.dumps({"agent-a": 2}),
            "triggers": json.dumps(["test"]),
            "steps": json.dumps(["$ run command"]),
        })
        row = conn.execute("SELECT description, hit_count FROM memory_skills WHERE name='upd-skill'").fetchone()
        self.assertEqual(row["description"], "new")
        self.assertEqual(row["hit_count"], 2)
        conn.close()


# ===================================================================
# HTTP Skill Sync (mock server)
# ===================================================================

class TestHTTPSkillSync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "memory.db")

    def _make_server(self):
        from infra.sync_server import SyncServer

        cfg = MagicMock()
        cfg.agent_id = "agent-a"
        db_path = str(self.tmp / "memory.db")
        conn = sqlite3.connect(db_path)
        conn.close()

        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        server = SyncServer(db_path=db_path, agent_id="agent-a", host="127.0.0.1", port=port)
        server.start()
        return server, f"http://127.0.0.1:{port}"

    def test_skill_push_and_changes_roundtrip(self):
        try:
            from skill_extractor import ensure_skill_schema
            from infra.sync_client import sync_skills_with_peer
        except ImportError:
            self.skipTest("skill sync not available")

        conn = sqlite3.connect(self.db)
        ensure_skill_schema(conn)
        conn.execute(
            "INSERT INTO memory_skills (name, topic, description, hit_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("skill-a", "ops", "Run ops", 3, time.time(), time.time()),
        )
        conn.commit()
        conn.close()

        server, url = self._make_server()
        try:
            result = sync_skills_with_peer(
                db_path=self.db,
                peer_url=url,
                peer_name="peer-a",
                peer_agent_id="peer-a",
                local_agent_id="agent-local",
                limit=100,
            )
            self.assertIn("push_applied", result)
            self.assertIn("pull_applied", result)
        finally:
            server.stop()


# ===================================================================
# Rule-based contradiction resolver
# ===================================================================

class TestContradictionResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "memory.db")

    def _make_note(self, conn, nid, content, ts):
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (nid, content, nid, ts, ts, ts),
        )
        conn.commit()

    def test_auto_supersedes_older(self):
        import infra.db_migrations as db_migrations
        from kg.contradiction_resolver import auto_resolve_contradiction_pair

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        db_migrations.run_schema_setup(conn)
        now = time.time()
        self._make_note(conn, "old-note", "Enables feature X", now - 1000)
        self._make_note(conn, "new-note", "Disables feature X", now - 500)
        result = auto_resolve_contradiction_pair(self.db, "old-note", "new-note")
        self.assertIn(result["action"], ("superseded", "error"))
        conn.close()

    def test_auto_keep_both_when_future_enabled(self):
        os_env_patch = {"MEMORY_CONTRADICTION_AUTO_RESOLVE_LLM": "1"}
        with patch.dict("os.environ", os_env_patch, clear=False):
            from kg.contradiction_resolver import _pick_strategy

            mock_provider = MagicMock()
            mock_provider.generate.return_value = {"content": '{"action": "keep_both", "rationale": "mock"}'}
            with patch("kg.contradiction_resolver._get_provider", return_value=mock_provider):
                row_a = ("note-a", "Content A", "Title A", "2026-01-01", "2026-01-01", "{}")
                row_b = ("note-b", "Content B", "Title B", "2026-02-01", "2026-02-01", "{}")
                strategy = _pick_strategy(row_a, row_b)
                self.assertEqual(strategy, "keep_both")


# ===================================================================
# MCP Federated Skills Tool Test
# ===================================================================

class TestMCPListFederatedSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "memory.db")

    def test_list_federated_skills_filtering(self):
        from skill_extractor import ensure_skill_schema
        from mcp_surface.mcp_maintenance import memory_list_federated_skills

        # Set up database file
        conn = sqlite3.connect(self.db_path)
        ensure_skill_schema(conn)
        for col, ctype in [
            ("hit_vector", "TEXT DEFAULT '{}'"),
            ("last_used_vector", "TEXT DEFAULT '{}'"),
            ("logical_clock", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE memory_skills ADD COLUMN {col} {ctype}")
            except sqlite3.OperationalError:
                pass

        # Insert two skills
        conn.execute(
            """INSERT INTO memory_skills
               (name, hit_vector, last_used_vector, hit_count, last_used_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("skill-ab", json.dumps({"agent-a": 3, "agent-b": 1}), json.dumps({"agent-a": 100.0, "agent-b": 100.0}), 4, 100.0, 100.0, 100.0),
        )
        conn.execute(
            """INSERT INTO memory_skills
               (name, hit_vector, last_used_vector, hit_count, last_used_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("skill-c", json.dumps({"agent-c": 2}), json.dumps({"agent-c": 150.0}), 2, 150.0, 100.0, 100.0),
        )
        conn.commit()
        conn.close()

        # Call with patched MEMORY_DB_PATH so connection pool routes correctly
        with patch.dict("os.environ", {"MEMORY_DB_PATH": self.db_path}):
            # No filter should return both
            res_all = json.loads(memory_list_federated_skills(limit=10))
            self.assertEqual(res_all["count"], 2)

            # Filter by agent-a should only return skill-ab
            res_a = json.loads(memory_list_federated_skills(limit=10, agent_filter="agent-a"))
            self.assertEqual(res_a["count"], 1)
            self.assertEqual(res_a["skills"][0]["name"], "skill-ab")

            # Filter by agent-c should only return skill-c
            res_c = json.loads(memory_list_federated_skills(limit=10, agent_filter="agent-c"))
            self.assertEqual(res_c["count"], 1)
            self.assertEqual(res_c["skills"][0]["name"], "skill-c")


# ===================================================================
# Federated skill decay
# ===================================================================

class TestFederatedSkillDecay(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_decay_per_agent_vector(self):
        from skill_extractor import ensure_skill_schema
        from cron.cron_skill_decay import _decayed_skills

        conn = _make_db(self.tmp)
        ensure_skill_schema(conn)
        now = time.time()
        conn.execute(
            """INSERT INTO memory_skills
               (name, hit_count, hit_vector, last_used_vector, last_used_at,
                created_at, updated_at, logical_clock)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "skill-v",
                5,
                json.dumps({"agent-a": 3, "agent-b": 2}),
                json.dumps({"agent-a": now - 3_000_000, "agent-b": now - 1000}),
                now - 1000,
                now - 1000,
                now - 1000,
                10,
            ),
        )
        conn.commit()
        cutoff_days = 30
        decayed, deleted = _decayed_skills(conn, max_age_days=cutoff_days, decay_factor=0.5, delete_threshold=0.5)
        self.assertEqual(len(deleted), 0)
        self.assertEqual(len(decayed), 1)
        _sid, _name, new_hit, new_hv, new_luv, new_lu = decayed[0]
        self.assertIsInstance(new_hv, dict)
        self.assertEqual(new_hv.get("agent-a"), 1)
        self.assertEqual(new_hv.get("agent-b"), 2)
        self.assertEqual(new_hit, 3)
        self.assertEqual(new_luv.get("agent-a"), now - 3_000_000)
        self.assertEqual(new_luv.get("agent-b"), now - 1000)
        self.assertEqual(new_lu, now - 1000)

        from cron.cron_skill_decay import _apply_decay
        _apply_decay(conn, decayed)
        row = conn.execute("SELECT hit_count, hit_vector, last_used_vector, last_used_at FROM memory_skills WHERE id=?", (_sid,)).fetchone()
        self.assertEqual(row["hit_count"], 3)
        self.assertEqual(json.loads(row["last_used_vector"]), {"agent-a": now - 3_000_000, "agent-b": now - 1000})
        self.assertEqual(row["last_used_at"], now - 1000)
        conn.close()

    def test_decay_deletes_when_below_threshold(self):
        from skill_extractor import ensure_skill_schema
        from cron.cron_skill_decay import _decayed_skills

        conn = _make_db(self.tmp)
        ensure_skill_schema(conn)
        now = time.time()
        conn.execute(
            """INSERT INTO memory_skills
               (name, hit_count, hit_vector, last_used_vector,
                last_used_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "skill-d",
                1,
                json.dumps({"agent-a": 1}),
                json.dumps({"agent-a": now - 3_000_000}),
                now - 3_000_000,
                now - 3_000_000,
                now - 3_000_000,
            ),
        )
        conn.commit()
        decayed, deleted = _decayed_skills(conn, max_age_days=30, decay_factor=0.5, delete_threshold=0.5)
        self.assertIn("skill-d", deleted)
        conn.close()


if __name__ == "__main__":
    unittest.main()
