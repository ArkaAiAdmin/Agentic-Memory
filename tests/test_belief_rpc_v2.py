"""Tests for SRV-16: Kernel Real Belief Lifecycle RPCs (/api/v1/beliefs/*)."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from belief.belief_lifecycle import ensure_belief_assertion
from eval._fixtures import bootstrap_temp_db_clean
from infra.api_server import APIServer
from infra.db import open_db
from infra.memory_common import connection_pool


class TestBeliefLifecycleRPCv2(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_dir = tempfile.mkdtemp(prefix="belief_rpc_v2_")
        cls.db_path = Path(cls.tmp_dir) / "memory.db"
        cls.old_db_path = os.environ.get("MEMORY_DB_PATH")
        cls.old_rerank = os.environ.get("MEMORY_RERANKER_DISABLED")
        cls.old_journal = os.environ.get("MEMORY_WRITE_JOURNAL_ENABLED")
        os.environ["MEMORY_DB_PATH"] = str(cls.db_path)
        os.environ["MEMORY_RERANKER_DISABLED"] = "true"
        os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = "0"

        connection_pool.clear()
        bootstrap_temp_db_clean(cls.db_path)
        cls.token = secrets.token_hex(16)
        cls.server = APIServer(
            db_path=cls.db_path,
            agent_id="test-belief-agent",
            host="127.0.0.1",
            port=0,
            token=cls.token,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        connection_pool.clear()
        if cls.old_db_path is not None:
            os.environ["MEMORY_DB_PATH"] = cls.old_db_path
        else:
            os.environ.pop("MEMORY_DB_PATH", None)
        if cls.old_rerank is not None:
            os.environ["MEMORY_RERANKER_DISABLED"] = cls.old_rerank
        else:
            os.environ.pop("MEMORY_RERANKER_DISABLED", None)
        if cls.old_journal is not None:
            os.environ["MEMORY_WRITE_JOURNAL_ENABLED"] = cls.old_journal
        else:
            os.environ.pop("MEMORY_WRITE_JOURNAL_ENABLED", None)
        import shutil

        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def setUp(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DELETE FROM belief_assertions")
            conn.execute("DELETE FROM kg_facts")
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM memory_events")
            conn.commit()
        finally:
            conn.close()

    def _auth_headers(self, tenant_id: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if tenant_id:
            headers["X-Tenant-ID"] = tenant_id
        return headers

    def test_auth_required_on_all_belief_endpoints(self) -> None:
        for method, path, body in (
            ("GET", "/api/v1/beliefs", None),
            ("POST", "/api/v1/beliefs/1/confirm", {}),
            ("POST", "/api/v1/beliefs/1/pin", {}),
            ("POST", "/api/v1/beliefs/1/supersede", {"content": "updated"}),
            ("POST", "/api/v1/beliefs/1/retract", {"reason": "stale"}),
        ):
            status, _ = self.server.handle_request_direct(
                method=method,
                path=path,
                body=body,
                headers={"Authorization": "Bearer wrong-token"},
            )
            self.assertEqual(status, 401, f"Expected 401 for {method} {path}")

    def test_confirm_and_pin_kg_belief_in_place_no_duplication(self) -> None:
        with open_db(self.db_path, write=True) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, source_file, tags, category, created_at, updated_at, observed_at, pinned, score, tenant_id) "
                "VALUES ('beliefs/linked-1', 'Service uses WAL mode', 'sess-1', '[]', 'belief', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', 0, 0.4, 'default')"
            )
            conn.execute(
                "INSERT INTO kg_facts (id, subject, predicate, object, confidence, tenant_id) "
                "VALUES (5001, 'SQLite', 'uses_mode', 'WAL', 0.4, 'default')"
            )
            ba_id = ensure_belief_assertion(
                conn,
                5001,
                memory_id="beliefs/linked-1",
                belief_status="unconfirmed",
                confidence=0.4,
            )
            conn.commit()

        self.assertIsNotNone(ba_id)

        # Confirm
        status, confirmed = self.server.handle_request_direct(
            method="POST",
            path=f"/api/v1/beliefs/{ba_id}/confirm",
            body={},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["confidence"], 1.0)
        self.assertEqual(confirmed["belief_status"], "active")
        self.assertGreaterEqual(confirmed["evidence_count"], 2)
        self.assertIn("source_sessions", confirmed)
        self.assertIn("updated_at", confirmed)

        # Pin
        status, pinned = self.server.handle_request_direct(
            method="POST",
            path=f"/api/v1/beliefs/{ba_id}/pin",
            body={},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200, pinned)
        self.assertTrue(pinned["pinned"])
        self.assertEqual(pinned["confidence"], 1.0)

        # Verify zero duplicate notes or belief assertions and outbox events emitted
        conn = sqlite3.connect(str(self.db_path))
        try:
            mem_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            ba_count = conn.execute("SELECT COUNT(*) FROM belief_assertions").fetchone()[0]
            mem_pinned = conn.execute("SELECT pinned FROM memories WHERE id = 'beliefs/linked-1'").fetchone()[0]
            events = conn.execute(
                "SELECT event_type, payload FROM memory_events WHERE event_type = 'belief.updated' ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(mem_count, 1)
        self.assertEqual(ba_count, 1)
        self.assertEqual(mem_pinned, 1)
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[0][1])["action"], "confirm")
        self.assertEqual(json.loads(events[1][1])["action"], "pin")

    def test_supersede_cascades_dependent_beliefs_and_lineage_filter(self) -> None:
        with open_db(self.db_path, write=True) as conn:
            conn.execute(
                "INSERT INTO kg_facts (id, subject, predicate, object, confidence, tenant_id) "
                "VALUES (6001, 'Auth', 'uses_protocol', 'JWT-v1', 0.9, 'default')"
            )
            conn.execute(
                "INSERT INTO kg_facts (id, subject, predicate, object, confidence, tenant_id) "
                "VALUES (6002, 'Gateway', 'relies_on', 'JWT-v1', 0.85, 'default')"
            )
            root_ba = ensure_belief_assertion(conn, 6001, belief_status="active", confidence=0.9)
            dep_ba = ensure_belief_assertion(
                conn,
                6002,
                belief_status="active",
                confidence=0.85,
                evidence_chain=[6001],
            )
            conn.commit()

        # Missing content returns 400
        bad_status, _ = self.server.handle_request_direct(
            method="POST",
            path=f"/api/v1/beliefs/{root_ba}/supersede",
            body={"content": "   "},
            headers=self._auth_headers(),
        )
        self.assertEqual(bad_status, 400)

        status, res = self.server.handle_request_direct(
            method="POST",
            path=f"/api/v1/beliefs/{root_ba}/supersede",
            body={"content": "JWT-v2 with EdDSA", "reason": "Upgraded signing algorithm"},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200, res)
        self.assertIn("superseded", res)
        self.assertIn("replacement", res)
        self.assertEqual(res["superseded"]["belief_status"], "deprecated")
        self.assertEqual(res["superseded"]["superseded_by"], res["replacement"]["id"])
        self.assertEqual(res["replacement"]["content"], "JWT-v2 with EdDSA")
        self.assertEqual(res["replacement"]["belief_status"], "active")

        # Dependent belief citing fact 6001 should have been deprecated by retract_dependent_beliefs
        conn = sqlite3.connect(str(self.db_path))
        try:
            dep_status = conn.execute(
                "SELECT belief_status FROM belief_assertions WHERE id = ?",
                (dep_ba,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(dep_status, "deprecated")

        # GET /api/v1/beliefs defaults include_superseded=true (returns both old & new)
        status_all, data_all = self.server.handle_request_direct(
            method="GET",
            path="/api/v1/beliefs",
            headers=self._auth_headers(),
        )
        self.assertEqual(status_all, 200)
        ids_all = {b["id"] for b in data_all["beliefs"]}
        self.assertIn(str(root_ba), ids_all)
        self.assertIn(res["replacement"]["id"], ids_all)

        # include_superseded=false excludes the superseded original
        status_no_sup, data_no_sup = self.server.handle_request_direct(
            method="GET",
            path="/api/v1/beliefs?include_superseded=false",
            headers=self._auth_headers(),
        )
        self.assertEqual(status_no_sup, 200)
        ids_no_sup = {b["id"] for b in data_no_sup["beliefs"]}
        self.assertNotIn(str(root_ba), ids_no_sup)
        self.assertIn(res["replacement"]["id"], ids_no_sup)

    def test_retract_preserves_audit_trail_and_handles_category_belief_memories(self) -> None:
        with open_db(self.db_path, write=True) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, source_file, tags, category, created_at, updated_at, observed_at, pinned, score, tenant_id) "
                "VALUES ('beliefs/mem-1', 'Cache TTL is 60s', 'sess-9', '[\"belief\"]', 'belief', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', 0, 0.6, 'default')"
            )
            conn.commit()

        # Listed in GET /api/v1/beliefs
        status, listed = self.server.handle_request_direct(
            method="GET",
            path="/api/v1/beliefs",
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(listed["count"], 1)
        b = listed["beliefs"][0]
        self.assertEqual(b["id"], "beliefs/mem-1")
        self.assertEqual(b["source_sessions"], ["sess-9"])

        # Pin via URL-encoded ID without duplicating
        status, pinned = self.server.handle_request_direct(
            method="POST",
            path="/api/v1/beliefs/beliefs%2Fmem-1/pin",
            body={},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200, pinned)
        self.assertTrue(pinned["pinned"])
        self.assertEqual(pinned["confidence"], 1.0)

        # Retract with reason
        status, retracted = self.server.handle_request_direct(
            method="POST",
            path="/api/v1/beliefs/beliefs%2Fmem-1/retract",
            body={"reason": "TTL changed to 10s"},
            headers=self._auth_headers(),
        )
        self.assertEqual(status, 200, retracted)
        self.assertEqual(retracted["belief_status"], "retracted")
        self.assertEqual(retracted["rationale"], "TTL changed to 10s")

        # Audit trail: row still exists in memories and is visible via status=retracted
        status_ret, data_ret = self.server.handle_request_direct(
            method="GET",
            path="/api/v1/beliefs?status=retracted",
            headers=self._auth_headers(),
        )
        self.assertEqual(status_ret, 200)
        self.assertEqual(data_ret["count"], 1)
        self.assertEqual(data_ret["beliefs"][0]["belief_status"], "retracted")

    def test_tenant_isolation_and_not_found(self) -> None:
        with open_db(self.db_path, write=True, tenant_id="tenant-a") as conn:
            conn.execute(
                "INSERT INTO kg_facts (id, subject, predicate, object, confidence, tenant_id) "
                "VALUES (7001, 'TenantA', 'owns', 'Secret', 0.8, 'tenant-a')"
            )
            ba_id = ensure_belief_assertion(conn, 7001, belief_status="active", confidence=0.8, tenant_id="tenant-a")
            conn.commit()

        # Tenant B cannot confirm or see Tenant A's belief
        status_b, _ = self.server.handle_request_direct(
            method="POST",
            path=f"/api/v1/beliefs/{ba_id}/confirm",
            body={},
            headers=self._auth_headers(tenant_id="tenant-b"),
        )
        self.assertEqual(status_b, 404)

        status_get_b, data_b = self.server.handle_request_direct(
            method="GET",
            path="/api/v1/beliefs",
            headers=self._auth_headers(tenant_id="tenant-b"),
        )
        self.assertEqual(status_get_b, 200)
        self.assertEqual(data_b["count"], 0)

    def test_supersede_rejects_cross_tenant_kg_fact_hijack_and_self_supersede(self) -> None:
        with open_db(self.db_path, write=True, tenant_id="tenant-a") as conn:
            conn.execute(
                "INSERT INTO kg_facts (id, subject, predicate, object, confidence, tenant_id) "
                "VALUES (8001, 'Runtime', 'uses_db', 'Postgres-16', 0.9, 'tenant-a')"
            )
            ba_a = ensure_belief_assertion(conn, 8001, belief_status="active", confidence=0.9, tenant_id="tenant-a")
            conn.commit()

        with open_db(self.db_path, write=True, tenant_id="tenant-b") as conn:
            conn.execute(
                "INSERT INTO kg_facts (id, subject, predicate, object, confidence, tenant_id) "
                "VALUES (8002, 'Runtime', 'uses_db', 'SQLite-3', 0.7, 'tenant-b')"
            )
            ba_b = ensure_belief_assertion(conn, 8002, belief_status="active", confidence=0.7, tenant_id="tenant-b")
            conn.commit()

        # Self-supersede with identical fact object must be rejected (400) without creating a self-loop
        self_status, self_err = self.server.handle_request_direct(
            method="POST",
            path=f"/api/v1/beliefs/{ba_b}/supersede",
            body={"content": "SQLite-3"},
            headers=self._auth_headers(tenant_id="tenant-b"),
        )
        self.assertEqual(self_status, 400, self_err)

        # Tenant B superseding with content colliding with Tenant A's (subject, predicate, object)
        # must NOT hijack Tenant A's kg_facts row 8001 or overwrite its tenant_id
        sup_status, sup_res = self.server.handle_request_direct(
            method="POST",
            path=f"/api/v1/beliefs/{ba_b}/supersede",
            body={"content": "Postgres-16", "reason": "Migrated"},
            headers=self._auth_headers(tenant_id="tenant-b"),
        )
        self.assertEqual(sup_status, 200, sup_res)
        self.assertEqual(sup_res["superseded"]["id"], str(ba_b))
        self.assertEqual(sup_res["superseded"]["belief_status"], "deprecated")
        self.assertNotEqual(sup_res["replacement"]["id"], str(ba_b))
        self.assertNotEqual(sup_res["replacement"]["id"], str(ba_a))
        self.assertIsNone(sup_res["replacement"]["fact_id"])
        self.assertEqual(sup_res["replacement"]["content"], "Postgres-16")

        conn = sqlite3.connect(str(self.db_path))
        try:
            row_a = conn.execute(
                "SELECT tenant_id, supersedes, belief_status FROM kg_facts WHERE id = 8001"
            ).fetchone()
            ba_row_b = conn.execute(
                "SELECT belief_status, superseded_by FROM belief_assertions WHERE id = ?",
                (ba_b,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row_a[0], "tenant-a")
        self.assertIsNone(row_a[1])
        self.assertEqual(row_a[2], "active")
        self.assertEqual(ba_row_b[0], "deprecated")
        self.assertEqual(ba_row_b[1], sup_res["replacement"]["id"])


if __name__ == "__main__":
    unittest.main()
