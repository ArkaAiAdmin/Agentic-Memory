"""Unit tests for Temporal Fact Supersession Engine."""

from __future__ import annotations

import sqlite3
import pytest
from pathlib import Path

from fact.fact_search import facts_search, facts_list
from fact.fact_temporal import (
    detect_fact_contradiction,
    supersede_fact,
    reconcile_fact_supersession,
    query_facts_at_time,
    query_fact_supersession_chain,
)
from fact.fact_schema import ensure_facts_schema


@pytest.fixture
def temp_db(tmp_path: Path):
    db_path = tmp_path / 'test_temporal.db'
    conn = sqlite3.connect(str(db_path))
    ensure_facts_schema(conn)
    yield conn
    conn.close()


def test_fact_supersession_creation_and_filtering(temp_db):
    """Test that superseded facts are excluded by default in facts_search and facts_list."""
    cur = temp_db.cursor()
    cur.execute(
        """INSERT INTO kg_facts (subject, predicate, object, confidence, first_seen, last_seen, mention_count)
           VALUES ('postgres', 'version', '15', 1.0, 1000.0, 1000.0, 1)"""
    )
    fact_id_1 = cur.lastrowid

    cur.execute(
        """INSERT INTO kg_facts (subject, predicate, object, confidence, first_seen, last_seen, mention_count)
           VALUES ('postgres', 'version', '16', 1.0, 2000.0, 2000.0, 1)"""
    )
    fact_id_2 = cur.lastrowid
    temp_db.commit()

    supersede_fact(temp_db, fact_id_1, fact_id_2, reason='upgrade')
    temp_db.commit()

    res_active = facts_search(temp_db, 'postgres', include_superseded=False)
    assert len(res_active) == 1
    assert res_active[0]['object'] == '16'

    res_all = facts_search(temp_db, 'postgres', include_superseded=True)
    assert len(res_all) == 2
    objects = {r['object'] for r in res_all}
    assert objects == {'15', '16'}

    list_active = facts_list(temp_db, include_superseded=False)
    assert len(list_active) == 1
    assert list_active[0]['object'] == '16'


def test_fact_supersession_chain(temp_db):
    """Test tracking the complete supersession provenance chain."""
    cur = temp_db.cursor()
    cur.execute(
        "INSERT INTO kg_facts (subject, predicate, object, confidence) VALUES ('app', 'port', '8080', 1.0)"
    )
    f1 = cur.lastrowid
    cur.execute(
        "INSERT INTO kg_facts (subject, predicate, object, confidence) VALUES ('app', 'port', '8081', 1.0)"
    )
    f2 = cur.lastrowid
    cur.execute(
        "INSERT INTO kg_facts (subject, predicate, object, confidence) VALUES ('app', 'port', '9000', 1.0)"
    )
    f3 = cur.lastrowid
    temp_db.commit()

    supersede_fact(temp_db, f1, f2, reason='port conflict')
    supersede_fact(temp_db, f2, f3, reason='production standard')
    temp_db.commit()

    chain = query_fact_supersession_chain(temp_db, f1)
    assert len(chain) == 3
    assert chain[0]['id'] == f1
    assert chain[1]['id'] == f2
    assert chain[2]['id'] == f3
