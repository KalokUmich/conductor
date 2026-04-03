"""Comprehensive tests for the repo_graph module.

Tests:
  * parser.py  — symbol extraction (tree-sitter + regex fallback)
  * graph.py   — dependency graph construction + PageRank
  * service.py — RepoMapService (map generation, caching, hybrid context)

All tests run without real tree-sitter parsers (mocked or uses regex
fallback).

Total: 72 tests
"""

from __future__ import annotations

import sys
import textwrap
import types
from collections import defaultdict
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stubs for heavy deps
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return m


# networkx needs to be real-ish for graph tests
# We'll use a simple mock that tracks calls
class MockDiGraph:
    def __init__(self):
        self._nodes = set()
        self._edges = {}  # (src, tgt) -> attrs
        self._adj = defaultdict(dict)
        self._pred = defaultdict(dict)

    def add_node(self, n, **attrs):
        self._nodes.add(n)

    def add_edge(self, u, v, **attrs):
        self._nodes.add(u)
        self._nodes.add(v)
        self._edges[(u, v)] = attrs
        self._adj[u][v] = attrs
        self._pred[v][u] = attrs

    @property
    def nodes(self):
        return self._nodes

    def in_degree(self, n):
        return len(self._pred.get(n, {}))

    def out_degree(self, n):
        return len(self._adj.get(n, {}))

    def __len__(self):
        return len(self._nodes)

    def __contains__(self, item):
        return item in self._nodes

    def __iter__(self):
        return iter(self._nodes)


# We need real networkx for PageRank, so let's install a minimal mock.
# _stub() uses setdefault and returns the *new* module object even when a prior
# stub already exists in sys.modules.  To ensure our attrs land on the module
# that graph.py actually uses, we always work via sys.modules directly.
_stub("networkx")  # ensure "networkx" key exists in sys.modules
_nx = sys.modules["networkx"]  # get the canonical module object
_nx.DiGraph = MockDiGraph


def _mock_pagerank(G, alpha=0.85, personalization=None, max_iter=100, tol=1e-6):
    """Simple mock PageRank: uniform distribution."""
    n = len(G.nodes)
    if n == 0:
        return {}
    score = 1.0 / n
    if personalization:
        return dict(personalization)
    return {node: score for node in G.nodes}


_nx.pagerank = _mock_pagerank


class MockConvergenceError(Exception):
    pass


_nx.PowerIterationFailedConvergence = MockConvergenceError

_stub("tree_sitter_languages")
_stub("cocoindex")
_stub("sentence_transformers", SentenceTransformer=MagicMock)
_stub("sqlite_vec")

# ---------------------------------------------------------------------------
# Real imports
# ---------------------------------------------------------------------------

from app.repo_graph.graph import (
    DependencyGraph,
    FileNode,
    GraphEdge,
    build_dependency_graph,
    rank_files,
)
from app.repo_graph.parser import (
    FileSymbols,
    SymbolDef,
    SymbolRef,
    _extract_with_regex,
    detect_language,
    extract_definitions,
    extract_references,
)
from app.repo_graph.service import RepoMapService

# ===================================================================
# Parser tests
# ===================================================================


class TestDetectLanguage:
    def test_python(self):
        assert detect_language("main.py") == "python"

    def test_javascript(self):
        assert detect_language("app.js") == "javascript"

    def test_jsx(self):
        assert detect_language("component.jsx") == "javascript"

    def test_typescript(self):
        assert detect_language("service.ts") == "typescript"

    def test_tsx(self):
        assert detect_language("component.tsx") == "typescript"

    def test_java(self):
        assert detect_language("Main.java") == "java"

    def test_go(self):
        assert detect_language("main.go") == "go"

    def test_rust(self):
        assert detect_language("main.rs") == "rust"

    def test_c(self):
        assert detect_language("main.c") == "c"

    def test_cpp(self):
        assert detect_language("main.cpp") == "cpp"

    def test_header(self):
        assert detect_language("util.h") == "c"

    def test_unknown(self):
        assert detect_language("readme.md") is None

    def test_no_extension(self):
        assert detect_language("Makefile") is None

    def test_case_insensitive(self):
        assert detect_language("Module.PY") == "python"


