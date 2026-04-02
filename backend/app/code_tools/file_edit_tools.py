"""File editing tools for the agent loop.

Ported from Claude Code's FileEditTool / FileWriteTool pattern with
Conductor-specific adaptations:

- Read-before-write enforcement via _file_read_state
- Staleness check (file not modified since last read)
- Path safety (workspace-only, blacklist for .git/node_modules/.env)
- Secret detection: warn + log (not block)
- Diff generation for user preview
- Auto-apply mode support

These tools are NOT in the code_tools TOOL_REGISTRY by default — they are
registered separately and only available when the agent has write access.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .schemas import ToolResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File read state tracking (per-session, thread-safe via GIL)
# ---------------------------------------------------------------------------

# Maps absolute file path → (content_hash, mtime, read_timestamp)
_file_read_state: Dict[str, tuple[str, float, float]] = {}


def record_file_read(abs_path: str, content: str) -> None:
    """Record that a file was read (called by read_file tool)."""
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        mtime = 0.0
    _file_read_state[abs_path] = (
        _content_hash(content),
        mtime,
        time.time(),
    )


def clear_file_read_state() -> None:
    """Clear all read state (call at session start)."""
    _file_read_state.clear()


def _content_hash(content: str) -> str:
    """Fast content hash for staleness comparison."""
    import hashlib
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

BLOCKED_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".tox",
})

BLOCKED_FILES = frozenset({
    ".env", ".env.local", ".env.production",
    ".gitconfig", ".bashrc", ".zshrc", ".profile",
})

# Patterns that suggest secrets
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|password|token|credential)\s*[:=]\s*['\"][^'\"]{8,}"),
    re.compile(r"(?i)-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
]


def _resolve_safe(workspace: str, path: str) -> tuple[Path, str | None]:
    """Resolve path within workspace, returning (abs_path, error_or_none)."""
    ws = Path(workspace).resolve()
    target = (ws / path).resolve()

    # Must be within workspace
    try:
        target.relative_to(ws)
    except ValueError:
        return target, f"Path escapes workspace: {path}"

    # Check blocked directories
    for part in target.relative_to(ws).parts[:-1]:
        if part in BLOCKED_DIRS:
            return target, f"Cannot edit files in {part}/ directory"

    # Check blocked filenames
    if target.name in BLOCKED_FILES:
        return target, f"Cannot edit protected file: {target.name} (contains secrets or system config)"

    return target, None


def _check_secrets(content: str) -> list[str]:
    """Check if content contains potential secrets. Returns list of warnings."""
    warnings = []
    for pattern in SECRET_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            warnings.append(f"Potential secret detected: {pattern.pattern[:40]}...")
    return warnings


def _generate_diff(old_content: str, new_content: str, file_path: str) -> str:
    """Generate unified diff for preview."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# file_edit tool
# ---------------------------------------------------------------------------

