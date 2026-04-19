"""AST-based symbol extraction using tree-sitter.

Extracts definitions (functions, classes, methods) and references
(identifiers, imports) from source files.  Used to build the
dependency graph that powers the repo map.

Supported languages: Python, JavaScript, TypeScript, Java, Go, Rust, C, C++.
Falls back gracefully if a language grammar is not installed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SymbolDef:
    """A symbol definition extracted from a source file."""

    name: str
    kind: str  # "function", "class", "method", "module"
    file_path: str
    start_line: int
    end_line: int
    signature: str = ""  # One-line signature for repo map display


@dataclass
class SymbolRef:
    """A reference (usage) of a symbol found in a source file."""

    name: str
    file_path: str
    line: int


@dataclass
class FileSymbols:
    """Definitions and references extracted from a single file.

    ``extracted_via`` records which backend produced the results — this
    matters because the regex fallback (used when tree-sitter times out
    or can't parse) has lower recall on nested definitions, arrow
    functions, decorators, etc. Tools that surface structural data should
    check this and pass a ``degraded_files`` list up to the caller so the
    agent knows to prefer ``grep`` / ``read_file`` for authoritative
    answers on those paths.
    """

    file_path: str
    definitions: List[SymbolDef] = field(default_factory=list)
    references: List[SymbolRef] = field(default_factory=list)
    language: Optional[str] = None
    extracted_via: str = "tree_sitter"  # or "regex" when fallback ran


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_EXT_TO_LANG: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}


def detect_language(file_path: str) -> Optional[str]:
    """Detect programming language from file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(ext)


# ---------------------------------------------------------------------------
# Tree-sitter based extraction
# ---------------------------------------------------------------------------

# Cache loaded parsers to avoid re-creating for each file
_parser_cache: Dict[str, object] = {}


def _get_parser(language: str):
    """Get or create a tree-sitter parser for *language*."""
    if language in _parser_cache:
        return _parser_cache[language]

    try:
        import tree_sitter_languages  # type: ignore

        parser = tree_sitter_languages.get_parser(language)
        _parser_cache[language] = parser
        return parser
    except (ImportError, Exception) as exc:
        logger.debug("tree-sitter parser for %s not available: %s", language, exc)
        return None


def _extract_with_tree_sitter(source: bytes, language: str, file_path: str) -> FileSymbols:
    """Extract symbols using tree-sitter AST parsing."""
    parser = _get_parser(language)
    if parser is None:
        return _extract_with_regex(source.decode("utf-8", errors="replace"), language, file_path)

    tree = parser.parse(source)
    root = tree.root_node

    symbols = FileSymbols(file_path=file_path, language=language)

    # Walk the tree and collect definitions
    _walk_for_definitions(root, source, file_path, symbols)

    # Collect references (identifiers not in definitions)
    def_names = {d.name for d in symbols.definitions}
    _walk_for_references(root, source, file_path, def_names, symbols)

    return symbols


def _walk_for_definitions(node, source: bytes, file_path: str, symbols: FileSymbols) -> None:
    """Recursively walk tree-sitter AST to find definitions."""
    # Python: function_definition, class_definition
    # JS/TS: function_declaration, class_declaration, method_definition
    # Go: function_declaration, method_declaration, type_spec (struct/interface)
    # Rust: function_item, impl_item, struct_item
    # Java: method_declaration, class_declaration, constructor_declaration
    # C/C++: function_definition (name nested in declarator chain),
    #        class_specifier, struct_specifier

    DEF_NODE_TYPES = {
        "function_definition",  # Python / C / C++
        "class_definition",  # Python
        "function_declaration",  # JS/TS/Go
        "class_declaration",  # JS/TS/Java
        "method_definition",  # JS/TS
        "method_declaration",  # Java/Go
        "constructor_declaration",  # Java
        "function_item",  # Rust
        "struct_item",  # Rust
        "impl_item",  # Rust
        "interface_declaration",  # TS/Java
        "type_alias_declaration",  # TS
        "class_specifier",  # C++
        "struct_specifier",  # C/C++
        "type_spec",  # Go (struct / interface / type alias)
    }

    KIND_MAP = {
        "function_definition": "function",
        "function_declaration": "function",
        "function_item": "function",
        "class_definition": "class",
        "class_declaration": "class",
        "class_specifier": "class",
        "struct_specifier": "class",
        "struct_item": "class",
        "impl_item": "class",
        "interface_declaration": "interface",
        "method_definition": "method",
        "method_declaration": "method",
        "constructor_declaration": "method",
        "type_alias_declaration": "type",
    }

    if node.type in DEF_NODE_TYPES:
        name_node = _resolve_def_name(node)

        if name_node is not None:
            name = source[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
            kind = KIND_MAP.get(node.type) or _kind_from_type_spec(node)

            # Build a one-line signature
            first_line = source[node.start_byte :].split(b"\n")[0]
            signature = first_line.decode("utf-8", errors="replace").strip()
            # Truncate long signatures
            if len(signature) > 120:
                signature = signature[:117] + "..."

            symbols.definitions.append(
                SymbolDef(
                    name=name,
                    kind=kind,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    signature=signature,
                )
            )

    for child in node.children:
        _walk_for_definitions(child, source, file_path, symbols)


def _resolve_def_name(node):
    """Find the identifier node that names this definition.

    Handles three tricky cases the simple "first identifier-like child" loop
    gets wrong:

    1. C/C++ function_definition: the name is buried inside a declarator chain
       like ``function_definition → declarator (function_declarator)
       → declarator (identifier|field_identifier)``. A naive search either
       finds nothing (when the function has a primitive return type) or grabs
       a `type_identifier` (e.g. picks ``T`` instead of ``identity`` for a
       template function).
    2. C++ class_specifier / struct_specifier: name is a `type_identifier`
       child, not under a "name" field.
    3. Go type_spec: name is a `type_identifier` child.
    """
    # Try the explicit "name" field first — works for Python, JS/TS, Java,
    # Rust, etc. This must come before any structural search so Java methods
    # don't pick up the return type by mistake.
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node

    # C/C++ function_definition: walk down the declarator chain.
    if node.type == "function_definition":
        decl = node.child_by_field_name("declarator")
        # Limit the walk to a few levels to avoid pathological cycles.
        for _ in range(8):
            if decl is None:
                break
            if decl.type in ("identifier", "field_identifier"):
                return decl
            decl = decl.child_by_field_name("declarator")
        # Fall through to the generic search below if the chain didn't yield
        # an identifier.

    # C++ class/struct specifier and Go type_spec: name is the type_identifier.
    if node.type in ("class_specifier", "struct_specifier", "type_spec"):
        for child in node.children:
            if child.type == "type_identifier":
                return child

    # Generic fallback: any identifier-like child. Kept last so it never
    # shadows a more specific resolution above.
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier", "type_identifier"):
            return child

    return None


def _kind_from_type_spec(node) -> str:
    """For a Go type_spec node, return 'class' for struct, 'interface' for
    interface, 'type' for any other type alias. Returns 'symbol' for any
    other node type so callers can chain this after KIND_MAP.get()."""
    if node.type != "type_spec":
        return "symbol"
    for child in node.children:
        if child.type == "struct_type":
            return "class"
        if child.type == "interface_type":
            return "interface"
    return "type"


def _walk_for_references(node, source: bytes, file_path: str, def_names: Set[str], symbols: FileSymbols) -> None:
    """Collect identifier references that are NOT local definitions."""
    if node.type == "identifier":
        name = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
        # Filter out Python keywords and very short names
        if len(name) > 1 and name not in _PYTHON_KEYWORDS:
            symbols.references.append(
                SymbolRef(
                    name=name,
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                )
            )

    for child in node.children:
        _walk_for_references(child, source, file_path, def_names, symbols)


_PYTHON_KEYWORDS = frozenset(
    {
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        "self",
        "cls",
    }
)


# ---------------------------------------------------------------------------
# Regex fallback (when tree-sitter is not available)
# ---------------------------------------------------------------------------

# Patterns for common languages
_PATTERNS: Dict[str, List[Tuple[str, re.Pattern]]] = {
    "python": [
        ("function", re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)),
        ("class", re.compile(r"^class\s+(\w+)\s*[:\(]", re.MULTILINE)),
    ],
    "javascript": [
        ("function", re.compile(r"(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE)),
        ("class", re.compile(r"class\s+(\w+)\s*[\{]", re.MULTILINE)),
        ("function", re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE)),
    ],
    "typescript": [
        ("function", re.compile(r"(?:async\s+)?function\s+(\w+)\s*[\(<]", re.MULTILINE)),
        ("class", re.compile(r"class\s+(\w+)\s*[\{<]", re.MULTILINE)),
        ("interface", re.compile(r"interface\s+(\w+)\s*[\{<]", re.MULTILINE)),
    ],
    "java": [
        # class / interface / enum / record / @interface
        (
            "class",
            re.compile(r"(?:public|private|protected|abstract|final|static)?\s*class\s+(\w+)\s*[\{<(]", re.MULTILINE),
        ),
        ("interface", re.compile(r"(?:public|private|protected)?\s*interface\s+(\w+)\s*[\{<]", re.MULTILINE)),
        ("class", re.compile(r"(?:public|private|protected)?\s*enum\s+(\w+)\s*[\{]", re.MULTILINE)),
        ("class", re.compile(r"(?:public|private|protected)?\s*record\s+(\w+)\s*[\(<]", re.MULTILINE)),
        # methods: access-modifier [static] return-type name(
        (
            "method",
            re.compile(
                r"^\s+(?:public|private|protected)\s+(?:static\s+)?(?:synchronized\s+)?(?:final\s+)?(?:[\w<>\[\],\s]+?)\s+(\w+)\s*\(",
                re.MULTILINE,
            ),
        ),
    ],
    "go": [
        ("function", re.compile(r"^func\s+(\w+)\s*\(", re.MULTILINE)),
        # method: func (receiver) Name(
        ("method", re.compile(r"^func\s+\([^)]+\)\s+(\w+)\s*\(", re.MULTILINE)),
        ("class", re.compile(r"^type\s+(\w+)\s+struct\s*\{", re.MULTILINE)),
        ("interface", re.compile(r"^type\s+(\w+)\s+interface\s*\{", re.MULTILINE)),
    ],
    "rust": [
        ("function", re.compile(r"(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)),
        ("class", re.compile(r"(?:pub\s+)?struct\s+(\w+)", re.MULTILINE)),
        ("class", re.compile(r"(?:pub\s+)?enum\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"(?:pub\s+)?trait\s+(\w+)", re.MULTILINE)),
        ("class", re.compile(r"impl(?:<[^>]+>)?\s+(\w+)", re.MULTILINE)),
    ],
    "c": [
        # function: return_type name(  (at start of line, not indented much)
        (
            "function",
            re.compile(
                r"^(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:unsigned\s+)?(?:struct\s+)?\w[\w*\s]+?\s+(\w+)\s*\([^;]*$",
                re.MULTILINE,
            ),
        ),
        ("class", re.compile(r"(?:typedef\s+)?struct\s+(\w+)\s*\{", re.MULTILINE)),
        ("class", re.compile(r"(?:typedef\s+)?enum\s+(\w+)\s*\{", re.MULTILINE)),
    ],
    "cpp": [
        (
            "function",
            re.compile(
                r"^(?:static\s+)?(?:virtual\s+)?(?:inline\s+)?(?:const\s+)?[\w:*&<>\s]+?\s+(\w+)\s*\([^;]*$",
                re.MULTILINE,
            ),
        ),
        ("class", re.compile(r"(?:class|struct)\s+(\w+)\s*[\{:]", re.MULTILINE)),
        ("interface", re.compile(r"namespace\s+(\w+)\s*\{", re.MULTILINE)),
    ],
}

# Reference pattern: identifiers that look like they reference other symbols
_REF_PATTERN = re.compile(r"\b([A-Z][a-zA-Z0-9_]*|[a-z_][a-zA-Z0-9_]{2,})\b")


def _extract_with_regex(source: str, language: str, file_path: str) -> FileSymbols:
    """Fallback extraction using regex patterns."""
    symbols = FileSymbols(file_path=file_path, language=language, extracted_via="regex")
    lines = source.split("\n")

    # Extract definitions
    patterns = _PATTERNS.get(language, _PATTERNS.get("python", []))
    for kind, pattern in patterns:
        for match in pattern.finditer(source):
            name = match.group(1)
            line_no = source[: match.start()].count("\n") + 1
            # Get the line as signature
            sig = lines[line_no - 1].strip() if line_no <= len(lines) else ""
            if len(sig) > 120:
                sig = sig[:117] + "..."
            symbols.definitions.append(
                SymbolDef(
                    name=name,
                    kind=kind,
                    file_path=file_path,
                    start_line=line_no,
                    end_line=line_no,
                    signature=sig,
                )
            )

    # Extract references
    for line_no, line in enumerate(lines, 1):
        for match in _REF_PATTERN.finditer(line):
            name = match.group(1)
            if name not in _PYTHON_KEYWORDS and len(name) > 1:
                symbols.references.append(
                    SymbolRef(
                        name=name,
                        file_path=file_path,
                        line=line_no,
                    )
                )

    return symbols


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_definitions_with_timeout(
    file_path: str,
    source: Optional[bytes] = None,
    timeout_s: Optional[float] = None,
) -> FileSymbols:
    """Like :func:`extract_definitions` but bounded by a wall-clock timeout.

    Phase 9.18 step 1. Pathological inputs (deeply nested TSX with generic
    type params) can blow up tree-sitter's GLR parser from milliseconds to
    3–9 minutes — sentry-007 diagnostic caught 4 files eating 200–530 s
    each. This wrapper caps each file at ``timeout_s`` seconds; if the
    parse doesn't finish we fall back to :func:`_extract_with_regex`
    (same safe fallback we already use on exceptions) and record the file
    in the current-session Fact Vault's ``skip_facts`` so future tool
    calls on that path short-circuit without retrying.

    ``timeout_s`` defaults to ``CONDUCTOR_PARSE_TIMEOUT_S`` (60 s). Set
    to ``0`` to disable the timeout, useful for tests.

    Implementation note: the parse runs in a persistent worker
    subprocess (see :mod:`app.repo_graph.parse_pool`) so we can SIGKILL
    it on timeout. An earlier daemon-thread design was shown by py-spy
    to be broken — tree-sitter's C binding holds the GIL through the
    parse, so the main thread could never reacquire the GIL to raise
    the timeout. The subprocess primitive is the only reliable one.
    """
    import os as _os
    import time as _t

    if timeout_s is None:
        try:
            timeout_s = float(_os.environ.get("CONDUCTOR_PARSE_TIMEOUT_S", "60"))
        except ValueError:
            timeout_s = 60.0

    if source is None:
        try:
            source = Path(file_path).read_bytes()
        except OSError as exc:
            logger.debug("Cannot read %s: %s", file_path, exc)
            return FileSymbols(file_path=file_path)

    language = detect_language(file_path)
    if language is None:
        return FileSymbols(file_path=file_path)

    # TSX/JSX heuristic — deeply nested JSX blows up tree-sitter's GLR
    # parser (the sentry-007 pathology). Instead of paying a 60s timeout
    # on the first encounter, run a cheap byte-level depth estimator and
    # route obviously-pathological files straight to regex. Only kicks
    # in on .tsx/.jsx > 20 KB because small files always parse quickly
    # regardless of JSX depth. Threshold 15 is conservative — the
    # pathological sentry-007 files were 30+ nested. False positives
    # (big TSX with wide-but-shallow JSX misrouted to regex) degrade to
    # the same signal the ``extracted_via: "regex"`` tag already carries,
    # so the agent knows to grep.
    if (
        timeout_s > 0
        and file_path.endswith((".tsx", ".jsx"))
        and len(source) > 20_000
    ):
        depth = _estimate_jsx_depth(source)
        if depth > 15:
            logger.info(
                "TSX heuristic skip: %s (jsx_depth~%d, size=%d) — regex only",
                file_path, depth, len(source),
            )
            store_h = _current_factstore_safe()
            if store_h is not None:
                try:
                    store_h.put_skip(
                        file_path,
                        reason=f"jsx-depth heuristic (~{depth} levels)",
                        duration_ms=0,
                    )
                except Exception as exc:
                    logger.debug("skip-fact write failed for %s: %s", file_path, exc)
            return _extract_with_regex(
                source.decode("utf-8", errors="replace"), language, file_path
            )

    # Pre-check: if an earlier call in this session already flagged this
    # path as pathological, don't re-trigger tree-sitter. Regex fallback
    # only — same symbol shape, zero timeout risk.
    store = _current_factstore_safe()
    if store is not None:
        try:
            if store.should_skip(file_path):
                return _extract_with_regex(
                    source.decode("utf-8", errors="replace"), language, file_path
                )
        except Exception as exc:  # best-effort; never block extraction on vault errors
            logger.debug("skip-list pre-check failed for %s: %s", file_path, exc)

    if timeout_s <= 0:
        # Timeout disabled — legacy in-process behaviour with the
        # existing regex fallback on exception. Useful for benchmarks /
        # tests that must not spawn a subprocess.
        try:
            return _extract_with_tree_sitter(source, language, file_path)
        except Exception as exc:
            logger.debug("tree-sitter extraction failed for %s: %s", file_path, exc)
            return _extract_with_regex(
                source.decode("utf-8", errors="replace"), language, file_path
            )

    from .parse_pool import get_parse_pool

    pool = get_parse_pool()
    t0 = _t.monotonic()
    result = pool.parse(source, language, file_path, timeout_s=timeout_s)
    elapsed_ms = int((_t.monotonic() - t0) * 1000)

    if result is None:
        # Pool returns None on timeout, pickle error, worker crash, or
        # any other failure. Treat all as "tree-sitter unavailable for
        # this file" — log + skip-list write + regex fallback.
        logger.warning(
            "tree-sitter parse failed after %.1fs (limit=%.1fs): %s — "
            "falling back to regex, file skipped for rest of session",
            elapsed_ms / 1000, timeout_s, file_path,
        )
        if store is not None:
            try:
                store.put_skip(
                    file_path,
                    reason=f"tree-sitter timeout/failure after {timeout_s:.0f}s",
                    duration_ms=elapsed_ms,
                )
            except Exception as exc:
                logger.debug("skip-fact write failed for %s: %s", file_path, exc)
        return _extract_with_regex(
            source.decode("utf-8", errors="replace"), language, file_path
        )

    return result


def _current_factstore_safe():
    """Return the active session FactStore, or None if scratchpad is
    disabled or not bound. Isolated helper so parser.py's import surface
    stays minimal — scratchpad is imported lazily."""
    try:
        from app.scratchpad.context import current_factstore

        return current_factstore()
    except Exception:
        return None


def _estimate_jsx_depth(source: bytes) -> int:
    """Byte-level max-depth estimator for JSX nesting.

    Used as a pre-filter for the TSX/JSX heuristic in
    :func:`extract_definitions_with_timeout`. Not a parser — it counts
    ``<Xxx`` opens (React-component style: ``<`` followed by an
    uppercase letter) and ``</`` / ``/>`` closes, tracking running
    depth. Imperfect across strings, template literals, and TypeScript
    generic type params sharing the ``<`` token, but good enough to
    catch the pathological case: deeply nested JSX (15+ levels) is the
    known trigger for tree-sitter-typescript GLR blowup.

    Cost: ~1-5 ms on a 30 KB file. Only called for ``.tsx``/``.jsx``
    over the size gate, so overall overhead is negligible.
    """
    depth = 0
    max_depth = 0
    n = len(source)
    i = 0
    while i < n - 1:
        b = source[i]
        nb = source[i + 1]
        if b == 0x3C:  # '<'
            if nb == 0x2F:  # '</' — JSX close tag
                depth = max(0, depth - 1)
                i += 2
                continue
            # '<' followed by uppercase ASCII letter — React component open
            if 0x41 <= nb <= 0x5A:
                depth += 1
                if depth > max_depth:
                    max_depth = depth
                i += 2
                continue
        elif b == 0x2F and nb == 0x3E:  # '/>'  self-closing
            depth = max(0, depth - 1)
            i += 2
            continue
        i += 1
    return max_depth


def extract_definitions(
    file_path: str,
    source: Optional[bytes] = None,
    *,
    timeout_s: Optional[float] = None,
) -> FileSymbols:
    """Extract symbol definitions and references from a source file.

    Phase 9.18 step 1: this call is now bounded by a wall-clock timeout
    via :func:`extract_definitions_with_timeout` (default 60 s, override
    with ``CONDUCTOR_PARSE_TIMEOUT_S``). Set ``timeout_s=0`` to keep
    legacy synchronous behaviour — useful for benchmark / reproducibility
    tests that must not spawn daemon threads.

    Parameters
    ----------
    file_path:
        Path to the source file (used for language detection and output).
    source:
        Raw file contents. If None, reads from *file_path*.

    Returns
    -------
    FileSymbols
        Definitions and references found in the file.
    """
    return extract_definitions_with_timeout(file_path, source, timeout_s=timeout_s)


def extract_references(file_path: str, source: Optional[bytes] = None) -> List[SymbolRef]:
    """Convenience: extract only references from a file."""
    symbols = extract_definitions(file_path, source)
    return symbols.references
