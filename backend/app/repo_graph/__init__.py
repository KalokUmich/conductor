"""RepoMap — Aider-style file dependency graph + PageRank.

Provides graph-based context selection to complement vector search.
Uses tree-sitter for AST parsing and networkx for the dependency graph.
"""
from .service import RepoMapService
from .parser import extract_definitions, extract_references
from .graph import build_dependency_graph, rank_files

__all__ = [
    "RepoMapService",
    "extract_definitions",
    "extract_references",
    "build_dependency_graph",
    "rank_files",
]
