"""Tests for the FAISS vector store.

Covers add/remove/search/save/load/filters/thread-safety/dim-mismatch.
"""
import threading

import numpy as np
import pytest

from app.rag.vector_store import ChunkMetadata, FaissVectorStore


DIM = 4


def _rand_vec(dim: int = DIM) -> list[float]:
    """Return a random float vector."""
    return np.random.randn(dim).tolist()


def _meta(file_path: str = "src/main.py", **kw) -> ChunkMetadata:
    """Shorthand for creating test metadata."""
    return ChunkMetadata(file_path=file_path, start_line=1, end_line=10, **kw)


# ---------------------------------------------------------------------------
# ChunkMetadata
# ---------------------------------------------------------------------------

class TestChunkMetadata:
    def test_to_dict_roundtrip(self):
        m = ChunkMetadata(
            file_path="a.py", start_line=1, end_line=10,
            symbol_name="foo", symbol_type="function", language="python",
        )
        d = m.to_dict()
        assert d["file_path"] == "a.py"
        restored = ChunkMetadata.from_dict(d)
        assert restored == m

    def test_from_dict_ignores_extra_keys(self):
        d = {"file_path": "a.py", "start_line": 1, "end_line": 5, "extra": "ignored"}
        m = ChunkMetadata.from_dict(d)
        assert m.file_path == "a.py"


# ---------------------------------------------------------------------------
# FaissVectorStore — basic operations
# ---------------------------------------------------------------------------

class TestFaissVectorStoreBasic:
    def test_empty_store(self):
        store = FaissVectorStore(dim=DIM)
        assert store.size == 0
        assert store.dim == DIM

    def test_add_single(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta())
        assert store.size == 1

    def test_add_batch(self):
        store = FaissVectorStore(dim=DIM)
        ids = ["c1", "c2", "c3"]
        vecs = [_rand_vec() for _ in ids]
        metas = [_meta() for _ in ids]
        store.add_batch(ids, vecs, metas)
        assert store.size == 3

    def test_add_batch_empty(self):
        store = FaissVectorStore(dim=DIM)
        store.add_batch([], [], [])
        assert store.size == 0

    def test_search_empty_store(self):
        store = FaissVectorStore(dim=DIM)
        results = store.search(_rand_vec(), top_k=5)
        assert results == []

    def test_search_returns_results(self):
        store = FaissVectorStore(dim=DIM)
        vec = [1.0, 0.0, 0.0, 0.0]
        store.add("c1", vec, _meta("a.py"))
        store.add("c2", [0.0, 1.0, 0.0, 0.0], _meta("b.py"))

        results = store.search(vec, top_k=2)
        assert len(results) == 2
        # The most similar should be the same direction vector
        assert results[0][0] == "c1"
        assert results[0][1] > results[1][1]  # higher score

    def test_search_respects_top_k(self):
        store = FaissVectorStore(dim=DIM)
        for i in range(10):
            store.add(f"c{i}", _rand_vec(), _meta())
        results = store.search(_rand_vec(), top_k=3)
        assert len(results) == 3

    def test_search_score_is_float(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta())
        results = store.search(_rand_vec(), top_k=1)
        assert len(results) == 1
        assert isinstance(results[0][1], float)


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

