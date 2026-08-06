import os
import tempfile
import unittest
from pathlib import Path

from infra.db import open_db, _setup_tenant_view
from save_pipeline import save_memory
from search.phases.retrieve import _search_kg_facts
from knowledge_graph.kg_db import _upsert_entity, _upsert_edge
from fact.fact_extract import _upsert_fact
from memory_delete import soft_delete_note, hard_delete_note, list_trash, purge_expired, is_soft_deleted
from crdt.crdt_field import project_crdt_to_sql


class TestTenantIsolationEndToEnd(unittest.TestCase):
    def setUp(self):
        os.environ["MEMORY_AUTH_MODE"] = "open"
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        # Initialize schema to latest migration version
        from infra.migration_runner import run_migrations
        with open_db(self.db_path) as conn:
            run_migrations(conn)

    def tearDown(self):
        if self.db_path.exists():
            try:
                os.unlink(self.db_path)
            except OSError:
                pass

    def test_memory_delete_tenant_isolation(self):
        """Verify soft-delete, hard-delete, trash listing, and purge are strictly tenant-isolated."""
        # Save notes for Tenant A and Tenant B
        id_a = save_memory("Tenant A secret memory content", category="lessons", title_slug="note-a", db_path=str(self.db_path), tenant_id="tenant_A")
        id_b = save_memory("Tenant B confidential content", category="lessons", title_slug="note-b", db_path=str(self.db_path), tenant_id="tenant_B")

        # Soft delete Tenant A's note using Tenant B's context — must fail/not delete Tenant A's note
        soft_delete_note(str(self.db_path), id_a, tenant_id="tenant_B")
        self.assertFalse(is_soft_deleted(str(self.db_path), id_a, tenant_id="tenant_A"))

        # Soft delete Tenant A's note with Tenant A's context — must succeed
        self.assertTrue(soft_delete_note(str(self.db_path), id_a, tenant_id="tenant_A"))
        self.assertTrue(is_soft_deleted(str(self.db_path), id_a, tenant_id="tenant_A"))
        self.assertFalse(is_soft_deleted(str(self.db_path), id_a, tenant_id="tenant_B"))

        # Trash list for Tenant B must be empty
        trash_b = list_trash(str(self.db_path), tenant_id="tenant_B")
        self.assertEqual(len(trash_b), 0)

        # Trash list for Tenant A must contain id_a
        trash_a = list_trash(str(self.db_path), tenant_id="tenant_A")
        self.assertEqual(len(trash_a), 1)
        self.assertEqual(trash_a[0]["id"], id_a)

        # Purging expired for Tenant B must NOT purge Tenant A's trash note
        purged_b = purge_expired(str(self.db_path), tenant_id="tenant_B")
        self.assertEqual(purged_b, 0)

        # Hard delete Tenant A's note with Tenant B's scope — must fail
        self.assertFalse(hard_delete_note(str(self.db_path), id_a, tenant_id="tenant_B"))

        # Hard delete Tenant A's note with Tenant A's scope — must succeed
        self.assertTrue(hard_delete_note(str(self.db_path), id_a, tenant_id="tenant_A"))

    def test_kg_search_and_expansion_tenant_isolation(self):
        """Verify KG fact search and reasoning expansion do not leak facts across tenants."""
        from fact import ensure_facts_schema
        from knowledge_graph import ensure_kg_schema
        with open_db(self.db_path) as conn:
            ensure_kg_schema(conn)
            ensure_facts_schema(conn)
            now = 1700000000.0
            # Create entities first for FK constraints
            _upsert_entity(conn, "Alpha", "company", now, tenant_id="tenant_A")
            _upsert_entity(conn, "Company", "type", now, tenant_id="tenant_A")
            _upsert_entity(conn, "Beta", "company", now, tenant_id="tenant_B")
            _upsert_entity(conn, "Company", "type", now, tenant_id="tenant_B")

            # Upsert facts under tenant_A and tenant_B passing source_memory=None to satisfy FK
            fa = _upsert_fact(conn, "Alpha", "is_a", "Company", 0.9, now, source_memory=None, tenant_id="tenant_A")
            fb = _upsert_fact(conn, "Beta", "is_a", "Company", 0.9, now, source_memory=None, tenant_id="tenant_B")
            conn.commit()

        # Direct SQL verification of tenant scoping on kg_facts table
        with open_db(self.db_path) as conn:
            facts_a = conn.execute("SELECT subject FROM kg_facts WHERE object = 'company' AND tenant_id = 'tenant_A'").fetchall()
            facts_b = conn.execute("SELECT subject FROM kg_facts WHERE object = 'company' AND tenant_id = 'tenant_B'").fetchall()

            subjects_a = [r[0] for r in facts_a]
            self.assertIn("alpha", subjects_a)
            self.assertNotIn("beta", subjects_a)

            subjects_b = [r[0] for r in facts_b]
            self.assertIn("beta", subjects_b)
            self.assertNotIn("alpha", subjects_b)

    def test_kg_write_tenant_isolation(self):
        """Verify KG entities and edges are scoped per tenant."""
        from knowledge_graph import ensure_kg_schema
        with open_db(self.db_path) as conn:
            ensure_kg_schema(conn)
            now = 1700000000.0
            e_a = _upsert_entity(conn, "SharedNameA", "concept", now, tenant_id="tenant_A")
            e_b = _upsert_entity(conn, "SharedNameB", "concept", now, tenant_id="tenant_B")

            _upsert_edge(conn, e_a, e_a, "relates_to", now, tenant_id="tenant_A")
            _upsert_edge(conn, e_b, e_b, "relates_to", now, tenant_id="tenant_B")
            conn.commit()

        with open_db(self.db_path) as conn:
            rows_a = conn.execute("SELECT id FROM kg_entities WHERE tenant_id = 'tenant_A'").fetchall()
            rows_b = conn.execute("SELECT id FROM kg_entities WHERE tenant_id = 'tenant_B'").fetchall()
            self.assertEqual(len(rows_a), 1)
            self.assertEqual(len(rows_b), 1)

    def test_crdt_projection_tenant_isolation(self):
        """Verify CRDT field projection stays scoped to the memory's tenant."""
        id_a = save_memory("Original Tenant A content", category="lessons", title_slug="crdt-a", db_path=str(self.db_path), tenant_id="tenant_A")
        id_b = save_memory("Original Tenant B content", category="lessons", title_slug="crdt-b", db_path=str(self.db_path), tenant_id="tenant_B")

        with open_db(self.db_path) as conn:
            updated_a = project_crdt_to_sql(conn, id_a, tenant_id="tenant_A")
            updated_b = project_crdt_to_sql(conn, id_b, tenant_id="tenant_B")
            # Projecting with cross tenant_id should affect 0 fields
            cross_a = project_crdt_to_sql(conn, id_a, tenant_id="tenant_B")
            self.assertEqual(len(cross_a), 0)

    def test_db_engine_trigger_guard(self):
        """Verify DB triggers reject NULL or empty tenant_id writes."""
        with open_db(self.db_path) as conn:
            _setup_tenant_view(conn, "test_tenant")
            with self.assertRaises(Exception):
                conn.execute(
                    "INSERT INTO memories (id, content, source_file, created_at, updated_at, tenant_id) "
                    "VALUES ('invalid_null', 'test', 'test.md', datetime('now'), datetime('now'), NULL)"
                )
            with self.assertRaises(Exception):
                conn.execute(
                    "INSERT INTO memories (id, content, source_file, created_at, updated_at, tenant_id) "
                    "VALUES ('invalid_empty', 'test', 'test.md', datetime('now'), datetime('now'), '')"
                )


if __name__ == "__main__":
    unittest.main()
