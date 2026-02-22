"""Symbol-aware code chunking for the RAG pipeline.

Splits source files into semantically meaningful chunks suitable for
embedding.  Each chunk includes the file's import header so the embedding
model has dependency context.

Language parsers are regex-based, mirroring the patterns used in the
extension's ``symbolExtractor.ts``.
"""
import re
from dataclasses import dataclass
from typing import List


@dataclass
class CodeChunk:
    """A single chunk of code ready for embedding."""

    content: str
    file_path: str
    start_line: int
    end_line: int
    symbol_name: str = ""
    symbol_type: str = ""  # function | class | method | block
    language: str = ""
    import_header: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chunk_file(
    content: str,
    file_path: str,
    language: str,
    max_lines: int = 200,
) -> List[CodeChunk]:
    """Split a source file into semantically meaningful chunks.

    Steps:
    1. Extract import block (first N lines until first non-import).
    2. Extract top-level symbols via language-specific regex.
    3. Remaining code → "block" chunks.
    4. Oversized symbols split at blank-line boundaries.
    5. Import header (first 30 lines) prepended to each chunk for context.

    Args:
        content:   Full file text.
        file_path: Workspace-relative path.
        language:  Language ID (python, typescript, javascript, java, go).
        max_lines: Maximum lines per chunk before splitting.

    Returns:
        List of CodeChunk instances.
    """
    if not content.strip():
        return []

    lines = content.splitlines()
    import_header = _extract_import_header(lines, language)

    # Extract symbols
    symbols = _extract_symbols(lines, language)

    if not symbols:
        # No symbols found — chunk the whole file as blocks
        return _chunk_as_blocks(lines, file_path, language, import_header, max_lines)

    chunks: List[CodeChunk] = []
    covered = set()  # line indices covered by symbols

    for sym in symbols:
        sym_lines = lines[sym["start"]:sym["end"]]
        sym_content = "\n".join(sym_lines)

        if len(sym_lines) <= max_lines:
            chunk = CodeChunk(
                content=_prepend_header(import_header, sym_content),
                file_path=file_path,
                start_line=sym["start"] + 1,
                end_line=sym["end"],
                symbol_name=sym["name"],
                symbol_type=sym["type"],
                language=language,
                import_header=import_header,
            )
            chunks.append(chunk)
        else:
            # Split oversized symbol at blank-line boundaries
            sub_chunks = _split_oversized(
                sym_lines, sym["start"], sym["name"], sym["type"],
                file_path, language, import_header, max_lines,
            )
            chunks.extend(sub_chunks)

        for i in range(sym["start"], sym["end"]):
            covered.add(i)

    # Remaining uncovered lines → block chunks
    block_lines: List[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if i not in covered:
            block_lines.append((i, line))
        else:
            if block_lines:
                _flush_block(block_lines, chunks, file_path, language, import_header, max_lines)
                block_lines = []

    if block_lines:
        _flush_block(block_lines, chunks, file_path, language, import_header, max_lines)

    return chunks


# ---------------------------------------------------------------------------
# Import header extraction
# ---------------------------------------------------------------------------

_IMPORT_PATTERNS: dict[str, re.Pattern] = {
    "python":     re.compile(r"^(?:import |from )\S"),
    "typescript": re.compile(r"^(?:import |const .+ = require)"),
    "javascript": re.compile(r"^(?:import |const .+ = require)"),
    "java":       re.compile(r"^(?:import |package )"),
    "go":         re.compile(r"^(?:import |package )"),
}

# Lines that are not import lines but are acceptable within the import block
_PASSTHROUGH = re.compile(r"^\s*$|^\s*//|^\s*#|^\s*\*|^\s*/\*|^\s*\*/")

MAX_IMPORT_HEADER_LINES = 30


def _extract_import_header(lines: list[str], language: str) -> str:
    """Return the import block from the top of the file (max 30 lines)."""
    pattern = _IMPORT_PATTERNS.get(language, re.compile(r"^(?:import |from |require|use )"))
    header_lines: list[str] = []
    found_import = False

    for line in lines:
        stripped = line.strip()
        if pattern.match(stripped):
            found_import = True
            header_lines.append(line)
        elif found_import and _PASSTHROUGH.match(stripped):
            header_lines.append(line)
        elif found_import:
            break  # Non-import, non-blank line after imports → done
        elif _PASSTHROUGH.match(stripped):
            header_lines.append(line)  # Comments/blanks before first import
        else:
            break  # Non-import code before any import → no header

        if len(header_lines) >= MAX_IMPORT_HEADER_LINES:
            break

    return "\n".join(header_lines)


# ---------------------------------------------------------------------------
# Symbol extraction (regex-based)
# ---------------------------------------------------------------------------

def _extract_symbols(lines: list[str], language: str) -> list[dict]:
    """Extract top-level symbols from the file.

    Returns a list of dicts with keys: name, type, start, end (line indices).
    """
    extractors = {
        "python":     _extract_python_symbols,
        "typescript": _extract_ts_js_symbols,
        "javascript": _extract_ts_js_symbols,
        "java":       _extract_java_symbols,
        "go":         _extract_go_symbols,
    }
    extractor = extractors.get(language)
    if extractor is None:
        return []

    return extractor(lines)


def _extract_python_symbols(lines: list[str]) -> list[dict]:
    """Extract Python functions and classes."""
    pattern = re.compile(r"^(async\s+)?def\s+(\w+)|^class\s+(\w+)")
    symbols: list[dict] = []

    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            is_async = m.group(1) is not None
            name = m.group(2) or m.group(3)
            sym_type = "class" if m.group(3) else "function"
            symbols.append({
                "name": name,
                "type": sym_type,
                "start": i,
                "end": i,  # will be extended
            })

    # Determine end of each symbol: next top-level definition or EOF
    for idx, sym in enumerate(symbols):
        if idx + 1 < len(symbols):
            sym["end"] = symbols[idx + 1]["start"]
        else:
            sym["end"] = len(lines)

    return symbols


def _extract_ts_js_symbols(lines: list[str]) -> list[dict]:
    """Extract TypeScript/JavaScript functions, classes, and arrow functions."""
    patterns = [
        (re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
        (re.compile(r"^(?:export\s+)?class\s+(\w+)"), "class"),
        (re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), "function"),
    ]
    symbols: list[dict] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        for pat, sym_type in patterns:
            m = pat.match(stripped)
            if m and _is_top_level_ts(line):
                symbols.append({
                    "name": m.group(1),
                    "type": sym_type,
                    "start": i,
                    "end": i,
                })
                break

    # Determine end of each symbol
    for idx, sym in enumerate(symbols):
        if idx + 1 < len(symbols):
            sym["end"] = symbols[idx + 1]["start"]
        else:
            sym["end"] = len(lines)

    return symbols


def _is_top_level_ts(line: str) -> bool:
    """Check if a line is at the top level (no indentation)."""
    return not line or not line[0].isspace()


def _extract_java_symbols(lines: list[str]) -> list[dict]:
    """Extract Java classes, interfaces, enums, and methods."""
    class_pattern = re.compile(
        r"^(?:public\s+|private\s+|protected\s+)?(?:abstract\s+|static\s+|final\s+)*"
        r"(?:class|interface|enum)\s+(\w+)"
    )
    symbols: list[dict] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        m = class_pattern.match(stripped)
        if m and _is_top_level_java(line):
            symbols.append({
                "name": m.group(1),
                "type": "class",
                "start": i,
                "end": i,
            })

    for idx, sym in enumerate(symbols):
        if idx + 1 < len(symbols):
            sym["end"] = symbols[idx + 1]["start"]
        else:
            sym["end"] = len(lines)

    return symbols


def _is_top_level_java(line: str) -> bool:
    """Check if a Java line is at class level (no or minimal indentation)."""
    return len(line) - len(line.lstrip()) <= 4


def _extract_go_symbols(lines: list[str]) -> list[dict]:
    """Extract Go functions and type declarations."""
    func_pattern = re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)")
    type_pattern = re.compile(r"^type\s+(\w+)\s+(?:struct|interface)")
    symbols: list[dict] = []

    for i, line in enumerate(lines):
        m = func_pattern.match(line)
        if m:
            symbols.append({
                "name": m.group(1),
                "type": "function",
                "start": i,
                "end": i,
            })
            continue
        m = type_pattern.match(line)
        if m:
            symbols.append({
                "name": m.group(1),
                "type": "class",
                "start": i,
                "end": i,
            })

    for idx, sym in enumerate(symbols):
        if idx + 1 < len(symbols):
            sym["end"] = symbols[idx + 1]["start"]
        else:
            sym["end"] = len(lines)

    return symbols


# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------

def _prepend_header(header: str, content: str) -> str:
    """Prepend import header to chunk content if non-empty."""
    if header:
        return header + "\n\n" + content
    return content


def _split_oversized(
    sym_lines: list[str],
    start_offset: int,
    name: str,
    sym_type: str,
    file_path: str,
    language: str,
    import_header: str,
    max_lines: int,
) -> List[CodeChunk]:
    """Split an oversized symbol at blank-line boundaries."""
    chunks: List[CodeChunk] = []
    current: list[str] = []
    current_start = 0

    for i, line in enumerate(sym_lines):
        current.append(line)
        # Split at blank lines once we exceed max_lines
        if len(current) >= max_lines and line.strip() == "":
            chunk_content = "\n".join(current)
            part_num = len(chunks) + 1
            chunks.append(CodeChunk(
                content=_prepend_header(import_header, chunk_content),
                file_path=file_path,
                start_line=start_offset + current_start + 1,
                end_line=start_offset + i + 1,
                symbol_name=f"{name} (part {part_num})",
                symbol_type=sym_type,
                language=language,
                import_header=import_header,
            ))
            current = []
            current_start = i + 1

    # Flush remaining lines
    if current:
        chunk_content = "\n".join(current)
        part_num = len(chunks) + 1
        chunks.append(CodeChunk(
            content=_prepend_header(import_header, chunk_content),
            file_path=file_path,
            start_line=start_offset + current_start + 1,
            end_line=start_offset + len(sym_lines),
            symbol_name=f"{name} (part {part_num})" if len(chunks) > 0 else name,
            symbol_type=sym_type,
            language=language,
            import_header=import_header,
        ))

    return chunks


