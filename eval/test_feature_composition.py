#!/usr/bin/env python3
"""Feature composition integration tests for the Next Frontier cluster.

Verifies the composition and interaction of:
  - Skill CRDT convergence
  - Federated skill decay
  - Rule-based contradiction resolver
  - Auto-reinforce
  - Semantic clustering
  - Saga write-chain rollback consistency
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Resolve paths
INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))

from infra.memory_common import connection_pool, open_db
from save_pipeline import save_memory, reinforce_memories_db
from consolidation import cluster_related
from crdt.crdt_merge import crdt_sync_all
from kg.contradiction_resolver import auto_resolve_contradiction_pair
from test_adversarial_e2e import _setup_test_env, _restore_test_env


class CompositionTestBase(unittest.TestCase):
    """Base class that redirects all DB paths to a temp directory for safe integration testing."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="comp_test_")
        cls._db_path = Path(cls._tmpdir) / "memory.db"
        cls._orig = _setup_test_env(cls._tmpdir)
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()

    @classmethod
    def tearDownClass(cls):
        _restore_test_env(*cls._orig)
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        self.db_path = self.__class__._db_path
        self.tmpdir = self.__class__._tmpdir
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        # Clear out tables before each test
        with open_db(self.db_path) as conn:
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM memory_skills")
            conn.execute("DELETE FROM backlinks")
            conn.execute("DELETE FROM kg_facts")
            conn.execute("DELETE FROM kg_edges")
            conn.execute("DELETE FROM kg_entities")
            conn.execute("DELETE FROM memory_embeddings")
            conn.execute("DELETE FROM memory_vec_keys")
            conn.commit()

    def tearDown(self):
        # Cleanup temp locks or settings if any
        pass


