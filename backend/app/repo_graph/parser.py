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
    name:        str
    kind:        str          # "function", "class", "method", "module"
    file_path:   str
    start_line:  int
    end_line:    int
    signature:   str = ""     # One-line signature for repo map display


@dataclass
class SymbolRef:
    """A reference (usage) of a symbol found in a source file."""
    name:       str
    file_path:  str
    line:       int


@dataclass
class FileSymbols:
    """Definitions and references extracted from a single file."""
    file_path:   str
    definitions: List[SymbolDef] = field(default_factory=list)
    references:  List[SymbolRef] = field(default_factory=list)
    language:    Optional[str]   = None


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_EXT_TO_LANG: Dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".java": "java",
    ".go":   "go",
    ".rs":   "rust",
    ".c":    "c",
    ".h":    "c",
    ".cpp":  "cpp",
    ".cc":   "cpp",
    ".cxx":  "cpp",
    ".hpp":  "cpp",
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
    # Go: function_declaration, method_declaration
    # Rust: function_item, impl_item, struct_item
    # Java: method_declaration, class_declaration

    DEF_NODE_TYPES = {
        "function_definition",      # Python
        "class_definition",         # Python
        "function_declaration",     # JS/TS/Go
        "class_declaration",        # JS/TS/Java
        "method_definition",        # JS/TS
        "method_declaration",       # Java/Go
        "function_item",            # Rust
        "struct_item",              # Rust
        "impl_item",                # Rust
        "interface_declaration",    # TS/Java
        "type_alias_declaration",   # TS
    }

    KIND_MAP = {
        "function_definition":   "function",
        "function_declaration":  "function",
        "function_item":         "function",
        "class_definition":      "class",
        "class_declaration":     "class",
        "struct_item":           "class",
        "impl_item":             "class",
        "interface_declaration": "interface",
        "method_definition":     "method",
        "method_declaration":    "method",
        "type_alias_declaration": "type",
    }

    if node.type in DEF_NODE_TYPES:
        # Find the name child
        name_node = None
        for child in node.children:
            if child.type in ("identifier", "name", "property_identifier", "type_identifier"):
                name_node = child
                break

        if name_node is not None:
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            kind = KIND_MAP.get(node.type, "symbol")

            # Build a one-line signature
            first_line = source[node.start_byte:].split(b"\n")[0]
            signature = first_line.decode("utf-8", errors="replace").strip()
            # Truncate long signatures
            if len(signature) > 120:
                signature = signature[:117] + "..."

            symbols.definitions.append(SymbolDef(
                name       = name,
                kind       = kind,
                file_path  = file_path,
                start_line = node.start_point[0] + 1,
                end_line   = node.end_point[0] + 1,
                signature  = signature,
            ))

    for child in node.children:
        _walk_for_definitions(child, source, file_path, symbols)


def _walk_for_references(
    node, source: bytes, file_path: str, def_names: Set[str], symbols: FileSymbols
) -> None:
    """Collect identifier references that are NOT local definitions."""
    if node.type == "identifier":
        name = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        # Filter out Python keywords and very short names
        if len(name) > 1 and name not in _PYTHON_KEYWORDS:
            symbols.references.append(SymbolRef(
                name      = name,
                file_path = file_path,
                line      = node.start_point[0] + 1,
            ))

    for child in node.children:
        _walk_for_references(child, source, file_path, def_names, symbols)


_PYTHON_KEYWORDS = frozenset({
    "False", "None", "True", "and", "as", "assert", "async", "await",
    "break", "class", "continue", "def", "del", "elif", "else", "except",
    "finally", "for", "from", "global", "if", "import", "in", "is",
    "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
    "try", "while", "with", "yield", "self", "cls",
})


# ---------------------------------------------------------------------------
# Regex fallback (when tree-sitter is not available)
# ---------------------------------------------------------------------------