def _chunk_as_blocks(
    lines: list[str],
    file_path: str,
    language: str,
    import_header: str,
    max_lines: int,
) -> List[CodeChunk]:
    """Chunk lines into fixed-size blocks."""
    chunks: List[CodeChunk] = []
    for start in range(0, len(lines), max_lines):
        end = min(start + max_lines, len(lines))
        block = lines[start:end]
        content = "\n".join(block)
        if not content.strip():
            continue
        chunks.append(CodeChunk(
            content=_prepend_header(import_header, content),
            file_path=file_path,
            start_line=start + 1,
            end_line=end,
            symbol_name="",
            symbol_type="block",
            language=language,
            import_header=import_header,
        ))
    return chunks


def _flush_block(
    block_lines: list[tuple[int, str]],
    chunks: List[CodeChunk],
    file_path: str,
    language: str,
    import_header: str,
    max_lines: int,
) -> None:
    """Flush accumulated uncovered lines as one or more block chunks."""
    if not block_lines:
        return

    content = "\n".join(line for _, line in block_lines)
    if not content.strip():
        return

    start_idx = block_lines[0][0]
    end_idx = block_lines[-1][0]

    if len(block_lines) <= max_lines:
        chunks.append(CodeChunk(
            content=_prepend_header(import_header, content),
            file_path=file_path,
            start_line=start_idx + 1,
            end_line=end_idx + 1,
            symbol_name="",
            symbol_type="block",
            language=language,
            import_header=import_header,
        ))
    else:
        # Split into max_lines-sized sub-blocks
        for i in range(0, len(block_lines), max_lines):
            sub = block_lines[i:i + max_lines]
            sub_content = "\n".join(line for _, line in sub)
            if sub_content.strip():
                chunks.append(CodeChunk(
                    content=_prepend_header(import_header, sub_content),
                    file_path=file_path,
                    start_line=sub[0][0] + 1,
                    end_line=sub[-1][0] + 1,
                    symbol_name="",
                    symbol_type="block",
                    language=language,
                    import_header=import_header,
                ))