class TestFaissVectorStoreRemove:
    def test_remove_existing(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta("a.py"))
        store.add("c2", _rand_vec(), _meta("b.py"))
        removed = store.remove(["c1"])
        assert removed == 1
        assert store.size == 1

    def test_remove_nonexistent(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta())
        removed = store.remove(["c999"])
        assert removed == 0
        assert store.size == 1

    def test_remove_all(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta())
        store.add("c2", _rand_vec(), _meta())
        removed = store.remove(["c1", "c2"])
        assert removed == 2
        assert store.size == 0

    def test_search_after_remove(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", [1.0, 0.0, 0.0, 0.0], _meta("a.py"))
        store.add("c2", [0.0, 1.0, 0.0, 0.0], _meta("b.py"))
        store.remove(["c1"])
        results = store.search([1.0, 0.0, 0.0, 0.0], top_k=5)
        assert len(results) == 1
        assert results[0][0] == "c2"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestFaissVectorStoreFilters:
    def test_filter_by_language(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta(language="python"))
        store.add("c2", _rand_vec(), _meta(language="typescript"))
        store.add("c3", _rand_vec(), _meta(language="python"))

        results = store.search(
            _rand_vec(), top_k=10,
            filters={"languages": ["python"]},
        )
        assert all(r[2].language == "python" for r in results)

    def test_filter_by_file_pattern(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta(file_path="src/main.py"))
        store.add("c2", _rand_vec(), _meta(file_path="tests/test_main.py"))
        store.add("c3", _rand_vec(), _meta(file_path="src/utils.py"))

        results = store.search(
            _rand_vec(), top_k=10,
            filters={"file_patterns": ["src/*.py"]},
        )
        assert all(r[2].file_path.startswith("src/") for r in results)

    def test_filter_combined(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta(file_path="src/main.py", language="python"))
        store.add("c2", _rand_vec(), _meta(file_path="src/app.ts", language="typescript"))
        store.add("c3", _rand_vec(), _meta(file_path="tests/test.py", language="python"))

        results = store.search(
            _rand_vec(), top_k=10,
            filters={"languages": ["python"], "file_patterns": ["src/*"]},
        )
        assert len(results) == 1
        assert results[0][0] == "c1"


# ---------------------------------------------------------------------------
# Persistence (save / load)
# ---------------------------------------------------------------------------

class TestFaissVectorStorePersistence:
    def test_save_and_load(self, tmp_path):
        store = FaissVectorStore(dim=DIM, data_dir=tmp_path / "index")
        vec1 = [1.0, 0.0, 0.0, 0.0]
        store.add("c1", vec1, _meta("a.py", symbol_name="foo"))
        store.add("c2", _rand_vec(), _meta("b.py"))
        store.save()

        # Load into a new store
        store2 = FaissVectorStore(dim=DIM, data_dir=tmp_path / "index")
        assert store2.load() is True
        assert store2.size == 2

        # Search should work
        results = store2.search(vec1, top_k=1)
        assert results[0][0] == "c1"
        assert results[0][2].symbol_name == "foo"

    def test_load_nonexistent(self, tmp_path):
        store = FaissVectorStore(dim=DIM, data_dir=tmp_path / "empty")
        assert store.load() is False

    def test_save_without_data_dir_raises(self):
        store = FaissVectorStore(dim=DIM)
        with pytest.raises(ValueError, match="data_dir"):
            store.save()


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

class TestFaissVectorStoreClear:
    def test_clear(self):
        store = FaissVectorStore(dim=DIM)
        store.add("c1", _rand_vec(), _meta())
        store.add("c2", _rand_vec(), _meta())
        store.clear()
        assert store.size == 0
        assert store.search(_rand_vec(), top_k=5) == []


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestFaissVectorStoreThreadSafety:
    def test_concurrent_adds(self):
        store = FaissVectorStore(dim=DIM)
        errors: list[Exception] = []

        def add_many(start: int):
            try:
                for i in range(50):
                    store.add(f"c{start}_{i}", _rand_vec(), _meta())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_many, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert store.size == 200

    def test_concurrent_search_during_add(self):
        store = FaissVectorStore(dim=DIM)
        # Pre-populate
        for i in range(20):
            store.add(f"c{i}", _rand_vec(), _meta())

        errors: list[Exception] = []

        def searcher():
            try:
                for _ in range(50):
                    store.search(_rand_vec(), top_k=5)
            except Exception as e:
                errors.append(e)

        def adder():
            try:
                for i in range(50):
                    store.add(f"new_{i}", _rand_vec(), _meta())
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=searcher)
        t2 = threading.Thread(target=adder)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
