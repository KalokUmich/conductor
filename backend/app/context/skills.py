"""CodebaseSkills — utilities for processing code context before LLM calls.

These "skills" are processing functions that work on file content provided by
the extension's ContextGatherer.  They are NOT LLM tool calls; they are
deterministic text-processing operations that enrich the context window sent
to the AI.

Design rationale (Augment Code style):
- The extension sends raw file content + LSP data to the backend.
- These skills extract structured information (imports, function signatures,
  symbol usage patterns) so the LLM receives a focused, well-organised prompt
  rather than a raw file dump.
- All functions are pure / stateless: easy to test and extend.
"""
import re
from typing import List, Optional


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

_IMPORT_PATTERNS: dict[str, re.Pattern] = {
    "python":     re.compile(r"^(?:import |from )\S", re.MULTILINE),
    "javascript": re.compile(r"^(?:import |const .+ = require)", re.MULTILINE),
    "typescript": re.compile(r"^(?:import |const .+ = require)", re.MULTILINE),
    "java":       re.compile(r"^import ", re.MULTILINE),
    "go":         re.compile(r"^import ", re.MULTILINE),
}


def extract_imports(file_content: str, language: str) -> List[str]:
    """Return deduplicated import lines from *file_content*.

    Args:
        file_content: Full or partial source file text.
        language: Language ID (python, typescript, javascript, java, go).

    Returns:
        List of import statement strings (max 30).
    """
    pattern = _IMPORT_PATTERNS.get(
        language,
        re.compile(r"^(?:import |from |require|use )", re.MULTILINE),
    )
    lines = [
        line.strip()
        for line in file_content.splitlines()
        if pattern.match(line.strip())
    ]
    # Deduplicate while preserving order
    seen: set = set()
    result: List[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            result.append(line)
        if len(result) >= 30:
            break
    return result


# ---------------------------------------------------------------------------
# Context window extraction
# ---------------------------------------------------------------------------

def extract_context_window(
    file_content: str,
    start_line: int,
    end_line: int,
    context_lines: int = 15,
) -> str:
    """Return a numbered slice of *file_content* around the given line range.

    Args:
        file_content: Source file text.
        start_line: 1-based start of the selection.
        end_line: 1-based end of the selection.
        context_lines: Lines of context to include before/after.

    Returns:
        Multi-line string with format "N: <line content>".
    """
    lines = file_content.splitlines()
    from_idx = max(0, start_line - 1 - context_lines)
    to_idx   = min(len(lines), end_line + context_lines)
    return "\n".join(
        f"{from_idx + i + 1}: {line}"
        for i, line in enumerate(lines[from_idx:to_idx])
    )


# ---------------------------------------------------------------------------
# Containing function / class
# ---------------------------------------------------------------------------

_DEF_PATTERNS: dict[str, re.Pattern] = {
    "python":     re.compile(r"^\s*(?:def |class |async def )"),
    "typescript": re.compile(
        r"^\s*(?:function |class |async function |"
        r"(?:public|private|protected|static)\s+\w|\bconst \w+ = (?:async\s*)?\()"
    ),
    "javascript": re.compile(
        r"^\s*(?:function |class |async function |const \w+ = (?:async\s*)?\()"
    ),
    "java":       re.compile(
        r"^\s*(?:public|private|protected|static|void|\w+)\s+\w+\s*\("
    ),
    "go":         re.compile(r"^\s*func "),
}


def find_containing_function(
    file_content: str,
    start_line: int,
    language: str,
) -> Optional[str]:
    """Walk backwards from *start_line* to find the enclosing function/class.

    Args:
        file_content: Source file text.
        start_line: 1-based line number to start searching from.
        language: Language ID.

    Returns:
        The first 120 chars of the enclosing definition line, or None.
    """
    lines = file_content.splitlines()
    scan_from = min(start_line - 1, len(lines) - 1)
    pattern = _DEF_PATTERNS.get(
        language,
        re.compile(r"^\s*(?:function |def |class |func )"),
    )
    for i in range(scan_from, -1, -1):
        if pattern.match(lines[i]):
            return lines[i].strip()[:120]
    return None


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_explanation_prompt(
    snippet: str,
    file_path: str,
    language: str,
    surrounding_code: Optional[str] = None,
    imports: Optional[List[str]] = None,
    containing_function: Optional[str] = None,
    related_files: Optional[list] = None,
    rag_context: Optional[str] = None,
) -> str:
    """Assemble a focused, context-rich prompt for code explanation.

    The prompt is structured with XML-style tags so the LLM receives clearly
    delimited sections and can reason about each independently.

    Args:
        snippet: The code the user wants explained.
        file_path: Workspace-relative file path.
        language: Language ID.
        surrounding_code: Lines around the snippet (from extract_context_window).
        imports: Import statements in the file.
        containing_function: Enclosing function signature.
        related_files: List of RelatedFileSnippet-like dicts.
        rag_context: Optional XML string of semantically related code chunks
                     retrieved from the RAG pipeline.

    Returns:
        Complete prompt string ready to send to the LLM.
    """
    imports_text = "\n".join(imports or []) or "(none)"
    function_text = containing_function or "(top-level or unknown)"

    related_section = ""
    for rf in (related_files or []):
        rpath = rf.get("relative_path", rf.get("relativePath", "?"))
        reason = rf.get("reason", "related")
        rsnippet = rf.get("snippet", "")
        related_section += (
            f"\n### {rpath}  [{reason}]\n"
            f"```{language}\n{rsnippet}\n```\n"
        )

    surrounding_section = ""
    if surrounding_code:
        surrounding_section = (
            f"<surrounding_code>\n"
            f"```{language}\n{surrounding_code}\n```\n"
            f"</surrounding_code>\n\n"
        )

    rag_section = ""
    if rag_context:
        rag_section = (
            f"<related_workspace_code>\n{rag_context}\n</related_workspace_code>\n\n"
        )

    return (
        f"You are an expert software engineer. Explain the following code clearly "
        f"and concisely so every team member — regardless of seniority — can "
        f"understand it.\n\n"
        f"<target_code>\n"
        f"File: {file_path}\n"
        f"Enclosing scope: {function_text}\n"
        f"```{language}\n{snippet}\n```\n"
        f"</target_code>\n\n"
        f"{surrounding_section}"
        f"<imports>\n{imports_text}\n</imports>\n\n"
        f"{'<related_files>' + related_section + '</related_files>' + chr(10) + chr(10) if related_section else ''}"
        f"{rag_section}"
        f"<instructions>\n"
        f"Provide a concise explanation (3–8 sentences) covering:\n"
        f"1. What this code does (purpose and behaviour)\n"
        f"2. Key design decisions or patterns used\n"
        f"3. Any non-obvious side-effects, edge cases, or gotchas\n\n"
        f"Write in plain English. You may use **bold** for emphasis, "
        f"`backticks` for inline code references, and - bullet lists. "
        f"No markdown headers (#). "
        f"Do NOT reproduce the code in your answer.\n"
        f"</instructions>"
    )
