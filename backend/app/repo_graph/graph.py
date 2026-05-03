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
    # Default exclude list — covers JS (node_modules / dist / build / .next /
    # .nuxt / coverage), Python (venv / __pycache__ / .mypy_cache /
    # .pytest_cache / .tox / .ruff_cache), Java/JVM (target / .gradle / out /
    # bin / .m2 / classes), IDE (.idea / .vscode), and version control (.git).
    # Compared with the prior list, target/.gradle/out/bin/.idea/.vscode/
    # .next/.nuxt/coverage/.tox/.ruff_cache/.m2/classes were missing — those
    # are why Java/JVM and front-end repos paid the full 270s walk cost on
    # large workspaces (e.g. render's 8.5GB / 293K files).
    exclude = set(
        exclude_patterns
        or [
            # JS / front-end
            "**/node_modules/**",
            "**/dist/**",
            "**/build/**",
            "**/.next/**",
            "**/.nuxt/**",
            "**/coverage/**",
            # Python
            "**/venv/**",
            "**/.venv/**",
            "**/__pycache__/**",
            "**/.mypy_cache/**",
            "**/.pytest_cache/**",
            "**/.tox/**",
            "**/.ruff_cache/**",
            # Java / JVM
            "**/target/**",
            "**/.gradle/**",
            "**/out/**",
            "**/bin/**",
            "**/.m2/**",
            "**/classes/**",
            # IDE
            "**/.idea/**",
            "**/.vscode/**",
            # VCS
            "**/.git/**",
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
    """Scan workspace for source files and extract symbols.

    Emits per-file timing diagnostics to the logger when
    ``CONDUCTOR_SCAN_SLOW_MS`` is set (default 0 — disabled). Any
    ``extract_definitions`` call that takes longer than the threshold is
    logged as a WARNING with the file path and duration, which lets us
    pinpoint pathological tree-sitter inputs when a large-repo eval stalls
    on Brain's Phase 1 pre-compute (e.g. the sentry-007 case we saw hang
    for 30+ minutes — this instrumentation identifies which files burn the
    budget). At the end of the scan, a summary with the top 10 slowest
    files is emitted.
    """
    import os
    import time as _time

    from .parser import detect_language

    slow_threshold_ms = int(os.environ.get("CONDUCTOR_SCAN_SLOW_MS", "0"))

    file_symbols: Dict[str, FileSymbols] = {}
    slow_files: List[tuple] = []  # (duration_ms, path, bytes)
    scan_start = _time.monotonic()
    file_count = 0
    extracted_count = 0

    # Pre-compute the simple directory-name set from the exclude glob list so
    # we can prune dirs in-place during os.walk. This avoids stat'ing the
    # ~250K node_modules / .git / .gradle files in large repos (e.g. render's
    # 8.5GB / 293K files paid 270s on the old ws.rglob walk because rglob
    # walks every file before the per-file exclude check runs).
    #
    # Glob patterns of the shape ``**/<name>/**`` reduce to dir names; more
    # specific globs (containing ``/`` after stripping wildcards) fall back
    # to per-path matching below.
    exclude_dir_names: Set[str] = set()
    glob_patterns: List[str] = []
    for pattern in exclude:
        clean = pattern.strip("*/")
        if clean and "/" not in clean:
            exclude_dir_names.add(clean)
        else:
            glob_patterns.append(pattern)

    for dirpath, dirnames, filenames in os.walk(ws):
        # Prune in-place — os.walk respects this and skips the subtree.
        # This is the load-bearing line: it stops node_modules/.git/.gradle
        # from being walked into at all.
        dirnames[:] = [d for d in dirnames if d not in exclude_dir_names]

        for fn in filenames:
            path = Path(dirpath) / fn
            file_count += 1
            rel_path = path.relative_to(ws)
            rel = str(rel_path)

            # Per-path glob check for the small set of remaining patterns
            # (e.g. specific file globs). Fast path: empty glob_patterns
            # for the default exclude list, so this loop typically no-ops.
            if glob_patterns:
                skip = False
                for pattern in glob_patterns:
                    if path.match(pattern):
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

            t0 = _time.monotonic()
            fsyms = extract_definitions(str(path), source)
            extract_ms = int((_time.monotonic() - t0) * 1000)

            if slow_threshold_ms and extract_ms >= slow_threshold_ms:
                logger.warning(
                    "slow tree-sitter parse: %s (%d bytes, %d ms)",
                    rel,
                    len(source),
                    extract_ms,
                )
                slow_files.append((extract_ms, rel, len(source)))

            # Store with relative path
            fsyms.file_path = rel
            for d in fsyms.definitions:
                d.file_path = rel
            for r in fsyms.references:
                r.file_path = rel
            file_symbols[rel] = fsyms
            extracted_count += 1

    total_ms = int((_time.monotonic() - scan_start) * 1000)
    logger.info(
        "_scan_workspace done: ws=%s files_seen=%d extracted=%d duration_ms=%d",
        ws,
        file_count,
        extracted_count,
        total_ms,
    )
    if slow_files:
        slow_files.sort(reverse=True)  # by duration_ms desc
        top = slow_files[:10]
        logger.warning(
            "_scan_workspace top-10 slow files (total %d over threshold %dms):\n%s",
            len(slow_files),
            slow_threshold_ms,
            "\n".join(f"  {ms:>6d} ms  {rel}  ({b} bytes)" for ms, rel, b in top),
        )

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
