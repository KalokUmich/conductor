"""Code intelligence tool implementations.

Each tool operates within a *workspace_path* sandbox. All file paths
accepted and returned are **relative** to the workspace root.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import json as _json
import subprocess
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schemas import (
    AstMatch,
    BlameEntry,
    CalleeInfo,
    CallerInfo,
    DependencyInfo,
    FileEntry,
    GitCommit,
    GrepMatch,
    ReferenceLocation,
    SymbolLocation,
    TestMatch,
    TestOutlineEntry,
    ToolResult,
)

logger = logging.getLogger(__name__)

# Directories to always exclude from traversal
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "target",
    "dist", "vendor", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".tox", "build", ".next", ".nuxt",
}

_MAX_FILE_SIZE = 512_000  # 500 KB — skip larger files in search/parse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(workspace: str, rel_path: str) -> Path:
    """Resolve a relative path within the workspace, preventing traversal."""
    ws = Path(workspace).resolve()
    target = (ws / rel_path).resolve()
    if not str(target).startswith(str(ws)):
        raise ValueError(f"Path escapes workspace: {rel_path}")
    return target


def _is_excluded(parts: tuple) -> bool:
    """Check if any path component is in the exclude set."""
    return any(p in _EXCLUDED_DIRS for p in parts)


def _run_git(workspace: str, args: List[str], max_output: int = 50_000) -> str:
    """Run a git command inside the workspace."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = proc.stdout
        if len(output) > max_output:
            output = output[:max_output] + "\n... (truncated)"
        return output
    except FileNotFoundError:
        return "(git not found)"
    except subprocess.TimeoutExpired:
        return "(git command timed out)"
    except Exception as exc:
        return f"(git error: {exc})"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def grep(
    workspace: str,
    pattern: str,
    path: Optional[str] = None,
    include_glob: Optional[str] = None,
    max_results: int = 50,
) -> ToolResult:
    """Search for a regex pattern using subprocess grep/rg."""
    search_root = _resolve(workspace, path or ".")
    if not search_root.exists():
        return ToolResult(tool_name="grep", success=False, error=f"Path not found: {path}")

    try:
        re.compile(pattern)
    except re.error as exc:
        return ToolResult(tool_name="grep", success=False, error=f"Invalid regex: {exc}")

    matches: List[Dict] = []
    ws = Path(workspace).resolve()

    if search_root.is_file():
        files_to_search = [search_root]
    else:
        files_to_search = []
        for dirpath, dirnames, filenames in os.walk(search_root):
            rel_dir = Path(dirpath).relative_to(ws)
            if _is_excluded(rel_dir.parts):
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
            for f in filenames:
                fp = Path(dirpath) / f
                if include_glob and not fnmatch.fnmatch(f, include_glob):
                    continue
                if fp.stat().st_size > _MAX_FILE_SIZE:
                    continue
                files_to_search.append(fp)

    compiled = re.compile(pattern)
    for fp in files_to_search:
        if len(matches) >= max_results:
            break
        try:
            text = fp.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if compiled.search(line):
                matches.append(GrepMatch(
                    file_path=str(fp.relative_to(ws)),
                    line_number=i,
                    content=line.rstrip()[:500],
                ).model_dump())
                if len(matches) >= max_results:
                    break

    return ToolResult(
        tool_name="grep",
        data=matches,
        truncated=len(matches) >= max_results,
    )


def read_file(
    workspace: str,
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> ToolResult:
    """Read file contents with optional line range."""
    fp = _resolve(workspace, path)
    if not fp.is_file():
        return ToolResult(tool_name="read_file", success=False, error=f"File not found: {path}")

    try:
        text = fp.read_text(errors="replace")
    except OSError as exc:
        return ToolResult(tool_name="read_file", success=False, error=str(exc))

    lines = text.split("\n")
    total = len(lines)

    if start_line or end_line:
        s = (start_line or 1) - 1
        e = end_line or total
        selected = lines[s:e]
        content = "\n".join(f"{s + i + 1:>4} | {l}" for i, l in enumerate(selected))
        truncated = e < total
    else:
        if total > 500:
            selected = lines[:500]
            content = "\n".join(f"{i + 1:>4} | {l}" for i, l in enumerate(selected))
            truncated = True
        else:
            content = "\n".join(f"{i + 1:>4} | {l}" for i, l in enumerate(lines))
            truncated = False

    ws = Path(workspace).resolve()
    return ToolResult(
        tool_name="read_file",
        data={"path": str(fp.relative_to(ws)), "total_lines": total, "content": content},
        truncated=truncated,
    )


def list_files(
    workspace: str,
    directory: str = ".",
    max_depth: Optional[int] = 3,
    include_glob: Optional[str] = None,
) -> ToolResult:
    """List files and directories."""
    root = _resolve(workspace, directory)
    if not root.is_dir():
        return ToolResult(tool_name="list_files", success=False, error=f"Directory not found: {directory}")

    ws = Path(workspace).resolve()
    entries: List[Dict] = []
    max_entries = 500

    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(ws)
        depth = len(rel.parts) - len(Path(directory).parts) if directory != "." else len(rel.parts)
        if max_depth and depth >= max_depth:
            dirnames.clear()
            continue
        if _is_excluded(rel.parts):
            dirnames.clear()
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)

        for d in dirnames:
            if len(entries) >= max_entries:
                break
            entries.append(FileEntry(path=str(rel / d), is_dir=True).model_dump())

        for f in sorted(filenames):
            if len(entries) >= max_entries:
                break
            if include_glob and not fnmatch.fnmatch(f, include_glob):
                continue
            fp = Path(dirpath) / f
            try:
                size = fp.stat().st_size
            except OSError:
                size = None
            entries.append(FileEntry(
                path=str(rel / f), is_dir=False, size=size,
            ).model_dump())

        if len(entries) >= max_entries:
            break

    return ToolResult(
        tool_name="list_files",
        data=entries,
        truncated=len(entries) >= max_entries,
    )


_symbol_index_cache: Dict[str, tuple] = {}  # workspace → (index, git_head)
_CONDUCTOR_DIR = ".conductor"
_SYMBOL_INDEX_FILE = "symbol_index.json"