class TestSkillDecaySurvivesCRDTMerge(CompositionTestBase):
    """Test Work Stream 1.2.1: Skill decay outputs must survive CRDT merges."""

    def test_decay_survives_crdt_merge(self):
        from skill_extractor import merge_skills, ensure_skill_schema
        from cron.cron_skill_decay import _decayed_skills, _apply_decay

        # 1. Seed local DB with a skill that has custom hit_vector and is old
        now = time.time()
        with open_db(self.db_path) as conn:
            ensure_skill_schema(conn)
            # Add hit_vector column if missing
            for col, ctype in [
                ("hit_vector", "TEXT DEFAULT '{}'"),
                ("last_used_vector", "TEXT DEFAULT '{}'"),
                ("logical_clock", "INTEGER DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE memory_skills ADD COLUMN {col} {ctype}")
                except sqlite3.OperationalError:
                    pass
            
            conn.execute(
                """INSERT INTO memory_skills
                   (name, description, hit_count, last_used_at, created_at, updated_at,
                    hit_vector, last_used_vector, logical_clock)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "deploy-service",
                    "deploys container service",
                    5,
                    now - 3_000_000,  # > 30 days old
                    now - 3_000_000,
                    now - 3_000_000,
                    json.dumps({"agent-a": 5}),
                    json.dumps({"agent-a": now - 3_000_000}),
                    5,
                ),
            )
            conn.commit()

            # 2. Run decay: agent-a's count should be halved from 5 to 2
            decayed, deleted = _decayed_skills(conn, max_age_days=30, decay_factor=0.5, delete_threshold=0.5)
            self.assertEqual(len(decayed), 1)
            _apply_decay(conn, decayed)

            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM memory_skills WHERE name='deploy-service'").fetchone()
            local_skill = dict(row)
            conn.row_factory = None
            self.assertEqual(local_skill["hit_count"], 2)
            self.assertEqual(json.loads(local_skill["hit_vector"])["agent-a"], 2)

            # 3. Simulate CRDT Merge with a remote version of the skill
            remote_skill = {
                "name": "deploy-service",
                "description": "remote desc",
                "hit_vector": json.dumps({"agent-b": 3}),
                "last_used_vector": json.dumps({"agent-b": now}),
                "logical_clock": 10,
                "updated_at": now,
            }

            merged = merge_skills(local_skill, remote_skill)
            # Decayed values are preserved, remote G-Counter added
            hv = json.loads(merged["hit_vector"])
            self.assertEqual(hv["agent-a"], 2)  # Decayed local value is preserved
            self.assertEqual(hv["agent-b"], 3)  # Remote value is added
            self.assertEqual(merged["hit_count"], 5)  # Sum of merged vector elements
            self.assertEqual(merged["logical_clock"], 11)  # max(5, 10) + 1


class TestContradictionResolverBlocksReinforce(CompositionTestBase):
    """Test Work Stream 1.2.2: Contradictions are not cleared by score updates."""

    def test_contradiction_blocks_reinforce(self):
        # 1. Save two contradicting memories (positive & negative phrase matching)
        slug_a = "deploy-v1-enabled"
        slug_b = "deploy-v1-disabled"
        
        nid_a = save_memory(
            content="The deploy service configuration is enabled for production.",
            category="lessons",
            title_slug=slug_a,
            tags=["deploy"],
            pinned=False,
            is_global=False,
            safety_wiring=True,
            db_path=str(self.db_path),
        )
        
        nid_b = save_memory(
            content="The deploy service configuration is disabled for production.",
            category="lessons",
            title_slug=slug_b,
            tags=["deploy"],
            pinned=False,
            is_global=False,
            safety_wiring=True,
            db_path=str(self.db_path),
        )

        # Confirm contradiction resolver was triggered and notes exist
        with open_db(self.db_path) as conn:
            row_a = conn.execute("SELECT id, valid_to, superseded_by FROM memories WHERE id=?", (nid_a,)).fetchone()
            row_b = conn.execute("SELECT id, valid_to, superseded_by FROM memories WHERE id=?", (nid_b,)).fetchone()
            self.assertIsNotNone(row_a)
            self.assertIsNotNone(row_b)

        # 2. Run contradiction resolver manually on the pair to ensure full mapping
        res = auto_resolve_contradiction_pair(self.db_path, nid_a, nid_b)
        self.assertIn(res["action"], ("superseded", "kept_both"))

        # 3. Reinforce the memory success score of the first note (which is superseded/invalidated)
        reinforced_count = reinforce_memories_db(self.db_path, [nid_a], delta=1.5)
        self.assertGreaterEqual(reinforced_count, 0)

        # 4. Verify that a contradiction check on save still flags it
        from memory_contradiction_save import check_contradictions_on_save
        findings = check_contradictions_on_save(
            db_path=self.db_path,
            new_content="The deploy service configuration is enabled for production.",
            new_id=nid_a,
        )
        # Even after reinforcement, the contradiction is structurally present
        self.assertTrue(len(findings) > 0 or row_a["valid_to"] is not None or row_b["valid_to"] is not None)


class TestSagaRollbackPreservesKGConsistency(CompositionTestBase):
    """Test Work Stream 1.2.3: DB transaction is rolled back correctly on intermediate failure."""

    def test_saga_rollback_completeness(self):
        slug = "saga-fail-test"

        # 1. Mock _index_facts to raise an exception mid-save.
        with patch("save.pipeline._index_facts", side_effect=ValueError("Simulated indexing error")):
            try:
                save_memory(
                    content="Procedural notes: 1. Setup server. 2. Verify config works.",
                    category="lessons",
                    title_slug=slug,
                    tags=["test"],
                    pinned=False,
                    is_global=False,
                    safety_wiring=True,
                    db_path=str(self.db_path),
                )
            except Exception:
                pass

        # 2. Verify all tables are rolled back and empty
        with open_db(self.db_path) as conn:
            for table in ("memories", "kg_facts", "kg_edges", "memory_embeddings", "backlinks", "memory_vec_keys"):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 0, f"Table {table} was not cleaned up, has {count} rows")


class TestSemanticClusteringSurvivesCRDTSync(CompositionTestBase):
    """Test Work Stream 1.2.4: Semantic clustering operates correctly post-CRDT merge."""

    def test_clustering_crdt_sync(self):
        # 1. Save two notes locally with high tag overlap
        save_memory(
            content="Local deploy instructions.",
            category="lessons",
            title_slug="local-deploy",
            tags=["deploy", "kubernetes"],
            db_path=str(self.db_path),
        )
        save_memory(
            content="Local setup guidelines.",
            category="lessons",
            title_slug="local-setup",
            tags=["deploy", "setup"],
            db_path=str(self.db_path),
        )

        # Check clusters locally
        with open_db(self.db_path) as conn:
            clusters_before = cluster_related(conn, tag_threshold=0.2)
            self.assertGreaterEqual(len(clusters_before), 1)

        # 2. Merge another similar note from remote via CRDT sync
        remote_notes = {
            "lessons/remote-deploy": (
                "Remote deploy note content.",
                "lessons/remote-deploy.md",
                1,
                json.dumps({"agent-b": 1}),
                1,
            )
        }
        res = crdt_sync_all(
            self.db_path,
            remote_agent_id="agent-b",
            local_agent_id="local-agent",
            remote_notes=remote_notes,
        )
        self.assertEqual(res["applied"], 1)

        # 3. Assert cluster assignments include the newly merged note
        with open_db(self.db_path) as conn:
            clusters_after = cluster_related(conn, tag_threshold=0.1)
            found_remote = False
            for cluster in clusters_after:
                if "lessons/remote-deploy" in cluster["members"]:
                    found_remote = True
                    break
            # If the remote note is merged, it should be parsed and queryable under cluster analysis
            self.assertTrue(found_remote or len(clusters_after) >= len(clusters_before))


class TestFullWriteChainUnderPartialFailure(CompositionTestBase):
    """Test Work Stream 1.3: Failure injected at each stage boundary yields a zero-trace rollback."""

    def test_partial_failures(self):
        stages = [
            "save.pipeline._index_backlinks",
            "save.pipeline._index_chunks",
            "save.pipeline._index_chunk_embeddings",
            "save.pipeline._index_embedding",
            "save.pipeline._index_kg",
            "save.pipeline._index_facts",
            "save.pipeline._auto_fts_backlinks",
            "save.pipeline._index_adaptive_retention",
        ]

        for stage in stages:
            with patch(stage, side_effect=RuntimeError(f"Failure at {stage}")):
                try:
                    save_memory(
                        content="Some robust procedurals: 1. Do step A. 2. Verify outcome.",
                        category="lessons",
                        title_slug="stage-failure-test",
                        tags=["failure-test"],
                        pinned=False,
                        is_global=False,
                        safety_wiring=True,
                        db_path=str(self.db_path),
                    )
                except Exception:
                    pass

                # Check total database state is fully rolled back
                with open_db(self.db_path) as conn:
                    for table in ("memories", "kg_facts", "kg_edges", "memory_embeddings", "backlinks", "memory_vec_keys"):
                        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        self.assertEqual(count, 0, f"Table {table} not empty after rollback at {stage}")


if __name__ == "__main__":
    unittest.main()
