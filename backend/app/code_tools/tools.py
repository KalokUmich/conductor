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
from collections import Counter, deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .schemas import (
    AstMatch,
    BlameEntry,
    CalleeInfo,
    CallerInfo,
    DependencyInfo,
    DiffFileEntry,
    FileEntry,
    GitCommit,
    GrepMatch,
    ReferenceLocation,
    SymbolLocation,
    TestMatch,
    TestOutlineEntry,
    TOOL_PARAM_MODELS,
    ToolResult,
)

logger = logging.getLogger(__name__)

# Directories to always exclude from traversal
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "target",
    "dist", "vendor", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".tox", "build", ".next", ".nuxt", ".yarn", ".pnp",
    ".conductor",
}

_MAX_FILE_SIZE = 512_000  # 500 KB — skip larger files in search/parse

# Binary/media file extensions to skip in grep searches
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".dylib",
    ".exe", ".bin", ".dat", ".db", ".sqlite",
}


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
    except OSError as exc:
        return f"(git error: {exc})"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _walk_files(
    search_root: Path,
    ws: Path,
    include_glob: Optional[str] = None,
) -> tuple:
    """Walk files under search_root, respecting exclusions and glob filter.

    Returns (files_list, skipped_size, skipped_glob, dirs_excluded).
    """
    files: List[Path] = []
    skipped_size = 0
    skipped_glob = 0
    dirs_excluded = 0
    # Normalize glob: fnmatch operates on bare filenames, strip **/ prefix.
    file_glob = None
    if include_glob:
        file_glob = include_glob.split("/")[-1] if "/" in include_glob else include_glob

    for dirpath, dirnames, filenames in os.walk(search_root):
        rel_dir = Path(dirpath).relative_to(ws)
        if _is_excluded(rel_dir.parts):
            dirs_excluded += 1
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for f in filenames:
            if file_glob and not fnmatch.fnmatch(f, file_glob):
                skipped_glob += 1
                continue
            fp = Path(dirpath) / f
            if fp.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            try:
                if fp.stat().st_size > _MAX_FILE_SIZE:
                    skipped_size += 1
                    continue
            except OSError:
                continue
            files.append(fp)
    return files, skipped_size, skipped_glob, dirs_excluded


_FILE_TYPE_MAP = {
    "py": "*.py", "js": "*.js", "ts": "*.ts", "tsx": "*.tsx",
    "java": "*.java", "go": "*.go", "rust": "*.rs", "rs": "*.rs",
    "c": "*.c", "cpp": "*.cpp", "h": "*.h", "rb": "*.rb",
    "php": "*.php", "swift": "*.swift", "kt": "*.kt", "scala": "*.scala",
    "css": "*.css", "html": "*.html", "json": "*.json", "yaml": "*.yaml",
    "yml": "*.yml", "xml": "*.xml", "sql": "*.sql", "sh": "*.sh",
    "md": "*.md", "txt": "*.txt",
}