def _get_git_head(workspace: str) -> Optional[str]:
    """Return the current HEAD commit hash, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _serialize_definitions(index: Dict[str, list]) -> Dict[str, list]:
    """Convert definition objects to JSON-serializable dicts."""
    out: Dict[str, list] = {}
    for rel, defs in index.items():
        out[rel] = [
            {"name": d.name, "kind": d.kind,
             "start_line": d.start_line, "end_line": d.end_line,
             "signature": getattr(d, "signature", "")}
            for d in defs
        ]
    return out


def _deserialize_definitions(raw: Dict[str, list]) -> Dict[str, list]:
    """Convert JSON dicts back to SimpleNamespace objects (duck-typed)."""
    from types import SimpleNamespace
    out: Dict[str, list] = {}
    for rel, defs in raw.items():
        out[rel] = [SimpleNamespace(**d) for d in defs]
    return out


def _disk_cache_path(workspace: str) -> Path:
    return Path(workspace) / _CONDUCTOR_DIR / _SYMBOL_INDEX_FILE


def _load_disk_cache(workspace: str) -> Optional[tuple]:
    """Load symbol index from disk. Returns (index, git_head) or None."""
    path = _disk_cache_path(workspace)
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        head = data.get("git_head")
        raw_index = data.get("index")
        if head and raw_index is not None:
            return _deserialize_definitions(raw_index), head
    except (OSError, _json.JSONDecodeError, KeyError):
        pass
    return None


def _save_disk_cache(workspace: str, index: Dict[str, list], git_head: str) -> None:
    """Persist symbol index to disk."""
    path = _disk_cache_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"git_head": git_head, "index": _serialize_definitions(index)}
        path.write_text(_json.dumps(payload), encoding="utf-8")
    except OSError:
        pass  # non-fatal — in-memory cache still works


def _get_symbol_index(workspace: str) -> Optional[Dict[str, list]]:
    """Build or return a cached symbol index for the workspace.

    The index maps relative file paths to their list of symbol definitions.
    Cache invalidation is based on the git HEAD commit — the index is
    rebuilt only when the commit changes. Three tiers:

    1. **In-memory** — instant, keyed by (workspace, git_head).
    2. **Disk** — ``{workspace}/.conductor/symbol_index.json``, survives
       server restarts.
    3. **Full scan** — AST walk, written to memory + disk.
    """
    try:
        from app.repo_graph.parser import extract_definitions, detect_language
    except ImportError:
        return None

    current_head = _get_git_head(workspace)

    # 1. In-memory hit
    entry = _symbol_index_cache.get(workspace)
    if entry is not None:
        index, cached_head = entry
        if cached_head == current_head:
            return index

    # 2. Disk hit
    if current_head is not None:
        disk_entry = _load_disk_cache(workspace)
        if disk_entry is not None:
            disk_index, disk_head = disk_entry
            if disk_head == current_head:
                _symbol_index_cache[workspace] = (disk_index, current_head)
                return disk_index

    # 3. Full scan
    ws = Path(workspace).resolve()
    index: Dict[str, list] = {}

    for dirpath, dirnames, filenames in os.walk(ws):
        rel_dir = Path(dirpath).relative_to(ws)
        if _is_excluded(rel_dir.parts):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for f in filenames:
            fp = Path(dirpath) / f
            if detect_language(str(fp)) is None:
                continue
            if fp.stat().st_size > _MAX_FILE_SIZE:
                continue
            try:
                source = fp.read_bytes()
            except OSError:
                continue
            rel = str(fp.relative_to(ws))
            symbols = extract_definitions(str(fp), source)
            if symbols.definitions:
                index[rel] = symbols.definitions

    cache_head = current_head or ""
    _symbol_index_cache[workspace] = (index, cache_head)
    if current_head:
        _save_disk_cache(workspace, index, current_head)
    return index


def invalidate_symbol_cache(workspace: Optional[str] = None) -> None:
    """Clear symbol index cache (in-memory + disk)."""
    if workspace:
        _symbol_index_cache.pop(workspace, None)
        try:
            _disk_cache_path(workspace).unlink(missing_ok=True)
        except OSError:
            pass
    else:
        for ws in list(_symbol_index_cache.keys()):
            try:
                _disk_cache_path(ws).unlink(missing_ok=True)
            except OSError:
                pass
        _symbol_index_cache.clear()


def find_symbol(
    workspace: str,
    name: str,
    kind: Optional[str] = None,
    _graph_cache: Optional[Dict] = None,
) -> ToolResult:
    """Find symbol definitions using a cached symbol index."""
    index = _get_symbol_index(workspace)
    if index is None:
        return ToolResult(
            tool_name="find_symbol", success=False,
            error="Symbol index not available (missing tree-sitter).",
        )

    results: List[Dict] = []
    name_lower = name.lower()

    for rel, definitions in index.items():
        for defn in definitions:
            if name_lower not in defn.name.lower():
                continue
            if kind and defn.kind != kind:
                continue
            results.append(SymbolLocation(
                name=defn.name,
                kind=defn.kind,
                file_path=rel,
                start_line=defn.start_line,
                end_line=defn.end_line,
                signature=defn.signature,
            ).model_dump())

    return ToolResult(tool_name="find_symbol", data=results)


def find_references(
    workspace: str,
    symbol_name: str,
    file: Optional[str] = None,
) -> ToolResult:
    """Find references to a symbol via grep + AST validation."""
    # First do a grep for the symbol name
    grep_result = grep(
        workspace=workspace,
        pattern=rf"\b{re.escape(symbol_name)}\b",
        path=file,
        max_results=100,
    )
    if not grep_result.success:
        return ToolResult(tool_name="find_references", success=False, error=grep_result.error)

    # Filter grep hits through AST reference data for files that support it
    from app.repo_graph.parser import extract_definitions, detect_language

    ws = Path(workspace).resolve()
    matches = grep_result.data or []
    validated: List[Dict] = []

    # Group by file to avoid re-parsing
    by_file: Dict[str, List[Dict]] = {}
    for m in matches:
        by_file.setdefault(m["file_path"], []).append(m)

    for fpath, file_matches in by_file.items():
        fp = ws / fpath
        if detect_language(str(fp)) is not None and fp.stat().st_size <= _MAX_FILE_SIZE:
            try:
                symbols = extract_definitions(str(fp), fp.read_bytes())
                ref_lines = {r.line for r in symbols.references if r.name == symbol_name}
                for m in file_matches:
                    if m["line_number"] in ref_lines:
                        validated.append(ReferenceLocation(
                            file_path=m["file_path"],
                            line_number=m["line_number"],
                            content=m["content"],
                        ).model_dump())
            except Exception:
                # Fall back to grep matches for this file
                for m in file_matches:
                    validated.append(ReferenceLocation(
                        file_path=m["file_path"],
                        line_number=m["line_number"],
                        content=m["content"],
                    ).model_dump())
        else:
            # Non-parseable files: keep grep matches as-is
            for m in file_matches:
                validated.append(ReferenceLocation(
                    file_path=m["file_path"],
                    line_number=m["line_number"],
                    content=m["content"],
                ).model_dump())

    return ToolResult(tool_name="find_references", data=validated)


def file_outline(workspace: str, path: str) -> ToolResult:
    """Get all definitions in a file."""
    from app.repo_graph.parser import extract_definitions

    fp = _resolve(workspace, path)
    if not fp.is_file():
        return ToolResult(tool_name="file_outline", success=False, error=f"File not found: {path}")

    try:
        source = fp.read_bytes()
    except OSError as exc:
        return ToolResult(tool_name="file_outline", success=False, error=str(exc))

    symbols = extract_definitions(str(fp), source)
    ws = Path(workspace).resolve()
    defs = [
        SymbolLocation(
            name=d.name,
            kind=d.kind,
            file_path=str(fp.relative_to(ws)),
            start_line=d.start_line,
            end_line=d.end_line,
            signature=d.signature,
        ).model_dump()
        for d in symbols.definitions
    ]
    return ToolResult(tool_name="file_outline", data=defs)


def get_dependencies(
    workspace: str,
    file_path: str,
    _graph_service=None,
) -> ToolResult:
    """Find files that this file depends on (out-edges in the dependency graph)."""
    graph = _ensure_graph(workspace, _graph_service)
    if graph is None:
        return ToolResult(
            tool_name="get_dependencies", success=False,
            error="Dependency graph not available (missing networkx or tree-sitter).",
        )

    deps: List[Dict] = []
    for edge in graph.edges:
        if edge.source == file_path:
            deps.append(DependencyInfo(
                file_path=edge.target,
                symbols=edge.symbols,
                weight=edge.weight,
            ).model_dump())

    deps.sort(key=lambda d: d["weight"], reverse=True)
    return ToolResult(tool_name="get_dependencies", data=deps)


def get_dependents(
    workspace: str,
    file_path: str,
    _graph_service=None,
) -> ToolResult:
    """Find files that depend on this file (in-edges in the dependency graph)."""
    graph = _ensure_graph(workspace, _graph_service)
    if graph is None:
        return ToolResult(
            tool_name="get_dependents", success=False,
            error="Dependency graph not available (missing networkx or tree-sitter).",
        )

    deps: List[Dict] = []
    for edge in graph.edges:
        if edge.target == file_path:
            deps.append(DependencyInfo(
                file_path=edge.source,
                symbols=edge.symbols,
                weight=edge.weight,
            ).model_dump())

    deps.sort(key=lambda d: d["weight"], reverse=True)
    return ToolResult(tool_name="get_dependents", data=deps)


def git_log(
    workspace: str,
    file: Optional[str] = None,
    n: int = 10,
) -> ToolResult:
    """Show recent git commits."""
    args = ["log", f"-{n}", "--format=%H|%s|%an|%ai"]
    if file:
        fp = _resolve(workspace, file)
        args += ["--", str(fp)]

    raw = _run_git(workspace, args)
    commits: List[Dict] = []
    for line in raw.strip().split("\n"):
        if not line or line.startswith("("):
            continue
        parts = line.split("|", 3)
        if len(parts) >= 2:
            commits.append(GitCommit(
                hash=parts[0][:8],
                message=parts[1],
                author=parts[2] if len(parts) > 2 else "",
                date=parts[3] if len(parts) > 3 else "",
            ).model_dump())

    return ToolResult(tool_name="git_log", data=commits)


def git_diff(
    workspace: str,
    ref1: Optional[str] = "HEAD~1",
    ref2: Optional[str] = "HEAD",
    file: Optional[str] = None,
) -> ToolResult:
    """Show diff between two git refs."""
    args = ["diff", ref1 or "HEAD~1", ref2 or "HEAD"]
    if file:
        fp = _resolve(workspace, file)
        args += ["--", str(fp)]

    raw = _run_git(workspace, args, max_output=100_000)
    return ToolResult(tool_name="git_diff", data={"diff": raw})


def _find_ast_grep() -> Optional[str]:
    """Locate the ast-grep binary, checking PATH and the venv bin dir."""
    import shutil
    import sys

    found = shutil.which("ast-grep")
    if found:
        return found
    # Check alongside the running Python executable (common in venvs)
    venv_bin = Path(sys.executable).parent / "ast-grep"
    if venv_bin.is_file():
        return str(venv_bin)
    return None


def ast_search(
    workspace: str,
    pattern: str,
    language: Optional[str] = None,
    path: Optional[str] = None,
    max_results: int = 30,
) -> ToolResult:
    """Structural AST search using ast-grep."""
    import json as _json

    ast_grep_bin = _find_ast_grep()
    if ast_grep_bin is None:
        return ToolResult(
            tool_name="ast_search", success=False,
            error="ast-grep not installed. Install with: pip install ast-grep-cli",
        )

    search_root = _resolve(workspace, path or ".")
    if not search_root.exists():
        return ToolResult(tool_name="ast_search", success=False, error=f"Path not found: {path}")

    cmd = [ast_grep_bin, "run", "-p", pattern, "--json"]
    if language:
        cmd += ["-l", language]
    cmd.append(str(search_root))

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=workspace,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(tool_name="ast_search", success=False, error="ast-grep timed out (30s)")

    if proc.returncode not in (0, 1):  # 1 = no matches
        return ToolResult(
            tool_name="ast_search", success=False,
            error=f"ast-grep error: {proc.stderr.strip()[:500]}",
        )

    try:
        raw_matches = _json.loads(proc.stdout) if proc.stdout.strip() else []
    except _json.JSONDecodeError:
        return ToolResult(tool_name="ast_search", success=False, error="Failed to parse ast-grep output")

    ws = Path(workspace).resolve()
    results: List[Dict] = []
    for m in raw_matches[:max_results]:
        file_abs = Path(m.get("file", ""))
        try:
            rel = str(file_abs.relative_to(ws))
        except ValueError:
            rel = str(file_abs)

        # Skip excluded dirs
        if _is_excluded(Path(rel).parts):
            continue

        meta = {}
        single = m.get("metaVariables", {}).get("single", {})
        for var_name, var_data in single.items():
            if isinstance(var_data, dict):
                meta[f"${var_name}"] = var_data.get("text", "")

        rng = m.get("range", {})
        start = rng.get("start", {})
        end = rng.get("end", {})

        text = m.get("text", m.get("lines", ""))
        if len(text) > 1000:
            text = text[:997] + "..."

        results.append(AstMatch(
            file_path=rel,
            start_line=start.get("line", 0) + 1,
            end_line=end.get("line", 0) + 1,
            text=text,
            meta_variables=meta,
        ).model_dump())

    return ToolResult(
        tool_name="ast_search",
        data=results,
        truncated=len(raw_matches) > max_results,
    )


def get_callees(
    workspace: str,
    function_name: str,
    file: str,
) -> ToolResult:
    """Find all functions/methods called within a specific function body."""
    from app.repo_graph.parser import extract_definitions, detect_language

    fp = _resolve(workspace, file)
    if not fp.is_file():
        return ToolResult(tool_name="get_callees", success=False, error=f"File not found: {file}")

    lang = detect_language(str(fp))
    if lang is None:
        return ToolResult(tool_name="get_callees", success=False, error=f"Unsupported language: {file}")

    try:
        source = fp.read_text(errors="replace")
    except OSError as exc:
        return ToolResult(tool_name="get_callees", success=False, error=str(exc))

    # Find the function's line range from AST
    symbols = extract_definitions(str(fp), fp.read_bytes())
    target_def = None
    for d in symbols.definitions:
        if d.name == function_name:
            target_def = d
            break

    if target_def is None:
        return ToolResult(
            tool_name="get_callees", success=False,
            error=f"Function '{function_name}' not found in {file}",
        )

    lines = source.split("\n")

    # When the regex fallback is used, end_line == start_line. In that case
    # infer the end by looking for the next top-level definition or EOF.
    end_line = target_def.end_line
    if end_line <= target_def.start_line:
        next_starts = sorted(
            d.start_line for d in symbols.definitions
            if d.start_line > target_def.start_line
        )
        end_line = (next_starts[0] - 1) if next_starts else len(lines)

    # Extract lines of the function body
    body_lines = lines[target_def.start_line - 1 : end_line]

    # Find function calls in the body using regex
    # Matches: name(...), obj.name(...), but not def name(... or class name(
    call_pattern = re.compile(r'(?<!\bdef\s)(?<!\bclass\s)\b([a-zA-Z_]\w*)\s*\(')
    ws = Path(workspace).resolve()

    seen: set = set()
    callees: List[Dict] = []
    for offset, line in enumerate(body_lines):
        line_no = target_def.start_line + offset
        for match in call_pattern.finditer(line):
            callee_name = match.group(1)
            # Skip Python keywords and builtins that look like calls
            if callee_name in _CALL_NOISE:
                continue
            if callee_name not in seen:
                seen.add(callee_name)
                callees.append(CalleeInfo(
                    callee_name=callee_name,
                    file_path=str(fp.relative_to(ws)),
                    line=line_no,
                ).model_dump())

    return ToolResult(tool_name="get_callees", data=callees)


def get_callers(
    workspace: str,
    function_name: str,
    path: Optional[str] = None,
) -> ToolResult:
    """Find all functions/methods that call a given function."""
    from app.repo_graph.parser import extract_definitions, detect_language

    ws = Path(workspace).resolve()
    search_root = _resolve(workspace, path or ".")
    if not search_root.exists():
        return ToolResult(tool_name="get_callers", success=False, error=f"Path not found: {path}")

    # Regex: function_name followed by ( — a call site
    call_re = re.compile(rf'\b{re.escape(function_name)}\s*\(')

    callers: List[Dict] = []
    for dirpath, dirnames, filenames in os.walk(search_root):
        rel_dir = Path(dirpath).relative_to(ws)
        if _is_excluded(rel_dir.parts):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for f in filenames:
            fp = Path(dirpath) / f
            if detect_language(str(fp)) is None:
                continue
            if fp.stat().st_size > _MAX_FILE_SIZE:
                continue

            try:
                source = fp.read_text(errors="replace")
            except OSError:
                continue

            # Quick check: does the file contain a call?
            if not call_re.search(source):
                continue

            rel = str(fp.relative_to(ws))
            symbols = extract_definitions(str(fp), fp.read_bytes())
            lines = source.split("\n")

            for defn in symbols.definitions:
                if defn.kind not in ("function", "method"):
                    continue
                # Infer end_line when regex fallback sets it == start_line
                end_ln = defn.end_line
                if end_ln <= defn.start_line:
                    next_starts = sorted(
                        d.start_line for d in symbols.definitions
                        if d.start_line > defn.start_line
                    )
                    end_ln = (next_starts[0] - 1) if next_starts else len(lines)
                # Skip the definition line itself (def foo(): matches \bfoo\s*\()
                body_lines = lines[defn.start_line : end_ln]
                for offset, line in enumerate(body_lines):
                    if call_re.search(line):
                        callers.append(CallerInfo(
                            caller_name=defn.name,
                            caller_kind=defn.kind,
                            file_path=rel,
                            line=defn.start_line + 1 + offset,
                            content=line.strip()[:200],
                        ).model_dump())
                        break  # one match per caller is enough

    return ToolResult(tool_name="get_callers", data=callers)


# Noise words to skip when extracting callees
_CALL_NOISE = frozenset({
    "if", "for", "while", "return", "print", "len", "str", "int", "float",
    "bool", "list", "dict", "set", "tuple", "type", "isinstance", "issubclass",
    "range", "enumerate", "zip", "map", "filter", "sorted", "reversed",
    "super", "property", "staticmethod", "classmethod", "getattr", "setattr",
    "hasattr", "delattr", "open", "repr", "hash", "id", "input", "abs",
    "min", "max", "sum", "round", "any", "all", "next", "iter",
})


# ---------------------------------------------------------------------------
# Graph helper
# ---------------------------------------------------------------------------

_GRAPH_TTL_SECONDS = 120  # rebuild graph after 2 minutes

_graph_cache: Dict[str, tuple] = {}  # workspace → (graph, monotonic_time)


def _ensure_graph(workspace: str, graph_service=None):
    """Build or return a cached dependency graph for the workspace."""
    import time

    entry = _graph_cache.get(workspace)
    if entry is not None:
        graph, ts = entry
        if (time.monotonic() - ts) < _GRAPH_TTL_SECONDS:
            return graph
        # expired — fall through to rebuild

    graph = None
    if graph_service is not None:
        graph = graph_service.build_graph(workspace)
    else:
        try:
            from app.repo_graph.graph import build_dependency_graph
            graph = build_dependency_graph(workspace)
        except ImportError:
            logger.warning("repo_graph not available — graph tools disabled.")
            return None

    _graph_cache[workspace] = (graph, time.monotonic())
    return graph


def invalidate_graph_cache(workspace: Optional[str] = None) -> None:
    """Clear graph cache (call after file changes)."""
    if workspace:
        _graph_cache.pop(workspace, None)
    else:
        _graph_cache.clear()


# ---------------------------------------------------------------------------
# Git semantic tools
# ---------------------------------------------------------------------------


def git_blame(
    workspace: str,
    file: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> ToolResult:
    """Run git blame on a file, returning structured per-line authorship data."""
    fp = _resolve(workspace, file)
    if not fp.is_file():
        return ToolResult(tool_name="git_blame", success=False, error=f"File not found: {file}")

    ws = Path(workspace).resolve()
    rel = str(fp.relative_to(ws))

    args = ["blame", "--line-porcelain"]
    if start_line and end_line:
        args.append(f"-L{start_line},{end_line}")
    elif start_line:
        args.append(f"-L{start_line},")
    args += ["--", rel]

    raw = _run_git(workspace, args, max_output=200_000)
    if raw.startswith("("):
        return ToolResult(tool_name="git_blame", success=False, error=raw)

    entries = _parse_blame_porcelain(raw)
    truncated = len(entries) > 200
    if truncated:
        entries = entries[:200]

    return ToolResult(tool_name="git_blame", data=entries, truncated=truncated)


def _parse_blame_porcelain(raw: str) -> List[Dict]:
    """Parse git blame --line-porcelain output into structured entries."""
    import datetime as _dt

    entries: List[Dict] = []
    cur: Dict[str, Any] = {}
    final_line = 0

    for line in raw.split("\n"):
        if not line:
            continue
        if line.startswith("\t"):
            # Content line — end of this block
            cur["content"] = line[1:]
            cur["line_number"] = final_line
            cur.setdefault("commit_hash", "?")
            cur.setdefault("author", "?")
            cur.setdefault("date", "?")
            entries.append(cur)
            cur = {}
        elif re.match(r"^[0-9a-f]{40}\s", line):
            parts = line.split()
            cur["commit_hash"] = parts[0][:8]
            final_line = int(parts[2]) if len(parts) >= 3 else 0
        elif line.startswith("author "):
            cur["author"] = line[7:]
        elif line.startswith("author-time "):
            try:
                ts = int(line[12:])
                cur["date"] = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError):
                cur["date"] = line[12:]
        # Skip other metadata lines (committer, summary, filename, etc.)

    return entries


def git_show(
    workspace: str,
    commit: str,
    file: Optional[str] = None,
) -> ToolResult:
    """Show full details of a git commit: message, author, date, diff."""
    if not re.match(r"^[a-zA-Z0-9_.^~/-]+$", commit):
        return ToolResult(tool_name="git_show", success=False, error=f"Invalid commit ref: {commit}")

    # Get metadata separately from diff for reliable parsing
    fmt = "%H%n%an%n%ai%n%B"
    meta_raw = _run_git(workspace, ["log", "-1", f"--format={fmt}", commit])
    if meta_raw.startswith("("):
        return ToolResult(tool_name="git_show", success=False, error=meta_raw)

    meta_lines = meta_raw.strip().split("\n")
    commit_hash = meta_lines[0] if meta_lines else ""
    author = meta_lines[1] if len(meta_lines) > 1 else ""
    date = meta_lines[2] if len(meta_lines) > 2 else ""
    message = "\n".join(meta_lines[3:]).strip() if len(meta_lines) > 3 else ""

    # Get the diff (--root handles the initial commit which has no parent)
    diff_args = ["diff-tree", "--root", "-p", commit]
    if file:
        fp = _resolve(workspace, file)
        diff_args += ["--", str(fp)]
    diff_raw = _run_git(workspace, diff_args, max_output=100_000)

    return ToolResult(tool_name="git_show", data={
        "commit_hash": commit_hash[:8],
        "author": author,
        "date": date,
        "message": message,
        "diff": diff_raw,
    })


# ---------------------------------------------------------------------------
# Test association tools
# ---------------------------------------------------------------------------

_TEST_FILE_PATTERNS = [
    re.compile(r"^test_.*\.py$"),
    re.compile(r"^.*_test\.py$"),
    re.compile(r"^.*\.test\.[jt]sx?$"),
    re.compile(r"^.*\.spec\.[jt]sx?$"),
    re.compile(r"^.*_test\.go$"),
]


def _is_test_file(filename: str) -> bool:
    return any(p.match(filename) for p in _TEST_FILE_PATTERNS)


# Regex patterns for finding test function definitions per language
_PY_TEST_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(test_\w+)\s*\(")
_PY_CLASS_DEF = re.compile(r"^(\s*)class\s+(Test\w+)")
_JS_TEST_BLOCK = re.compile(r"(test|it)\s*\(\s*['\"`](.+?)['\"`]")
_GO_TEST_FUNC = re.compile(r"^func\s+(Test\w+|Benchmark\w+)\s*\(")


def _find_enclosing_test(
    lines: List[str],
    match_line: int,
    lang: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Walk backward from match_line (1-based) to find enclosing test function."""
    idx = match_line - 1  # to 0-based

    if lang in ("javascript", "typescript"):
        for i in range(idx, -1, -1):
            m = _JS_TEST_BLOCK.search(lines[i])
            if m:
                return {"name": m.group(2), "line": i + 1}
        return None

    if lang == "go":
        for i in range(idx, -1, -1):
            m = _GO_TEST_FUNC.match(lines[i])
            if m:
                return {"name": m.group(1), "line": i + 1}
        return None

    # Default: Python
    for i in range(idx, -1, -1):
        m = _PY_TEST_DEF.match(lines[i])
        if m:
            func_name = m.group(2)
            indent_len = len(m.group(1))
            # Check if inside a test class
            if indent_len > 0:
                for j in range(i - 1, -1, -1):
                    cm = _PY_CLASS_DEF.match(lines[j])
                    if cm and len(cm.group(1)) < indent_len:
                        func_name = f"{cm.group(2)}::{func_name}"
                        break
            return {"name": func_name, "line": i + 1}

    return None