def file_edit(
    workspace: str,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolResult:
    """Edit an existing file by replacing exact string matches.

    Follows Claude Code's FileEditTool pattern:
    1. Read-before-write check
    2. Staleness check (file not modified since read)
    3. Path safety (workspace boundary, blacklist)
    4. Secret detection (warn, not block)
    5. Exact string replacement
    6. Returns diff for user preview
    """
    tool_name = "file_edit"

    # Validate inputs
    if not old_string:
        return ToolResult(tool_name=tool_name, success=False, error="old_string cannot be empty")
    if old_string == new_string:
        return ToolResult(tool_name=tool_name, success=False, error="old_string and new_string are identical — no change needed")

    # Path safety
    target, err = _resolve_safe(workspace, path)
    if err:
        return ToolResult(tool_name=tool_name, success=False, error=err)

    abs_path = str(target)

    # File must exist
    if not target.is_file():
        return ToolResult(tool_name=tool_name, success=False, error=f"File not found: {path}")

    # Read-before-write check
    if abs_path not in _file_read_state:
        return ToolResult(
            tool_name=tool_name, success=False,
            error=f"File has not been read yet. Use read_file on '{path}' first before editing.",
        )

    # Staleness check
    try:
        current_mtime = os.path.getmtime(abs_path)
    except OSError:
        current_mtime = 0.0
    _, recorded_mtime, _ = _file_read_state[abs_path]
    if current_mtime > recorded_mtime + 0.5:  # 0.5s tolerance for filesystem lag
        return ToolResult(
            tool_name=tool_name, success=False,
            error=f"File '{path}' has been modified since you last read it. Please read_file again to get the latest content.",
        )

    # Read current content
    try:
        old_content = target.read_text(errors="replace")
    except OSError as exc:
        return ToolResult(tool_name=tool_name, success=False, error=f"Cannot read file: {exc}")

    # Find and replace
    count = old_content.count(old_string)
    if count == 0:
        return ToolResult(
            tool_name=tool_name, success=False,
            error=f"old_string not found in '{path}'. Make sure it matches the file content exactly (including whitespace and indentation).",
        )
    if count > 1 and not replace_all:
        return ToolResult(
            tool_name=tool_name, success=False,
            error=f"Found {count} matches of old_string in '{path}'. Set replace_all=true to replace all, or provide more context to make the match unique.",
        )

    if replace_all:
        new_content = old_content.replace(old_string, new_string)
    else:
        new_content = old_content.replace(old_string, new_string, 1)

    # Secret detection (warn, not block)
    secret_warnings = _check_secrets(new_string)

    # Generate diff
    diff = _generate_diff(old_content, new_content, path)

    # Write the file
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content)
    except OSError as exc:
        return ToolResult(tool_name=tool_name, success=False, error=f"Write failed: {exc}")

    # Update read state
    record_file_read(abs_path, new_content)

    logger.info("file_edit: %s (%d replacement(s), %d→%d bytes)",
                path, count if replace_all else 1, len(old_content), len(new_content))

    return ToolResult(tool_name=tool_name, success=True, data={
        "path": path,
        "replacements": count if replace_all else 1,
        "diff": diff,
        "secret_warnings": secret_warnings,
        "bytes_before": len(old_content),
        "bytes_after": len(new_content),
    })


# ---------------------------------------------------------------------------
# file_write tool
# ---------------------------------------------------------------------------

def file_write(
    workspace: str,
    path: str,
    content: str,
) -> ToolResult:
    """Create a new file or overwrite an existing file.

    If the file exists, it must have been read first (read-before-write).
    New file creation does not require a prior read.
    """
    tool_name = "file_write"

    # Path safety
    target, err = _resolve_safe(workspace, path)
    if err:
        return ToolResult(tool_name=tool_name, success=False, error=err)

    abs_path = str(target)
    is_new = not target.exists()

    # If file exists, enforce read-before-write
    if not is_new:
        if abs_path not in _file_read_state:
            return ToolResult(
                tool_name=tool_name, success=False,
                error=f"File '{path}' already exists. Use read_file first before overwriting.",
            )
        # Staleness check
        try:
            current_mtime = os.path.getmtime(abs_path)
        except OSError:
            current_mtime = 0.0
        _, recorded_mtime, _ = _file_read_state[abs_path]
        if current_mtime > recorded_mtime + 0.5:
            return ToolResult(
                tool_name=tool_name, success=False,
                error=f"File '{path}' has been modified since you last read it. Please read_file again.",
            )

    # Secret detection
    secret_warnings = _check_secrets(content)

    # Generate diff (for existing files)
    diff = ""
    if not is_new:
        try:
            old_content = target.read_text(errors="replace")
            diff = _generate_diff(old_content, content, path)
        except OSError:
            pass

    # Write
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    except OSError as exc:
        return ToolResult(tool_name=tool_name, success=False, error=f"Write failed: {exc}")

    # Update read state
    record_file_read(abs_path, content)

    action = "created" if is_new else "overwritten"
    logger.info("file_write: %s (%s, %d bytes)", path, action, len(content))

    return ToolResult(tool_name=tool_name, success=True, data={
        "path": path,
        "action": action,
        "diff": diff,
        "secret_warnings": secret_warnings,
        "bytes": len(content),
    })


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

FILE_EDIT_TOOL_REGISTRY = {
    "file_edit": file_edit,
    "file_write": file_write,
}
