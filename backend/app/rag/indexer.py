"""Incremental indexing pipeline for the RAG vector store.

Manages per-workspace FAISS indices, handles file upsert/delete, embeds
code chunks via the shared ``EmbeddingService``, and provides search.
"""
import logging
from pathlib import Path
from typing import Optional

from app.embeddings.service import get_embedding_service

from .chunker import CodeChunk, chunk_file
from .schemas import SearchFilters, SearchResultItem
from .vector_store import ChunkMetadata, FaissVectorStore

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 96

# Language detection from file extension
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
}


class RagIndexer:
    """Manages per-workspace FAISS vector stores and incremental indexing.

    Args:
        data_dir: Root directory for persisted indices.
        dim:      Vector dimensionality (must match the embedding model).
    """

    def __init__(self, data_dir: str, dim: int) -> None:
        self._data_dir = Path(data_dir)
        self._dim = dim
        self._stores: dict[str, FaissVectorStore] = {}
        # workspace_id → { file_path → [chunk_ids] }
        self._file_chunks: dict[str, dict[str, list[str]]] = {}

    # ------------------------------------------------------------------
    # Store management
    # ------------------------------------------------------------------

    def get_store(self, workspace_id: str) -> FaissVectorStore:
        """Return the store for *workspace_id*, creating or loading as needed."""
        if workspace_id not in self._stores:
            store_dir = self._data_dir / workspace_id
            store = FaissVectorStore(dim=self._dim, data_dir=store_dir)
            store.load()  # no-op if nothing persisted yet
            self._stores[workspace_id] = store
            self._file_chunks.setdefault(workspace_id, {})
        return self._stores[workspace_id]

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_files(
        self,
        workspace_id: str,
        files: list[dict],
    ) -> tuple[int, int]:
        """Incrementally index files (upsert or delete).

        Args:
            workspace_id: Workspace identifier.
            files:        List of dicts with ``path``, ``content``, ``action``.

        Returns:
            ``(chunks_added, chunks_removed)`` counts.
        """
        store = self.get_store(workspace_id)
        file_map = self._file_chunks.setdefault(workspace_id, {})
        total_added = 0
        total_removed = 0

        upsert_files: list[dict] = []
        delete_paths: list[str] = []

        for f in files:
            path = f["path"]
            action = f.get("action", "upsert")

            if action == "delete":
                delete_paths.append(path)
            elif action == "upsert" and f.get("content") is not None:
                # Remove old chunks for this file first
                delete_paths.append(path)
                upsert_files.append(f)

        # Remove old chunks
        for path in delete_paths:
            old_ids = file_map.pop(path, [])
            if old_ids:
                removed = store.remove(old_ids)
                total_removed += removed

        # Chunk + embed new files
        if upsert_files:
            all_chunks: list[CodeChunk] = []
            chunk_file_map: list[str] = []  # parallel list: which file each chunk belongs to

            for f in upsert_files:
                path = f["path"]
                content = f["content"]
                language = _detect_language(path)
                file_chunks = chunk_file(content, path, language)
                for chunk in file_chunks:
                    all_chunks.append(chunk)
                    chunk_file_map.append(path)

            if all_chunks:
                # Generate chunk IDs
                # Track per-file chunk counters
                file_counters: dict[str, int] = {}
                chunk_ids: list[str] = []
                for chunk, fpath in zip(all_chunks, chunk_file_map):
                    idx = file_counters.get(fpath, 0)
                    file_counters[fpath] = idx + 1
                    chunk_ids.append(_generate_chunk_id(fpath, idx))

                # Embed in batches
                vectors = self._embed_chunks(all_chunks)

                if vectors and len(vectors) == len(all_chunks):
                    # Build metadata
                    metadatas = [
                        ChunkMetadata(
                            file_path=c.file_path,
                            start_line=c.start_line,
                            end_line=c.end_line,
                            symbol_name=c.symbol_name,
                            symbol_type=c.symbol_type,
                            language=c.language,
                        )
                        for c in all_chunks
                    ]

                    store.add_batch(chunk_ids, vectors, metadatas)
                    total_added += len(chunk_ids)

                    # Update file_chunks map
                    for cid, fpath in zip(chunk_ids, chunk_file_map):
                        file_map.setdefault(fpath, []).append(cid)

                    logger.info(
                        "[RagIndexer] Indexed %d chunks for workspace %s",
                        len(chunk_ids), workspace_id,
                    )

        # Persist after indexing
        try:
            store.save()
        except Exception as exc:
            logger.warning("[RagIndexer] Failed to persist index: %s", exc)

        return total_added, total_removed

    def reindex(
        self,
        workspace_id: str,
        files: list[dict],
    ) -> tuple[int, int]:
        """Full reindex: clear the store, then index all files.

        Args:
            workspace_id: Workspace identifier.
            files:        List of dicts with ``path``, ``content``, ``action``.

        Returns:
            ``(chunks_added, chunks_removed)`` counts.
        """
        store = self.get_store(workspace_id)
        old_count = store.size
        store.clear()
        self._file_chunks[workspace_id] = {}

        added, _ = self.index_files(workspace_id, files)
        return added, old_count

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        workspace_id: str,
        query: str,
        top_k: int = 10,
        filters: Optional[SearchFilters] = None,
    ) -> list[SearchResultItem]:
        """Search for code chunks relevant to *query*.

        Args:
            workspace_id: Workspace identifier.
            query:        Natural language or code query.
            top_k:        Maximum results.
            filters:      Optional search filters.

        Returns:
            List of SearchResultItem sorted by relevance.
        """
        store = self.get_store(workspace_id)
        if store.size == 0:
            return []

        # Embed the query
        svc = get_embedding_service()
        if svc is None:
            logger.warning("[RagIndexer] No embedding service for search")
            return []

        try:
            query_vectors = svc.embed([query], input_type="search_query")
            query_vector = query_vectors[0]
        except Exception as exc:
            logger.warning("[RagIndexer] Failed to embed query: %s", exc)
            return []

        # Build filter dict
        filter_dict = None
        if filters:
            filter_dict = {}
            if filters.languages:
                filter_dict["languages"] = filters.languages
            if filters.file_patterns:
                filter_dict["file_patterns"] = filters.file_patterns

        results = store.search(query_vector, top_k=top_k, filters=filter_dict)

        # Convert to SearchResultItem, attaching content from chunk ID
        items: list[SearchResultItem] = []
        for chunk_id, score, meta in results:
            items.append(SearchResultItem(
                file_path=meta.file_path,
                start_line=meta.start_line,
                end_line=meta.end_line,
                symbol_name=meta.symbol_name,
                symbol_type=meta.symbol_type,
                content="",  # Content not stored in FAISS; caller can read file
                score=score,
                language=meta.language,
            ))

        return items

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _embed_chunks(self, chunks: list[CodeChunk]) -> list[list[float]]:
        """Embed chunks in batches using the global EmbeddingService."""
        svc = get_embedding_service()
        if svc is None:
            logger.warning("[RagIndexer] No embedding service available")
            return []

        all_vectors: list[list[float]] = []
        texts = [c.content for c in chunks]

        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i:i + EMBED_BATCH_SIZE]
            try:
                vectors = svc.embed(batch, input_type="search_document")
                all_vectors.extend(vectors)
            except Exception as exc:
                logger.error(
                    "[RagIndexer] Embedding batch %d-%d failed: %s",
                    i, i + len(batch), exc,
                )
                return []  # Abort on failure

        return all_vectors


def _detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(ext, "")


def _generate_chunk_id(file_path: str, index: int) -> str:
    """Generate a deterministic chunk ID."""
    return f"{file_path}::chunk_{index}"