def find_tests(
    workspace: str,
    name: str,
    path: Optional[str] = None,
) -> ToolResult:
    """Find test functions that test a given function or class."""
    from app.repo_graph.parser import detect_language

    ws = Path(workspace).resolve()
    search_root = _resolve(workspace, path or ".")

    name_re = re.compile(rf"\b{re.escape(name)}\b")
    seen: set = set()
    results: List[Dict] = []

    for dirpath, dirnames, filenames in os.walk(search_root):
        rel_dir = Path(dirpath).relative_to(ws)
        if _is_excluded(rel_dir.parts):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for f in filenames:
            is_test_dir = "__tests__" in rel_dir.parts or "tests" in rel_dir.parts
            if not _is_test_file(f) and not is_test_dir:
                continue
            fp = Path(dirpath) / f
            if fp.stat().st_size > _MAX_FILE_SIZE:
                continue

            try:
                source = fp.read_text(errors="replace")
            except OSError:
                continue

            if not name_re.search(source):
                continue

            lines = source.split("\n")
            rel = str(fp.relative_to(ws))
            lang = detect_language(str(fp))

            for line_no, line_text in enumerate(lines, 1):
                if not name_re.search(line_text):
                    continue
                test_fn = _find_enclosing_test(lines, line_no, lang)
                if test_fn:
                    key = (rel, test_fn["name"])
                    if key not in seen:
                        seen.add(key)
                        results.append(TestMatch(
                            test_file=rel,
                            test_function=test_fn["name"],
                            line_number=test_fn["line"],
                            context=line_text.strip()[:200],
                        ).model_dump())

            if len(results) >= 50:
                break
        if len(results) >= 50:
            break

    return ToolResult(
        tool_name="find_tests",
        data=results,
        truncated=len(results) >= 50,
    )


