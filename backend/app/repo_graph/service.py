"""RepoMap Service — generates concise repository maps for AI context.

Aider-style approach:
  1. Parse source files with tree-sitter to extract definitions/references
  2. Build a file dependency graph (networkx)
  3. Use PageRank to find the most important files
  4. Generate a compact map showing file → symbol structure

The repo map is fed to the AI alongside vector search results so it
understands the overall repository structure.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .graph import DependencyGraph, build_dependency_graph, build_dependency_graph_from_json, rank_files

logger = logging.getLogger(__name__)


class RepoMapService:
    """Builds and caches repo maps for workspaces."""

    def __init__(self, top_n: int = 10) -> None:
        self._top_n = top_n
        # workspace_path → cached graph
        self._graph_cache: Dict[str, DependencyGraph] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_graph(
        self,
        workspace_path: str,
        force_rebuild: bool = False,
    ) -> DependencyGraph:
        """Build (or return cached) dependency graph for a workspace."""
        if not force_rebuild and workspace_path in self._graph_cache:
            return self._graph_cache[workspace_path]

        graph = build_dependency_graph(workspace_path)
        self._graph_cache[workspace_path] = graph
        return graph

    def get_ranked_files(
        self,
        workspace_path: str,
        query_files: Optional[List[str]] = None,
        top_n: Optional[int] = None,
    ) -> List[Tuple[str, float]]:
        """Get top-ranked files by PageRank for a workspace.

        Parameters
        ----------
        workspace_path:
            Root directory of the repository.
        query_files:
            Files identified as relevant by vector search.
            If provided, PageRank is personalised towards these.
        top_n:
            Override the default number of files to return.

        Returns
        -------
        List of (file_path, pagerank_score) tuples.
        """
        n = top_n or self._top_n
        graph = self.build_graph(workspace_path)
        return rank_files(graph, query_files=query_files, top_n=n)

    def generate_repo_map(
        self,
        workspace_path: str,
        query_files: Optional[List[str]] = None,
        top_n: Optional[int] = None,
    ) -> str:
        """Generate a concise text-based repo map.

        The map shows the top-ranked files and their key symbols,
        formatted for inclusion in an AI prompt.

        Example output::

            ## Repository Map (top 5 files by importance)

            backend/app/config.py
            ├── class AppSettings
            ├── class CodeSearchSettings
            ├── def load_settings()
            └── def _inject_embedding_env_vars()

            backend/app/main.py
            ├── async def lifespan()
            └── def create_app()
        """
        n = top_n or self._top_n
        graph = self.build_graph(workspace_path)
        ranked = rank_files(graph, query_files=query_files, top_n=n)

        if not ranked:
            return "## Repository Map\n\n(No source files found)\n"

        lines = [f"## Repository Map (top {len(ranked)} files by importance)\n"]

        for fpath, _ in ranked:
            node = graph.nodes.get(fpath)
            if node is None:
                continue

            lines.append(f"\n{fpath}")

            defs = node.definitions
            if not defs:
                lines.append("    (no definitions)")
                continue

            for i, defn in enumerate(defs):
                is_last = i == len(defs) - 1
                prefix = "└──" if is_last else "├──"
                kind_str = defn.kind
                if defn.signature:
                    lines.append(f"    {prefix} {kind_str} {defn.signature}")
                else:
                    lines.append(f"    {prefix} {kind_str} {defn.name}")

        return "\n".join(lines) + "\n"

    def get_context_files(
        self,
        workspace_path: str,
        vector_search_files: List[str],
        top_n: Optional[int] = None,
    ) -> List[str]:
        """Combine vector search results with graph-ranked files.

        Returns a deduplicated list of file paths that merges:
        1. Files found by vector search (preserved in order)
        2. Additional important files from PageRank (not already in vector results)

        This gives the AI both the directly relevant files AND the
        structurally important context files.
        """
        n = top_n or self._top_n

        # Get graph-ranked files personalised to vector results
        ranked = self.get_ranked_files(
            workspace_path,
            query_files=vector_search_files,
            top_n=n * 2,  # get more to have extras after dedup
        )

        # Merge: vector results first, then graph additions
        seen = set(vector_search_files)
        result = list(vector_search_files)

        for fpath, _ in ranked:
            if fpath not in seen:
                result.append(fpath)
                seen.add(fpath)
                if len(result) >= len(vector_search_files) + n:
                    break

        return result

    def load_graph_from_json(self, workspace_path: str, raw_data: Dict) -> DependencyGraph:
        """Build and cache a graph from JSON data sent by the VS Code extension.

        Called in local-mode when the extension sends the repo graph it built
        using LSP.  This replaces the tree-sitter scan entirely.
        """
        graph = build_dependency_graph_from_json(raw_data)
        self._graph_cache[workspace_path] = graph
        logger.info(
            "Loaded repo graph from extension: %d files, %d edges",
            graph.stats.get("total_files", 0),
            graph.stats.get("total_edges", 0),
        )
        return graph

    def invalidate_cache(self, workspace_path: Optional[str] = None) -> None:
        """Clear cached graphs.

        If *workspace_path* is given, only that workspace is cleared.
        Otherwise all caches are cleared.
        """
        if workspace_path:
            self._graph_cache.pop(workspace_path, None)
        else:
            self._graph_cache.clear()

    def get_graph_stats(self, workspace_path: str) -> Dict:
        """Return statistics about the cached graph for a workspace."""
        graph = self._graph_cache.get(workspace_path)
        if graph is None:
            return {"cached": False}
        return {
            "cached": True,
            **graph.stats,
        }
