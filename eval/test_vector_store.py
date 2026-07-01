"""Tests for vector_store.py — Phase 5.1 vector store abstraction.

Covers:
  * NumpyVectorStore: add, remove, search, save/load
  * USearchVectorStore: API contracts (skipped if usearch not installed)
  * Factory: get_vector_store, from_blob
  * SearchHit dataclass
  * Distance metric math
  * Round-trip persistence (save → load)
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from infra.vector_store import (  # noqa: E402
    NumpyVectorStore,
    SearchHit,
    USearchVectorStore,
    _cosine_distance,
    _inner_product,
    _l2sq_distance,
    from_blob,
    get_vector_store,
)


class TestSearchHit(unittest.TestCase):
    def test_frozen(self):
        import dataclasses

        h = SearchHit(key=1, distance=0.5, rank=0)
        self.assertTrue(dataclasses.is_dataclass(h))
        # Frozen: assigning to any field should raise FrozenInstanceError.
        with self.assertRaises(dataclasses.FrozenInstanceError):
            h.key = 2  # type: ignore[misc]

    def test_fields(self):
        h = SearchHit(key=42, distance=0.1, rank=3)
        self.assertEqual(h.key, 42)
        self.assertEqual(h.distance, 0.1)
        self.assertEqual(h.rank, 3)


class TestNumpyVectorStoreBasic(unittest.TestCase):
    def setUp(self):
        self.store = NumpyVectorStore(ndim=4, metric="cos")

    def test_empty(self):
        self.assertEqual(len(self.store), 0)

    def test_invalid_metric(self):
        with self.assertRaises(ValueError):
            NumpyVectorStore(ndim=4, metric="bogus")

    def test_add(self):
        self.store.add(1, [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(len(self.store), 1)

    def test_add_updates_existing_key(self):
        self.store.add(1, [0.1, 0.2, 0.3, 0.4])
        self.store.add(1, [0.5, 0.6, 0.7, 0.8])
        # Same key, not appended.
        self.assertEqual(len(self.store), 1)
        hits = self.store.search([0.5, 0.6, 0.7, 0.8], k=1)
        self.assertAlmostEqual(hits[0].distance, 0.0, places=5)

    def test_remove(self):
        self.store.add(1, [0.1, 0.2, 0.3, 0.4])
        self.store.add(2, [0.5, 0.6, 0.7, 0.8])
        self.store.remove(1)
        self.assertEqual(len(self.store), 1)

    def test_remove_nonexistent_silent(self):
        self.store.add(1, [0.1, 0.2, 0.3, 0.4])
        # Should not raise.
        self.store.remove(999)
        self.assertEqual(len(self.store), 1)


class TestNumpyVectorStoreSearch(unittest.TestCase):
    def setUp(self):
        self.store = NumpyVectorStore(ndim=3, metric="cos")
        # Three orthogonal-ish vectors.
        self.store.add(1, [1.0, 0.0, 0.0])
        self.store.add(2, [0.0, 1.0, 0.0])
        self.store.add(3, [0.0, 0.0, 1.0])

    def test_search_empty_store(self):
        store = NumpyVectorStore(ndim=3, metric="cos")
        self.assertEqual(store.search([1, 0, 0], k=5), [])

    def test_search_k_zero(self):
        self.assertEqual(self.store.search([1, 0, 0], k=0), [])

    def test_search_returns_ranked(self):
        hits = self.store.search([1, 0, 0], k=3)
        self.assertEqual(len(hits), 3)
        # First hit should be the matching vector (key=1).
        self.assertEqual(hits[0].key, 1)
        self.assertAlmostEqual(hits[0].distance, 0.0, places=5)
        # Ranks are 0, 1, 2.
        self.assertEqual([h.rank for h in hits], [0, 1, 2])

    def test_search_k_limits_results(self):
        hits = self.store.search([1, 0, 0], k=2)
        self.assertEqual(len(hits), 2)

    def test_search_distances_sorted(self):
        hits = self.store.search([1, 0, 0], k=3)
        dists = [h.distance for h in hits]
        self.assertEqual(dists, sorted(dists))

    def test_search_l2sq(self):
        store = NumpyVectorStore(ndim=2, metric="l2sq")
        store.add(1, [0.0, 0.0])
        store.add(2, [1.0, 0.0])
        store.add(3, [2.0, 0.0])
        hits = store.search([0.0, 0.0], k=3)
        # Closest to origin: key=1.
        self.assertEqual(hits[0].key, 1)
        self.assertEqual(hits[1].key, 2)
        self.assertEqual(hits[2].key, 3)
        # Distances: 0, 1, 4.
        self.assertAlmostEqual(hits[0].distance, 0.0)
        self.assertAlmostEqual(hits[1].distance, 1.0)
        self.assertAlmostEqual(hits[2].distance, 4.0)

    def test_search_inner_product(self):
        store = NumpyVectorStore(ndim=2, metric="ip")
        store.add(1, [1.0, 0.0])
        store.add(2, [0.0, 1.0])
        # Query [1, 0] should rank key=1 first (highest IP).
        hits = store.search([1.0, 0.0], k=2)
        self.assertEqual(hits[0].key, 1)
        self.assertEqual(hits[1].key, 2)


class TestNumpyVectorStorePersistence(unittest.TestCase):
    def test_save_load_roundtrip(self):
        store = NumpyVectorStore(ndim=4, metric="cos")
        store.add(1, [0.1, 0.2, 0.3, 0.4])
        store.add(2, [0.5, 0.6, 0.7, 0.8])
        blob = store.save()
        restored = NumpyVectorStore.load(blob, ndim=4, metric="cos")
        self.assertEqual(len(restored), 2)
        hits = restored.search([0.1, 0.2, 0.3, 0.4], k=1)
        self.assertEqual(hits[0].key, 1)


class TestDistanceMath(unittest.TestCase):
    def test_cosine_distance_identical(self):
        self.assertAlmostEqual(_cosine_distance([1, 0, 0], [1, 0, 0]), 0.0)

    def test_cosine_distance_orthogonal(self):
        self.assertAlmostEqual(_cosine_distance([1, 0, 0], [0, 1, 0]), 1.0)

    def test_cosine_distance_opposite(self):
        # 1 - (-1) = 2
        self.assertAlmostEqual(_cosine_distance([1, 0, 0], [-1, 0, 0]), 2.0)

    def test_cosine_distance_zero_vector(self):
        # Should not crash; returns 1.0 (max distance).
        self.assertEqual(_cosine_distance([0, 0, 0], [1, 0, 0]), 1.0)

    def test_l2sq_distance(self):
        self.assertAlmostEqual(_l2sq_distance([0, 0], [3, 4]), 25.0)

    def test_inner_product(self):
        self.assertAlmostEqual(_inner_product([1, 2, 3], [4, 5, 6]), 32.0)


class TestFactory(unittest.TestCase):
    def test_get_vector_store_numpy(self):
        store = get_vector_store(backend="numpy", ndim=4, metric="cos")
        self.assertIsInstance(store, NumpyVectorStore)
        self.assertEqual(store.ndim, 4)
        self.assertEqual(store.metric, "cos")

    def test_get_vector_store_usearch(self):
        try:
            store = get_vector_store(backend="usearch", ndim=4, metric="cos")
            self.assertIsInstance(store, USearchVectorStore)
        except RuntimeError as e:
            # usearch may not be installed in the test env.
            self.skipTest(f"usearch not available: {e}")

    def test_get_vector_store_unknown_raises(self):
        with self.assertRaises(ValueError):
            get_vector_store(backend="qdrant", ndim=4, metric="cos")

    def test_from_blob_numpy(self):
        store = NumpyVectorStore(ndim=3, metric="cos")
        store.add(1, [1.0, 0.0, 0.0])
        blob = store.save()
        restored = from_blob("numpy", blob, ndim=3, metric="cos")
        self.assertIsInstance(restored, NumpyVectorStore)
        self.assertEqual(len(restored), 1)

    def test_from_blob_unknown_raises(self):
        with self.assertRaises(ValueError):
            from_blob("chroma", b"x", ndim=3)


class TestUSearchVectorStoreContract(unittest.TestCase):
    """Tests that verify the public API matches NumpyVectorStore's.

    The usearch-specific behavior (ANN accuracy, k limits) is
    covered by usearch's own tests — we just check the wrapper
    exposes the same surface and behaves consistently.
    """

    def setUp(self):
        try:
            self.store = USearchVectorStore(ndim=4, metric="cos")
            if len(self.store) == 0 and self.store._idx is None:
                self.skipTest("usearch not installed")
        except RuntimeError:
            self.skipTest("usearch not installed")

    def test_api_matches_numpy(self):
        # Same methods, same signatures.
        for method in ("add", "remove", "search", "save"):
            self.assertTrue(
                hasattr(self.store, method),
                f"USearchVectorStore missing method: {method}",
            )
        self.assertTrue(hasattr(self.store, "__len__"))

    def test_invalid_metric(self):
        with self.assertRaises(ValueError):
            USearchVectorStore(ndim=4, metric="bogus")

    def test_search_no_usearch(self):
        """If usearch isn't installed, search raises a clear error."""
        store = USearchVectorStore(ndim=4, metric="cos")
        store._idx = None
        with self.assertRaises(RuntimeError) as ctx:
            store.search([0, 0, 0, 0], k=1)
        self.assertIn("usearch", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