def grep(
    workspace: str,
    pattern: str,
    path: Optional[str] = None,
    include_glob: Optional[str] = None,
    max_results: int = 50,
    output_mode: str = "content",
    context_lines: int = 0,
    case_insensitive: bool = False,
    multiline: bool = False,
    file_type: Optional[str] = None,
) -> ToolResult:
    """Search for a regex pattern using subprocess grep/rg."""
    search_root = _resolve(workspace, path or ".")
    if not search_root.exists():
        return ToolResult(tool_name="grep", success=False, error=f"Path not found: {path}")

    # Resolve file_type to include_glob if not already set
    if file_type and not include_glob:
        include_glob = _FILE_TYPE_MAP.get(file_type.lower())
        if not include_glob:
            return ToolResult(
                tool_name="grep", success=False,
                error=f"Unknown file_type '{file_type}'. Supported: {', '.join(sorted(_FILE_TYPE_MAP))}",
            )

    # Validate output_mode
    if output_mode not in ("content", "files_only", "count"):
        output_mode = "content"

    re_flags = 0
    if case_insensitive:
        re_flags |= re.IGNORECASE
    if multiline:
        re_flags |= re.DOTALL

    try:
        re.compile(pattern, re_flags)
    except re.error as exc:
        return ToolResult(tool_name="grep", success=False, error=f"Invalid regex: {exc}")

    ws = Path(workspace).resolve()

    if search_root.is_file():
        files_to_search = [search_root]
        glob_dropped = False
    else:
        files_to_search, skipped_size, skipped_glob, dirs_excluded = _walk_files(
            search_root, ws, include_glob,
        )
        logger.info(
            "grep: pattern=%r path=%r files_to_search=%d "
            "skipped_size=%d skipped_glob=%d dirs_excluded=%d include_glob=%r",
            pattern, path, len(files_to_search),
            skipped_size, skipped_glob, dirs_excluded, include_glob,
        )
        # Self-healing: if include_glob filtered out ALL files, retry without
        # it. The LLM often guesses the wrong file extension (e.g. *.py for a
        # Java codebase), which makes grep useless.
        glob_dropped = False
        if not files_to_search and include_glob and skipped_glob > 0:
            logger.warning(
                "grep: include_glob=%r filtered out all %d files — "
                "retrying without glob filter",
                include_glob, skipped_glob,
            )
            files_to_search, skipped_size, _, dirs_excluded = _walk_files(
                search_root, ws, None,
            )
            glob_dropped = True
            logger.info(
                "grep: retry without glob: files_to_search=%d",
                len(files_to_search),
            )

    compiled = re.compile(pattern, re_flags)
    matches: List[Dict] = []
    file_counts: Dict[str, int] = {}  # for count mode
    seen_files: List[str] = []        # for files_only mode
    read_errors = 0

    for fp in files_to_search:
        if output_mode == "content" and len(matches) >= max_results:
            break
        if output_mode == "files_only" and len(seen_files) >= max_results:
            break
        if output_mode == "count" and len(file_counts) >= max_results:
            break

        try:
            text = fp.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            read_errors += 1
            continue

        rel_path = str(fp.relative_to(ws))

        if multiline:
            # Multiline: search entire file text at once
            if compiled.search(text):
                if output_mode == "files_only":
                    seen_files.append(rel_path)
                elif output_mode == "count":
                    file_counts[rel_path] = len(compiled.findall(text))
                else:
                    # Find line numbers of each match
                    lines = text.split("\n")
                    for m in compiled.finditer(text):
                        line_num = text[:m.start()].count("\n") + 1
                        match_line = lines[line_num - 1] if line_num <= len(lines) else ""
                        matches.append(GrepMatch(
                            file_path=rel_path,
                            line_number=line_num,
                            content=match_line.rstrip()[:500],
                        ).model_dump())
                        if len(matches) >= max_results:
                            break
            continue

        # Standard per-line search
        lines = text.split("\n")
        file_match_count = 0
        for i, line in enumerate(lines):
            if compiled.search(line):
                file_match_count += 1
                if output_mode == "files_only":
                    if rel_path not in seen_files:
                        seen_files.append(rel_path)
                    break  # one match per file is enough
                elif output_mode == "count":
                    pass  # just counting
                else:
                    # Build content with optional context lines
                    if context_lines > 0:
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)
                        ctx_parts = []
                        for j in range(start, end):
                            prefix = "> " if j == i else "  "
                            ctx_parts.append(f"{prefix}{lines[j].rstrip()}")
                        content = "\n".join(ctx_parts)[:1000]
                    else:
                        content = line.rstrip()[:500]
                    matches.append(GrepMatch(
                        file_path=rel_path,
                        line_number=i + 1,
                        content=content,
                    ).model_dump())
                    if len(matches) >= max_results:
                        break

        if output_mode == "count" and file_match_count > 0:
            file_counts[rel_path] = file_match_count

    # Build result based on output_mode
    if output_mode == "files_only":
        data = [GrepMatch(file_path=f, line_number=0, content="").model_dump() for f in seen_files]
        truncated = len(seen_files) >= max_results
    elif output_mode == "count":
        data = [GrepMatch(file_path=f, line_number=0, content=f"{c} matches").model_dump()
                for f, c in file_counts.items()]
        truncated = len(file_counts) >= max_results
    else:
        data = matches
        truncated = len(matches) >= max_results

    logger.info(
        "grep: pattern=%r matches=%d read_errors=%d glob_dropped=%s mode=%s",
        pattern, len(data), read_errors, glob_dropped, output_mode,
    )
    return ToolResult(
        tool_name="grep",
        data=data,
        truncated=truncated,
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

    # Max lines to return when no line range is specified.
    # Kept small to force the agent to use file_outline → targeted read_file.
    _AUTO_TRUNCATE_LINES = 200

    if start_line or end_line:
        s = (start_line or 1) - 1
        e = end_line or total
        selected = lines[s:e]
        content = "\n".join(f"{s + i + 1:>4} | {l}" for i, l in enumerate(selected))
        truncated = e < total
    else:
        if total > _AUTO_TRUNCATE_LINES:
            selected = lines[:_AUTO_TRUNCATE_LINES]
            content = "\n".join(f"{i + 1:>4} | {l}" for i, l in enumerate(selected))
            content += (
                f"\n\n... (showing first {_AUTO_TRUNCATE_LINES} of {total} lines) "
                f"Use file_outline to see all definitions, then read_file with "
                f"start_line/end_line to read specific sections."
            )
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
            if include_glob:
                file_glob = include_glob.split("/")[-1] if "/" in include_glob else include_glob
                if not fnmatch.fnmatch(f, file_glob):
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
    """Load symbol index from disk. Returns (index, git_head) or None.

    Rejects empty indexes (0 definitions) — these are likely stale caches
    from an incomplete workspace checkout.
    """
    path = _disk_cache_path(workspace)
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        head = data.get("git_head")
        raw_index = data.get("index")
        if head and raw_index is not None:
            index = _deserialize_definitions(raw_index)
            if not index:
                logger.warning(
                    "Disk cache has 0 definitions — discarding stale cache "
                    "(workspace=%s, head=%s)",
                    workspace, head[:8],
                )
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            return index, head
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
    except ImportError as exc:
        logger.warning("Symbol index unavailable: cannot import parser (%s)", exc)
        return None

    current_head = _get_git_head(workspace)

    # 1. In-memory hit
    entry = _symbol_index_cache.get(workspace)
    if entry is not None:
        index, cached_head = entry
        if cached_head == current_head:
            logger.debug(
                "Symbol index in-memory hit: %d files, head=%s",
                len(index), (current_head or "")[:8],
            )
            return index

    # 2. Disk hit
    if current_head is not None:
        disk_entry = _load_disk_cache(workspace)
        if disk_entry is not None:
            disk_index, disk_head = disk_entry
            if disk_head == current_head:
                logger.debug(
                    "Symbol index disk hit: %d files, head=%s",
                    len(disk_index), current_head[:8],
                )
                _symbol_index_cache[workspace] = (disk_index, current_head)
                return disk_index

    # 3. Full scan
    ws = Path(workspace).resolve()
    if not ws.is_dir():
        logger.warning("Symbol index scan: workspace does not exist: %s", ws)
        return {}

    index: Dict[str, list] = {}
    files_scanned = 0
    files_skipped_lang = 0
    files_skipped_size = 0
    total_defs = 0

    for dirpath, dirnames, filenames in os.walk(ws):
        rel_dir = Path(dirpath).relative_to(ws)
        if _is_excluded(rel_dir.parts):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for f in filenames:
            fp = Path(dirpath) / f
            if detect_language(str(fp)) is None:
                files_skipped_lang += 1
                continue
            try:
                fsize = fp.stat().st_size
            except OSError:
                continue
            if fsize > _MAX_FILE_SIZE:
                files_skipped_size += 1
                continue
            try:
                source = fp.read_bytes()
            except OSError:
                continue
            files_scanned += 1
            rel = str(fp.relative_to(ws))
            symbols = extract_definitions(str(fp), source)
            if symbols.definitions:
                index[rel] = symbols.definitions
                total_defs += len(symbols.definitions)

    logger.info(
        "Symbol index built: %d files with %d definitions "
        "(scanned=%d, skipped_lang=%d, skipped_size=%d, workspace=%s, head=%s)",
        len(index), total_defs, files_scanned,
        files_skipped_lang, files_skipped_size,
        workspace, (current_head or "")[:8],
    )

    cache_head = current_head or ""
    _symbol_index_cache[workspace] = (index, cache_head)
    # Only persist to disk if the index is non-trivial.
    # An empty or near-empty index may indicate the workspace wasn't fully
    # checked out yet (e.g. git worktree still in progress).  By skipping
    # disk caching we ensure the next call does a fresh scan.
    if current_head and total_defs > 0:
        _save_disk_cache(workspace, index, current_head)
    elif current_head and total_defs == 0 and files_scanned > 0:
        logger.warning(
            "Symbol index has 0 definitions from %d scanned files — "
            "NOT saving to disk cache (possible incomplete checkout)",
            files_scanned,
        )
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


# ---------------------------------------------------------------------------
# Symbol Role Classification
# ---------------------------------------------------------------------------

# Role priority for sorting (lower = more important to show first)
_ROLE_PRIORITY = {
    "route_entry": 0,
    "business_logic": 1,
    "domain_model": 2,
    "infrastructure": 3,
    "utility": 4,
    "test": 5,
    "unknown": 6,
}

# File path patterns → role
_PATH_ROLE_PATTERNS = [
    (re.compile(r"test[s_/]|_test\.|\.test\.|\.spec\."), "test"),
    (re.compile(r"route[rs]?[/.]|endpoint|handler|controller|view[s]?[/.]"), "route_entry"),
    (re.compile(r"service[s]?[/.]|usecase|interactor"), "business_logic"),
    (re.compile(r"model[s]?[/.]|schema[s]?[/.]|entit(?:y|ies)[/.]|domain[/.]"), "domain_model"),
    (re.compile(r"util[s]?[/.]|helper[s]?[/.]|common[/.]|lib[/.]"), "utility"),
    (re.compile(r"repo(?:sitory)?[/.]|dao[/.]|adapter[/.]|client[/.]|infra[/.]|db[/.]"), "infrastructure"),
]

# Signature / name patterns → role
_SIG_ROLE_PATTERNS = [
    # Route decorators (Python, Java, TS)
    (re.compile(r"@(?:app|router|api)\.\s*(?:get|post|put|delete|patch|route)"), "route_entry"),
    (re.compile(r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping"), "route_entry"),
    (re.compile(r"@Controller|@RestController|@Resource"), "route_entry"),
    # Service/business logic
    (re.compile(r"@Service|@Component|@Injectable"), "business_logic"),
    (re.compile(r"class\s+\w*Service"), "business_logic"),
    # Domain models
    (re.compile(r"@Entity|@Table|@Document|@dataclass"), "domain_model"),
    (re.compile(r"class\s+\w*(?:Model|Schema|Entity|DTO)"), "domain_model"),
    (re.compile(r"class\s+\w+\(.*(?:Base|Model|Schema|DeclarativeBase)"), "domain_model"),
    # Infrastructure
    (re.compile(r"@Repository|@Mapper"), "infrastructure"),
    (re.compile(r"class\s+\w*(?:Repository|Repo|DAO|Client|Adapter)"), "infrastructure"),
    # Test
    (re.compile(r"(?:def|function)\s+test_|@Test|@pytest|#\[test\]|#\[tokio::test\]"), "test"),
    (re.compile(r"class\s+Test\w+|describe\s*\("), "test"),
]


def _classify_symbol_role(
    name: str,
    kind: str,
    file_path: str,
    signature: str,
    workspace: str,
    start_line: int = 0,
) -> str:
    """Classify a symbol's role based on naming, decorators, and file path.

    Checks (in priority order):
      1. Signature + decorator context patterns (most specific)
      2. File path patterns (reliable heuristic)
      3. Name patterns (fallback)
    """
    # 1. Check signature patterns — also read 5 lines above the symbol
    #    to catch decorators like @app.route, @Service, @Entity
    context = signature
    if start_line > 1:
        try:
            full_path = Path(workspace).resolve() / file_path
            if full_path.is_file() and full_path.stat().st_size < _MAX_FILE_SIZE:
                with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                    all_lines = fh.readlines()
                # Grab up to 5 lines before the symbol definition
                deco_start = max(0, start_line - 6)  # 0-indexed
                deco_end = min(len(all_lines), start_line)  # up to (not incl.) the def line
                context = "".join(all_lines[deco_start:deco_end]) + "\n" + signature
        except (OSError, ValueError):
            pass

    for pat, role in _SIG_ROLE_PATTERNS:
        if pat.search(context):
            return role

    # 2. Check file path
    fp_lower = file_path.lower().replace("\\", "/")
    for pat, role in _PATH_ROLE_PATTERNS:
        if pat.search(fp_lower):
            return role

    # 3. Name-based fallback
    n_lower = name.lower()
    if n_lower.startswith("test") or n_lower.endswith("test"):
        return "test"
    if any(s in n_lower for s in ("service", "usecase", "interactor")):
        return "business_logic"
    if any(s in n_lower for s in ("model", "schema", "entity")):
        return "domain_model"
    if any(s in n_lower for s in ("handler", "controller", "endpoint", "route", "view")):
        return "route_entry"
    if any(s in n_lower for s in ("repository", "repo", "dao", "client", "adapter")):
        return "infrastructure"
    if any(s in n_lower for s in ("util", "helper", "common")):
        return "utility"

    return "unknown"


def glob_files(
    workspace: str,
    pattern: str,
    path: Optional[str] = None,
) -> ToolResult:
    """Fast file pattern matching, returns paths sorted by modification time."""
    search_root = _resolve(workspace, path or ".")
    if not search_root.exists():
        return ToolResult(tool_name="glob", success=False, error=f"Path not found: {path}")
    if not search_root.is_dir():
        return ToolResult(tool_name="glob", success=False, error=f"Not a directory: {path}")

    ws = Path(workspace).resolve()
    results: List[Dict] = []

    for match in search_root.glob(pattern):
        if not match.is_file():
            continue
        # Skip excluded directories
        try:
            rel = match.relative_to(ws)
        except ValueError:
            continue
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        # Skip binary files
        if match.suffix.lower() in _BINARY_EXTENSIONS:
            continue
        try:
            stat = match.stat()
        except OSError:
            continue
        results.append({
            "path": str(rel),
            "size": stat.st_size,
        })

    # Sort by modification time descending (most recent first)
    results.sort(key=lambda r: -Path(ws / r["path"]).stat().st_mtime)

    # Cap results
    max_results = 100
    truncated = len(results) > max_results
    if truncated:
        results = results[:max_results]

    return ToolResult(
        tool_name="glob",
        data=results,
        truncated=truncated,
    )


def find_symbol(
    workspace: str,
    name: str,
    kind: Optional[str] = None,
    _graph_cache: Optional[Dict] = None,
) -> ToolResult:
    """Find symbol definitions using a cached symbol index.

    Results are sorted by role priority: route_entry > business_logic >
    domain_model > infrastructure > utility > test > unknown.
    Within the same role, exact name matches come first.
    """
    index = _get_symbol_index(workspace)
    if index is None:
        return ToolResult(
            tool_name="find_symbol", success=False,
            error="Symbol index not available (parser import failed — check tree-sitter-languages install).",
        )
    if not index:
        logger.warning(
            "find_symbol('%s'): index is empty (0 files indexed) for workspace=%s",
            name, workspace,
        )
        return ToolResult(
            tool_name="find_symbol", data=[],
            error="Symbol index is empty — no parseable source files found in workspace.",
        )

    results: List[Dict] = []
    name_lower = name.lower()

    for rel, definitions in index.items():
        for defn in definitions:
            if name_lower not in defn.name.lower():
                continue
            if kind and defn.kind != kind:
                continue
            role = _classify_symbol_role(
                name=defn.name,
                kind=defn.kind,
                file_path=rel,
                signature=defn.signature,
                workspace=workspace,
                start_line=defn.start_line,
            )
            d = SymbolLocation(
                name=defn.name,
                kind=defn.kind,
                file_path=rel,
                start_line=defn.start_line,
                end_line=defn.end_line,
                signature=defn.signature,
            ).model_dump()
            d["role"] = role
            results.append(d)

    # Sort: role priority first, then exact match before substring match
    results.sort(key=lambda r: (
        _ROLE_PRIORITY.get(r.get("role", "unknown"), 99),
        0 if r["name"].lower() == name_lower else 1,
    ))

    logger.info(
        "find_symbol('%s'%s): %d results from %d indexed files",
        name, f", kind={kind}" if kind else "",
        len(results), len(index),
    )
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
            except Exception:  # TODO: narrow once extract_definitions exception surface is known
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
    max_depth: int = 1,
    _graph_service=None,
) -> ToolResult:
    """Find files that this file depends on (out-edges in the dependency graph).

    When *max_depth* > 1 (max 3), performs BFS traversal to collect transitive
    dependencies.  Each result carries a ``depth`` field (1 = direct).
    """
    graph = _ensure_graph(workspace, _graph_service)
    if graph is None:
        return ToolResult(
            tool_name="get_dependencies", success=False,
            error="Dependency graph not available (missing networkx or tree-sitter).",
        )

    max_depth = max(1, min(int(max_depth), 3))

    if max_depth == 1:
        # Fast path — original behaviour
        deps: List[Dict] = []
        for edge in graph.edges:
            if edge.source == file_path:
                d = DependencyInfo(
                    file_path=edge.target,
                    symbols=edge.symbols,
                    weight=edge.weight,
                ).model_dump()
                d["depth"] = 1
                deps.append(d)
        deps.sort(key=lambda d: d["weight"], reverse=True)
        return ToolResult(tool_name="get_dependencies", data=deps)

    # BFS for transitive dependencies
    visited: set = {file_path}
    queue: deque = deque()
    queue.append((file_path, 0))
    deps = []

    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= max_depth:
            continue
        for edge in graph.edges:
            if edge.source == current and edge.target not in visited:
                visited.add(edge.target)
                d = DependencyInfo(
                    file_path=edge.target,
                    symbols=edge.symbols,
                    weight=edge.weight,
                ).model_dump()
                d["depth"] = current_depth + 1
                deps.append(d)
                queue.append((edge.target, current_depth + 1))

    deps.sort(key=lambda d: (d["depth"], -d["weight"]))
    return ToolResult(tool_name="get_dependencies", data=deps)


def get_dependents(
    workspace: str,
    file_path: str,
    max_depth: int = 1,
    _graph_service=None,
) -> ToolResult:
    """Find files that depend on this file (in-edges in the dependency graph).

    When *max_depth* > 1 (max 3), performs BFS traversal to collect transitive
    dependents.  Each result carries a ``depth`` field (1 = direct).
    """
    graph = _ensure_graph(workspace, _graph_service)
    if graph is None:
        return ToolResult(
            tool_name="get_dependents", success=False,
            error="Dependency graph not available (missing networkx or tree-sitter).",
        )

    max_depth = max(1, min(int(max_depth), 3))

    if max_depth == 1:
        # Fast path — original behaviour
        deps: List[Dict] = []
        for edge in graph.edges:
            if edge.target == file_path:
                d = DependencyInfo(
                    file_path=edge.source,
                    symbols=edge.symbols,
                    weight=edge.weight,
                ).model_dump()
                d["depth"] = 1
                deps.append(d)
        deps.sort(key=lambda d: d["weight"], reverse=True)
        return ToolResult(tool_name="get_dependents", data=deps)

    # BFS for transitive dependents
    visited: set = {file_path}
    queue: deque = deque()
    queue.append((file_path, 0))
    deps = []

    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= max_depth:
            continue
        for edge in graph.edges:
            if edge.target == current and edge.source not in visited:
                visited.add(edge.source)
                d = DependencyInfo(
                    file_path=edge.source,
                    symbols=edge.symbols,
                    weight=edge.weight,
                ).model_dump()
                d["depth"] = current_depth + 1
                deps.append(d)
                queue.append((edge.source, current_depth + 1))

    deps.sort(key=lambda d: (d["depth"], -d["weight"]))
    return ToolResult(tool_name="get_dependents", data=deps)


def git_log(
    workspace: str,
    file: Optional[str] = None,
    n: int = 10,
    search: Optional[str] = None,
) -> ToolResult:
    """Show recent git commits with changed-file summaries.

    Each commit includes a ``files`` list showing which files were touched
    and how many lines were added/deleted.  This lets the LLM decide which
    commits are relevant without needing to call ``git_show`` on each one.
    """
    # Step 1: get commit metadata
    args = ["log", f"-{n}", "--format=%H|%s|%an|%ai"]
    if search:
        args += [f"--grep={search}", "-i"]
    if file:
        fp = _resolve(workspace, file)
        args += ["--", str(fp)]

    raw = _run_git(workspace, args)
    commits: List[Dict] = []
    hashes: List[str] = []
    for line in raw.strip().split("\n"):
        if not line or line.startswith("("):
            continue
        parts = line.split("|", 3)
        if len(parts) >= 2:
            full_hash = parts[0]
            hashes.append(full_hash)
            commits.append(GitCommit(
                hash=full_hash[:8],
                message=parts[1],
                author=parts[2] if len(parts) > 2 else "",
                date=parts[3] if len(parts) > 3 else "",
            ).model_dump())

    # Step 2: get --stat for each commit (files changed + line counts)
    if hashes:
        stat_args = ["log", f"-{n}", "--format=%H", "--stat=120"]
        if search:
            stat_args += [f"--grep={search}", "-i"]
        if file:
            stat_args += ["--", str(_resolve(workspace, file))]
        stat_raw = _run_git(workspace, stat_args, max_output=50_000)

        # Parse stat output: group lines by commit hash
        current_hash = ""
        hash_files: Dict[str, List[str]] = {}
        for line in stat_raw.split("\n"):
            line = line.strip()
            if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                current_hash = line
                hash_files[current_hash] = []
            elif current_hash and "|" in line and not line.startswith("("):
                hash_files.setdefault(current_hash, []).append(line)

        # Attach file stats to commits
        for commit in commits:
            full = next((h for h in hashes if h[:8] == commit["hash"]), "")
            stat_lines = hash_files.get(full, [])
            commit["files_changed"] = len(stat_lines)
            commit["stat"] = stat_lines[:10]  # cap to 10 files per commit

    return ToolResult(tool_name="git_log", data=commits)


def git_diff(
    workspace: str,
    ref1: Optional[str] = "HEAD~1",
    ref2: Optional[str] = "HEAD",
    file: Optional[str] = None,
    context_lines: int = 10,
) -> ToolResult:
    """Show diff between two git refs.

    Args:
        context_lines: Number of surrounding context lines in the unified diff
                       (default 10, git default is 3). More context reduces the
                       need for separate read_file calls during code review.
    """
    args = ["diff", f"--unified={context_lines}", ref1 or "HEAD~1", ref2 or "HEAD"]
    if file:
        fp = _resolve(workspace, file)
        args += ["--", str(fp)]

    raw = _run_git(workspace, args, max_output=100_000)
    return ToolResult(tool_name="git_diff", data={"diff": raw})


_DIFF_STATUS_MAP = {
    "A": "added",
    "C": "copied",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "T": "type_changed",
}


# ---------------------------------------------------------------------------
# File review priority classification — language-agnostic
# ---------------------------------------------------------------------------

# Category display order (lower = review first)
_CATEGORY_ORDER = {
    "business_logic": 1,
    "controller":     2,
    "model":          3,
    "repository":     4,
    "config":         5,
    "test":           6,
    "docs":           7,
    "generated":      8,
}

# Patterns checked against the lowercased full path and filename.
# Order matters: first match wins.
_FILE_PRIORITY_RULES: List[tuple] = [
    # --- generated / vendor / skip ---
    ("generated",      lambda p, f: any(x in p for x in (
        "/generated/", "/gen/", "/vendor/", "/node_modules/",
        "/dist/", "/build/", "/__pycache__/", "/target/classes/",
    ))),
    ("generated",      lambda p, f: f.endswith((
        ".lock", ".min.js", ".min.css", ".map", ".pb.go", ".pb.h",
        ".generated.ts", ".generated.java", ".g.dart",
    ))),
    ("generated",      lambda p, f: f in (
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum",
        "poetry.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
    )),

    # --- docs ---
    ("docs",           lambda p, f: f.endswith((".md", ".rst", ".txt", ".adoc"))),
    ("docs",           lambda p, f: f in ("LICENSE", "CHANGELOG", "AUTHORS", "CONTRIBUTING")),

    # --- tests ---
    ("test",           lambda p, f: "/test/" in p or "/tests/" in p or "/spec/" in p
                                    or "/__tests__/" in p or "/test_" in f),
    ("test",           lambda p, f: f.startswith("test_") or f.endswith((
        "_test.py", "_test.go", "_test.rs", "_test.dart",
        ".test.ts", ".test.js", ".test.tsx", ".test.jsx",
        ".spec.ts", ".spec.js", ".spec.tsx", ".spec.jsx",
        "test.java", "tests.java", "spec.java",
        "_test.rb", "_spec.rb",
    ))),

    # --- config / infra ---
    ("config",         lambda p, f: f.endswith((
        ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env",
        ".properties", ".xml", ".gradle", ".sbt",
        "dockerfile", ".dockerignore",
    ))),
    ("config",         lambda p, f: f in (
        "pom.xml", "build.gradle", "settings.gradle",
        "makefile", "cmakelists.txt", "cargo.toml", "go.mod", "go.sum",
        "package.json", "tsconfig.json", "webpack.config.js", "vite.config.ts",
        ".eslintrc.js", ".prettierrc", "pyproject.toml", "setup.py", "setup.cfg",
        "gemfile", "rakefile", "composer.json",
    )),
    ("config",         lambda p, f: "/config/" in p or "/infra/" in p
                                    or "/deploy/" in p or "/.github/" in p),

    # --- repository / data access ---
    ("repository",     lambda p, f: any(x in f for x in (
        "repository", "repo", "dao", "mapper", "store",
    )) and not f.endswith((".md", ".txt"))),
    ("repository",     lambda p, f: "/repository/" in p or "/repositories/" in p
                                    or "/dao/" in p or "/mappers/" in p),

    # --- model / entity / schema ---
    ("model",          lambda p, f: any(x in f for x in (
        "model", "entity", "schema", "dto", "vo", "pojo",
        "dataclass", "struct", "type", "proto",
    )) and not f.endswith((".md", ".txt"))),
    ("model",          lambda p, f: any(x in p for x in (
        "/model/", "/models/", "/entity/", "/entities/",
        "/schema/", "/schemas/", "/dto/", "/types/", "/domain/",
        "/proto/", "/graphql/",
    ))),

    # --- controller / handler / route / API ---
    ("controller",     lambda p, f: any(x in f for x in (
        "controller", "handler", "router", "route", "endpoint",
        "resource", "resolver", "view", "api",
    )) and not f.endswith((".md", ".txt"))),
    ("controller",     lambda p, f: any(x in p for x in (
        "/controller/", "/controllers/", "/handler/", "/handlers/",
        "/router/", "/routers/", "/routes/", "/api/", "/endpoint/",
        "/resource/", "/resources/", "/resolvers/", "/views/",
    ))),

    # --- business logic (highest priority source code) ---
    ("business_logic", lambda p, f: any(x in f for x in (
        "service", "usecase", "interactor", "manager", "processor",
        "provider", "facade", "orchestrator", "workflow", "engine",
        "validator", "checker", "consumer", "producer", "listener",
        "subscriber", "publisher", "worker", "job", "task",
        "middleware", "interceptor", "filter", "guard",
        "helper", "util", "utils",
    )) and not f.endswith((".md", ".txt"))),
    ("business_logic", lambda p, f: any(x in p for x in (
        "/service/", "/services/", "/usecase/", "/usecases/",
        "/core/", "/business/", "/logic/", "/impl/",
    ))),

    # --- fallback: any source code file → business_logic ---
    ("business_logic", lambda p, f: f.endswith((
        ".java", ".py", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx",
        ".kt", ".scala", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
        ".swift", ".dart", ".ex", ".exs", ".clj", ".hs", ".lua",
        ".r", ".R", ".jl", ".zig", ".nim", ".v", ".ml", ".fs",
    ))),
]


def _classify_file_priority(path: str) -> str:
    """Classify a file into a review priority category.

    Returns one of: business_logic, controller, model, repository,
    config, test, docs, generated.
    """
    p = path.lower().replace("\\", "/")
    f = p.rsplit("/", 1)[-1] if "/" in p else p

    for category, check in _FILE_PRIORITY_RULES:
        try:
            if check(p, f):
                return category
        except (TypeError, AttributeError):
            continue

    return "business_logic"  # default: treat unknown as source code


def git_diff_files(
    workspace: str,
    ref: str,
) -> ToolResult:
    """List files changed in a git diff with status and line counts.

    Combines ``git diff --numstat`` and ``git diff --name-status`` to
    produce a structured list of changed files.
    """
    # Split ref into git args — handle "master...feature", "HEAD~5", "a b"
    ref_parts = ref.strip().split()

    # --numstat for additions/deletions
    numstat_raw = _run_git(workspace, ["diff", "--numstat"] + ref_parts)
    # --name-status for change type (A/M/D/R/C)
    status_raw = _run_git(workspace, ["diff", "--name-status"] + ref_parts)

    if numstat_raw.startswith("(git") or status_raw.startswith("(git"):
        error_msg = numstat_raw if numstat_raw.startswith("(git") else status_raw
        return ToolResult(
            tool_name="git_diff_files", success=False,
            error=f"Git command failed: {error_msg}",
        )

    # Parse --name-status: "M\tpath" or "R100\told\tnew"
    status_map: Dict[str, tuple] = {}  # path → (status_str, old_path)
    for line in status_raw.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status_code = parts[0]
        status_letter = status_code[0]  # R100 → R
        status_str = _DIFF_STATUS_MAP.get(status_letter, "modified")
        if status_letter in ("R", "C") and len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
            status_map[new_path] = (status_str, old_path)
        else:
            status_map[parts[1]] = (status_str, None)

    # Parse --numstat: "25\t8\tpath" or "0\t0\told => new"
    entries: List[Dict] = []
    for line in numstat_raw.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        add_str, del_str, filepath = parts
        # Binary files show as "-\t-\tfile"
        additions = int(add_str) if add_str != "-" else 0
        deletions = int(del_str) if del_str != "-" else 0
        # Handle rename notation: "old => new" or "{old => new}/path"
        if " => " in filepath:
            # Use the new path (last part after =>)
            filepath = filepath.split(" => ")[-1].rstrip("}")
            if filepath.startswith("/"):
                filepath = filepath[1:]

        status_str, old_path = status_map.get(filepath, ("modified", None))
        entry: Dict[str, Any] = {
            "path": filepath,
            "status": status_str,
            "additions": additions,
            "deletions": deletions,
        }
        if old_path:
            entry["old_path"] = old_path
        entries.append(entry)

    # Classify and sort by review priority
    for entry in entries:
        entry["category"] = _classify_file_priority(entry["path"])

    entries.sort(key=lambda e: (
        _CATEGORY_ORDER.get(e["category"], 50),   # tier first
        -(e["additions"] + e["deletions"]),        # then by change size desc
    ))

    return ToolResult(
        tool_name="git_diff_files",
        data=entries,
        truncated=len(entries) > 100,
    )


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
    re.compile(r"^.*Test\.java$"),
    re.compile(r"^.*Tests\.java$"),
    re.compile(r"^.*_test\.rs$"),
    re.compile(r"^.*_test\.c$"),
    re.compile(r"^.*_test\.cpp$"),
    re.compile(r"^.*_test\.cc$"),
]


def _is_test_file(filename: str) -> bool:
    return any(p.match(filename) for p in _TEST_FILE_PATTERNS)


# Regex patterns for finding test function definitions per language
_PY_TEST_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(test_\w+)\s*\(")
_PY_CLASS_DEF = re.compile(r"^(\s*)class\s+(Test\w+)")
_JS_TEST_BLOCK = re.compile(r"(test|it)\s*\(\s*['\"`](.+?)['\"`]")
_GO_TEST_FUNC = re.compile(r"^func\s+(Test\w+|Benchmark\w+)\s*\(")
_JAVA_TEST_ANNOTATION = re.compile(r"^\s*@(Test|ParameterizedTest|RepeatedTest)\b")
_JAVA_METHOD_DEF = re.compile(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:void|[\w<>\[\]]+)\s+(\w+)\s*\(")
_RUST_TEST_ATTR = re.compile(r"^\s*#\[(test|tokio::test|rstest)\]")
_RUST_FN_DEF = re.compile(r"^\s*(?:async\s+)?fn\s+(\w+)\s*\(")


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

    if lang == "java":
        # Walk backward: find a method def preceded by @Test / @ParameterizedTest
        for i in range(idx, -1, -1):
            mm = _JAVA_METHOD_DEF.match(lines[i])
            if mm:
                # Check lines above for a test annotation
                for k in range(i - 1, max(i - 5, -1), -1):
                    if _JAVA_TEST_ANNOTATION.match(lines[k]):
                        return {"name": mm.group(1), "line": i + 1}
                    stripped = lines[k].strip()
                    if stripped and not stripped.startswith("@") and not stripped.startswith("//"):
                        break
        return None

    if lang == "rust":
        # Walk backward: find fn def preceded by #[test] / #[tokio::test]
        for i in range(idx, -1, -1):
            fm = _RUST_FN_DEF.match(lines[i])
            if fm:
                for k in range(i - 1, max(i - 5, -1), -1):
                    if _RUST_TEST_ATTR.match(lines[k]):
                        return {"name": fm.group(1), "line": i + 1}
                    stripped = lines[k].strip()
                    if stripped and not stripped.startswith("#[") and not stripped.startswith("//"):
                        break
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
    elif lang == "java":
        entries = _java_test_outline(lines)
    elif lang == "go":
        entries = _go_test_outline(lines)
    elif lang == "rust":
        entries = _rust_test_outline(lines)
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


# Java test outline helpers
_JAVA_MOCK_RE = [
    re.compile(r"@Mock\b"),
    re.compile(r"@InjectMocks\b"),
    re.compile(r"@MockBean\b"),
    re.compile(r"@SpyBean\b"),
    re.compile(r"(?:Mockito\.)?(?:mock|spy|when)\((.{0,60}?)\)"),
]
_JAVA_ASSERT_RE = re.compile(
    r"(assert\w+\(.{0,80}|assertThat\(.{0,80}|verify\(.{0,60})"
)


def _java_test_outline(lines: List[str]) -> List[TestOutlineEntry]:
    """Parse Java test file for @Test methods, mocks (Mockito), assertions."""
    entries: List[TestOutlineEntry] = []
    # Find class name
    class_name = ""
    class_re = re.compile(r"^\s*(?:public\s+)?class\s+(\w+)")
    for line in lines:
        cm = class_re.match(line)
        if cm:
            class_name = cm.group(1)
            break

    # Scan for @Test annotated methods
    i = 0
    while i < len(lines):
        if _JAVA_TEST_ANNOTATION.match(lines[i]):
            annotation_line = i
            # Advance past annotations to find the method def
            j = i + 1
            while j < len(lines):
                mm = _JAVA_METHOD_DEF.match(lines[j])
                if mm:
                    method_name = mm.group(1)
                    full_name = f"{class_name}::{method_name}" if class_name else method_name
                    # Scan method body (brace counting)
                    brace = 0
                    started = False
                    body_lines: List[str] = []
                    end_line = j + 1
                    for k in range(j, min(j + 200, len(lines))):
                        brace += lines[k].count("{") - lines[k].count("}")
                        if "{" in lines[k]:
                            started = True
                        body_lines.append(lines[k])
                        if started and brace <= 0:
                            end_line = k + 1
                            break
                    body_text = "\n".join(body_lines)
                    mocks: List[str] = []
                    for mp in _JAVA_MOCK_RE:
                        for m in mp.finditer(body_text):
                            val = m.group(1).strip()[:60] if m.lastindex else m.group(0).strip()[:60]
                            mocks.append(val)
                    assertions = [
                        m.group(1).strip()[:80]
                        for m in _JAVA_ASSERT_RE.finditer(body_text)
                    ]
                    entries.append(TestOutlineEntry(
                        name=full_name, kind="test_function",
                        line_number=j + 1, end_line=end_line,
                        mocks=mocks[:10], assertions=assertions[:10],
                    ))
                    i = end_line
                    break
                stripped = lines[j].strip()
                if stripped and not stripped.startswith("@") and not stripped.startswith("//"):
                    break
                j += 1
            else:
                i += 1
                continue
            continue
        i += 1
    return entries


def _go_test_outline(lines: List[str]) -> List[TestOutlineEntry]:
    """Parse Go test file for Test*/Benchmark* functions, assertions."""
    entries: List[TestOutlineEntry] = []
    go_assert_re = re.compile(
        r"(t\.(?:Error|Fatal|Log|Run|Helper|Skip|Parallel)\w*\(.{0,60}|"
        r"assert\.\w+\(.{0,60}|require\.\w+\(.{0,60})"
    )
    go_mock_re = re.compile(r"(gomock\.NewController|mock\.\w+)")

    for i, line in enumerate(lines):
        fm = _GO_TEST_FUNC.match(line)
        if not fm:
            continue
        func_name = fm.group(1)
        # Scan body via brace counting
        brace = 0
        started = False
        body_lines: List[str] = []
        end_line = i + 1
        for k in range(i, min(i + 300, len(lines))):
            brace += lines[k].count("{") - lines[k].count("}")
            if "{" in lines[k]:
                started = True
            body_lines.append(lines[k])
            if started and brace <= 0:
                end_line = k + 1
                break
        body_text = "\n".join(body_lines)
        assertions = [m.group(1).strip()[:80] for m in go_assert_re.finditer(body_text)]
        mocks = [m.group(1).strip()[:60] for m in go_mock_re.finditer(body_text)]
        entries.append(TestOutlineEntry(
            name=func_name, kind="test_function",
            line_number=i + 1, end_line=end_line,
            mocks=mocks[:10], assertions=assertions[:10],
        ))
    return entries


def _rust_test_outline(lines: List[str]) -> List[TestOutlineEntry]:
    """Parse Rust test file for #[test] / #[tokio::test] functions, assertions."""
    entries: List[TestOutlineEntry] = []
    rust_assert_re = re.compile(
        r"(assert!\(.{0,80}|assert_eq!\(.{0,80}|assert_ne!\(.{0,80}|"
        r"panic!\(.{0,60}|should_panic)"
    )
    # Detect mod tests { ... } block
    mod_test_re = re.compile(r"^\s*mod\s+tests\b")
    in_test_mod = False

    i = 0
    while i < len(lines):
        if mod_test_re.match(lines[i]):
            in_test_mod = True

        if _RUST_TEST_ATTR.match(lines[i]):
            # Advance to the fn def
            j = i + 1
            while j < len(lines):
                fm = _RUST_FN_DEF.match(lines[j])
                if fm:
                    func_name = fm.group(1)
                    # Scan body via brace counting
                    brace = 0
                    started = False
                    body_lines: List[str] = []
                    end_line = j + 1
                    for k in range(j, min(j + 200, len(lines))):
                        brace += lines[k].count("{") - lines[k].count("}")
                        if "{" in lines[k]:
                            started = True
                        body_lines.append(lines[k])
                        if started and brace <= 0:
                            end_line = k + 1
                            break
                    body_text = "\n".join(body_lines)
                    assertions = [
                        m.group(1).strip()[:80]
                        for m in rust_assert_re.finditer(body_text)
                    ]
                    entries.append(TestOutlineEntry(
                        name=func_name, kind="test_function",
                        line_number=j + 1, end_line=end_line,
                        assertions=assertions[:10],
                    ))
                    i = end_line
                    break
                stripped = lines[j].strip()
                if stripped and not stripped.startswith("#[") and not stripped.startswith("//"):
                    break
                j += 1
            else:
                i += 1
                continue
            continue
        i += 1
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
# Compressed view / module summary / expand symbol
# ---------------------------------------------------------------------------

_SIDE_EFFECT_PATTERNS = {
    "db_write": [
        "session.add", "session.commit", ".save()", ".create(",
        ".update(", ".delete(", "bulk_create", ".objects.create",
        "INSERT", "UPDATE", "db.add", "db.flush", "db.execute",
    ],
    "http_call": [
        "requests.", "httpx.", "aiohttp.", "fetch(",
        "urllib", "ClientSession",
    ],
    "event_publish": [
        "publish(", "emit(", "send_event(", "dispatch(",
        "notify(", "event_bus.", "broker.",
    ],
    "file_write": [
        ".write(", "mkdir(", "shutil.", "copyfile",
    ],
    "cache_write": [
        "cache.set", "redis.", "memcached.", ".cache(",
    ],
}


def _detect_side_effects(body_text: str) -> List[str]:
    """Detect side effects by pattern matching in function body."""
    if not body_text:
        return []
    effects = []
    for effect_type, markers in _SIDE_EFFECT_PATTERNS.items():
        if any(m in body_text for m in markers):
            effects.append(effect_type.replace("_", " "))
    return effects


def _extract_callees_from_body(body_lines: List[str]) -> List[str]:
    """Quick extraction of function/method calls from body text."""
    call_re = re.compile(r"(?:self\.)?(\w+(?:\.\w+)*)\s*\(")
    seen: set = set()
    result: List[str] = []
    for line in body_lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        for m in call_re.finditer(line):
            name = m.group(1)
            # Skip common builtins / keywords
            if name in ("if", "for", "while", "return", "print", "len",
                        "str", "int", "float", "bool", "list", "dict",
                        "set", "tuple", "range", "super", "isinstance",
                        "hasattr", "getattr", "setattr", "type", "None"):
                continue
            if name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _extract_raises(body_text: str) -> List[str]:
    """Extract exception types raised in a function body."""
    raise_re = re.compile(r"raise\s+(\w+)")
    throws_re = re.compile(r"throw\s+new\s+(\w+)")
    seen: set = set()
    result: List[str] = []
    for m in raise_re.finditer(body_text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    for m in throws_re.finditer(body_text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _extract_symbols_rich(lines: List[str], lang: Optional[str]) -> List[Dict[str, Any]]:
    """Extract symbols including methods within classes (richer than repo_graph parser).

    Returns list of dicts: {name, kind, indent, start_line, end_line, signature, parent}.
    """
    symbols: List[Dict[str, Any]] = []

    if lang in ("python", None):
        class_re = re.compile(r"^(\s*)class\s+(\w+)\s*[:\(]")
        func_re = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)")
    elif lang in ("javascript", "typescript"):
        class_re = re.compile(r"^(\s*)(?:export\s+)?class\s+(\w+)")
        func_re = re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?(?:function\s+)?(\w+)\s*\(([^)]*)\)")
    elif lang == "java":
        class_re = re.compile(r"^(\s*)(?:public|private|protected|abstract|final|static)?\s*class\s+(\w+)")
        func_re = re.compile(r"^(\s*)(?:public|private|protected)?\s*(?:static\s+)?(?:\w[\w<>\[\],\s]*?)\s+(\w+)\s*\(([^)]*)\)")
    elif lang == "go":
        class_re = re.compile(r"^(\s*)type\s+(\w+)\s+struct")
        func_re = re.compile(r"^(\s*)func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(([^)]*)\)")
    elif lang == "rust":
        class_re = re.compile(r"^(\s*)(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)")
        func_re = re.compile(r"^(\s*)(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(([^)]*)\)")
    else:
        class_re = re.compile(r"^(\s*)class\s+(\w+)")
        func_re = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)")

    for i, line in enumerate(lines):
        cm = class_re.match(line)
        if cm:
            symbols.append({
                "name": cm.group(2), "kind": "class",
                "indent": len(cm.group(1)), "start_line": i + 1,
                "end_line": i + 1, "signature": line.strip(), "parent": None,
            })
            continue
        fm = func_re.match(line)
        if fm:
            name = fm.group(2)
            # Skip dunder methods that are noise in compressed view
            if name.startswith("__") and name not in ("__init__", "__call__", "__enter__", "__exit__"):
                continue
            indent = len(fm.group(1))
            kind = "method" if indent > 0 else "function"
            # Find parent class
            parent = None
            for prev in reversed(symbols):
                if prev["kind"] == "class" and prev["indent"] < indent:
                    parent = prev["name"]
                    break
            symbols.append({
                "name": name, "kind": kind,
                "indent": indent, "start_line": i + 1,
                "end_line": i + 1, "signature": line.strip(), "parent": parent,
            })

    # Compute end_line for each symbol
    for idx, sym in enumerate(symbols):
        for nxt in symbols[idx + 1:]:
            if nxt["indent"] <= sym["indent"]:
                sym["end_line"] = nxt["start_line"] - 1
                break
        else:
            sym["end_line"] = len(lines)

    return symbols


def compressed_view(
    workspace: str,
    file_path: str,
    focus: Optional[str] = None,
) -> ToolResult:
    """Compressed view of a file: signatures + call relationships + side effects.

    Saves ~80% tokens vs read_file while preserving structural information.
    Agent uses this FIRST to understand a file, then expand_symbol for details.
    """
    from app.repo_graph.parser import detect_language

    fp = _resolve(workspace, file_path)
    if not fp.is_file():
        return ToolResult(tool_name="compressed_view", success=False,
                          error=f"File not found: {file_path}")

    try:
        source = fp.read_text(errors="replace")
    except OSError as exc:
        return ToolResult(tool_name="compressed_view", success=False, error=str(exc))

    lines = source.split("\n")
    total_lines = len(lines)
    lang = detect_language(str(fp))

    symbols = _extract_symbols_rich(lines, lang)

    if focus:
        focus_lower = focus.lower()
        symbols = [s for s in symbols
                   if focus_lower in s["name"].lower()
                   or (s.get("parent") and focus_lower in s["parent"].lower())]

    ws = Path(workspace).resolve()
    rel_path = str(fp.relative_to(ws))
    header = f"## {rel_path} ({total_lines} lines, {len(symbols)} symbols)"

    output_lines: List[str] = [header, ""]
    for sym in symbols:
        indent = "    " if sym["kind"] == "method" else ""
        sig = sym["signature"]
        output_lines.append(f"{indent}{sig}")

        # Extract body for analysis
        body_start = sym["start_line"] - 1
        body_end = min(sym["end_line"], total_lines)
        body_lines = lines[body_start:body_end]
        body_text = "\n".join(body_lines)

        # Callees
        callees = _extract_callees_from_body(body_lines)
        if callees:
            callee_strs = [f"{c}()" for c in callees[:8]]
            if len(callees) > 8:
                callee_strs.append(f"... +{len(callees) - 8} more")
            output_lines.append(f"{indent}    calls: {', '.join(callee_strs)}")

        # Side effects
        effects = _detect_side_effects(body_text)
        if effects:
            output_lines.append(f"{indent}    side_effects: {', '.join(effects)}")

        # Exceptions raised
        exceptions = _extract_raises(body_text)
        if exceptions:
            output_lines.append(f"{indent}    raises: {', '.join(exceptions)}")

        output_lines.append("")

    return ToolResult(
        tool_name="compressed_view",
        data={"content": "\n".join(output_lines), "path": rel_path,
              "total_lines": total_lines, "symbol_count": len(symbols)},
    )


def module_summary(
    workspace: str,
    module_path: str,
) -> ToolResult:
    """High-level module summary: responsibilities, key services, dependencies.

    Saves ~95% tokens vs reading all files. Results are computed from AST analysis.
    """
    from app.repo_graph.parser import extract_definitions, detect_language

    ws = Path(workspace).resolve()
    mod_dir = _resolve(workspace, module_path)
    if not mod_dir.is_dir():
        return ToolResult(tool_name="module_summary", success=False,
                          error=f"Directory not found: {module_path}")

    # Collect source files (all supported languages)
    _LANG_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cpp"}
    source_files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(mod_dir):
        rel = Path(dirpath).relative_to(ws)
        if _is_excluded(rel.parts):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for f in filenames:
            if Path(f).suffix in _LANG_EXTS:
                source_files.append(Path(dirpath) / f)

    if not source_files:
        return ToolResult(tool_name="module_summary",
                          data={"content": f"## Module: {module_path}\nNo source files found.",
                                "file_count": 0, "loc": 0})

    total_loc = 0
    all_classes: List[str] = []
    all_functions: List[str] = []
    import_modules: set = set()

    for f in source_files[:100]:  # cap for very large modules
        if f.stat().st_size > _MAX_FILE_SIZE:
            continue
        try:
            content = f.read_text(errors="replace")
            total_loc += len(content.splitlines())

            syms = extract_definitions(str(f), f.read_bytes())
            for d in syms.definitions:
                if d.kind == "class":
                    all_classes.append(d.name)
                elif d.kind in ("function", "method"):
                    all_functions.append(d.name)

            # Quick import extraction
            for line in content.splitlines()[:100]:
                stripped = line.strip()
                if stripped.startswith("from ") or stripped.startswith("import "):
                    # Extract module name
                    parts = stripped.split()
                    if len(parts) >= 2:
                        mod = parts[1].split(".")[0]
                        if mod and mod not in (".", ".."):
                            import_modules.add(mod)
        except (OSError, UnicodeDecodeError):
            continue

    # Classify notable symbols
    services = [c for c in all_classes if "Service" in c or "Manager" in c]
    models = [c for c in all_classes
              if any(kw in c for kw in ("Model", "Schema", "Entity", "DTO"))]
    controllers = [c for c in all_classes
                   if any(kw in c for kw in ("Controller", "Router", "Handler", "View"))]
    remaining_classes = [c for c in all_classes
                         if c not in services and c not in models and c not in controllers]

    # Build summary text
    rel_module = str(mod_dir.relative_to(ws))
    lines = [f"## Module: {rel_module} ({len(source_files)} files, {total_loc:,} LOC)", ""]

    if services:
        lines.append(f"Key Services: {', '.join(services[:15])}")
    if models:
        lines.append(f"Key Models: {', '.join(models[:15])}")
    if controllers:
        lines.append(f"Controllers: {', '.join(controllers[:15])}")
    if remaining_classes:
        lines.append(f"Other Classes: {', '.join(remaining_classes[:15])}")

    # Show top functions (by heuristic importance)
    notable_fns = [f for f in all_functions
                   if not f.startswith("_") and f not in ("__init__", "setUp", "tearDown")]
    if notable_fns:
        lines.append(f"Key Functions ({len(notable_fns)} total): {', '.join(notable_fns[:20])}")

    if import_modules:
        lines.append(f"\nExternal Imports: {', '.join(sorted(import_modules)[:20])}")

    # List files
    lines.append(f"\nFiles ({len(source_files)}):")
    for f in sorted(source_files)[:30]:
        try:
            loc = len(f.read_text(errors="replace").splitlines())
            lines.append(f"  {f.relative_to(ws)} ({loc} lines)")
        except OSError:
            lines.append(f"  {f.relative_to(ws)}")
    if len(source_files) > 30:
        lines.append(f"  ... and {len(source_files) - 30} more files")

    return ToolResult(
        tool_name="module_summary",
        data={"content": "\n".join(lines), "file_count": len(source_files),
              "loc": total_loc},
    )


def expand_symbol(
    workspace: str,
    symbol_name: str,
    file_path: Optional[str] = None,
) -> ToolResult:
    """Expand a symbol to its full source code.

    Agent workflow: compressed_view → identify symbol → expand_symbol.
    This implements the "compress first, expand on demand" principle.
    """
    from app.repo_graph.parser import extract_definitions

    ws = Path(workspace).resolve()

    # If file_path is provided, search within that file
    if file_path:
        fp = _resolve(workspace, file_path)
        if not fp.is_file():
            return ToolResult(tool_name="expand_symbol", success=False,
                              error=f"File not found: {file_path}")

        syms = extract_definitions(str(fp), fp.read_bytes())
        matches = [s for s in syms.definitions if s.name == symbol_name]
        if not matches:
            # Try substring match
            matches = [s for s in syms.definitions
                       if symbol_name.lower() in s.name.lower()]

        if not matches:
            available = [s.name for s in syms.definitions][:20]
            return ToolResult(
                tool_name="expand_symbol", success=False,
                error=f"Symbol '{symbol_name}' not found in {file_path}. "
                      f"Available: {', '.join(available)}",
            )

        sym = matches[0]
        try:
            source = fp.read_text(errors="replace")
            lines = source.split("\n")
            body = "\n".join(lines[sym.start_line - 1 : sym.end_line])
        except OSError as exc:
            return ToolResult(tool_name="expand_symbol", success=False, error=str(exc))

        rel = str(fp.relative_to(ws))
        return ToolResult(
            tool_name="expand_symbol",
            data={
                "symbol_name": sym.name,
                "kind": sym.kind,
                "file_path": rel,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "signature": sym.signature,
                "source": body,
            },
        )

    # No file_path — search the entire workspace
    candidates = []
    for dirpath, dirnames, filenames in os.walk(ws):
        rel_dir = Path(dirpath).relative_to(ws)
        if _is_excluded(rel_dir.parts):
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for f in filenames:
            fpath = Path(dirpath) / f
            if fpath.suffix not in (".py", ".js", ".jsx", ".ts", ".tsx",
                                     ".java", ".go", ".rs", ".c", ".cpp"):
                continue
            if fpath.stat().st_size > _MAX_FILE_SIZE:
                continue
            try:
                syms = extract_definitions(str(fpath), fpath.read_bytes())
                for s in syms.definitions:
                    if s.name == symbol_name:
                        candidates.append((s, fpath))
                    elif symbol_name.lower() in s.name.lower() and not candidates:
                        candidates.append((s, fpath))
            except (OSError, UnicodeDecodeError):
                continue
            if len(candidates) >= 5:
                break
        if len(candidates) >= 5:
            break

    if not candidates:
        return ToolResult(
            tool_name="expand_symbol", success=False,
            error=f"Symbol '{symbol_name}' not found in the workspace.",
        )

    sym, fpath = candidates[0]
    try:
        source = fpath.read_text(errors="replace")
        lines = source.split("\n")
        body = "\n".join(lines[sym.start_line - 1 : sym.end_line])
    except OSError as exc:
        return ToolResult(tool_name="expand_symbol", success=False, error=str(exc))

    rel = str(fpath.relative_to(ws))
    data = {
        "symbol_name": sym.name,
        "kind": sym.kind,
        "file_path": rel,
        "start_line": sym.start_line,
        "end_line": sym.end_line,
        "signature": sym.signature,
        "source": body,
    }

    # If multiple candidates, show alternatives
    if len(candidates) > 1:
        data["alternatives"] = [
            {"name": s.name, "file_path": str(fp.relative_to(ws)),
             "kind": s.kind, "line": s.start_line}
            for s, fp in candidates[1:5]
        ]

    return ToolResult(tool_name="expand_symbol", data=data)


# ---------------------------------------------------------------------------
# detect_patterns — architectural pattern scanner
# ---------------------------------------------------------------------------

# Each category maps to a list of (compiled_regex, description) tuples.
# Patterns are applied per-line for speed.
_PATTERN_CATEGORIES = {
    "webhook": [
        (re.compile(r"(?i)@(post|put|delete|patch)mapping\b.*(?:callback|hook|notify|webhook)"), "webhook endpoint"),
        (re.compile(r"(?i)app\.(post|put)\(.*(?:callback|hook|notify|webhook)"), "webhook route"),
        (re.compile(r"(?i)router\.(post|put)\(.*(?:callback|hook|notify|webhook)"), "webhook route"),
        (re.compile(r"(?i)def\s+\w*(?:callback|hook|webhook|notify)\w*\s*\("), "webhook/callback handler"),
        (re.compile(r"(?i)(?:on_?event|event_?handler|subscribe|add_?listener)\s*\("), "event listener"),
        (re.compile(r"(?i)httpx?\.(post|put)\(.*(?:callback|hook|notify)"), "outbound webhook call"),
        (re.compile(r"(?i)requests\.(post|put)\(.*(?:callback|hook|notify)"), "outbound webhook call"),
    ],
    "queue": [
        (re.compile(r"(?i)@(?:rabbit|sqs|kafka|jms)listener\b"), "queue consumer annotation"),
        (re.compile(r"(?i)\b(?:consume|consumer|subscriber|on_message)\s*\("), "queue consumer"),
        (re.compile(r"(?i)\b(?:publish|produce|send_message|enqueue)\s*\("), "queue producer"),
        (re.compile(r"(?i)(?:kafka|sqs|rabbit|amqp|pubsub|celery|rq)\."), "message queue usage"),
        (re.compile(r"(?i)channel\.(basic_consume|basic_publish|queue_declare)"), "AMQP channel op"),
        (re.compile(r"(?i)@app\.task|@shared_task|@celery\.task"), "Celery task"),
    ],
    "retry": [
        (re.compile(r"(?i)@retry\b|@backoff\b|@retrying\b"), "retry decorator"),
        (re.compile(r"(?i)\bretry[\s_]*(count|max|limit|attempts)\b"), "retry config"),
        (re.compile(r"(?i)\b(exponential_?backoff|backoff_?factor|retry_?delay)\b"), "backoff config"),
        (re.compile(r"(?i)for\s+\w+\s+in\s+range\(.*retry"), "retry loop"),
        (re.compile(r"(?i)while\s+.*(?:retries?|attempts?)\s*[<>]"), "retry while-loop"),
        (re.compile(r"(?i)Retrying|tenacity\.retry|urllib3\.util\.retry"), "retry library"),
    ],
    "lock": [
        (re.compile(r"(?i)\b(acquire|release)\s*\(\s*\)"), "lock acquire/release"),
        (re.compile(r"(?i)\b(Lock|RLock|Semaphore|Mutex|ReentrantLock)\s*\("), "lock creation"),
        (re.compile(r"(?i)with\s+\w*lock"), "lock context manager"),
        (re.compile(r"(?i)synchronized\b"), "synchronized block (Java)"),
        (re.compile(r"(?i)\b(redis|distributed)[\s_]*lock\b"), "distributed lock"),
        (re.compile(r"(?i)SELECT\s+.*\s+FOR\s+UPDATE"), "SELECT FOR UPDATE"),
        (re.compile(r"(?i)\.lock\(\)|\.tryLock\(|\.unlock\("), "lock method call"),
    ],
    "check_then_act": [
        (re.compile(r"(?i)if\s+.*(?:exists?|is_?available|has_?\w+|count)\s*[:(].*\n\s*(?:create|insert|save|update|delete|remove)"), "check-then-act (multi-line)"),
        (re.compile(r"(?i)if\s+not\s+.*(?:exists?|find|get)\b.*:\s*$"), "check-then-act guard"),
        (re.compile(r"(?i)\.get_or_create\b|\.find_or_create\b|\.upsert\b"), "atomic alternative (good)"),
        (re.compile(r"(?i)if\s+.*is\s+None.*:\s*\n\s*\w+\s*="), "null-check-then-assign"),
    ],
    "transaction": [
        (re.compile(r"(?i)@transactional\b"), "transaction annotation"),
        (re.compile(r"(?i)\b(begin|commit|rollback)\s*\("), "transaction boundary"),
        (re.compile(r"(?i)with\s+.*(?:transaction|session|atomic)\b"), "transaction context"),
        (re.compile(r"(?i)(?:connection|session|db)\.(begin|commit|rollback)"), "explicit transaction"),
        (re.compile(r"(?i)auto_?commit\s*=\s*(True|true|1)"), "auto-commit enabled (risky)"),
        (re.compile(r"(?i)savepoint\b"), "savepoint"),
    ],
    "token_lifecycle": [
        (re.compile(r"(?i)\b(generate|create|issue)[\s_]*(token|jwt|session)\b"), "token creation"),
        (re.compile(r"(?i)\b(validate|verify|decode)[\s_]*(token|jwt)\b"), "token validation"),
        (re.compile(r"(?i)\b(refresh|renew|rotate)[\s_]*(token|jwt|session)\b"), "token refresh"),
        (re.compile(r"(?i)\b(revoke|invalidate|expire|blacklist)[\s_]*(token|jwt|session)\b"), "token revocation"),
        (re.compile(r"(?i)token[\s_]*(expir|ttl|lifetime|max_?age)\b"), "token expiry config"),
    ],
    "side_effect_chain": [
        (re.compile(r"(?i)\b(send_?email|send_?notification|send_?sms|notify)\s*\("), "notification side effect"),
        (re.compile(r"(?i)\b(audit_?log|log_?event|track|emit_?event)\s*\("), "audit/event side effect"),
        (re.compile(r"(?i)\b(charge|refund|transfer|debit|credit)\s*\("), "financial side effect"),
        (re.compile(r"(?i)\b(upload|write_?file|s3\.put|blob\.upload)\s*\("), "storage side effect"),
        (re.compile(r"(?i)\bhttpx?\.(post|put|delete|patch)\b"), "outbound HTTP side effect"),
        (re.compile(r"(?i)\brequests\.(post|put|delete|patch)\b"), "outbound HTTP side effect"),
    ],
}


def detect_patterns(
    workspace: str,
    path: Optional[str] = None,
    categories: Optional[List[str]] = None,
    max_results: int = 50,
) -> ToolResult:
    """Scan files for architectural patterns.

    Returns a list of detected pattern matches grouped by category.
    """
    ws = Path(workspace).resolve()
    scan_root = _resolve(workspace, path) if path else ws

    if not scan_root.exists():
        return ToolResult(
            tool_name="detect_patterns", success=False,
            error=f"Path not found: {path or '.'}",
        )

    # Filter categories
    active_categories = _PATTERN_CATEGORIES
    if categories:
        valid = {c for c in categories if c in _PATTERN_CATEGORIES}
        if not valid:
            return ToolResult(
                tool_name="detect_patterns", success=False,
                error=f"Unknown categories: {categories}. "
                f"Valid: {sorted(_PATTERN_CATEGORIES.keys())}",
            )
        active_categories = {k: v for k, v in _PATTERN_CATEGORIES.items() if k in valid}

    results_by_category: Dict[str, List[dict]] = {}
    total_matches = 0

    # Collect files to scan
    files_to_scan: List[Path] = []
    if scan_root.is_file():
        files_to_scan.append(scan_root)
    else:
        for dirpath, dirnames, filenames in os.walk(scan_root):
            rel = Path(dirpath).relative_to(ws)
            if _is_excluded(rel.parts):
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                # Skip binary/large files
                try:
                    if fpath.stat().st_size > _MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                # Only scan source-like files
                ext = fpath.suffix.lower()
                if ext in {
                    ".py", ".java", ".kt", ".scala", ".go", ".rs",
                    ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
                    ".rb", ".php", ".cs", ".cpp", ".c", ".h",
                    ".yaml", ".yml", ".toml", ".properties",
                }:
                    files_to_scan.append(fpath)

    for fpath in files_to_scan:
        if total_matches >= max_results:
            break
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = content.split("\n")
        rel_path = str(fpath.relative_to(ws))

        for cat_name, patterns in active_categories.items():
            if total_matches >= max_results:
                break
            for line_num, line in enumerate(lines, 1):
                if total_matches >= max_results:
                    break
                for pat, desc in patterns:
                    if pat.search(line):
                        cat_list = results_by_category.setdefault(cat_name, [])
                        cat_list.append({
                            "file": rel_path,
                            "line": line_num,
                            "pattern": desc,
                            "snippet": line.strip()[:200],
                        })
                        total_matches += 1
                        break  # one match per line per category

    # Build summary
    summary = {
        cat: len(matches)
        for cat, matches in results_by_category.items()
    }

    data = {
        "summary": summary,
        "total_matches": total_matches,
        "categories_scanned": sorted(active_categories.keys()),
        "files_scanned": len(files_to_scan),
        "matches": results_by_category,
    }
    truncated = total_matches >= max_results

    return ToolResult(
        tool_name="detect_patterns",
        data=data,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Test execution (verification tool)
# ---------------------------------------------------------------------------


def _detect_test_runner(workspace: str, test_file: str) -> tuple:
    """Detect the test framework and build the run command.

    Returns (command_list, description) or raises ValueError.
    """
    ext = Path(test_file).suffix.lower()
    name = Path(test_file).name

    if ext == ".py":
        # Python: prefer pytest, fall back to unittest
        return ["python", "-m", "pytest", "-x", "-q"], "pytest"
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        # JS/TS: check for common runners
        if (Path(workspace) / "node_modules" / ".bin" / "jest").exists():
            return ["npx", "jest", "--no-coverage"], "jest"
        elif (Path(workspace) / "node_modules" / ".bin" / "vitest").exists():
            return ["npx", "vitest", "run"], "vitest"
        return ["npx", "jest", "--no-coverage"], "jest"
    elif ext == ".go":
        return ["go", "test", "-v", "-run"], "go test"
    elif ext == ".java":
        if (Path(workspace) / "pom.xml").exists():
            return ["mvn", "-pl", ".", "test", "-Dtest="], "maven"
        elif (Path(workspace) / "build.gradle").exists():
            return ["./gradlew", "test", "--tests"], "gradle"
        return ["mvn", "-pl", ".", "test", "-Dtest="], "maven"
    elif ext == ".rs":
        return ["cargo", "test"], "cargo test"
    else:
        raise ValueError(f"Unsupported test file extension: {ext}")


def run_test(
    workspace: str,
    test_file: str,
    test_name: Optional[str] = None,
    timeout: int = 30,
) -> ToolResult:
    """Run a specific test file or test function and return the result.

    This is a verification tool — use it to confirm a suspected bug by
    running the relevant test and checking if it passes or fails.
    """
    fp = _resolve(workspace, test_file)
    if not fp.exists():
        return ToolResult(
            tool_name="run_test", success=False,
            error=f"Test file not found: {test_file}",
        )

    try:
        base_cmd, runner = _detect_test_runner(workspace, test_file)
    except ValueError as e:
        return ToolResult(tool_name="run_test", success=False, error=str(e))

    # Build the command
    if runner == "pytest":
        target = str(fp)
        if test_name:
            target += f"::{test_name}"
        cmd = base_cmd + [target, f"--timeout={timeout}"]
    elif runner == "go test":
        cmd = base_cmd + [test_name or ".", f"-timeout={timeout}s"]
        # go test needs the package path
        cmd = ["go", "test", "-v", "-run", test_name or ".", str(fp.parent)]
    elif runner in ("maven", "gradle"):
        test_spec = test_name or Path(test_file).stem
        cmd = base_cmd[:-1] + [base_cmd[-1] + test_spec]
    elif runner == "cargo test":
        cmd = base_cmd + [test_name or ""]
    else:
        # jest/vitest
        target = str(fp)
        cmd = base_cmd + [target]
        if test_name:
            cmd += ["-t", test_name]

    try:
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = proc.stdout[-3000:] if len(proc.stdout) > 3000 else proc.stdout
        stderr = proc.stderr[-1000:] if len(proc.stderr) > 1000 else proc.stderr

        passed = proc.returncode == 0

        return ToolResult(
            tool_name="run_test",
            data={
                "passed": passed,
                "return_code": proc.returncode,
                "runner": runner,
                "test_file": test_file,
                "test_name": test_name,
                "output": output,
                "stderr": stderr if not passed else "",
            },
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            tool_name="run_test",
            data={
                "passed": False,
                "return_code": -1,
                "runner": runner,
                "test_file": test_file,
                "test_name": test_name,
                "output": f"Test timed out after {timeout}s",
                "stderr": "",
            },
        )
    except FileNotFoundError as e:
        return ToolResult(
            tool_name="run_test", success=False,
            error=f"Test runner not found: {e}",
        )
    except OSError as exc:
        return ToolResult(
            tool_name="run_test", success=False,
            error=f"Test execution failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Git hotspots
# ---------------------------------------------------------------------------


def git_hotspots(
    workspace: str,
    days: int = 90,
    top_n: int = 15,
) -> ToolResult:
    """Analyze git history to find frequently changed files (hotspots).

    Returns both all-time hotspots over the given *days* window and a
    "recently active" subset (last 7 days) so the caller can distinguish
    chronic churn from current activity.
    """
    days = max(1, int(days))
    top_n = max(1, min(int(top_n), 100))

    # --- hotspots over the full window ---
    raw = _run_git(
        workspace,
        ["log", f"--since={days} days ago", "--name-only", "--pretty=format:"],
        max_output=200_000,
    )
    counts: Counter = Counter()
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line:
            counts[line] += 1

    hotspots = [
        {"file": f, "change_count": c}
        for f, c in counts.most_common(top_n)
    ]

    # --- recently active (last 7 days) ---
    raw_recent = _run_git(
        workspace,
        ["log", "--since=7 days ago", "--name-only", "--pretty=format:"],
        max_output=100_000,
    )
    recent_counts: Counter = Counter()
    for line in raw_recent.strip().split("\n"):
        line = line.strip()
        if line:
            recent_counts[line] += 1

    recently_active = [
        {"file": f, "change_count": c}
        for f, c in recent_counts.most_common(top_n)
    ]

    return ToolResult(
        tool_name="git_hotspots",
        data={
            "hotspots": hotspots,
            "recently_active": recently_active,
            "period_days": days,
        },
    )


# ---------------------------------------------------------------------------
# Endpoint extraction
# ---------------------------------------------------------------------------

# Pre-compiled patterns for route detection across frameworks.
_ENDPOINT_PATTERNS: List[tuple] = [
    # Python Flask/FastAPI — @app.get("/path") or @router.post("/path")
    (re.compile(
        r'@(?:app|router)\.(get|post|put|delete|patch|options|head)\s*\(\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ), "fastapi/flask"),
    # Python @app.route("/path", methods=[...])
    (re.compile(
        r'@(?:app|blueprint|bp)\s*\.\s*route\s*\(\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ), "flask-route"),
    # Django path() / url()
    (re.compile(
        r"""(?:path|url)\s*\(\s*[r]?['"]([^'"]+)['"]""",
    ), "django"),
    # Django REST @api_view
    (re.compile(
        r'@api_view\s*\(\s*\[([^\]]*)\]',
    ), "django-rest"),
    # Java Spring — @GetMapping("/path")
    (re.compile(
        r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']?([^"\')\s,]+)',
    ), "spring"),
    # JS/TS Express — router.get("/path") or app.post("/path")
    (re.compile(
        r'(?:router|app)\.(get|post|put|delete|patch|all|use)\s*\(\s*["\']([^"\']+)["\']',
    ), "express"),
    # Go — r.GET("/path", ...) or http.HandleFunc("/path", ...)
    (re.compile(
        r'(?:r|router|mux)\.(GET|POST|PUT|DELETE|PATCH|Handle|HandleFunc)\s*\(\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    ), "go"),
]


def list_endpoints(
    workspace: str,
    path: Optional[str] = None,
    max_results: int = 100,
) -> ToolResult:
    """Extract API endpoints/routes from the codebase.

    Scans source files for route decorator patterns across Python, Java,
    JS/TS, and Go frameworks.
    """
    ws = Path(workspace).resolve()
    scan_root = _resolve(workspace, path) if path else ws
    if not scan_root.exists():
        return ToolResult(
            tool_name="list_endpoints", success=False,
            error=f"Path not found: {path or '.'}",
        )

    max_results = max(1, min(int(max_results), 500))

    source_exts = {
        ".py", ".java", ".kt", ".scala", ".go",
        ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    }

    files_to_scan: List[Path] = []
    if scan_root.is_file():
        files_to_scan.append(scan_root)
    else:
        for dirpath, dirnames, filenames in os.walk(scan_root):
            rel = Path(dirpath).relative_to(ws)
            if _is_excluded(rel.parts):
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if fpath.suffix.lower() in source_exts:
                    try:
                        if fpath.stat().st_size > _MAX_FILE_SIZE:
                            continue
                    except OSError:
                        continue
                    files_to_scan.append(fpath)

    endpoints: List[Dict] = []
    for fpath in files_to_scan:
        if len(endpoints) >= max_results:
            break
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel_path = str(fpath.relative_to(ws))
        lines = content.split("\n")

        for line_num, line in enumerate(lines, 1):
            if len(endpoints) >= max_results:
                break
            for pat, framework in _ENDPOINT_PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                groups = m.groups()
                if framework == "flask-route":
                    # @app.route — method not in regex, default GET
                    route_path = groups[0]
                    method = "GET"
                elif framework == "django":
                    route_path = groups[0]
                    method = "ANY"
                elif framework == "django-rest":
                    methods_str = groups[0].replace("'", "").replace('"', "")
                    method = methods_str.strip()
                    route_path = ""
                elif framework == "spring":
                    verb = groups[0].upper()
                    route_path = groups[1] if len(groups) > 1 else ""
                    method = {"REQUEST": "ANY"}.get(verb, verb.replace("MAPPING", ""))
                else:
                    # fastapi/flask, express, go — group 0 is method, group 1 is path
                    method = groups[0].upper()
                    route_path = groups[1] if len(groups) > 1 else ""

                endpoints.append({
                    "method": method,
                    "path": route_path,
                    "file": rel_path,
                    "line": line_num,
                    "framework": framework,
                })
                break  # one match per line

    return ToolResult(
        tool_name="list_endpoints",
        data={"endpoints": endpoints},
        truncated=len(endpoints) >= max_results,
    )


# ---------------------------------------------------------------------------
# Docstring extraction
# ---------------------------------------------------------------------------

# Patterns per language family
_PY_DEF_RE = re.compile(r'^\s*((?:async\s+)?def|class)\s+(\w+)')
_PY_DOCSTRING_START_RE = re.compile(r'''^\s*("""|\'\'\'|r"""|r\'\'\')(.*)''')
_JSDOC_BLOCK_START_RE = re.compile(r'^\s*/\*\*')
_JSDOC_BLOCK_END_RE = re.compile(r'\*/')
_JS_DECL_RE = re.compile(
    r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?'
    r'(?:function\s+(\w+)|class\s+(\w+)|(?:const|let|var)\s+(\w+))',
)
_GO_COMMENT_RE = re.compile(r'^\s*//\s?(.*)')
_GO_FUNC_RE = re.compile(r'^func\s+(?:\([^)]+\)\s+)?(\w+)')


def extract_docstrings(
    workspace: str,
    path: str,
    symbol_name: Optional[str] = None,
) -> ToolResult:
    """Extract function/class-level documentation from a file.

    Supports Python docstrings, JS/TS/Java JSDoc blocks, and Go doc comments.
    """
    fp = _resolve(workspace, path)
    if not fp.is_file():
        return ToolResult(
            tool_name="extract_docstrings", success=False,
            error=f"File not found: {path}",
        )

    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(
            tool_name="extract_docstrings", success=False,
            error=str(exc),
        )

    ws = Path(workspace).resolve()
    rel_path = str(fp.relative_to(ws))
    ext = fp.suffix.lower()
    lines = content.split("\n")

    docstrings: List[Dict] = []

    if ext == ".py":
        docstrings = _extract_py_docstrings(lines, rel_path)
    elif ext in {".go"}:
        docstrings = _extract_go_docstrings(lines, rel_path)
    elif ext in {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".java", ".kt"}:
        docstrings = _extract_jsdoc_docstrings(lines, rel_path)

    if symbol_name:
        docstrings = [d for d in docstrings if d["symbol"] == symbol_name]

    return ToolResult(
        tool_name="extract_docstrings",
        data={"docstrings": docstrings},
    )


def _extract_py_docstrings(lines: List[str], rel_path: str) -> List[Dict]:
    """Extract Python triple-quote docstrings following def/class."""
    results: List[Dict] = []
    i = 0
    while i < len(lines):
        m = _PY_DEF_RE.match(lines[i])
        if m:
            kind = "function" if "def" in m.group(1) else "class"
            name = m.group(2)
            def_line = i + 1

            # Advance past the full signature to find the colon ending it.
            # For single-line defs the colon is on the same line; for
            # multi-line signatures we must scan forward.
            j = i
            while j < len(lines):
                if re.search(r':\s*(?:#.*)?$', lines[j]):
                    j += 1  # move past the colon line
                    break
                j += 1

            # Skip blank lines between signature and body
            while j < len(lines) and lines[j].strip() == "":
                j += 1

            if j < len(lines):
                dm = _PY_DOCSTRING_START_RE.match(lines[j])
                if dm:
                    quote = dm.group(1).replace("r", "")
                    doc_lines = [dm.group(2)]
                    if quote in lines[j][lines[j].index(quote) + len(quote):]:
                        # Single-line docstring
                        rest = lines[j][lines[j].index(quote) + len(quote):]
                        end_idx = rest.index(quote)
                        doc_text = rest[:end_idx].strip()
                    else:
                        k = j + 1
                        while k < len(lines) and quote not in lines[k]:
                            doc_lines.append(lines[k])
                            k += 1
                        if k < len(lines):
                            doc_lines.append(lines[k].split(quote)[0])
                        doc_text = "\n".join(doc_lines).strip()

                    results.append({
                        "symbol": name,
                        "kind": kind,
                        "file": rel_path,
                        "line": def_line,
                        "docstring": doc_text[:2000],
                    })
        i += 1
    return results


def _extract_jsdoc_docstrings(lines: List[str], rel_path: str) -> List[Dict]:
    """Extract JSDoc/Javadoc /** ... */ blocks before function/class declarations."""
    results: List[Dict] = []
    i = 0
    while i < len(lines):
        if _JSDOC_BLOCK_START_RE.match(lines[i]):
            doc_lines = []
            j = i
            # Collect the entire block
            while j < len(lines):
                doc_lines.append(lines[j])
                if _JSDOC_BLOCK_END_RE.search(lines[j]) and j > i:
                    break
                if j == i and _JSDOC_BLOCK_END_RE.search(lines[j]):
                    break
                j += 1

            doc_text = "\n".join(doc_lines).strip()
            doc_start = i + 1

            # Look ahead for a declaration
            k = j + 1
            while k < len(lines) and lines[k].strip() == "":
                k += 1

            if k < len(lines):
                dm = _JS_DECL_RE.match(lines[k])
                if dm:
                    name = dm.group(1) or dm.group(2) or dm.group(3)
                    kind = "class" if dm.group(2) else "function"
                    results.append({
                        "symbol": name,
                        "kind": kind,
                        "file": rel_path,
                        "line": k + 1,
                        "docstring": doc_text[:2000],
                    })
                elif "@" in lines[k]:
                    # Java annotation — look one more line
                    k2 = k + 1
                    while k2 < len(lines) and lines[k2].strip().startswith("@"):
                        k2 += 1
                    if k2 < len(lines):
                        dm2 = re.match(
                            r'\s*(?:public|private|protected|static|final|abstract|\s)*'
                            r'(?:class|interface|enum)\s+(\w+)',
                            lines[k2],
                        )
                        if dm2:
                            results.append({
                                "symbol": dm2.group(1),
                                "kind": "class",
                                "file": rel_path,
                                "line": k2 + 1,
                                "docstring": doc_text[:2000],
                            })
                        else:
                            # Possibly a method
                            dm3 = re.match(
                                r'\s*(?:public|private|protected|static|final|abstract|\s)*'
                                r'\w+\s+(\w+)\s*\(',
                                lines[k2],
                            )
                            if dm3:
                                results.append({
                                    "symbol": dm3.group(1),
                                    "kind": "function",
                                    "file": rel_path,
                                    "line": k2 + 1,
                                    "docstring": doc_text[:2000],
                                })

            i = j + 1
        else:
            i += 1
    return results


def _extract_go_docstrings(lines: List[str], rel_path: str) -> List[Dict]:
    """Extract Go doc comments (// blocks before func declarations)."""
    results: List[Dict] = []
    i = 0
    while i < len(lines):
        gm = _GO_FUNC_RE.match(lines[i])
        if gm:
            func_name = gm.group(1)
            # Look backward for consecutive // comment lines
            doc_lines = []
            j = i - 1
            while j >= 0:
                cm = _GO_COMMENT_RE.match(lines[j])
                if cm:
                    doc_lines.insert(0, cm.group(1))
                    j -= 1
                elif lines[j].strip() == "":
                    j -= 1
                else:
                    break

            if doc_lines:
                results.append({
                    "symbol": func_name,
                    "kind": "function",
                    "file": rel_path,
                    "line": i + 1,
                    "docstring": "\n".join(doc_lines).strip()[:2000],
                })
        i += 1
    return results


# ---------------------------------------------------------------------------
# Database schema extraction
# ---------------------------------------------------------------------------

# Patterns for ORM model detection
_ORM_CLASS_PATTERNS = [
    # Python SQLAlchemy / Flask-SQLAlchemy
    re.compile(r'class\s+(\w+)\s*\(.*(?:Base|db\.Model|DeclarativeBase|Model)\s*.*\)'),
    # Python Django
    re.compile(r'class\s+(\w+)\s*\(.*models\.Model.*\)'),
]
_TABLE_NAME_PATTERNS = [
    # SQLAlchemy __tablename__
    re.compile(r'__tablename__\s*=\s*["\'](\w+)["\']'),
    # Java @Table(name = "...")
    re.compile(r'@Table\s*\(\s*(?:name\s*=\s*)?["\'](\w+)["\']'),
]
_FIELD_PATTERNS = [
    # SQLAlchemy Column(Type, ...)  or mapped_column(Type, ...)
    (re.compile(r'(\w+)\s*[=:]\s*(?:Column|mapped_column)\s*\(\s*(\w+)'), "sqlalchemy"),
    # Django models.Field
    (re.compile(r'(\w+)\s*=\s*models\.(\w+)\s*\('), "django"),
    # Java JPA @Column on a field
    (re.compile(r'(?:private|protected|public)\s+(\w+(?:<[^>]+>)?)\s+(\w+)\s*;'), "jpa"),
    # TypeORM @Column()
    (re.compile(r'(\w+)\s*[?!]?\s*:\s*(\w+)'), "typeorm"),
]
_JAVA_ENTITY_RE = re.compile(r'@Entity')
_JAVA_CLASS_RE = re.compile(r'(?:public\s+)?class\s+(\w+)')
_TS_ENTITY_RE = re.compile(r'@Entity\s*\(')
_TS_CLASS_RE = re.compile(r'(?:export\s+)?class\s+(\w+)')


def db_schema(
    workspace: str,
    path: Optional[str] = None,
    max_results: int = 50,
) -> ToolResult:
    """Extract database schema information from ORM model files.

    Scans for SQLAlchemy, Django, JPA, and TypeORM patterns and returns
    model names, table names, and field definitions.
    """
    ws = Path(workspace).resolve()
    scan_root = _resolve(workspace, path) if path else ws
    if not scan_root.exists():
        return ToolResult(
            tool_name="db_schema", success=False,
            error=f"Path not found: {path or '.'}",
        )

    max_results = max(1, min(int(max_results), 200))

    source_exts = {
        ".py", ".java", ".kt", ".scala",
        ".js", ".ts", ".jsx", ".tsx",
    }

    files_to_scan: List[Path] = []
    if scan_root.is_file():
        files_to_scan.append(scan_root)
    else:
        for dirpath, dirnames, filenames in os.walk(scan_root):
            rel = Path(dirpath).relative_to(ws)
            if _is_excluded(rel.parts):
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if fpath.suffix.lower() in source_exts:
                    try:
                        if fpath.stat().st_size > _MAX_FILE_SIZE:
                            continue
                    except OSError:
                        continue
                    files_to_scan.append(fpath)

    models: List[Dict] = []

    for fpath in files_to_scan:
        if len(models) >= max_results:
            break
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel_path = str(fpath.relative_to(ws))
        lines = content.split("\n")
        ext = fpath.suffix.lower()

        if ext == ".py":
            models.extend(_extract_py_orm_models(lines, rel_path, max_results - len(models)))
        elif ext in {".java", ".kt"}:
            models.extend(_extract_jpa_models(lines, rel_path, max_results - len(models)))
        elif ext in {".ts", ".js", ".tsx", ".jsx"}:
            models.extend(_extract_typeorm_models(lines, rel_path, max_results - len(models)))

    return ToolResult(
        tool_name="db_schema",
        data={"models": models},
        truncated=len(models) >= max_results,
    )


def _extract_py_orm_models(
    lines: List[str], rel_path: str, limit: int,
) -> List[Dict]:
    """Extract Python ORM model definitions (SQLAlchemy / Django)."""
    results: List[Dict] = []
    i = 0
    while i < len(lines) and len(results) < limit:
        model_name = None
        for pat in _ORM_CLASS_PATTERNS:
            m = pat.match(lines[i])
            if m:
                model_name = m.group(1)
                break

        if model_name:
            class_line = i + 1
            table_name = None
            fields: List[Dict] = []

            # Scan the class body (indented lines after the class declaration)
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if stripped == "" or lines[j][0:1] in (" ", "\t"):
                    # Check for __tablename__
                    for tp in _TABLE_NAME_PATTERNS:
                        tm = tp.search(lines[j])
                        if tm:
                            table_name = tm.group(1)

                    # Check for field definitions
                    for fp, framework in _FIELD_PATTERNS[:2]:  # sqlalchemy, django only
                        fm = fp.search(lines[j])
                        if fm:
                            fname = fm.group(1)
                            ftype = fm.group(2)
                            if not fname.startswith("_"):
                                fields.append({
                                    "name": fname,
                                    "type": ftype,
                                    "line": j + 1,
                                })
                    j += 1
                elif stripped and not stripped.startswith("#") and not stripped.startswith("@"):
                    break
                else:
                    j += 1

            results.append({
                "name": model_name,
                "table_name": table_name or model_name.lower(),
                "file": rel_path,
                "line": class_line,
                "fields": fields,
            })
            i = j
        else:
            i += 1

    return results


def _extract_jpa_models(
    lines: List[str], rel_path: str, limit: int,
) -> List[Dict]:
    """Extract Java/Kotlin JPA entity definitions."""
    results: List[Dict] = []
    i = 0
    while i < len(lines) and len(results) < limit:
        if _JAVA_ENTITY_RE.search(lines[i]):
            # Scan forward for @Table and class declaration
            table_name = None
            class_name = None
            class_line = i + 1
            j = i + 1
            while j < len(lines) and j < i + 10:
                for tp in _TABLE_NAME_PATTERNS:
                    tm = tp.search(lines[j])
                    if tm:
                        table_name = tm.group(1)
                cm = _JAVA_CLASS_RE.match(lines[j])
                if cm:
                    class_name = cm.group(1)
                    class_line = j + 1
                    break
                j += 1

            if class_name:
                fields: List[Dict] = []
                # Scan class body for fields
                k = j + 1
                brace_depth = 0
                for k_line in range(j, len(lines)):
                    brace_depth += lines[k_line].count("{") - lines[k_line].count("}")
                    if brace_depth <= 0 and k_line > j:
                        break
                    fm = _FIELD_PATTERNS[2][0].search(lines[k_line])
                    if fm:
                        fields.append({
                            "name": fm.group(2),
                            "type": fm.group(1),
                            "line": k_line + 1,
                        })

                results.append({
                    "name": class_name,
                    "table_name": table_name or class_name.lower(),
                    "file": rel_path,
                    "line": class_line,
                    "fields": fields,
                })
            i = j + 1
        else:
            i += 1

    return results


def _extract_typeorm_models(
    lines: List[str], rel_path: str, limit: int,
) -> List[Dict]:
    """Extract TypeORM entity definitions."""
    results: List[Dict] = []
    i = 0
    while i < len(lines) and len(results) < limit:
        if _TS_ENTITY_RE.search(lines[i]):
            # Look for class declaration nearby
            j = i + 1
            while j < len(lines) and j < i + 5:
                cm = _TS_CLASS_RE.match(lines[j])
                if cm:
                    class_name = cm.group(1)
                    class_line = j + 1
                    fields: List[Dict] = []

                    # Scan body — look for @Column() annotations followed by field
                    k = j + 1
                    brace_depth = 0
                    for k_line in range(j, len(lines)):
                        brace_depth += lines[k_line].count("{") - lines[k_line].count("}")
                        if brace_depth <= 0 and k_line > j:
                            break
                        if re.search(r'@Column\s*\(', lines[k_line]):
                            # Next line should have the field
                            if k_line + 1 < len(lines):
                                fm = _FIELD_PATTERNS[3][0].search(lines[k_line + 1])
                                if fm:
                                    fields.append({
                                        "name": fm.group(1),
                                        "type": fm.group(2),
                                        "line": k_line + 2,
                                    })

                    results.append({
                        "name": class_name,
                        "table_name": class_name.lower(),
                        "file": rel_path,
                        "line": class_line,
                        "fields": fields,
                    })
                    i = k_line + 1
                    break
                j += 1
            else:
                i = j
        else:
            i += 1

    return results


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_REGISTRY = {
    "grep": grep,
    "read_file": read_file,
    "list_files": list_files,
    "glob": glob_files,
    "find_symbol": find_symbol,
    "find_references": find_references,
    "file_outline": file_outline,
    "get_dependencies": get_dependencies,
    "get_dependents": get_dependents,
    "git_log": git_log,
    "git_diff": git_diff,
    "git_diff_files": git_diff_files,
    "ast_search": ast_search,
    "get_callees": get_callees,
    "get_callers": get_callers,
    "git_blame": git_blame,
    "git_show": git_show,
    "find_tests": find_tests,
    "test_outline": outline_tests,
    "trace_variable": trace_variable,
    "compressed_view": compressed_view,
    "module_summary": module_summary,
    "expand_symbol": expand_symbol,
    "detect_patterns": detect_patterns,
    "run_test": run_test,
    "git_hotspots": git_hotspots,
    "list_endpoints": list_endpoints,
    "extract_docstrings": extract_docstrings,
    "db_schema": db_schema,
}

# --- Browser tools (Playwright) ---
try:
    from app.browser.tools import BROWSER_TOOL_REGISTRY
    TOOL_REGISTRY.update(BROWSER_TOOL_REGISTRY)
except ImportError:
    logger.debug("Browser tools unavailable (playwright not installed)")


def _repair_tool_params(tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-repair common LLM mistakes before Pydantic validation.

    Handles structural errors that Pydantic coercion alone cannot fix,
    e.g. Qwen packing two values into one field: ``"start_line": "298, 422"``.
    """
    params = dict(params)  # shallow copy to avoid mutating caller's dict

    # --- Pattern 0: XML <parameter> fragments in dict keys --------------------
    # Some models (Qwen, DeepSeek) mix XML parameter tags into JSON, producing
    # garbled keys like:
    #   'end_line": 234</parameter>\n<parameter name="path'
    # with the actual value of 'path' as the dict value for that key.
    # Detect and reconstruct the intended parameters.
    _XML_FRAG_RE = re.compile(r'</parameter>|<parameter\s')
    if any(_XML_FRAG_RE.search(str(k)) for k in params):
        repaired: Dict[str, Any] = {}
        for key, val in params.items():
            key_str = str(key)
            if '</parameter>' not in key_str and '<parameter' not in key_str:
                # Clean key — keep as-is
                repaired[key_str] = val
                continue
            # Garbled key — extract embedded parameters.
            # Typical pattern: '{key1}": {val1}</parameter>\n<parameter name="{key2}'
            # where val is the dict value for key2.
            # Extract the first key (before any quote/colon/closing tag)
            first_key_m = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)', key_str)
            # Extract embedded value after first key (digits, possibly with quotes)
            embedded_val_m = re.search(
                r'["\s:]+\s*([^<]+?)\s*</parameter>', key_str,
            )
            # Extract the last parameter name
            last_key_m = re.search(
                r'<parameter\s+name=["\']([a-zA-Z_][a-zA-Z0-9_]*)', key_str,
            )
            if first_key_m and embedded_val_m:
                fk = first_key_m.group(1)
                fv = embedded_val_m.group(1).strip().strip('"').strip("'")
                # Try to convert to int if it looks numeric
                if fv.isdigit():
                    repaired[fk] = int(fv)
                else:
                    repaired[fk] = fv
            if last_key_m:
                lk = last_key_m.group(1)
                repaired[lk] = val
            elif first_key_m and not embedded_val_m:
                # No embedded value found — just use the first key
                repaired[first_key_m.group(1)] = val
        if repaired:
            logger.warning(
                "Repaired XML-garbled params for %s: %s → %s",
                tool_name, list(params.keys()), list(repaired.keys()),
            )
            params = repaired

    # --- Pattern 1: comma-separated integers in a single field ---------------
    # e.g. start_line="298, 422" → start_line=298, end_line=422
    _LINE_RANGE_TOOLS = {"read_file", "git_blame"}
    if tool_name in _LINE_RANGE_TOOLS:
        for src_key, dst_key in [("start_line", "end_line")]:
            val = params.get(src_key)
            if isinstance(val, str) and "," in val:
                parts = [p.strip() for p in val.split(",") if p.strip()]
                if len(parts) >= 2:
                    params[src_key] = parts[0]
                    # Only fill dst if the caller didn't already provide it
                    if dst_key not in params or params[dst_key] is None:
                        params[dst_key] = parts[1]
                elif len(parts) == 1:
                    params[src_key] = parts[0]

    # --- Pattern 2: file_path ↔ path alias ------------------------------------
    # Many tools use `path` while others use `file_path`. LLMs frequently
    # confuse the two. Map the wrong key to the right one based on the tool's
    # actual schema.
    _TOOLS_EXPECTING_PATH = {"read_file", "file_outline", "test_outline", "grep", "ast_search", "get_callers"}
    _TOOLS_EXPECTING_FILE_PATH = {"get_dependencies", "get_dependents", "compressed_view", "expand_symbol"}
    if tool_name in _TOOLS_EXPECTING_PATH and "file_path" in params and "path" not in params:
        params["path"] = params.pop("file_path")
    elif tool_name in _TOOLS_EXPECTING_FILE_PATH and "path" in params and "file_path" not in params:
        params["file_path"] = params.pop("path")

    # --- Pattern 3: strip leading/trailing whitespace from string values ------
    for key, val in params.items():
        if isinstance(val, str):
            params[key] = val.strip()

    return params


def execute_tool(tool_name: str, workspace: str, params: Dict[str, Any]) -> ToolResult:
    """Execute a tool by name with the given parameters."""
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return ToolResult(tool_name=tool_name, success=False, error=f"Unknown tool: {tool_name}")

    # Pre-repair common structural mistakes from weaker LLMs (e.g. Qwen)
    params = _repair_tool_params(tool_name, params)

    # Validate & coerce params through the Pydantic model for this tool.
    # This fixes non-Claude models (e.g. Qwen) that return numbers as strings
    # ("240" → int 240) and provides friendly validation errors instead of
    # cryptic TypeErrors deep inside tool implementations.
    param_model = TOOL_PARAM_MODELS.get(tool_name)
    if param_model:
        try:
            validated = param_model.model_validate(params)
            params = validated.model_dump(exclude_none=True)
        except ValidationError as ve:
            logger.warning("Tool %s param validation failed: %s", tool_name, ve)
            return ToolResult(tool_name=tool_name, success=False, error=f"Invalid parameters: {ve}")

    try:
        return fn(workspace=workspace, **params)
    except Exception as exc:  # tool functions can raise any exception; must catch all
        logger.exception("Tool %s failed", tool_name)
        return ToolResult(tool_name=tool_name, success=False, error=str(exc))
