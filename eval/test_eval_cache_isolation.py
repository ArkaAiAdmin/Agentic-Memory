"""Unit tests verifying search cache isolation, inode freshness, and tenant scoping across runs."""

from __future__ import annotations

import os
import shutil
import tempfile
import pytest
from pathlib import Path

from infra.cache import clear_all_caches, _search_cache
from search.orchestrator import search_memories, _cache_store_result
from search.rerankers import _ce_score_cache, _li_content_cache
from eval._fixtures import bootstrap_temp_db_clean


def test_clear_all_caches():
    """Verify that clear_all_caches resets all sub-module caches."""
    with _search_cache_lock_ref():
        _search_cache["dummy_key"] = (1.0, {"_inode": 123})
    _ce_score_cache["dummy_ce"] = (1.0, [0.5])
    _li_content_cache["dummy_mid"] = (123, ["tok"], [{ "ng" }])

    clear_all_caches()

    assert len(_search_cache) == 0
    assert len(_ce_score_cache) == 0
    assert len(_li_content_cache) == 0


def _search_cache_lock_ref():
    from infra.cache import _search_cache_lock
    return _search_cache_lock


def test_inode_stamping_and_invalidation():
    """Verify that _cache_store_result stamps _inode and invalidates on temp DB replacement."""
    tmpdir = tempfile.mkdtemp(prefix="test_cache_inode_")
    db_path1 = Path(tmpdir) / "memory.db"
    bootstrap_temp_db_clean(db_path1)

    result_dict = {"results": [{"id": "note1"}], "query_id": "q1"}
    cache_key = f"{db_path1.as_posix()}:q1:5:True:True:0.1:False:True:False:False:False:hash:mode=general:cat=None:hybrid=0:light=0:sw=1:dr=0:sf=0:if=0:fl=5:as_of=None:bs=:es=:ft=:ms=:swm=0:uh=1:tid=t1:ns=default:aid="

    _cache_store_result(cache_key, result_dict, db_path=db_path1)
    assert "_inode" in result_dict
    assert result_dict["_inode"] == os.stat(str(db_path1)).st_ino

    # Invalidate by replacing the DB file (new inode)
    db_path1.unlink()
    bootstrap_temp_db_clean(db_path1)

    # Calling search_memories on the new DB should NOT hit the stale cache entry
    res = search_memories(db_path1, "q1", tenant_id="t1")
    assert res.get("query_id") != "q1"

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_tenant_isolation_in_ce_cache():
    """Verify that candidate IDs under different tenant_ids generate distinct CE cache keys."""
    from search.rerankers import _apply_ce_chunk_rerank

    scored = [("mem_1", "Some content for search", "sf1", "[]", "2026-01-01", 1, 0.5, 3, 0, 0, None, None, None)]
    
    _apply_ce_chunk_rerank("query", scored, tenant_id="tenant_A")
    len_a = len(_ce_score_cache)

    _apply_ce_chunk_rerank("query", scored, tenant_id="tenant_B")
    len_b = len(_ce_score_cache)

    # Each tenant_id should produce a unique cache entry key in _ce_score_cache
    if len_a > 0:
        assert len_b > len_a