# Mock / assertion patterns for test_outline
_PY_MOCK_RE = [
    re.compile(r'@(?:mock\.)?patch\([\'"](.+?)[\'"]\s*[\),]'),
    re.compile(r'@(?:mock\.)?patch\.object\(\s*(\w+\s*,\s*[\'"]?\w+)'),
    re.compile(r"mocker\.(?:patch|spy)\(['\"](.+?)['\"]\)"),
    re.compile(r"monkeypatch\.setattr\((.+?),"),
    re.compile(r"(\w+)\s*=\s*(?:Mock|MagicMock|AsyncMock)\("),
]
_PY_ASSERT_RE = re.compile(
    r"(assert\s+.{0,80}|self\.assert\w+\(.{0,60}|pytest\.raises\(.{0,60}\))"
)
_PY_FIXTURE_RE = re.compile(r"def\s+test_\w+\(([^)]*)\)")

_JS_MOCK_RE = [
    re.compile(r"jest\.(?:fn|mock|spyOn)\((.{0,60}?)\)"),
    re.compile(r"vi\.(?:fn|mock|spyOn)\((.{0,60}?)\)"),
    re.compile(r"sinon\.(?:stub|spy|mock)\((.{0,60}?)\)"),
]
_JS_ASSERT_RE = re.compile(r"(expect\(.{0,60}\)[\s\S]{0,5}\.[\w.]+\(.{0,60}?\))")


