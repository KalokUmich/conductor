"""CocoIndex Code Search Service.

Wraps cocoindex-code to provide:
  * Index building (AST-aware chunking + embedding + vector storage)
  * Semantic search over code chunks
  * Per-workspace index management
  * Incremental processing (only re-index changed files)

Storage backends:
  * **sqlite** (default) — embedded, no setup required
  * **postgres** — incremental processing, concurrent access, production-ready

Embedding is handled via LiteLLM (100+ providers) or local SentenceTransformers.
The embedding model string is passed to cocoindex-code via the
``COCOINDEX_CODE_EMBEDDING_MODEL`` environment variable so both our
EmbeddingProvider and cocoindex-code use the same model.

"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from .embedding_provider import EmbeddingProvider, create_embedding_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


class _IndexRecord:
    """Tracks the state of a single workspace index."""

    __slots__ = (
        "workspace_path",
        "index_id",
        "files_count",
        "chunks_count",
        "last_updated",
        "is_incremental",
    )

    def __init__(self, workspace_path: str, index_id: str) -> None:
        self.workspace_path = workspace_path
        self.index_id       = index_id
        self.files_count    = 0
        self.chunks_count   = 0
        self.last_updated:  Optional[str] = None
        self.is_incremental = False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CodeSearchService:
    """Manages CocoIndex-based code search across multiple workspaces."""

    def __init__(self) -> None:
        self._index_dir: Path = Path("./cocoindex_data")
        self._embedding_model: str = "bedrock/cohere.embed-v4:0"
        self._top_k_default: int = 5
        self._indices: Dict[str, _IndexRecord] = {}  # workspace_path → record
        self._initialized: bool = False
        self._cocoindex = None  # lazy import
        self._embedding_provider: Optional[EmbeddingProvider] = None

        # Postgres / incremental processing
        self._storage_backend: str = "sqlite"
        self._postgres_url: Optional[str] = None
        self._incremental: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, settings, secrets=None) -> None:
        """Call once from app lifespan.

        Parameters
        ----------
        settings:
            ``CodeSearchSettings`` from config.
        secrets:
            ``Secrets`` object for credential injection (optional).
        """
        self._index_dir = Path(settings.index_dir)
        self._top_k_default = settings.top_k_results
        self._index_dir.mkdir(parents=True, exist_ok=True)

        # Resolve embedding model string
        self._embedding_model = getattr(settings, "embedding_model", None)
        if self._embedding_model is None:
            # Legacy fallback
            from .embedding_provider import _legacy_backend_to_model
            backend = getattr(settings, "embedding_backend", "local")
            self._embedding_model = _legacy_backend_to_model(backend, settings)

        # Storage backend
        self._storage_backend = getattr(settings, "storage_backend", "sqlite")
        self._postgres_url = getattr(settings, "postgres_url", None)
        self._incremental = getattr(settings, "incremental", False)

        # Set env var so cocoindex-code uses the same embedding model
        os.environ["COCOINDEX_CODE_EMBEDDING_MODEL"] = self._embedding_model

        # Set Postgres URL for CocoIndex if configured
        if self._storage_backend == "postgres" and self._postgres_url:
            os.environ["COCOINDEX_DATABASE_URL"] = self._postgres_url

        # Create embedding provider
        try:
            self._embedding_provider = create_embedding_provider(settings)
            logger.info(
                "Embedding provider created: %s (dims=%d)",
                self._embedding_provider.name,
                self._embedding_provider.dimensions,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create embedding provider (%s): %s — "
                "CodeSearchService will be degraded.",
                self._embedding_model,
                exc,
            )

        try:
            import cocoindex  # type: ignore
            self._cocoindex = cocoindex
            logger.info(
                "CocoIndex loaded (model=%s, storage=%s, index_dir=%s, incremental=%s)",
                self._embedding_model,
                self._storage_backend,
                self._index_dir,
                self._incremental,
            )
        except ImportError:
            logger.warning(
                "cocoindex package not found — CodeSearchService will be degraded. "
                "Install with: pip install cocoindex-code"
            )

        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False

    @property
    def embedding_provider(self) -> Optional[EmbeddingProvider]:
        """Access the configured embedding provider (may be None if init failed)."""
        return self._embedding_provider

    @property
    def storage_backend(self) -> str:
        """Return the current storage backend ('sqlite' or 'postgres')."""
        return self._storage_backend

    @property
    def is_incremental(self) -> bool:
        """Whether incremental processing is enabled."""
        return self._incremental and self._storage_backend == "postgres"

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    async def build_index(
        self,
        workspace_path: str,
        force_rebuild: bool = False,
        file_filter: Optional[str] = None,
    ):
        """Build or update the code index for *workspace_path*.

        When incremental processing is enabled (Postgres backend), only
        changed files are re-indexed.  ``force_rebuild`` bypasses this.
        """
        from .schemas import IndexBuildResult

        if self._cocoindex is None:
            return IndexBuildResult(
                workspace_path=workspace_path,
                success=False,
                files_indexed=0,
                chunks_indexed=0,
                duration_ms=0.0,
                message="cocoindex package not available",
            )

        start = time.monotonic()
        index_id = self._index_id_for(workspace_path)

        try:
            build_kwargs = {
                "source_dir": workspace_path,
                "embedding": self._embedding_model,
                "file_filter": file_filter,
                "force_rebuild": force_rebuild,
            }

            if self._storage_backend == "postgres" and self._postgres_url:
                # Postgres backend: cocoindex uses COCOINDEX_DATABASE_URL env
                # and manages its own storage tables
                build_kwargs["incremental"] = self._incremental and not force_rebuild
            else:
                # SQLite backend: pass index_db path
                index_db = str(self._index_dir / f"{index_id}.db")
                build_kwargs["index_db"] = index_db

            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._cocoindex.build(**build_kwargs),
            )
            elapsed = (time.monotonic() - start) * 1000

            record = _IndexRecord(workspace_path=workspace_path, index_id=index_id)
            record.files_count = getattr(result, "files_indexed", 0)
            record.chunks_count = getattr(result, "chunks_indexed", 0)
            record.is_incremental = (
                self._incremental
                and self._storage_backend == "postgres"
                and not force_rebuild
            )
            import datetime
            record.last_updated = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._indices[workspace_path] = record

            return IndexBuildResult(
                workspace_path=workspace_path,
                success=True,
                files_indexed=record.files_count,
                chunks_indexed=record.chunks_count,
                duration_ms=elapsed,
                message=(
                    "Incremental update completed"
                    if record.is_incremental
                    else "Index built successfully"
                ),
            )
        except Exception as exc:  # pylint: disable=broad-except
            elapsed = (time.monotonic() - start) * 1000
            logger.error("Index build failed for %s: %s", workspace_path, exc)
            return IndexBuildResult(
                workspace_path=workspace_path,
                success=False,
                files_indexed=0,
                chunks_indexed=0,
                duration_ms=elapsed,
                message=str(exc),
            )

    def get_index_status(self, workspace_path: str):
        """Return current index status for *workspace_path*."""
        from .schemas import IndexStatusResponse

        record = self._indices.get(workspace_path)
        if record is None:
            return IndexStatusResponse(
                workspace_path=workspace_path,
                indexed=False,
                files_count=0,
                chunks_count=0,
            )
        return IndexStatusResponse(
            workspace_path=workspace_path,
            indexed=True,
            files_count=record.files_count,
            chunks_count=record.chunks_count,
            last_updated=record.last_updated,
            index_id=record.index_id,
            storage_backend=self._storage_backend,
            is_incremental=record.is_incremental,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query:          str,
        workspace_path: str,
        top_k:          Optional[int] = None,
        file_filter:    Optional[str] = None,
    ):
        """Run a semantic code search over the indexed workspace."""
        from .schemas import CodeSearchResponse, CodeChunk

        k = top_k if top_k is not None else self._top_k_default

        if self._cocoindex is None:
            return CodeSearchResponse(
                query=query, results=[], total=0
            )

        record   = self._indices.get(workspace_path)
        index_id = record.index_id if record else self._index_id_for(workspace_path)

        try:
            search_kwargs = {
                "query": query,
                "top_k": k,
                "file_filter": file_filter,
            }

            if self._storage_backend == "postgres" and self._postgres_url:
                # Postgres backend: cocoindex queries via database
                pass  # COCOINDEX_DATABASE_URL env is already set
            else:
                # SQLite backend
                index_db = str(self._index_dir / f"{index_id}.db")
                search_kwargs["index_db"] = index_db

            raw_results = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._cocoindex.search(**search_kwargs),
            )

            chunks = [
                CodeChunk(
                    file_path   = r.file_path,
                    start_line  = r.start_line,
                    end_line    = r.end_line,
                    content     = r.content,
                    score       = r.score,
                    language    = getattr(r, "language", None),
                    symbol_name = getattr(r, "symbol_name", None),
                    symbol_type = getattr(r, "symbol_type", None),
                )
                for r in raw_results
            ]
            return CodeSearchResponse(
                query=query, results=chunks, total=len(chunks), index_id=index_id
            )

        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Code search failed for workspace %s: %s", workspace_path, exc)
            return CodeSearchResponse(query=query, results=[], total=0)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _index_id_for(workspace_path: str) -> str:
        import hashlib
        return hashlib.sha256(workspace_path.encode()).hexdigest()[:16]