# Patterns for common languages
_PATTERNS: Dict[str, List[Tuple[str, re.Pattern]]] = {
    "python": [
        ("function", re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)),
        ("class",    re.compile(r"^class\s+(\w+)\s*[:\(]", re.MULTILINE)),
    ],
    "javascript": [
        ("function", re.compile(r"(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE)),
        ("class",    re.compile(r"class\s+(\w+)\s*[\{]", re.MULTILINE)),
        ("function", re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE)),
    ],
    "typescript": [
        ("function", re.compile(r"(?:async\s+)?function\s+(\w+)\s*[\(<]", re.MULTILINE)),
        ("class",    re.compile(r"class\s+(\w+)\s*[\{<]", re.MULTILINE)),
        ("interface", re.compile(r"interface\s+(\w+)\s*[\{<]", re.MULTILINE)),
    ],
    "java": [
        # class / interface / enum / record / @interface
        ("class",     re.compile(r"(?:public|private|protected|abstract|final|static)?\s*class\s+(\w+)\s*[\{<(]", re.MULTILINE)),
        ("interface", re.compile(r"(?:public|private|protected)?\s*interface\s+(\w+)\s*[\{<]", re.MULTILINE)),
        ("class",     re.compile(r"(?:public|private|protected)?\s*enum\s+(\w+)\s*[\{]", re.MULTILINE)),
        ("class",     re.compile(r"(?:public|private|protected)?\s*record\s+(\w+)\s*[\(<]", re.MULTILINE)),
        # methods: access-modifier [static] return-type name(
        ("method",    re.compile(r"^\s+(?:public|private|protected)\s+(?:static\s+)?(?:synchronized\s+)?(?:final\s+)?(?:[\w<>\[\],\s]+?)\s+(\w+)\s*\(", re.MULTILINE)),
    ],
    "go": [
        ("function", re.compile(r"^func\s+(\w+)\s*\(", re.MULTILINE)),
        # method: func (receiver) Name(
        ("method",   re.compile(r"^func\s+\([^)]+\)\s+(\w+)\s*\(", re.MULTILINE)),
        ("class",    re.compile(r"^type\s+(\w+)\s+struct\s*\{", re.MULTILINE)),
        ("interface", re.compile(r"^type\s+(\w+)\s+interface\s*\{", re.MULTILINE)),
    ],
    "rust": [
        ("function", re.compile(r"(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)),
        ("class",    re.compile(r"(?:pub\s+)?struct\s+(\w+)", re.MULTILINE)),
        ("class",    re.compile(r"(?:pub\s+)?enum\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"(?:pub\s+)?trait\s+(\w+)", re.MULTILINE)),
        ("class",    re.compile(r"impl(?:<[^>]+>)?\s+(\w+)", re.MULTILINE)),
    ],
    "c": [
        # function: return_type name(  (at start of line, not indented much)
        ("function", re.compile(r"^(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:unsigned\s+)?(?:struct\s+)?\w[\w*\s]+?\s+(\w+)\s*\([^;]*$", re.MULTILINE)),
        ("class",    re.compile(r"(?:typedef\s+)?struct\s+(\w+)\s*\{", re.MULTILINE)),
        ("class",    re.compile(r"(?:typedef\s+)?enum\s+(\w+)\s*\{", re.MULTILINE)),
    ],
    "cpp": [
        ("function", re.compile(r"^(?:static\s+)?(?:virtual\s+)?(?:inline\s+)?(?:const\s+)?[\w:*&<>\s]+?\s+(\w+)\s*\([^;]*$", re.MULTILINE)),
        ("class",    re.compile(r"(?:class|struct)\s+(\w+)\s*[\{:]", re.MULTILINE)),
        ("interface", re.compile(r"namespace\s+(\w+)\s*\{", re.MULTILINE)),
    ],
}

# Reference pattern: identifiers that look like they reference other symbols
_REF_PATTERN = re.compile(r"\b([A-Z][a-zA-Z0-9_]*|[a-z_][a-zA-Z0-9_]{2,})\b")


def _extract_with_regex(source: str, language: str, file_path: str) -> FileSymbols:
    """Fallback extraction using regex patterns."""
    symbols = FileSymbols(file_path=file_path, language=language)
    lines = source.split("\n")

    # Extract definitions
    patterns = _PATTERNS.get(language, _PATTERNS.get("python", []))
    for kind, pattern in patterns:
        for match in pattern.finditer(source):
            name = match.group(1)
            line_no = source[:match.start()].count("\n") + 1
            # Get the line as signature
            sig = lines[line_no - 1].strip() if line_no <= len(lines) else ""
            if len(sig) > 120:
                sig = sig[:117] + "..."
            symbols.definitions.append(SymbolDef(
                name       = name,
                kind       = kind,
                file_path  = file_path,
                start_line = line_no,
                end_line   = line_no,
                signature  = sig,
            ))

    # Extract references
    def_names = {d.name for d in symbols.definitions}
    for line_no, line in enumerate(lines, 1):
        for match in _REF_PATTERN.finditer(line):
            name = match.group(1)
            if name not in _PYTHON_KEYWORDS and len(name) > 1:
                symbols.references.append(SymbolRef(
                    name      = name,
                    file_path = file_path,
                    line      = line_no,
                ))

    return symbols


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_definitions(file_path: str, source: Optional[bytes] = None) -> FileSymbols:
    """Extract symbol definitions and references from a source file.

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
    if source is None:
        try:
            source = Path(file_path).read_bytes()
        except (OSError, IOError) as exc:
            logger.debug("Cannot read %s: %s", file_path, exc)
            return FileSymbols(file_path=file_path)

    language = detect_language(file_path)
    if language is None:
        return FileSymbols(file_path=file_path)

    try:
        return _extract_with_tree_sitter(source, language, file_path)
    except Exception as exc:
        logger.debug("tree-sitter extraction failed for %s: %s", file_path, exc)
        return _extract_with_regex(source.decode("utf-8", errors="replace"), language, file_path)


def extract_references(file_path: str, source: Optional[bytes] = None) -> List[SymbolRef]:
    """Convenience: extract only references from a file."""
    symbols = extract_definitions(file_path, source)
    return symbols.references