def outline_tests(
    workspace: str,
    path: str,
) -> ToolResult:
    """Get detailed test-aware structure of a test file."""
    from app.repo_graph.parser import detect_language

    fp = _resolve(workspace, path)
    if not fp.is_file():
        return ToolResult(tool_name="test_outline", success=False, error=f"File not found: {path}")

    try:
        source = fp.read_text(errors="replace")
    except OSError as exc:
        return ToolResult(tool_name="test_outline", success=False, error=str(exc))

    lines = source.split("\n")
    lang = detect_language(str(fp))

    if lang in ("javascript", "typescript"):
        entries = _js_test_outline(lines)
    else:
        entries = _py_test_outline(lines)

    return ToolResult(tool_name="test_outline", data=[e.model_dump() for e in entries])


def _py_test_outline(lines: List[str]) -> List[TestOutlineEntry]:
    """Parse Python test file for classes, functions, mocks, assertions, fixtures."""
    entries: List[TestOutlineEntry] = []

    # First pass: find test classes and test functions with their line ranges
    defs: List[Dict[str, Any]] = []
    for i, line in enumerate(lines):
        cm = _PY_CLASS_DEF.match(line)
        if cm:
            defs.append({"name": cm.group(2), "kind": "test_class",
                         "line": i + 1, "indent": len(cm.group(1))})
            continue
        fm = _PY_TEST_DEF.match(line)
        if fm:
            defs.append({"name": fm.group(2), "kind": "test_function",
                         "line": i + 1, "indent": len(fm.group(1))})

    # Compute end_line for each def (next def at same/lesser indent, or EOF)
    for idx, d in enumerate(defs):
        end = len(lines)
        for nxt in defs[idx + 1:]:
            if nxt["indent"] <= d["indent"]:
                end = nxt["line"] - 1
                break
        d["end_line"] = end

    for d in defs:
        # Include decorator lines above the def (walk backward from def line)
        decorator_start = d["line"] - 1  # 0-based index of def line
        for k in range(d["line"] - 2, -1, -1):
            stripped = lines[k].strip()
            if stripped.startswith("@"):
                decorator_start = k
            elif stripped == "" or stripped.startswith("#"):
                continue  # skip blank/comment lines between decorators
            else:
                break

        body = lines[decorator_start : d["end_line"]]
        body_text = "\n".join(body)

        # Extract mocks
        mocks: List[str] = []
        for pattern in _PY_MOCK_RE:
            for m in pattern.finditer(body_text):
                mocks.append(m.group(1).strip()[:80])

        # Extract assertions
        assertions: List[str] = []
        for m in _PY_ASSERT_RE.finditer(body_text):
            assertions.append(m.group(1).strip()[:80])

        # Extract fixtures (function params minus 'self')
        fixtures: List[str] = []
        if d["kind"] == "test_function":
            # Find the def line within the body (may be preceded by decorators)
            def_line = lines[d["line"] - 1] if d["line"] - 1 < len(lines) else ""
            fm = _PY_FIXTURE_RE.search(def_line)
            if fm and fm.group(1):
                for p in fm.group(1).split(","):
                    p = p.strip().split(":")[0].split("=")[0].strip()
                    if p and p != "self":
                        fixtures.append(p)

        # Prefix class name for methods
        name = d["name"]
        if d["kind"] == "test_function" and d["indent"] > 0:
            for prev in reversed(defs):
                if prev["kind"] == "test_class" and prev["indent"] < d["indent"]:
                    name = f"{prev['name']}::{d['name']}"
                    break

        entries.append(TestOutlineEntry(
            name=name,
            kind=d["kind"],
            line_number=d["line"],
            end_line=d["end_line"],
            mocks=mocks[:10],
            assertions=assertions[:10],
            fixtures=fixtures,
        ))

    return entries


