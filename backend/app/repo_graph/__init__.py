"""RepoMap — Aider-style file dependency graph + PageRank.

Provides graph-based context selection to complement vector search.
Uses tree-sitter for AST parsing and networkx for the dependency graph.
"""

from .graph import build_dependency_graph, rank_files
from .parser import extract_definitions, extract_references
from .service import RepoMapService

__all__ = [
    "RepoMapService",
    "build_dependency_graph",
    "extract_definitions",
    "extract_references",
    "rank_files",
]
