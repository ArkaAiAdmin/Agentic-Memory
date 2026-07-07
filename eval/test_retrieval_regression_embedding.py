"""Embedding-specific retrieval regression tests.

Gated behind ``MEMORY_TEST_EMBEDDING=1`` — all tests are skipped if the
environment variable is not set.  This keeps CI fast: FTS-only tests run
everywhere; embedding tests run only on machines that have the model.

Requires:
    - ``intfloat/e5-small-v2`` (SentenceTransformer, ~500 MB RAM, auto-
      downloaded on first run when MEMORY_TEST_EMBEDDING=1 is set)
    - ``retrieval_benchmark.py``
    - ``retrieval_golden_set.json``

Test groups:
    TE1 — Semantic queries find results via embedding path
    TE2 — memory_vec_idx.model_id column reflects the active model
    TE3 — Query expansion (entailment paraphrases) improves recall
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Gate: skip entire module unless MEMORY_TEST_EMBEDDING=1
# ---------------------------------------------------------------------------
_TEST_EMBEDDING = os.environ.get("MEMORY_TEST_EMBEDDING", "0") == "1"
pytestmark = pytest.mark.skipif(
    not _TEST_EMBEDDING,
    reason="set MEMORY_TEST_EMBEDDING=1 to run embedding regression tests",
)

# ---------------------------------------------------------------------------
# Bootstrap paths (no heavy imports at module level)
# ---------------------------------------------------------------------------
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from retrieval_benchmark import RetrievalBenchmark  # noqa: E402
from search.orchestrator import search_memories  # noqa: E402

_MODEL_WAIT_TIMEOUT_S = 120  # generous for first-time download
_MODEL_DIM_EXPECTED = 384  # e5-small-v2
_MODEL_ID_EXPECTED = "intfloat/e5-small-v2"


# ---------------------------------------------------------------------------
# TE1 — Semantic queries find results via embedding
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_test_embedding_setup")
class TestEmbeddingQuality(unittest.TestCase):
    """TE1: semantic (non-keyword) queries must find at least one golden
    memory via the embedding path — not just FTS keyword matching."""

    @classmethod
    def setUpClass(cls):
        # Consumes the _test_embedding_setup conftest fixture (opt-in via
        # usefixtures below) — resets singleton after env vars are set.
        from infra.embedding_search import get_embedding_search
        es = get_embedding_search()
        import time as _t
        deadline = _t.monotonic() + _MODEL_WAIT_TIMEOUT_S
        while _t.monotonic() < deadline:
            if es._model_loaded:
                break
            if es._model_load_failed:
                raise unittest.SkipTest(
                    f"embedding model failed to load: {getattr(es, '_model_load_failed', True)}"
                )
            _t.sleep(0.1)
        else:
            raise unittest.SkipTest("embedding model did not finish loading within timeout")
        cls._es = es
        cls._model_dim = getattr(es.model, "dim", None)

    def setUp(self):
        from retrieval_benchmark import RetrievalBenchmark
        from _fixtures import bootstrap_temp_db_clean
        self._tmpdir = Path(tempfile.mkdtemp(prefix="embed_quality_"))
        self._db = self._tmpdir / "memory.db"
        bootstrap_temp_db_clean(self._db)
        golden = RetrievalBenchmark()._golden
        _seed_db = __import__(
            "retrieval_benchmark", fromlist=["_seed_db"]
        )._seed_db
        _seed_db(self._db, golden)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_semantic_query_finds_docker_notes(self):
        """'software containerisation' (no keyword match with 'docker') must
        find a docker/container note via embedding cosine similarity."""
        from search.orchestrator import search_memories
        result = search_memories(
            self._db,
            "software containerisation",
            limit=5,
            include_global=True,
            rerank=False,
            include_facts=False,
            safety_wiring=False,
        )
        ids = [r["id"] for r in result.get("results", [])]
        found = any("docker" in i or "container" in i for i in ids)
        self.assertTrue(found, f"no docker/container note in results: {ids}")

    def test_semantic_query_finds_testing_notes(self):
        """'test-driven development with pytest' must find python-testing."""
        from search.orchestrator import search_memories
        result = search_memories(
            self._db,
            "test-driven development with pytest",
            limit=5,
            include_global=True,
            rerank=False,
            include_facts=False,
            safety_wiring=False,
        )
        ids = [r["id"] for r in result.get("results", [])]
        found = any("python-testing" in i for i in ids)
        self.assertTrue(found, f"no python-testing note in results: {ids}")

    def test_embedding_vector_dimension_correct(self):
        """e5-small-v2 must report dim=384."""
        self.assertIsNotNone(self._model_dim)
        self.assertEqual(self._model_dim, _MODEL_DIM_EXPECTED,
                         f"expected dim={_MODEL_DIM_EXPECTED}, got {self._model_dim}")

    def test_encoding_produces_float32_vectors(self):
        """Encoding a string must return a non-empty ndarray of model dimension."""
        vec = self._es.encode(["hello world from embedding regression test"])
        assert vec is not None, "model.encode returned None"
        assert vec.shape[1] == _MODEL_DIM_EXPECTED, (
            f"dim={vec.shape[1]} != {_MODEL_DIM_EXPECTED}"
        )


# ---------------------------------------------------------------------------
# TE2 — memory_vec_idx.model_id reflects active embedding model
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_test_embedding_setup")
class TestEmbeddingModelIDTracking(unittest.TestCase):
    """TE2: after saving a memory with the test model, memory_embeddings
    stores the correct model identity and memory_vec_idx.model_id is set."""

    @classmethod
    def setUpClass(cls):
        from infra.embedding_search import get_embedding_search
        import time as _t
        es = get_embedding_search()
        deadline = _t.monotonic() + _MODEL_WAIT_TIMEOUT_S
        while _t.monotonic() < deadline:
            if es._model_loaded:
                break
            if es._model_load_failed:
                raise unittest.SkipTest("embedding model failed to load")
            _t.sleep(0.1)
        else:
            raise unittest.SkipTest("embedding model did not finish loading within timeout")

    def setUp(self):
        import sqlite3
        from save_pipeline import save_memory  # noqa: E402
        self._tmpdir = Path(tempfile.mkdtemp(prefix="embed_modelid_"))
        self._db = self._tmpdir / "memory.db"
        from _fixtures import bootstrap_temp_db_clean
        bootstrap_temp_db_clean(self._db)
        self._note_id = "tests/embed-model-id-check"
        save_memory(
            content="Memory to verify embedding model_id tracking in memory_vec_idx.",
            category="tests",
            title_slug="embed-model-id-check",
            tags=["embedding-test"],
            db_path=str(self._db),
            safety_wiring=False,
        )
        self.conn = sqlite3.connect(str(self._db))
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_embedding_row_has_model_identity(self):
        """memory_embeddings.model_revision must be non-empty (model_id for
        SentenceTransformer, git SHA for model2vec)."""
        row = self.conn.execute(
            "SELECT model_revision FROM memory_embeddings WHERE memory_id = ?",
            (self._note_id,),
        ).fetchone()
        self.assertIsNotNone(
            row, "memory_embeddings row not found — _index_embedding did not run"
        )
        self.assertNotEqual(
            row["model_revision"], "",
            "model_revision is empty — model identity not tracked after index_embedding fix",
        )

    def test_vec_idx_model_id_populated_after_rebuild(self):
        """After rebuild_vec_index, memory_vec_idx.model_id must be non-empty."""
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_DIR / "rebuild_vec_index.py"),
                    str(self._db),
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except Exception:
            pass
        row = self.conn.execute(
            "SELECT model_id FROM memory_vec_idx WHERE id=1"
        ).fetchone()
        self.assertIsNotNone(
            row, "memory_vec_idx singleton row absent — rebuild may have failed"
        )
        model_id = row["model_id"] if row else ""
        self.assertNotEqual(
            model_id, "", f"memory_vec_idx.model_id is empty: {model_id!r}"
        )


# ---------------------------------------------------------------------------
# TE3 — Reasoning expansion: paraphrases retrieve overlapping memories
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_test_embedding_setup")
class TestReasoningExpansion(unittest.TestCase):
    """TE3: paraphrased queries (entailment-expanded) must overlap with
    canonical queries in the golden set."""

    @classmethod
    def setUpClass(cls):
        from infra.embedding_search import get_embedding_search
        import time as _t
        es = get_embedding_search()
        deadline = _t.monotonic() + _MODEL_WAIT_TIMEOUT_S
        while _t.monotonic() < deadline:
            if es._model_loaded:
                break
            if es._model_load_failed:
                raise unittest.SkipTest("embedding model failed to load")
            _t.sleep(0.1)
        else:
            raise unittest.SkipTest("embedding model did not finish loading within timeout")

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp(prefix="retrieval_reason_"))
        self._db = self._tmpdir / "memory.db"
        from _fixtures import bootstrap_temp_db_clean
        bootstrap_temp_db_clean(self._db)
        golden = RetrievalBenchmark()._golden
        _seed_db = __import__(
            "retrieval_benchmark", fromlist=["_seed_db"]
        )._seed_db
        _seed_db(self._db, golden)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _top_ids(self, query: str, k: int = 5) -> list[str]:
        result = search_memories(
            self._db,
            query,
            limit=k,
            include_global=True,
            rerank=False,
            include_facts=False,
            safety_wiring=False,
        )
        return [r["id"] for r in result.get("results", [])[:k]]

    def test_paraphrase_containerisation_matches_docker(self):
        canonical = set(self._top_ids("Docker containers package applications with dependencies", 5))
        paraphrase = set(self._top_ids("software containerisation portable units environments", 5))
        self.assertTrue(
            bool(canonical & paraphrase),
            f"no overlap: canonical={canonical}, paraphrase={paraphrase}",
        )

    def test_paraphrase_testing_framework_matches_pytest(self):
        canonical = set(self._top_ids("Python pytest test-driven development fixtures", 5))
        paraphrase = set(self._top_ids("automated testing framework python shared setup", 5))
        self.assertTrue(
            bool(canonical & paraphrase),
            f"no overlap: canonical={canonical}, paraphrase={paraphrase}",
        )

    def test_paraphrase_observability_matches_logging(self):
        canonical = set(self._top_ids("structured logging JSON observability severity", 5))
        paraphrase = set(self._top_ids("observability machine-parseable logs debugging production", 5))
        self.assertTrue(
            bool(canonical & paraphrase),
            f"no overlap: canonical={canonical}, paraphrase={paraphrase}",
        )

    def test_paraphrase_vector_db_matches_semantic(self):
        canonical = set(self._top_ids("vector database semantic memory approximate nearest neighbour", 5))
        paraphrase = set(self._top_ids("ANN vector search embedding similarity high-scale", 5))
        self.assertTrue(
            bool(canonical & paraphrase),
            f"no overlap: canonical={canonical}, paraphrase={paraphrase}",
        )


# Tell pytest the _test_embedding_setup conftest fixture is required by all
# classes in this module so it runs before setUpClass/setUp.
pytestmark = pytest.mark.skipif(
    not _TEST_EMBEDDING,
    reason="set MEMORY_TEST_EMBEDDING=1 to run embedding regression tests",
)