def _js_test_outline(lines: List[str]) -> List[TestOutlineEntry]:
    """Parse JS/TS test file for describe/it/test blocks, mocks, assertions."""
    entries: List[TestOutlineEntry] = []
    describe_re = re.compile(r"(describe)\s*\(\s*['\"`](.+?)['\"`]")
    test_re = re.compile(r"(test|it)\s*\(\s*['\"`](.+?)['\"`]")

    # Track nesting via brace counting
    describe_stack: List[str] = []
    brace_depth = 0
    describe_depths: List[int] = []

    for i, line in enumerate(lines):
        # Track braces
        brace_depth += line.count("{") - line.count("}")

        # Pop describe stack if we've exited
        while describe_depths and brace_depth <= describe_depths[-1]:
            describe_stack.pop()
            describe_depths.pop()

        dm = describe_re.search(line)
        if dm:
            desc_name = dm.group(2)
            full_name = " > ".join(describe_stack + [desc_name]) if describe_stack else desc_name
            entries.append(TestOutlineEntry(
                name=full_name, kind="describe_block", line_number=i + 1,
            ))
            describe_stack.append(desc_name)
            describe_depths.append(brace_depth - 1)
            continue

        tm = test_re.search(line)
        if tm:
            test_name = tm.group(2)
            full_name = " > ".join(describe_stack + [test_name]) if describe_stack else test_name

            # Scan ahead for mocks and assertions in this test body
            mocks: List[str] = []
            assertions: List[str] = []
            inner_brace = 0
            started = False
            for j in range(i, min(i + 100, len(lines))):
                tl = lines[j]
                inner_brace += tl.count("{") - tl.count("}")
                if "{" in tl:
                    started = True
                if started and inner_brace <= 0:
                    break
                for mp in _JS_MOCK_RE:
                    for mm in mp.finditer(tl):
                        mocks.append(mm.group(1).strip()[:60])
                am = _JS_ASSERT_RE.search(tl)
                if am:
                    assertions.append(am.group(1).strip()[:80])

            entries.append(TestOutlineEntry(
                name=full_name, kind="test_function",
                line_number=i + 1,
                mocks=mocks[:10], assertions=assertions[:10],
            ))

    return entries


# ---------------------------------------------------------------------------
# Data flow tracing
# ---------------------------------------------------------------------------


def trace_variable(
    workspace: str,
    variable_name: str,
    file: str,
    function_name: Optional[str] = None,
    direction: str = "forward",
) -> ToolResult:
    """Trace a variable's data flow through function calls.

    Forward: find aliases, outgoing call sites (with argument → parameter
    mapping), and sink patterns (ORM, SQL, HTTP, return).

    Backward: find where the value originates — callers that pass this
    parameter, plus source patterns (HTTP request, config, DB result).
    """
    from app.repo_graph.parser import extract_definitions, detect_language

    fp = _resolve(workspace, file)
    if not fp.is_file():
        return ToolResult(tool_name="trace_variable", success=False,
                          error=f"File not found: {file}")

    lang = detect_language(str(fp))
    if lang is None:
        return ToolResult(tool_name="trace_variable", success=False,
                          error=f"Unsupported language: {file}")

    try:
        source = fp.read_text(errors="replace")
    except OSError as exc:
        return ToolResult(tool_name="trace_variable", success=False, error=str(exc))

    lines = source.split("\n")
    symbols = extract_definitions(str(fp), fp.read_bytes())

    # Resolve the target function
    target_def = None
    if function_name:
        for d in symbols.definitions:
            if d.name == function_name:
                target_def = d
                break
        if target_def is None:
            return ToolResult(
                tool_name="trace_variable", success=False,
                error=f"Function '{function_name}' not found in {file}",
            )
    else:
        # Auto-detect: first function/method whose body contains the variable
        for d in symbols.definitions:
            if d.kind not in ("function", "method"):
                continue
            end = _infer_end_line(d, symbols.definitions, len(lines))
            body = "\n".join(lines[d.start_line - 1 : end])
            if re.search(rf"\b{re.escape(variable_name)}\b", body):
                target_def = d
                break
        if target_def is None:
            return ToolResult(
                tool_name="trace_variable", success=False,
                error=f"No function in {file} references '{variable_name}'",
            )

    # Get function body line range
    start_line = target_def.start_line
    end_line = _infer_end_line(target_def, symbols.definitions, len(lines))
    body_lines = lines[start_line - 1 : end_line]

    ws = Path(workspace).resolve()

    # Find aliases of the variable within this function
    aliases = _find_aliases(body_lines, start_line, variable_name)
    all_names = {variable_name} | {a["name"] for a in aliases}

    result: Dict[str, Any] = {
        "variable": variable_name,
        "file": str(fp.relative_to(ws)),
        "function": target_def.name,
        "direction": direction,
        "aliases": aliases,
        "flows_to": [],
        "sinks": [],
        "flows_from": [],
        "sources": [],
    }

    if direction == "forward":
        result["flows_to"] = _find_forward_flows(
            workspace, body_lines, start_line, all_names, symbols,
        )
        result["sinks"] = _detect_sinks(body_lines, start_line, all_names)
    else:
        # Backward: determine parameter position, find callers
        param_pos = _get_param_position(target_def, variable_name, lines)
        if param_pos is not None:
            result["flows_from"] = _find_backward_flows(
                workspace, target_def.name, param_pos,
            )
        result["sources"] = _detect_sources(body_lines, start_line, variable_name)

    return ToolResult(tool_name="trace_variable", data=result)