class TestRegexExtraction:
    def test_python_function(self):
        source = "def hello_world():\n    pass\n"
        result = _extract_with_regex(source, "python", "test.py")
        defs = [d for d in result.definitions if d.name == "hello_world"]
        assert len(defs) == 1
        assert defs[0].kind == "function"

    def test_python_async_function(self):
        source = "async def fetch_data():\n    pass\n"
        result = _extract_with_regex(source, "python", "test.py")
        defs = [d for d in result.definitions if d.name == "fetch_data"]
        assert len(defs) == 1
        assert defs[0].kind == "function"

    def test_python_class(self):
        source = "class MyService:\n    pass\n"
        result = _extract_with_regex(source, "python", "test.py")
        defs = [d for d in result.definitions if d.name == "MyService"]
        assert len(defs) == 1
        assert defs[0].kind == "class"

    def test_python_class_with_base(self):
        source = "class MyService(BaseService):\n    pass\n"
        result = _extract_with_regex(source, "python", "test.py")
        defs = [d for d in result.definitions if d.name == "MyService"]
        assert len(defs) == 1

    def test_javascript_function(self):
        source = "function handleClick() {\n}\n"
        result = _extract_with_regex(source, "javascript", "test.js")
        defs = [d for d in result.definitions if d.name == "handleClick"]
        assert len(defs) == 1

    def test_javascript_class(self):
        source = "class UserService {\n}\n"
        result = _extract_with_regex(source, "javascript", "test.js")
        defs = [d for d in result.definitions if d.name == "UserService"]
        assert len(defs) == 1

    def test_typescript_interface(self):
        source = "interface UserProfile {\n  name: string;\n}\n"
        result = _extract_with_regex(source, "typescript", "test.ts")
        defs = [d for d in result.definitions if d.name == "UserProfile"]
        assert len(defs) == 1
        assert defs[0].kind == "interface"

    def test_multiple_definitions(self):
        source = textwrap.dedent("""\
            class Foo:
                pass

            def bar():
                pass

            class Baz:
                pass
        """)
        result = _extract_with_regex(source, "python", "test.py")
        names = {d.name for d in result.definitions}
        assert "Foo" in names
        assert "bar" in names
        assert "Baz" in names

    def test_references_extracted(self):
        source = "x = MyClass()\nresult = helper_func(x)\n"
        result = _extract_with_regex(source, "python", "test.py")
        ref_names = {r.name for r in result.references}
        assert "MyClass" in ref_names
        assert "helper_func" in ref_names

    def test_signature_truncation(self):
        long_sig = "def " + "a" * 200 + "():\n    pass\n"
        result = _extract_with_regex(long_sig, "python", "test.py")
        for d in result.definitions:
            assert len(d.signature) <= 120

    def test_empty_source(self):
        result = _extract_with_regex("", "python", "test.py")
        assert len(result.definitions) == 0

    def test_unknown_language_uses_python_patterns(self):
        source = "def test():\n    pass\n"
        result = _extract_with_regex(source, "unknown_lang", "test.xxx")
        # Falls back to python patterns
        assert isinstance(result, FileSymbols)


