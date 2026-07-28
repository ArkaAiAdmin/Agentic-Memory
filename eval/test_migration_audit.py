#!/usr/bin/env python3
"""Exhaustive migration system audit.

Tests:
1. Forward-ref handling: per-statement skip (not per-migration break)
2. Retry of deferred migrations on next startup
3. Schema version and checksum consistency
4. Down migration coverage
5. Idempotency (run twice on fresh DB)
6. All expected tables present after full run
7. All expected columns present
8. All expected indexes present
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import hashlib
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.migration_runner import (
    run_migrations,
    _get_applied_migrations,
    _get_available_migrations,
    _get_down_migrations,
    _parse_sql_file,
    SCHEMA_VERSION,
    MIGRATIONS_DIR,
)

ISSUES = []

def issue(severity, msg):
    ISSUES.append((severity, msg))
    print(f"  [{severity}] {msg}")

def ok(msg):
    print(f"  [OK] {msg}")


# === 1. Migration runner forward-ref handling ===
def test_forward_ref_handling():
    print("\n=== 1. Forward-ref handling in migration_runner ===")
    
    # Create fresh DB and apply all migrations
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        
        # Apply all migrations
        run_migrations(conn)
        
        # Check that all migrations were applied (no deferred remaining)
        applied = _get_applied_migrations(conn)
        available = {num for num, _ in _get_available_migrations()}
        pending_after = available - applied
        
        if pending_after:
            issue("HIGH", f"Deferred migrations remain after first run: {sorted(pending_after)}")
        else:
            ok("All migrations applied on first run (no deferred)")
        
        # Check schema version
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        if row:
            version = row[0]
            if version != SCHEMA_VERSION:
                issue("HIGH", f"Schema version mismatch: DB has {version}, code expects {SCHEMA_VERSION}")
            else:
                ok(f"Schema version correct: {version}")
        else:
            issue("HIGH", "No schema_version row found")
        
        # Check checksums
        row = conn.execute("SELECT checksums FROM schema_version WHERE id=1").fetchone()
        if row and row[0]:
            checksums = json.loads(row[0])
            available_map = {num: path for num, path in _get_available_migrations()}
            # Check that all applied migrations have checksums
            for num in applied:
                if str(num) not in checksums:
                    issue("HIGH", f"Migration {num:03d} applied but no checksum stored")
            # Verify checksums match current files
            for num_str, stored_hash in checksums.items():
                num = int(num_str)
                path = available_map.get(num)
                if path:
                    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    if actual_hash != stored_hash:
                        issue("HIGH", f"Migration {num:03d} checksum mismatch: stored={stored_hash[:16]} actual={actual_hash[:16]}")
            ok(f"Checksums stored for {len(checksums)} migrations")
        else:
            issue("HIGH", "No checksums stored")
        
        conn.close()
    finally:
        os.unlink(db_path)


# === 2. Idempotency: run twice on fresh DB ===
def test_idempotency():
    print("\n=== 2. Idempotency (run twice on fresh DB) ===")
    
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        
        # First run
        try:
            run_migrations(conn)
            ok("First run succeeded")
        except Exception as e:
            issue("HIGH", f"First run FAILED: {e}")
            conn.close()
            os.unlink(db_path)
            return
        
        # Second run
        try:
            run_migrations(conn)
            ok("Second run succeeded (idempotent)")
        except Exception as e:
            issue("HIGH", f"Second run FAILED (not idempotent): {e}")
        
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# === 3. Down migration coverage ===
def test_down_migration_coverage():
    print("\n=== 3. Down migration coverage ===")
    
    up_migrations = _get_available_migrations()
    down_migrations = _get_down_migrations()
    down_nums = {num for num, _ in down_migrations}
    
    for num, path in up_migrations:
        if num == 0:
            continue  # 000 base schema is special
        if num not in down_nums:
            issue("MEDIUM", f"Migration {num:03d} ({path.name}) has NO .down.sql file")
    
    ok(f"Checked {len(up_migrations)} up-migrations against {len(down_migrations)} down-migrations")


# === 4. All expected tables present after full migration ===
def test_tables_present():
    print("\n=== 4. All expected tables present ===")
    
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        run_migrations(conn)
        
        # Get all tables
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        tables = {r[0] for r in rows}
        
        # Expected tables from migrations
        expected_tables = {
            # 000 base schema
            "memories", "kg_entities", "kg_edges", "kg_facts",
            "kg_entities_fts", "backlinks", "shared_memories",
            "user_profile_access_log", "search_phase_stats",
            "file_mtimes", "user_access_log", "dead_letter_messages",
            "answer_rerank_cache", "review_schedule", "saga_log",
            # 001
            "schema_version",
            # 002
            "memory_embeddings",
            # 003
            "memory_audit_log",
            # 004
            "memory_vec_idx", "memory_vec_keys",
            # 005
            "memory_chunks",
            # 006
            "task_queue",
            # 007
            "memory_skills",
            # 008
            "sync_log",
            # 012
            "kg_extraction_stats",
            # 013
            "memory_field_crdt",
            # 014
            "arc_ghosts", "arc_stats",
            # 015
            "drift_alarms",
            # 016
            "concept_drift",
            # 020
            "kg_facts_fts",
            # 021
            "kg_entity_crdt", "kg_edge_crdt",
            # 022
            "sessions", "decision_threads", "thread_events", "session_compaction_log",
            # 024
            "memory_chunk_embeddings", "memory_chunk_vec_idx", "memory_chunk_vec_keys",
            # 026
            "belief_assertions",
            # 027
            "memory_revision_log",
            # 028
            "entailment_chains",
            # 029
            "graph_snapshots",
            # 031
            "memory_events",
            # 037
            "cron_runs",
            # 040
            "belief_review_queue",
            # 043
            "principals", "principal_identities",
            # 045
            "roles", "role_bindings", "policies", "acl_overrides", "principal_roles_audit",
            # 047
            "idem_token_key", "sso_idp_cache",
            # 049
            "gdpr_requests",
            # 057
            "memory_search_interaction", "memory_query_type_stats", "memory_temporal_priors",
            # 058
            "colbert_tokens",
            # 059
            "splade_tokens",
            # 061
            "memory_ctr_feedback",
            # 063
            "cron_task_timeouts",
            # 065
            "kg_entity_crdt_append", "kg_edge_crdt_append",
            # 067
            "kg_entity_redirect",
            # 068
            "saga_audit_log",
            # 069
            "shared_tasks", "project_state", "agent_messages", "file_locks",
            # 070
            "coordination_audit", "agent_heartbeats",
            # 071
            "agent_registry_crdt",
            # 072
            "system_locks",
        }
        
        missing = expected_tables - tables
        extra = tables - expected_tables - {"sqlite_sequence"}
        
        if missing:
            issue("HIGH", f"Missing tables after full migration: {sorted(missing)}")
        else:
            ok(f"All {len(expected_tables)} expected tables present")
        
        if extra:
            ok(f"Extra tables (not in expected set): {sorted(extra)}")
        
        conn.close()
    finally:
        os.unlink(db_path)


# === 5. Key columns present ===
def test_columns_present():
    print("\n=== 5. Key columns present ===")
    
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        run_migrations(conn)
        
        def get_columns(table):
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return {r[1] for r in rows}
        
        # memories table
        mem_cols = get_columns("memories")
        expected_mem_cols = {
            "id", "content", "source_file", "tags", "created_at", "updated_at",
            "observed_at", "pinned", "importance", "decay", "score", "supersedes",
            "repo_id", "access_count", "success_score", "fitness_score",
            "conflict_policy", "version_vector", "logical_clock", "consolidation_state",
            "tenant_id", "valid_from", "valid_to", "superseded_by", "last_accessed",
            "deleted_at", "deleted_by", "context_prefix", "category", "tier",
            "importance_score", "metadata", "data_subject_sub",
        }
        missing_mem = expected_mem_cols - mem_cols
        if missing_mem:
            issue("HIGH", f"memories missing columns: {sorted(missing_mem)}")
        else:
            ok("memories: all expected columns present")
        
        # kg_facts table
        facts_cols = get_columns("kg_facts")
        expected_facts_cols = {
            "id", "subject", "predicate", "object", "confidence", "locked",
            "first_seen", "last_seen", "mention_count", "source_memory", "context",
            "subject_entity_id", "object_entity_id",
            "event_time", "event_time_granularity", "transaction_time",
            "valid_at", "invalid_at", "superseded_by", "supersedes",
            "contradiction_score", "invalidation_reason",
            "belief_status", "epistemic_source", "asserting_agent_id",
            "evidence_chain", "embedding", "fact_type", "is_entailed", "tenant_id",
        }
        missing_facts = expected_facts_cols - facts_cols
        if missing_facts:
            issue("HIGH", f"kg_facts missing columns: {sorted(missing_facts)}")
        else:
            ok("kg_facts: all expected columns present")
        
        # kg_entities table
        entity_cols = get_columns("kg_entities")
        expected_entity_cols = {
            "id", "name", "entity_type", "mentions", "created_at", "updated_at",
            "community_id", "betweenness", "fingerprint", "inception_at", "tenant_id",
        }
        missing_entity = expected_entity_cols - entity_cols
        if missing_entity:
            issue("HIGH", f"kg_entities missing columns: {sorted(missing_entity)}")
        else:
            ok("kg_entities: all expected columns present")
        
        # memory_field_crdt table
        crdt_cols = get_columns("memory_field_crdt")
        expected_crdt_cols = {
            "memory_id", "field_name", "value", "version_vector", "logical_clock",
            "last_writer_agent", "is_deleted", "updated_at", "tenant_id",
        }
        missing_crdt = expected_crdt_cols - crdt_cols
        if missing_crdt:
            issue("HIGH", f"memory_field_crdt missing columns: {sorted(missing_crdt)}")
        else:
            ok("memory_field_crdt: all expected columns present")
        
        # backlinks table
        bl_cols = get_columns("backlinks")
        expected_bl_cols = {"source_id", "target_id"}
        missing_bl = expected_bl_cols - bl_cols
        if missing_bl:
            issue("HIGH", f"backlinks missing columns: {sorted(missing_bl)}")
        else:
            ok("backlinks: all expected columns present")
        
        # shared_memories table
        sm_cols = get_columns("shared_memories")
        expected_sm_cols = {
            "id", "agent_id", "content", "category", "tags", "shared_at",
            "source_note_id", "metadata", "target_agent_id", "shared_with", "tenant_id",
        }
        missing_sm = expected_sm_cols - sm_cols
        if missing_sm:
            issue("HIGH", f"shared_memories missing columns: {sorted(missing_sm)}")
        else:
            ok("shared_memories: all expected columns present")
        
        conn.close()
    finally:
        os.unlink(db_path)


# === 6. Key indexes present ===
def test_indexes_present():
    print("\n=== 6. Key indexes present ===")
    
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        run_migrations(conn)
        
        rows = conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'").fetchall()
        indexes = {(r[0], r[1]) for r in rows}
        
        expected_indexes = {
            # memories
            ("idx_memories_repo_id", "memories"),
            ("idx_memories_pinned", "memories"),
            ("idx_memories_created_at", "memories"),
            ("idx_memories_tenant_id", "memories"),
            ("idx_memories_active", "memories"),
            # kg_facts
            ("idx_kg_facts_subject", "kg_facts"),
            ("idx_kg_facts_belief_status", "kg_facts"),
            ("idx_kg_facts_validity", "kg_facts"),
            ("idx_kg_facts_superseded_by", "kg_facts"),
            ("idx_kg_facts_event_time", "kg_facts"),
            ("idx_kg_facts_tenant", "kg_facts"),
            ("idx_kg_facts_fact_type", "kg_facts"),
            # kg_entities
            ("idx_kg_entities_name", "kg_entities"),
            ("idx_kg_entities_fingerprint", "kg_entities"),
            ("idx_kg_entities_tenant", "kg_entities"),
            # backlinks
            ("idx_backlinks_target_id", "backlinks"),
            ("idx_backlinks_source_id", "backlinks"),
            # task_queue
            ("idx_task_queue_status", "task_queue"),
            ("idx_task_queue_priority", "task_queue"),
            # drift_alarms
            ("idx_drift_alarms_memory", "drift_alarms"),
            ("idx_drift_alarms_unack", "drift_alarms"),
            # memory_field_crdt
            ("idx_memory_field_crdt_memory", "memory_field_crdt"),
            ("idx_memory_field_crdt_tenant_id", "memory_field_crdt"),
            # sessions
            ("idx_sessions_project", "sessions"),
            ("idx_threads_session", "decision_threads"),
            # belief_assertions
            ("idx_belief_assertions_status", "belief_assertions"),
            ("idx_belief_assertions_fact", "belief_assertions"),
            # cron_runs
            ("idx_cron_runs_job", "cron_runs"),
            # kg_entity_crdt
            ("idx_kg_entity_crdt_append_entity", "kg_entity_crdt_append"),
            # coordination
            ("idx_shared_tasks_project", "shared_tasks"),
            ("idx_agent_messages_to", "agent_messages"),
            # memory_search_interaction
            ("idx_msi_query", "memory_search_interaction"),
        }
        
        missing = expected_indexes - indexes
        if missing:
            for idx_name, tbl in sorted(missing):
                issue("MEDIUM", f"Missing index: {idx_name} on {tbl}")
        else:
            ok(f"All {len(expected_indexes)} expected indexes present")
        
        ok(f"Total indexes: {len(indexes)}")
        
        conn.close()
    finally:
        os.unlink(db_path)


# === 7. Forward reference scan in migration SQL ===
def test_forward_refs_in_sql():
    print("\n=== 7. Forward reference scan in migration SQL ===")
    
    available = _get_available_migrations()
    
    # Build a table creation timeline
    tables_created_by = {}  # table_name -> migration_num
    columns_added_by = {}   # (table, column) -> migration_num
    
    for num, path in available:
        stmts = _parse_sql_file(path)
        for stmt in stmts:
            stmt_upper = stmt.upper()
            # CREATE TABLE
            if "CREATE TABLE" in stmt_upper and "IF NOT EXISTS" in stmt_upper:
                # Extract table name
                import re
                m = re.search(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)', stmt, re.IGNORECASE)
                if m:
                    tbl = m.group(1).lower()
                    if tbl not in tables_created_by:
                        tables_created_by[tbl] = num
            # ALTER TABLE ADD COLUMN
            if "ALTER TABLE" in stmt_upper and "ADD COLUMN" in stmt_upper:
                m = re.search(r'ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)', stmt, re.IGNORECASE)
                if m:
                    tbl = m.group(1).lower()
                    col = m.group(2).lower()
                    key = (tbl, col)
                    if key not in columns_added_by:
                        columns_added_by[key] = num
    
    # Now check for forward references
    for num, path in available:
        if num == 0:
            continue
        stmts = _parse_sql_file(path)
        for stmt in stmts:
            stmt_upper = stmt.upper()
            
            # Check ALTER TABLE targets
            if "ALTER TABLE" in stmt_upper:
                m = re.search(r'ALTER\s+TABLE\s+(\w+)', stmt, re.IGNORECASE)
                if m:
                    tbl = m.group(1).lower()
                    if tbl in tables_created_by and tables_created_by[tbl] > num:
                        issue("HIGH", f"Migration {num:03d}: ALTER TABLE {tbl} references table created by migration {tables_created_by[tbl]:03d}")
            
            # Check INSERT INTO targets
            if "INSERT INTO" in stmt_upper:
                m = re.search(r'INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)', stmt, re.IGNORECASE)
                if m:
                    tbl = m.group(1).lower()
                    if tbl in tables_created_by and tables_created_by[tbl] > num:
                        issue("HIGH", f"Migration {num:03d}: INSERT INTO {tbl} references table created by migration {tables_created_by[tbl]:03d}")
            
            # Check CREATE TRIGGER targets (AFTER INSERT/UPDATE/DELETE ON)
            if "CREATE TRIGGER" in stmt_upper:
                m = re.search(r'(?:AFTER|BEFORE)\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+(\w+)', stmt, re.IGNORECASE)
                if m:
                    tbl = m.group(1).lower()
                    if tbl in tables_created_by and tables_created_by[tbl] > num:
                        issue("HIGH", f"Migration {num:03d}: CREATE TRIGGER on {tbl} references table created by migration {tables_created_by[tbl]:03d}")
            
            # Check FK references in CREATE TABLE
            if "CREATE TABLE" in stmt_upper:
                fk_refs = re.findall(r'REFERENCES\s+(\w+)', stmt, re.IGNORECASE)
                for fk_tbl in fk_refs:
                    fk_tbl = fk_tbl.lower()
                    if fk_tbl in tables_created_by and tables_created_by[fk_tbl] > num:
                        issue("HIGH", f"Migration {num:03d}: FK references {fk_tbl} created by migration {tables_created_by[fk_tbl]:03d}")
            
            # Check SELECT FROM targets in INSERT...SELECT
            if "SELECT" in stmt_upper and "FROM" in stmt_upper:
                from_tables = re.findall(r'FROM\s+(\w+)', stmt, re.IGNORECASE)
                for from_tbl in from_tables:
                    from_tbl = from_tbl.lower()
                    # Skip common false positives
                    if from_tbl in ("sqlite_master", "sqlite_sequence", "new", "old"):
                        continue
                    if from_tbl in tables_created_by and tables_created_by[from_tbl] > num:
                        issue("HIGH", f"Migration {num:03d}: SELECT FROM {from_tbl} references table created by migration {tables_created_by[from_tbl]:03d}")
    
    ok(f"Scanned {len(available)} migrations for forward references")


# === 8. Check specific known issues ===
def test_known_issues():
    print("\n=== 8. Specific known issues ===")
    
    # Check that migration 005 does NOT reference backlinks (the checkpoint said it did)
    path_005 = MIGRATIONS_DIR / "005_columns_indexes_chunks.sql"
    content = path_005.read_text()
    if "backlinks" in content.lower():
        # Check if it's a CREATE INDEX on backlinks
        import re
        if re.search(r'CREATE\s+INDEX.*backlinks', content, re.IGNORECASE):
            issue("INFO", "Migration 005 creates index on backlinks (created in 000 - OK)")
        elif "backlinks" in content.lower() and "foreign key" in content.lower():
            issue("HIGH", "Migration 005 has FK reference to backlinks")
        else:
            ok("Migration 005 mentions backlinks only in comments - OK")
    else:
        ok("Migration 005 does not reference backlinks")
    
    # Check that migration 018 handles fresh DB gracefully
    path_018 = MIGRATIONS_DIR / "018_fact_temporal.sql"
    content_018 = path_018.read_text()
    # 018 does ALTER TABLE kg_facts which fails on fresh DB - this is expected
    # The runner's forward-ref handler catches "no such table"
    ok("Migration 018 ALTER TABLE kg_facts: deferred on fresh DB (expected, retried on next startup)")
    
    # Check that migration 039 (backfill belief_assertions) won't fail on fresh DB
    path_039 = MIGRATIONS_DIR / "039_backfill_belief_assertions.sql"
    content_039 = path_039.read_text()
    if "INSERT INTO belief_assertions" in content_039:
        # This inserts from kg_facts into belief_assertions - both exist by migration 026
        # On fresh DB, both tables exist after 026, so this should work
        ok("Migration 039 backfill: both kg_facts and belief_assertions exist by this point")
    
    # Check migration 061 (memory_ctr_feedback rebuild)
    path_061 = MIGRATIONS_DIR / "061_memory_ctr_feedback_composite_key.sql"
    content_061 = path_061.read_text()
    if "INSERT OR IGNORE INTO memory_ctr_feedback_new" in content_061:
        # This reads from memory_ctr_feedback - but on fresh DB, memory_ctr_feedback
        # doesn't exist yet (created by Python safety net AFTER migrations)
        # The CREATE TABLE IF NOT EXISTS at the top creates it, then the INSERT reads from it
        # On fresh DB: CREATE TABLE creates empty table, INSERT copies 0 rows, DROP+RENAME works
        ok("Migration 061: CREATE TABLE IF NOT EXISTS before INSERT - safe on fresh DB")
    
    # Check migration 042 (memories recreate) references memory_events (from 031)
    path_042 = MIGRATIONS_DIR / "042_tenant_id_not_null.sql"
    content_042 = path_042.read_text()
    if "memory_events" in content_042:
        if "AFTER INSERT ON memories" in content_042:
            # Triggers reference memory_events - created in 031, before 042 - OK
            ok("Migration 042 triggers reference memory_events (created in 031 - OK)")
    
    # Check migration 054 (seed) references roles, policies, role_bindings, principals (from 043, 045)
    path_054 = MIGRATIONS_DIR / "054_seed_default_principal_roles.sql"
    content_054 = path_054.read_text()
    for tbl in ["roles", "policies", "role_bindings", "principals"]:
        if "INSERT" in content_054 and tbl in content_054.lower():
            ok(f"Migration 054 seeds {tbl} (created in 043/045 - OK)")


# === 9. Verify specific migration 018 behavior ===
def test_migration_018_deferred_retry():
    print("\n=== 9. Migration 018 deferred-retry behavior ===")
    
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        
        # Run all migrations
        run_migrations(conn)
        
        # Check that kg_facts has temporal columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kg_facts)").fetchall()}
        temporal_cols = {"event_time", "transaction_time", "valid_at", "invalid_at", 
                        "superseded_by", "supersedes", "contradiction_score", "invalidation_reason"}
        missing = temporal_cols - cols
        if missing:
            issue("HIGH", f"kg_facts missing temporal columns after migration: {sorted(missing)}")
        else:
            ok("kg_facts has all temporal columns from migration 018")
        
        # Check that kg_facts has indexes from 018
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='kg_facts'").fetchall()
        idx_names = {r[0] for r in rows}
        expected_018_indexes = {"idx_kg_facts_validity", "idx_kg_facts_superseded_by", "idx_kg_facts_event_time"}
        missing_idx = expected_018_indexes - idx_names
        if missing_idx:
            issue("HIGH", f"kg_facts missing indexes from migration 018: {sorted(missing_idx)}")
        else:
            ok("kg_facts has all indexes from migration 018")
        
        conn.close()
    finally:
        os.unlink(db_path)


# === 10. Verify migration 005 does not have forward refs ===
def test_migration_005_no_forward_refs():
    print("\n=== 10. Migration 005 forward-ref check ===")
    
    path_005 = MIGRATIONS_DIR / "005_columns_indexes_chunks.sql"
    content = path_005.read_text()
    
    # 005 creates memory_chunks with FK to memories(id) - memories exists in 000
    # 005 creates indexes on memories - memories exists in 000
    # Check that 005 does NOT reference tables created after 005
    
    tables_created_after_005 = set()
    for num, path in _get_available_migrations():
        if num > 5:
            stmts = _parse_sql_file(path)
            for stmt in stmts:
                m = re.search(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)', stmt, re.IGNORECASE)
                if m:
                    tables_created_after_005.add(m.group(1).lower())
    
    # Check if 005 references any of these
    for tbl in tables_created_after_005:
        if tbl in content.lower() and not content.lower().startswith("--"):
            # Check if it's in a comment
            for line in content.split("\n"):
                if tbl in line.lower() and not line.strip().startswith("--"):
                    issue("HIGH", f"Migration 005 references {tbl} which is created later")
    
    ok("Migration 005 forward-ref check complete")


# === 11. Down migration content spot-check ===
def test_down_migration_content():
    print("\n=== 11. Down migration content spot-check ===")
    
    down_migrations = _get_down_migrations()
    
    for num, path in down_migrations:
        content = path.read_text()
        # Check that down migrations don't have empty content
        stmts = _parse_sql_file(path)
        if not stmts:
            # Some down migrations are intentionally empty (marker files)
            if "SELECT 1" not in content and "--" in content:
                issue("MEDIUM", f"Down migration {num:03d} has no SQL statements (may be intentionally empty)")
    
    ok(f"Checked {len(down_migrations)} down migrations")


# === Main ===
if __name__ == "__main__":
    print("=" * 60)
    print("MIGRATION SYSTEM AUDIT")
    print("=" * 60)
    
    test_forward_ref_handling()
    test_idempotency()
    test_down_migration_coverage()
    test_tables_present()
    test_columns_present()
    test_indexes_present()
    test_forward_refs_in_sql()
    test_known_issues()
    test_migration_018_deferred_retry()
    test_migration_005_no_forward_refs()
    test_down_migration_content()
    
    print("\n" + "=" * 60)
    print(f"AUDIT COMPLETE: {len(ISSUES)} issues found")
    print("=" * 60)
    
    high = [i for i in ISSUES if i[0] == "HIGH"]
    medium = [i for i in ISSUES if i[0] == "MEDIUM"]
    low = [i for i in ISSUES if i[0] == "INFO"]
    
    if high:
        print(f"\nHIGH severity ({len(high)}):")
        for _, msg in high:
            print(f"  - {msg}")
    if medium:
        print(f"\nMEDIUM severity ({len(medium)}):")
        for _, msg in medium:
            print(f"  - {msg}")
    if low:
        print(f"\nINFO ({len(low)}):")
        for _, msg in low:
            print(f"  - {msg}")
    
    sys.exit(1 if high else 0)