def _infer_end_line(defn, all_defs, total_lines: int) -> int:
    """Infer a function's end line when the parser only gives start_line."""
    if defn.end_line > defn.start_line:
        return defn.end_line
    next_starts = sorted(
        d.start_line for d in all_defs if d.start_line > defn.start_line
    )
    return (next_starts[0] - 1) if next_starts else total_lines


# -- Alias detection --------------------------------------------------------

def _find_aliases(
    body_lines: List[str], start_line: int, variable: str,
) -> List[Dict[str, Any]]:
    """Find variable aliases within a function body.

    Detects patterns like ``x = variable``, ``x = variable.attr``,
    ``x: Type = variable`` (Python type annotation), and transitive
    aliases (``y = x`` when x is already an alias).
    """
    aliases: List[Dict[str, Any]] = []
    known: set = {variable}

    # Multiple passes to catch transitive aliases (a = var; b = a)
    for _pass in range(3):
        found_new = False
        for offset, line in enumerate(body_lines):
            stripped = line.strip()
            # Skip comments
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            for name in list(known):
                # x = name | x: Type = name | x = name.attr
                m = re.search(
                    rf"\b(\w+)\s*(?::\s*\w[\w\[\], ]*\s*)?=\s*\b{re.escape(name)}\b",
                    stripped,
                )
                if m:
                    alias = m.group(1)
                    if alias not in known and alias not in ("self", "cls", "this"):
                        known.add(alias)
                        aliases.append({
                            "name": alias,
                            "line": start_line + offset,
                            "expression": stripped[:200],
                        })
                        found_new = True
        if not found_new:
            break

    return aliases


# -- Forward flow detection -------------------------------------------------

def _find_forward_flows(
    workspace: str,
    body_lines: List[str],
    start_line: int,
    all_names: set,
    symbols,
) -> List[Dict[str, Any]]:
    """Find function calls where the variable (or alias) is passed as argument."""
    # Pattern: func_name(  —  captures the function name before the open-paren
    call_re = re.compile(r"(?<!\bdef\s)(?<!\bclass\s)\b([\w.]+)\s*\(")

    flows: List[Dict[str, Any]] = []
    seen_calls: set = set()  # avoid duplicate entries

    for offset, line in enumerate(body_lines):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue

        for call_match in call_re.finditer(stripped):
            func_expr = call_match.group(1)
            call_start = call_match.end()  # position after '('

            args_str = _extract_paren_content(stripped, call_start - 1)
            if args_str is None:
                continue

            arg_parts = _split_call_args(args_str)

            for arg_idx, arg_text in enumerate(arg_parts):
                arg_text_stripped = arg_text.strip()
                # Check if any tracked name appears as this argument
                for name in all_names:
                    if not re.search(rf"\b{re.escape(name)}\b", arg_text_stripped):
                        continue

                    # Determine keyword or positional
                    kw_match = re.match(r"(\w+)\s*=\s*", arg_text_stripped)
                    keyword = kw_match.group(1) if kw_match else None

                    func_simple = func_expr.split(".")[-1]
                    dedup_key = (func_simple, arg_idx, start_line + offset)
                    if dedup_key in seen_calls:
                        continue
                    seen_calls.add(dedup_key)

                    # Resolve the callee's parameter name
                    callee_info = _resolve_callee(workspace, func_simple)
                    if keyword:
                        as_param = keyword
                        confidence = "high"
                    elif callee_info:
                        params = callee_info["params"]
                        # Adjust for self/cls in method calls
                        effective_idx = arg_idx
                        if as_param := (params[effective_idx]
                                        if effective_idx < len(params) else None):
                            confidence = "high"
                        else:
                            as_param = f"arg[{arg_idx}]"
                            confidence = "medium"
                    else:
                        as_param = keyword or f"arg[{arg_idx}]"
                        confidence = "low"

                    flow: Dict[str, Any] = {
                        "callee_function": func_simple,
                        "full_expression": func_expr,
                        "as_parameter": as_param,
                        "arg_expression": arg_text_stripped,
                        "call_line": start_line + offset,
                        "arg_position": arg_idx,
                        "confidence": confidence,
                    }
                    if callee_info:
                        flow["callee_file"] = callee_info["file"]
                    flows.append(flow)
                    break  # one match per argument is enough

    return flows


