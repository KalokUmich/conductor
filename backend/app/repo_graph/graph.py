"""Dependency graph construction and PageRank ranking.

Builds a directed graph where:
  * Each node is a source file
  * An edge ``A → B`` means file A references a symbol defined in file B
  * Edge weight = number of cross-file references

Uses NetworkX for graph storage and PageRank computation.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx  # type: ignore

from .parser import FileSymbols, SymbolDef, extract_definitions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class FileNode:
    """A node in the dependency graph representing a source file."""

    path: str
    definitions: List[SymbolDef] = field(default_factory=list)
    in_degree: int = 0  # files that depend on this file
    out_degree: int = 0  # files this file depends on
    pagerank: float = 0.0


@dataclass
class GraphEdge:
    """A directed edge: source_file references symbols defined in target_file."""

    source: str
    target: str
    weight: int = 1
    symbols: List[str] = field(default_factory=list)  # referenced symbol names


@dataclass
class DependencyGraph:
    """The complete dependency graph for a repository."""

    nodes: Dict[str, FileNode]
    edges: List[GraphEdge]
    graph: nx.DiGraph
    stats: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_dependency_graph_from_json(raw_data: Dict) -> DependencyGraph:
    """Build a DependencyGraph from JSON data sent by the VS Code extension.

    The extension builds the graph using LSP and sends it as a JSON dict
    with a ``files`` key containing per-file symbol data.  This function
    converts that into the same ``DependencyGraph`` that tree-sitter produces.
    """
    from .parser import FileSymbols, SymbolDef, SymbolRef

    files_raw = raw_data.get("files", {})
    file_symbols: Dict[str, FileSymbols] = {}

    for rel_path, fdata in files_raw.items():
        defs = [
            SymbolDef(
                name=d["name"],
                kind=d["kind"],
                file_path=d["file_path"],
                start_line=d["start_line"],
                end_line=d["end_line"],
                signature=d.get("signature", d["name"]),
            )
            for d in fdata.get("definitions", [])
        ]
        refs = [
            SymbolRef(name=r["name"], file_path=r["file_path"], line=r["line"]) for r in fdata.get("references", [])
        ]
        file_symbols[rel_path] = FileSymbols(
            file_path=rel_path,
            definitions=defs,
            references=refs,
            language=fdata.get("language"),
        )

    return build_dependency_graph(
        workspace_path=".",  # not used when file_symbols is provided
        file_symbols=file_symbols,
    )


def build_dependency_graph(
    workspace_path: str,
    file_symbols: Optional[Dict[str, FileSymbols]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> DependencyGraph:
    """Build a file dependency graph from source files.

    Parameters
    ----------
    workspace_path:
        Root directory of the repository.
    file_symbols:
        Pre-computed FileSymbols per file. If None, scans *workspace_path*.
    exclude_patterns:
        Glob patterns to exclude (e.g. ``["**/node_modules/**"]``).

    Returns
    -------
    DependencyGraph
    """
    ws = Path(workspace_path)
    exclude = set(
        exclude_patterns
        or [
            "**/node_modules/**",
            "**/.git/**",
            "**/venv/**",
            "**/__pycache__/**",
            "**/dist/**",
            "**/build/**",
            "**/.mypy_cache/**",
            "**/.pytest_cache/**",
        ]
    )

    # 1. Collect file symbols
    if file_symbols is None:
        file_symbols = _scan_workspace(ws, exclude)

    # 2. Build symbol → defining file lookup
    symbol_to_file: Dict[str, Set[str]] = defaultdict(set)
    for fpath, fsyms in file_symbols.items():
        for defn in fsyms.definitions:
            symbol_to_file[defn.name].add(fpath)

    # 3. Build directed graph
    G = nx.DiGraph()
    nodes: Dict[str, FileNode] = {}
    edge_map: Dict[Tuple[str, str], List[str]] = defaultdict(list)

    for fpath, fsyms in file_symbols.items():
        node = FileNode(path=fpath, definitions=fsyms.definitions)
        nodes[fpath] = node
        G.add_node(fpath)

        # For each reference in this file, check if it resolves to a
        # definition in another file
        for ref in fsyms.references:
            if ref.name in symbol_to_file:
                for target_file in symbol_to_file[ref.name]:
                    if target_file != fpath:  # skip self-references
                        edge_map[(fpath, target_file)].append(ref.name)

    # Add weighted edges
    edges: List[GraphEdge] = []
    for (src, tgt), syms in edge_map.items():
        weight = len(syms)
        G.add_edge(src, tgt, weight=weight)
        edges.append(GraphEdge(source=src, target=tgt, weight=weight, symbols=list(set(syms))))

    # Update degree info
    for fpath in nodes:
        if fpath in G:
            nodes[fpath].in_degree = G.in_degree(fpath)
            nodes[fpath].out_degree = G.out_degree(fpath)

    stats = {
        "total_files": len(nodes),
        "total_edges": len(edges),
        "total_definitions": sum(len(fs.definitions) for fs in file_symbols.values()),
        "total_references": sum(len(fs.references) for fs in file_symbols.values()),
    }

    logger.info(
        "Built dependency graph: %d files, %d edges, %d definitions",
        stats["total_files"],
        stats["total_edges"],
        stats["total_definitions"],
    )

    return DependencyGraph(nodes=nodes, edges=edges, graph=G, stats=stats)


def _scan_workspace(ws: Path, exclude: Set[str]) -> Dict[str, FileSymbols]:
    """Scan workspace for source files and extract symbols."""
    from .parser import detect_language

    file_symbols: Dict[str, FileSymbols] = {}

    for path in ws.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(ws))

        # Check exclude patterns against relative path components.
        # pathlib.Path.match("**/name/**") is unreliable in Python ≤ 3.12, so
        # we strip the glob wildcards and check the parent directory names directly.
        rel_parts = path.relative_to(ws).parts  # includes filename as last element
        skip = False
        for pattern in exclude:
            clean = pattern.strip("*/")  # "**/node_modules/**" → "node_modules"
            if "/" not in clean and clean:
                # Simple directory name — check any parent component
                if clean in rel_parts[:-1]:
                    skip = True
                    break
            elif path.match(pattern):
                skip = True
                break
        if skip:
            continue

        # Only process files with known language extensions
        lang = detect_language(str(path))
        if lang is None:
            continue

        try:
            source = path.read_bytes()
        except OSError:
            continue

        # Skip large files (>500KB) to avoid slowdowns
        if len(source) > 500_000:
            logger.debug("Skipping large file: %s (%d bytes)", rel, len(source))
            continue

        fsyms = extract_definitions(str(path), source)
        # Store with relative path
        fsyms.file_path = rel
        for d in fsyms.definitions:
            d.file_path = rel
        for r in fsyms.references:
            r.file_path = rel
        file_symbols[rel] = fsyms

    return file_symbols


# ---------------------------------------------------------------------------
# PageRank ranking
# ---------------------------------------------------------------------------


def rank_files(
    dep_graph: DependencyGraph,
    query_files: Optional[List[str]] = None,
    top_n: int = 10,
    personalization_weight: float = 0.5,
) -> List[Tuple[str, float]]:
    """Rank files using PageRank, optionally personalised to query files.

    Parameters
    ----------
    dep_graph:
        The dependency graph built by :func:`build_dependency_graph`.
    query_files:
        Files relevant to the current query (from vector search).
        If provided, PageRank is personalised so these files receive
        higher teleportation probability.
    top_n:
        Number of top-ranked files to return.
    personalization_weight:
        How much of the teleport probability is concentrated on
        *query_files* (0 = uniform, 1 = only query files).

    Returns
    -------
    List of (file_path, pagerank_score) tuples, sorted by score descending.
    """
    G = dep_graph.graph
    if len(G.nodes) == 0:
        return []

    personalization = None
    if query_files:
        # Personalised PageRank: bias towards files found by vector search
        n = len(G.nodes)
        base = (1 - personalization_weight) / n
        personalization = {}
        for node in G.nodes:
            if node in query_files:
                personalization[node] = base + personalization_weight / max(len(query_files), 1)
            else:
                personalization[node] = base

    try:
        scores = nx.pagerank(
            G,
            alpha=0.85,
            personalization=personalization,
            max_iter=100,
            tol=1e-6,
        )
    except nx.PowerIterationFailedConvergence:
        logger.warning("PageRank did not converge, using uniform scores")
        scores = {n: 1.0 / len(G.nodes) for n in G.nodes}

    # Update node pagerank values
    for fpath, score in scores.items():
        if fpath in dep_graph.nodes:
            dep_graph.nodes[fpath].pagerank = score

    # Sort and return top-N
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_n]