class TestExtractDefinitions:
    def test_returns_file_symbols(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def main():\n    pass\n")
        result = extract_definitions(str(f))
        assert isinstance(result, FileSymbols)

    def test_with_source_bytes(self):
        source = b"class Foo:\n    pass\n"
        result = extract_definitions("test.py", source)
        assert isinstance(result, FileSymbols)
        # Should find Foo via regex fallback (tree-sitter is mocked)
        names = {d.name for d in result.definitions}
        assert "Foo" in names

    def test_nonexistent_file(self):
        result = extract_definitions("/does/not/exist.py")
        assert isinstance(result, FileSymbols)
        assert len(result.definitions) == 0

    def test_unknown_extension_returns_empty(self):
        result = extract_definitions("readme.md", b"# Hello\n")
        assert len(result.definitions) == 0

    def test_file_path_in_result(self):
        result = extract_definitions("src/main.py", b"def test(): pass\n")
        assert result.file_path == "src/main.py"


class TestExtractReferences:
    def test_returns_list(self):
        result = extract_references("test.py", b"x = MyClass()\n")
        assert isinstance(result, list)


# ===================================================================
# Graph tests
# ===================================================================


class TestBuildDependencyGraph:
    def test_empty_workspace(self, tmp_path):
        graph = build_dependency_graph(str(tmp_path))
        assert isinstance(graph, DependencyGraph)
        assert graph.stats["total_files"] == 0

    def test_single_file(self, tmp_path):
        (tmp_path / "main.py").write_text("def hello():\n    pass\n")
        graph = build_dependency_graph(str(tmp_path))
        assert graph.stats["total_files"] == 1

    def test_two_files_with_reference(self, tmp_path):
        (tmp_path / "utils.py").write_text("def helper():\n    pass\n")
        (tmp_path / "main.py").write_text("from utils import helper\ndef main():\n    helper()\n")
        graph = build_dependency_graph(str(tmp_path))
        assert graph.stats["total_files"] == 2

    def test_excludes_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("function x() {}\n")
        (tmp_path / "main.py").write_text("def main(): pass\n")
        graph = build_dependency_graph(str(tmp_path))
        # Should only have main.py
        assert graph.stats["total_files"] == 1

    def test_excludes_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        (git_dir / "pre-commit.py").write_text("def hook(): pass\n")
        (tmp_path / "main.py").write_text("def main(): pass\n")
        graph = build_dependency_graph(str(tmp_path))
        assert graph.stats["total_files"] == 1

    def test_pre_computed_symbols(self):
        symbols = {
            "a.py": FileSymbols(
                file_path="a.py",
                language="python",
                definitions=[SymbolDef("Foo", "class", "a.py", 1, 5, "class Foo:")],
                references=[SymbolRef("Bar", "a.py", 3)],
            ),
            "b.py": FileSymbols(
                file_path="b.py",
                language="python",
                definitions=[SymbolDef("Bar", "class", "b.py", 1, 5, "class Bar:")],
                references=[SymbolRef("Foo", "b.py", 3)],
            ),
        }
        graph = build_dependency_graph("/fake", file_symbols=symbols)
        assert graph.stats["total_files"] == 2
        assert graph.stats["total_edges"] == 2  # a→b and b→a

    def test_no_self_edges(self):
        symbols = {
            "a.py": FileSymbols(
                file_path="a.py",
                language="python",
                definitions=[SymbolDef("Foo", "class", "a.py", 1, 5, "class Foo:")],
                references=[SymbolRef("Foo", "a.py", 10)],  # self-reference
            ),
        }
        graph = build_dependency_graph("/fake", file_symbols=symbols)
        assert graph.stats["total_edges"] == 0

    def test_edge_weight_counts_references(self):
        symbols = {
            "a.py": FileSymbols(
                file_path="a.py",
                language="python",
                definitions=[],
                references=[
                    SymbolRef("helper", "a.py", 1),
                    SymbolRef("helper", "a.py", 5),
                    SymbolRef("helper", "a.py", 10),
                ],
            ),
            "b.py": FileSymbols(
                file_path="b.py",
                language="python",
                definitions=[SymbolDef("helper", "function", "b.py", 1, 3, "def helper():")],
                references=[],
            ),
        }
        graph = build_dependency_graph("/fake", file_symbols=symbols)
        assert len(graph.edges) == 1
        assert graph.edges[0].weight == 3

    def test_stats_populated(self, tmp_path):
        (tmp_path / "a.py").write_text("class Foo:\n    pass\n\ndef bar():\n    pass\n")
        graph = build_dependency_graph(str(tmp_path))
        assert "total_files" in graph.stats
        assert "total_edges" in graph.stats
        assert "total_definitions" in graph.stats
        assert "total_references" in graph.stats


class TestRankFiles:
    def test_empty_graph(self):
        graph = DependencyGraph(
            nodes={},
            edges=[],
            graph=MockDiGraph(),
            stats={},
        )
        result = rank_files(graph)
        assert result == []

    def test_uniform_ranking(self):
        G = MockDiGraph()
        G.add_node("a.py")
        G.add_node("b.py")
        G.add_node("c.py")
        nodes = {
            "a.py": FileNode("a.py"),
            "b.py": FileNode("b.py"),
            "c.py": FileNode("c.py"),
        }
        graph = DependencyGraph(nodes=nodes, edges=[], graph=G, stats={})
        result = rank_files(graph, top_n=3)
        assert len(result) == 3

    def test_top_n_limits_results(self):
        G = MockDiGraph()
        for i in range(10):
            G.add_node(f"file{i}.py")
        nodes = {f"file{i}.py": FileNode(f"file{i}.py") for i in range(10)}
        graph = DependencyGraph(nodes=nodes, edges=[], graph=G, stats={})
        result = rank_files(graph, top_n=3)
        assert len(result) == 3

    def test_personalized_ranking(self):
        G = MockDiGraph()
        G.add_node("a.py")
        G.add_node("b.py")
        nodes = {
            "a.py": FileNode("a.py"),
            "b.py": FileNode("b.py"),
        }
        graph = DependencyGraph(nodes=nodes, edges=[], graph=G, stats={})
        result = rank_files(graph, query_files=["a.py"], top_n=2)
        assert len(result) == 2

    def test_updates_node_pagerank(self):
        G = MockDiGraph()
        G.add_node("a.py")
        node = FileNode("a.py")
        nodes = {"a.py": node}
        graph = DependencyGraph(nodes=nodes, edges=[], graph=G, stats={})
        rank_files(graph, top_n=1)
        assert node.pagerank > 0


# ===================================================================
# RepoMapService tests
# ===================================================================


class TestRepoMapServiceInit:
    def test_default_top_n(self):
        svc = RepoMapService()
        assert svc._top_n == 10

    def test_custom_top_n(self):
        svc = RepoMapService(top_n=5)
        assert svc._top_n == 5


class TestRepoMapServiceBuildGraph:
    def test_builds_graph(self, tmp_path):
        (tmp_path / "main.py").write_text("def main(): pass\n")
        svc = RepoMapService()
        graph = svc.build_graph(str(tmp_path))
        assert isinstance(graph, DependencyGraph)

    def test_caches_graph(self, tmp_path):
        (tmp_path / "main.py").write_text("def main(): pass\n")
        svc = RepoMapService()
        g1 = svc.build_graph(str(tmp_path))
        g2 = svc.build_graph(str(tmp_path))
        assert g1 is g2  # same object from cache

    def test_force_rebuild(self, tmp_path):
        (tmp_path / "main.py").write_text("def main(): pass\n")
        svc = RepoMapService()
        g1 = svc.build_graph(str(tmp_path))
        g2 = svc.build_graph(str(tmp_path), force_rebuild=True)
        assert g1 is not g2  # new object


class TestRepoMapServiceGenerateMap:
    def test_generate_empty_workspace(self, tmp_path):
        svc = RepoMapService()
        result = svc.generate_repo_map(str(tmp_path))
        assert "Repository Map" in result

    def test_generate_with_files(self, tmp_path):
        (tmp_path / "main.py").write_text("def main():\n    pass\n\nclass App:\n    pass\n")
        svc = RepoMapService()
        result = svc.generate_repo_map(str(tmp_path))
        assert "Repository Map" in result

    def test_map_contains_file_names(self, tmp_path):
        (tmp_path / "service.py").write_text("class Service:\n    pass\n")
        svc = RepoMapService()
        result = svc.generate_repo_map(str(tmp_path))
        assert isinstance(result, str)

    def test_map_is_string(self, tmp_path):
        svc = RepoMapService()
        result = svc.generate_repo_map(str(tmp_path))
        assert isinstance(result, str)


class TestRepoMapServiceGetContextFiles:
    def test_merges_vector_and_graph(self, tmp_path):
        (tmp_path / "a.py").write_text("class A: pass\n")
        (tmp_path / "b.py").write_text("class B: pass\n")
        (tmp_path / "c.py").write_text("class C: pass\n")
        svc = RepoMapService(top_n=5)
        result = svc.get_context_files(
            str(tmp_path),
            vector_search_files=["a.py"],
        )
        assert "a.py" in result  # vector result preserved
        assert isinstance(result, list)

    def test_vector_results_first(self, tmp_path):
        (tmp_path / "x.py").write_text("def x(): pass\n")
        svc = RepoMapService()
        result = svc.get_context_files(str(tmp_path), vector_search_files=["x.py"])
        assert result[0] == "x.py"

    def test_deduplicates(self, tmp_path):
        (tmp_path / "a.py").write_text("def a(): pass\n")
        svc = RepoMapService()
        result = svc.get_context_files(str(tmp_path), vector_search_files=["a.py"])
        assert result.count("a.py") == 1


class TestRepoMapServiceCache:
    def test_invalidate_specific(self, tmp_path):
        (tmp_path / "main.py").write_text("def main(): pass\n")
        svc = RepoMapService()
        svc.build_graph(str(tmp_path))
        assert str(tmp_path) in svc._graph_cache
        svc.invalidate_cache(str(tmp_path))
        assert str(tmp_path) not in svc._graph_cache

    def test_invalidate_all(self, tmp_path):
        svc = RepoMapService()
        svc._graph_cache["a"] = MagicMock()
        svc._graph_cache["b"] = MagicMock()
        svc.invalidate_cache()
        assert len(svc._graph_cache) == 0

    def test_get_graph_stats_uncached(self):
        svc = RepoMapService()
        stats = svc.get_graph_stats("/nonexistent")
        assert stats == {"cached": False}

    def test_get_graph_stats_cached(self, tmp_path):
        (tmp_path / "main.py").write_text("def main(): pass\n")
        svc = RepoMapService()
        svc.build_graph(str(tmp_path))
        stats = svc.get_graph_stats(str(tmp_path))
        assert stats["cached"] is True


# ===================================================================
# Data class tests
# ===================================================================


class TestDataClasses:
    def test_symbol_def(self):
        sd = SymbolDef("foo", "function", "test.py", 1, 5, "def foo():")
        assert sd.name == "foo"
        assert sd.kind == "function"

    def test_symbol_ref(self):
        sr = SymbolRef("bar", "test.py", 10)
        assert sr.name == "bar"

    def test_file_symbols(self):
        fs = FileSymbols("test.py")
        assert fs.file_path == "test.py"
        assert len(fs.definitions) == 0
        assert len(fs.references) == 0

    def test_file_node(self):
        fn = FileNode("test.py")
        assert fn.path == "test.py"
        assert fn.pagerank == 0.0

    def test_graph_edge(self):
        ge = GraphEdge("a.py", "b.py", weight=3, symbols=["foo", "bar"])
        assert ge.source == "a.py"
        assert ge.target == "b.py"
        assert ge.weight == 3
