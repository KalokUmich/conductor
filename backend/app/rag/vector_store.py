"""FAISS-based vector store for code chunk embeddings.

Uses ``IndexFlatIP`` on L2-normalised vectors to compute cosine similarity.
Brute-force search is fast enough for the expected scale (<50 K chunks per
workspace).  Persistence uses ``faiss.write_index`` + a JSON metadata sidecar.

Thread safety: all mutating operations acquire ``_lock``; read-only
``search()`` is lock-free because FAISS flat-index search is safe to call
concurrently with (append-only) additions.
"""
import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chunk metadata
# ---------------------------------------------------------------------------

@dataclass
class ChunkMetadata:
    """Metadata attached to each indexed code chunk."""

    file_path: str
    start_line: int
    end_line: int
    symbol_name: str = ""
    symbol_type: str = ""  # function | class | method | block
    language: str = ""
    last_modified: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChunkMetadata":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# FAISS vector store
# ---------------------------------------------------------------------------

class FaissVectorStore:
    """Thin wrapper around a FAISS ``IndexFlatIP`` index.

    Args:
        dim:      Vector dimensionality.
        data_dir: Optional directory for persistence (``save`` / ``load``).
    """

    def __init__(self, dim: int, data_dir: Optional[Path] = None) -> None:
        import faiss

        self._dim = dim
        self._data_dir = Path(data_dir) if data_dir else None
        self._index = faiss.IndexFlatIP(dim)
        self._metadata: dict[str, ChunkMetadata] = {}
        self._id_map: list[str] = []  # position → chunk_id
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def size(self) -> int:
        return self._index.ntotal

    # ------------------------------------------------------------------
    # Write operations (under lock)
    # ------------------------------------------------------------------

    def add(self, chunk_id: str, vector: list[float], metadata: ChunkMetadata) -> None:
        """Add a single chunk to the index.

        The vector is L2-normalised before insertion so that inner-product
        search produces cosine similarity scores.
        """
        vec = self._normalise(np.array([vector], dtype=np.float32))
        with self._lock:
            self._index.add(vec)
            self._id_map.append(chunk_id)
            self._metadata[chunk_id] = metadata

    def add_batch(
        self,
        chunk_ids: list[str],
        vectors: list[list[float]],
        metadatas: list[ChunkMetadata],
    ) -> None:
        """Add multiple chunks in a single locked operation."""
        if not chunk_ids:
            return
        vecs = self._normalise(np.array(vectors, dtype=np.float32))
        with self._lock:
            self._index.add(vecs)
            self._id_map.extend(chunk_ids)
            for cid, meta in zip(chunk_ids, metadatas):
                self._metadata[cid] = meta

    def remove(self, chunk_ids: list[str]) -> int:
        """Remove chunks by ID.  Rebuilds the index without the removed IDs.

        Returns:
            Number of chunks actually removed.
        """
        import faiss

        to_remove = set(chunk_ids)
        with self._lock:
            keep_indices = [
                i for i, cid in enumerate(self._id_map) if cid not in to_remove
            ]
            removed = len(self._id_map) - len(keep_indices)
            if removed == 0:
                return 0

            if keep_indices:
                # Reconstruct kept vectors
                kept_vecs = np.vstack(
                    [self._index.reconstruct(i).reshape(1, -1) for i in keep_indices]
                )
            else:
                kept_vecs = np.empty((0, self._dim), dtype=np.float32)

            new_id_map = [self._id_map[i] for i in keep_indices]

            # Remove metadata for deleted chunks
            for cid in to_remove:
                self._metadata.pop(cid, None)

            # Rebuild index
            self._index = faiss.IndexFlatIP(self._dim)
            if kept_vecs.shape[0] > 0:
                self._index.add(kept_vecs)
            self._id_map = new_id_map

        return removed

    def clear(self) -> None:
        """Reset the index and all metadata."""
        import faiss

        with self._lock:
            self._index = faiss.IndexFlatIP(self._dim)
            self._metadata.clear()
            self._id_map.clear()

    # ------------------------------------------------------------------
    # Search (lock-free)
    # ------------------------------------------------------------------

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filters: Optional[dict] = None,
    ) -> list[tuple[str, float, ChunkMetadata]]:
        """Search for the nearest chunks.

        Args:
            query_vector: Query embedding (will be L2-normalised).
            top_k:        Maximum results to return.
            filters:      Optional dict with ``languages`` (list[str]) and/or
                          ``file_patterns`` (list[str] glob patterns).

        Returns:
            List of ``(chunk_id, score, metadata)`` tuples sorted by score
            descending.
        """
        if self._index.ntotal == 0:
            return []

        vec = self._normalise(np.array([query_vector], dtype=np.float32))

        # Over-fetch when filtering to increase the chance of returning top_k
        # results after post-filtering.
        fetch_k = min(top_k * 3, self._index.ntotal) if filters else min(top_k, self._index.ntotal)

        scores, indices = self._index.search(vec, fetch_k)

        results: list[tuple[str, float, ChunkMetadata]] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            chunk_id = self._id_map[idx]
            meta = self._metadata.get(chunk_id)
            if meta is None:
                continue
            if filters and not self._matches_filters(meta, filters):
                continue
            results.append((chunk_id, float(score), meta))
            if len(results) >= top_k:
                break

        return results

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist the index and metadata to ``data_dir``."""
        import faiss

        if self._data_dir is None:
            raise ValueError("No data_dir configured for persistence")

        self._data_dir.mkdir(parents=True, exist_ok=True)
        index_path = self._data_dir / "index.faiss"
        meta_path = self._data_dir / "metadata.json"

        with self._lock:
            faiss.write_index(self._index, str(index_path))
            payload = {
                "id_map": self._id_map,
                "metadata": {k: v.to_dict() for k, v in self._metadata.items()},
            }
            meta_path.write_text(json.dumps(payload))

        logger.info(
            "Saved FAISS index: %d vectors to %s", self._index.ntotal, index_path
        )

    def load(self) -> bool:
        """Load a previously saved index.  Returns True on success."""
        import faiss

        if self._data_dir is None:
            return False

        index_path = self._data_dir / "index.faiss"
        meta_path = self._data_dir / "metadata.json"

        if not index_path.exists() or not meta_path.exists():
            return False

        try:
            loaded_index = faiss.read_index(str(index_path))
            payload = json.loads(meta_path.read_text())

            with self._lock:
                self._index = loaded_index
                self._id_map = payload["id_map"]
                self._metadata = {
                    k: ChunkMetadata.from_dict(v)
                    for k, v in payload["metadata"].items()
                }

            logger.info(
                "Loaded FAISS index: %d vectors from %s",
                self._index.ntotal,
                index_path,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to load FAISS index: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(vecs: np.ndarray) -> np.ndarray:
        """L2-normalise each row in-place and return the array."""
        import faiss

        faiss.normalize_L2(vecs)
        return vecs

    @staticmethod
    def _matches_filters(meta: ChunkMetadata, filters: dict) -> bool:
        """Return True if *meta* passes the given filters."""
        import fnmatch

        languages = filters.get("languages")
        if languages and meta.language not in languages:
            return False

        file_patterns = filters.get("file_patterns")
        if file_patterns:
            if not any(fnmatch.fnmatch(meta.file_path, p) for p in file_patterns):
                return False

        return True