def _extract_paren_content(text: str, open_pos: int) -> Optional[str]:
    """Extract content between matched parentheses starting at open_pos."""
    if open_pos >= len(text) or text[open_pos] != "(":
        return None
    depth = 1
    pos = open_pos + 1
    while pos < len(text) and depth > 0:
        ch = text[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch in ('"', "'"):
            # Skip string literals
            quote = ch
            pos += 1
            while pos < len(text) and text[pos] != quote:
                if text[pos] == "\\":
                    pos += 1
                pos += 1
        pos += 1
    return text[open_pos + 1 : pos - 1] if depth == 0 else None


def _split_call_args(args_str: str) -> List[str]:
    """Split function call arguments by comma, respecting nested parens/brackets/strings."""
    args: List[str] = []
    depth = 0
    current: List[str] = []
    i = 0
    while i < len(args_str):
        ch = args_str[i]
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch in ('"', "'"):
            current.append(ch)
            quote = ch
            i += 1
            while i < len(args_str) and args_str[i] != quote:
                if args_str[i] == "\\":
                    current.append(args_str[i])
                    i += 1
                    if i < len(args_str):
                        current.append(args_str[i])
                else:
                    current.append(args_str[i])
                i += 1
            if i < len(args_str):
                current.append(args_str[i])
        elif ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        remainder = "".join(current).strip()
        if remainder:
            args.append(remainder)
    return args


def _resolve_callee(workspace: str, func_name: str) -> Optional[Dict[str, Any]]:
    """Find a function definition in the workspace and return file + param names."""
    if func_name in _CALL_NOISE:
        return None

    index = _get_symbol_index(workspace)
    if index is None:
        return None

    ws = Path(workspace).resolve()
    for rel, definitions in index.items():
        for defn in definitions:
            if defn.name == func_name and defn.kind in ("function", "method"):
                params = _parse_params_from_signature(defn.signature)
                return {"file": rel, "params": params}

    return None


def _parse_params_from_signature(sig: str) -> List[str]:
    """Extract parameter names from a function signature string.

    Handles: ``func(a, b: int, c=3)``, ``func(self, a, b)``,
    ``function(a: string, b: number)``.
    Strips ``self``/``cls``/``this`` and type annotations.
    """
    m = re.search(r"\(([^)]*)\)", sig)
    if not m:
        return []
    args_str = m.group(1)
    params: List[str] = []
    for arg in _split_call_args(args_str):
        arg = arg.strip()
        if not arg or arg in ("self", "cls", "this"):
            continue
        # Skip *args, **kwargs
        if arg.startswith("*"):
            continue
        # Get name before : or =
        name_m = re.match(r"(\w+)", arg)
        if name_m:
            params.append(name_m.group(1))
    return params


# -- Backward flow detection ------------------------------------------------

def _get_param_position(defn, variable_name: str, lines: List[str]) -> Optional[int]:
    """Find the 0-based position of variable_name in the function's parameter list."""
    sig = defn.signature
    if not sig:
        # Fallback: read the def line from source
        if defn.start_line - 1 < len(lines):
            sig = lines[defn.start_line - 1]
    if not sig:
        return None
    params = _parse_params_from_signature(sig)
    try:
        return params.index(variable_name)
    except ValueError:
        return None


def _find_backward_flows(
    workspace: str,
    func_name: str,
    param_pos: int,
) -> List[Dict[str, Any]]:
    """Find callers and determine what value they pass for the target parameter."""
    from app.repo_graph.parser import extract_definitions, detect_language

    ws = Path(workspace).resolve()
    call_re = re.compile(rf"\b{re.escape(func_name)}\s*\(")
    flows: List[Dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(ws):
        rel_dir = Path(dirpath).relative_to(ws)
        if _is_excluded(rel_dir.parts):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for f in filenames:
            fp = Path(dirpath) / f
            if detect_language(str(fp)) is None:
                continue
            if fp.stat().st_size > _MAX_FILE_SIZE:
                continue
            try:
                source = fp.read_text(errors="replace")
            except OSError:
                continue
            if not call_re.search(source):
                continue

            rel = str(fp.relative_to(ws))
            file_symbols = extract_definitions(str(fp), fp.read_bytes())
            src_lines = source.split("\n")

            for caller_def in file_symbols.definitions:
                if caller_def.kind not in ("function", "method"):
                    continue
                end_ln = _infer_end_line(caller_def, file_symbols.definitions, len(src_lines))
                body = src_lines[caller_def.start_line - 1 : end_ln]

                for off, line in enumerate(body):
                    for cm in call_re.finditer(line):
                        args_str = _extract_paren_content(line, cm.end() - 1)
                        if args_str is None:
                            continue
                        arg_parts = _split_call_args(args_str)
                        if param_pos < len(arg_parts):
                            arg_expr = arg_parts[param_pos].strip()
                            flows.append({
                                "caller_file": rel,
                                "caller_function": caller_def.name,
                                "arg_expression": arg_expr,
                                "call_line": caller_def.start_line + off,
                                "param_position": param_pos,
                                "confidence": "high",
                            })

    return flows


# -- Sink / source pattern detection ----------------------------------------

def _detect_sinks(
    body_lines: List[str], start_line: int, all_names: set,
) -> List[Dict[str, Any]]:
    """Detect data sink patterns (ORM, SQL, HTTP, return, log)."""
    sinks: List[Dict[str, Any]] = []
    names_pattern = "|".join(re.escape(n) for n in all_names)

    patterns: List[tuple] = [
        # ORM filter patterns
        ("orm_filter", re.compile(
            rf"\.(?:filter|filter_by|where|having)\s*\([^)]*\b({names_pattern})\b",
        )),
        ("orm_get", re.compile(
            rf"\.(?:get|get_or_404|first_or_404|find|findOne|findUnique|findFirst)\s*\([^)]*\b({names_pattern})\b",
        )),
        # JPA / Spring Data patterns
        ("jpa_query", re.compile(
            rf"\.(?:findBy\w*|getBy\w*|deleteBy\w*|countBy\w*|existsBy\w*)\s*\([^)]*\b({names_pattern})\b",
        )),
        # SQL parameter patterns (use .* instead of [^)]* to handle nested parens in SQL strings)
        ("sql_param", re.compile(
            rf"\.(?:execute|executemany|raw|nativeQuery)\b.*\b({names_pattern})\b",
        )),
        ("sql_fstring", re.compile(
            rf"(?:SELECT|INSERT|UPDATE|DELETE|WHERE|SET|VALUES)[^;]*\b({names_pattern})\b",
            re.IGNORECASE,
        )),
        # HTTP outbound body
        ("http_body", re.compile(
            rf"(?:json|data|body|params)\s*[:=]\s*\{{[^}}]*\b({names_pattern})\b",
        )),
        # Return
        ("return", re.compile(
            rf"\breturn\b[^;\n]*\b({names_pattern})\b",
        )),
        # Logging
        ("log", re.compile(
            rf"(?:logger?|console|log)\.\w+\([^)]*\b({names_pattern})\b",
        )),
    ]

    seen: set = set()
    for offset, line in enumerate(body_lines):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        for kind, pat in patterns:
            m = pat.search(stripped)
            if m:
                key = (kind, start_line + offset)
                if key not in seen:
                    seen.add(key)
                    sinks.append({
                        "kind": kind,
                        "expression": stripped[:200],
                        "line": start_line + offset,
                        "matched_variable": m.group(1),
                        "confidence": "high",
                    })

    return sinks


def _detect_sources(
    body_lines: List[str], start_line: int, variable: str,
) -> List[Dict[str, Any]]:
    """Detect data source patterns (HTTP request, config, DB result)."""
    sources: List[Dict[str, Any]] = []
    var_esc = re.escape(variable)

    patterns: List[tuple] = [
        # HTTP request sources
        ("http_request", re.compile(
            rf"\b{var_esc}\s*=\s*.*(?:request|req)\s*\.\s*(?:json|body|form|args|params|query|data)"
            rf"|(?:request|req)\s*\.\s*(?:json|body|form|args|params|query|data)\s*"
            rf"(?:\[|\.get\(|\.)\s*['\"]?{var_esc}",
        )),
        # Java annotations (on previous line or same line)
        ("http_annotation", re.compile(
            rf"@(?:RequestParam|PathVariable|RequestBody|QueryParam|PathParam|Body)\b.*\b{var_esc}\b"
            rf"|\b{var_esc}\b.*@(?:RequestParam|PathVariable|RequestBody|QueryParam|PathParam|Body)",
        )),
        # Pydantic / dataclass model field
        ("model_field", re.compile(
            rf"\b{var_esc}\s*[=:]\s*Field\s*\("
            rf"|\b{var_esc}\s*:\s*\w+.*=\s*Field\s*\(",
        )),
        # Config / settings
        ("config", re.compile(
            rf"\b{var_esc}\s*=\s*.*(?:settings|config|env|os\.environ)",
        )),
        # DB query result
        ("db_result", re.compile(
            rf"\b{var_esc}\s*=\s*.*\.(?:fetchone|fetchall|first|scalar|one|all|execute)\s*\(",
        )),
        # Dict / object destructuring
        ("destructure", re.compile(
            rf"\b{var_esc}\s*=\s*\w+\s*\[\s*['\"]",
        )),
    ]

    for offset, line in enumerate(body_lines):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        for kind, pat in patterns:
            if pat.search(stripped):
                sources.append({
                    "kind": kind,
                    "expression": stripped[:200],
                    "line": start_line + offset,
                    "confidence": "high",
                })

    return sources


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_REGISTRY = {
    "grep": grep,
    "read_file": read_file,
    "list_files": list_files,
    "find_symbol": find_symbol,
    "find_references": find_references,
    "file_outline": file_outline,
    "get_dependencies": get_dependencies,
    "get_dependents": get_dependents,
    "git_log": git_log,
    "git_diff": git_diff,
    "ast_search": ast_search,
    "get_callees": get_callees,
    "get_callers": get_callers,
    "git_blame": git_blame,
    "git_show": git_show,
    "find_tests": find_tests,
    "test_outline": outline_tests,
    "trace_variable": trace_variable,
}


def execute_tool(tool_name: str, workspace: str, params: Dict[str, Any]) -> ToolResult:
    """Execute a tool by name with the given parameters."""
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return ToolResult(tool_name=tool_name, success=False, error=f"Unknown tool: {tool_name}")

    try:
        return fn(workspace=workspace, **params)
    except Exception as exc:
        logger.exception("Tool %s failed", tool_name)
        return ToolResult(tool_name=tool_name, success=False, error=str(exc))
