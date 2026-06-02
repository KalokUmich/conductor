"""Deterministic existence / phantom-symbol / stub-call scanners for PR review.

Pure, LLM-free functions (plus their regex/constant tables) extracted verbatim
from ``pr_brain.py`` so the PR Brain module holds only its LLM-orchestration
concern. These power Phase 2 (existence checks: P13 missing-import/reference
scanners for Python/Go/Java) and Phase 4 (post-processing: missing-symbol +
stub-caller injection, reflection against existence facts, diff-scope filtering,
own-fix filtering, merge-recommendation recompute, verdict/JSON parsing).

Zero coupling to ``PRBrainOrchestrator`` — every function takes plain
strings/dicts and returns plain data. ``pr_brain`` re-exports all names
(``from existence_scanners import *``) so existing callers and the test suite,
which reference these both by name and via module attribute, keep working.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.code_review.models import ReviewFinding

logger = logging.getLogger(__name__)


_SEVERITY_RANK = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "nit": 1,
    "praise": 0,
}


def _dedup_findings_by_location(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge findings pointing at the same (file, line±5) range.

    Keeps the finding with the highest (severity_rank, confidence) tuple.
    This catches the "coordinator produces the concrete bug finding PLUS
    a meta-finding about it" duplication observed on requests-012.
    """
    if not findings:
        return findings

    keep: List[Dict[str, Any]] = []

    def _better(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        sev_a = _SEVERITY_RANK.get(str(a.get("severity", "low")).lower(), 1)
        sev_b = _SEVERITY_RANK.get(str(b.get("severity", "low")).lower(), 1)
        if sev_a != sev_b:
            return sev_a > sev_b
        return float(a.get("confidence", 0) or 0) > float(b.get("confidence", 0) or 0)

    for f in findings:
        file_ = f.get("file", "") or ""
        start = int(f.get("start_line", 0) or 0)
        end = int(f.get("end_line", 0) or start or 0)

        merged = False
        for i, existing in enumerate(keep):
            ef = existing.get("file", "") or ""
            if ef != file_:
                continue
            es = int(existing.get("start_line", 0) or 0)
            ee = int(existing.get("end_line", 0) or es or 0)
            # Overlap or adjacency within 5 lines
            if start <= ee + 5 and end >= es - 5:
                # Same region — keep the stronger one
                if _better(f, existing):
                    keep[i] = f
                merged = True
                break
        if not merged:
            keep.append(f)

    return keep


def _finding_covers_symbol(
    finding: Dict[str, Any],
    symbol_name: str,
    reference_file: str,
) -> bool:
    """True if ``finding`` already reports the missing-symbol bug for
    ``symbol_name``. Matching rules — ANY one is enough:
      * title contains the symbol name (case-sensitive: class/method
        names are meaningful identifiers)
      * any evidence entry mentions the symbol name
      * the finding's file matches the reference site AND the title
        signals a runtime error (ImportError/NameError/TypeError/
        undefined/not defined)
    """
    if not symbol_name:
        return True  # nothing to enforce

    title = str(finding.get("title", "") or "")
    if symbol_name in title:
        return True

    evidence = finding.get("evidence") or []
    if isinstance(evidence, list):
        for e in evidence:
            if symbol_name in str(e):
                return True
    elif isinstance(evidence, str) and symbol_name in evidence:
        return True

    # Fallback: same file + runtime-error title phrasing.
    f_file = str(finding.get("file", "") or "")
    ref_file = reference_file.split(":", 1)[0] if reference_file else ""
    if f_file and ref_file and f_file == ref_file:
        lowered = title.lower()
        for marker in (
            "importerror",
            "nameerror",
            "typeerror",
            "undefined",
            "not defined",
            "does not exist",
            "missing symbol",
        ):
            if marker in lowered:
                return True
    return False


def _parse_reference_location(ref: str) -> tuple[str, int]:
    """Split ``"path/to/file.py:42"`` → ``("path/to/file.py", 42)``.
    Falls back to ``(ref, 0)`` when no colon or unparsable line number."""
    if not ref:
        return ("", 0)
    if ":" not in ref:
        return (ref, 0)
    path, _, tail = ref.rpartition(":")
    try:
        return (path, int(tail.strip()))
    except (ValueError, TypeError):
        return (ref, 0)


def _inject_missing_symbol_findings(
    findings: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """Ensure every Phase-2 missing symbol AND signature mismatch has a
    finding in the review.

    Two classes of enforcement:
      * ``exists=False`` — symbol referenced but never defined anywhere.
        Synthesize an ImportError/NameError finding at the reference site.
      * ``exists=True`` with ``signature_info.missing_params`` — method
        is called with kwargs it doesn't accept. Synthesize a TypeError
        finding at the call site.

    Returns ``(findings_with_injections, injected_count)``. Safe to call
    when no FactStore is active — returns the input unchanged.
    """
    from app.scratchpad import current_factstore

    store = current_factstore()
    if store is None:
        return (findings, 0)

    try:
        missing = list(store.iter_existence(exists=False))
        present = list(store.iter_existence(exists=True))
    except Exception as exc:
        logger.warning(
            "[PR Brain v2] missing-symbol post-pass skipped — " "iter_existence failed: %s",
            exc,
        )
        return (findings, 0)

    sig_mismatches = [p for p in present if p.signature_info and p.signature_info.get("missing_params")]

    if not missing and not sig_mismatches:
        return (findings, 0)

    injected = 0
    result = list(findings)

    for m in missing:
        if any(_finding_covers_symbol(f, m.symbol_name, m.referenced_at or "") for f in result):
            continue
        ref_file, ref_line = _parse_reference_location(m.referenced_at or "")
        evidence_detail = (m.evidence or "").strip()[:300]
        synthetic = {
            "title": (f"ImportError at runtime: {m.symbol_name} " f"not defined in codebase"),
            "severity": "critical",
            "confidence": 0.99,
            "file": ref_file,
            "start_line": ref_line,
            "end_line": ref_line,
            "evidence": [
                f"Phase 2 verifier: no definition found for `{m.symbol_name}` "
                f"({m.symbol_kind}) anywhere in the workspace.",
                evidence_detail or "grep/find_symbol returned 0 matches.",
            ],
            "risk": (
                f"Every call path that loads `{ref_file}` raises "
                f"ImportError/NameError at runtime — the PR is unshippable "
                f"as-is."
            ),
            "suggested_fix": (
                f"Either define `{m.symbol_name}` in the imported module, "
                f"or remove the reference at {m.referenced_at or ref_file}."
            ),
            "category": "correctness",
            "_injected_from": "phase2_existence_missing",
        }
        result.append(synthetic)
        injected += 1

    for p in sig_mismatches:
        bad_params = p.signature_info.get("missing_params") or []
        if not bad_params:
            continue
        bad_list = [str(bp) for bp in bad_params]
        # Check each bad-param name against existing findings — skip if
        # any kwarg is already covered.
        if any(any(_finding_covers_symbol(f, bp, p.referenced_at or "") for f in result) for bp in bad_list):
            continue
        ref_file, ref_line = _parse_reference_location(p.referenced_at or "")
        accepted = p.signature_info.get("actual_params") or []
        synthetic = {
            "title": (f"TypeError at runtime: {p.symbol_name}() does not accept " f"{', '.join(bad_list)}"),
            "severity": "high",
            "confidence": 0.97,
            "file": ref_file,
            "start_line": ref_line,
            "end_line": ref_line,
            "evidence": [
                f"Phase 2 verifier: `{p.symbol_name}` signature accepts "
                f"{accepted}; this call passes {bad_list} which are not in "
                f"the signature.",
            ],
            "risk": (
                f"Every invocation raises `TypeError: unexpected keyword " f"argument '{bad_list[0]}'` at runtime."
            ),
            "suggested_fix": (
                f"Either extend `{p.symbol_name}`'s signature to accept "
                f"{bad_list}, or drop the unsupported kwarg(s) from the "
                f"call at {p.referenced_at or ref_file}."
            ),
            "category": "correctness",
            "_injected_from": "phase2_existence_sigmismatch",
        }
        result.append(synthetic)
        injected += 1

    return (result, injected)


_PYTHON_FROM_IMPORT_RE = re.compile(
    r"^\+\s*from\s+([.\w]+)\s+import\s+(.+?)\s*$",
)
_PYTHON_BARE_IMPORT_RE = re.compile(
    r"^\+\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$",
)
_DIFF_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def _scan_new_python_imports_for_missing(
    workspace_path: str,
    file_diffs: Dict[str, str],
    *,
    max_symbols_checked: int = 24,
    grep_timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """P13 — Deterministic Python import verifier.

    Scans each Python file's unified diff for newly added imports
    (``+from X import Y`` or ``+import X``) and verifies each imported
    name is defined somewhere in the workspace via a mechanical grep
    for ``class Y`` / ``def Y`` / ``Y = ...``. Returns the list of
    UNDEFINED names, each as ``{"name", "referenced_at", "evidence"}``.

    This is a safety net against the LLM Phase 2 worker missing a
    phantom symbol. Runs always; cheap; Python-only.

    Guards:
      * caps at ``max_symbols_checked`` greps per PR to bound runtime on
        large diffs
      * ``grep_timeout_s`` on each subprocess (so a giant repo cannot
        wedge the review)
      * skips wildcard (``*``), relative (``from .foo import``), and
        framework (``os/re/typing/logging/django/...``) imports
      * fails soft — any exception just returns current findings
    """
    if not workspace_path or not file_diffs:
        return []

    found: List[Dict[str, str]] = []
    checked = 0
    seen_names: set = set()

    for file_path, diff_text in file_diffs.items():
        if not file_path.endswith(".py"):
            continue
        current_new_line = 0
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = _DIFF_HUNK_HEADER_RE.match(raw)
                if m:
                    current_new_line = int(m.group(1))
                continue
            if raw.startswith("---") or raw.startswith("+++"):
                continue
            is_addition = raw.startswith("+")
            if is_addition:
                from_match = _PYTHON_FROM_IMPORT_RE.match(raw)
                if from_match:
                    module = from_match.group(1)
                    if module.startswith("."):
                        # Relative imports — skip (would need file path
                        # resolution; rare phantom-bug source).
                        pass
                    elif _is_framework_module(module):
                        pass
                    elif not _module_is_first_party(workspace_path, module):
                        # Module doesn't resolve to a file in the workspace
                        # — it's an external package (e.g. `arroyo`, `kombu`).
                        # We can't verify external symbols via workspace grep
                        # without false positives. Skip.
                        pass
                    else:
                        names_chunk = from_match.group(2)
                        for name in _split_import_names(names_chunk):
                            if name in seen_names:
                                continue
                            if checked >= max_symbols_checked:
                                break
                            seen_names.add(name)
                            checked += 1
                            if _python_symbol_defined_anywhere(
                                workspace_path,
                                name,
                                timeout_s=grep_timeout_s,
                            ):
                                continue
                            found.append(
                                {
                                    "name": name,
                                    "referenced_at": (f"{file_path}:{current_new_line}"),
                                    "evidence": (
                                        f"Deterministic grep for `class {name}`, "
                                        f"`def {name}`, `{name} =` in "
                                        f"`*.py` → 0 matches. Import `from "
                                        f"{module} import {name}` will raise "
                                        f"ImportError at runtime."
                                    ),
                                }
                            )
                else:
                    bare_match = _PYTHON_BARE_IMPORT_RE.match(raw)
                    if bare_match:
                        module = bare_match.group(1)
                        if not module.startswith(".") and not _is_framework_module(module):
                            # For `import X.Y`, we check if the root module
                            # X has any .py file. Skip for now — bare
                            # imports rarely produce phantom-symbol bugs.
                            pass
            # advance new-line counter for + and context (unchanged)
            if not raw.startswith("-"):
                current_new_line += 1
            if checked >= max_symbols_checked:
                break
        if checked >= max_symbols_checked:
            break
    return found


_FRAMEWORK_MODULE_PREFIXES = (
    "os",
    "sys",
    "re",
    "json",
    "typing",
    "logging",
    "abc",
    "collections",
    "contextlib",
    "dataclasses",
    "enum",
    "functools",
    "io",
    "itertools",
    "math",
    "pathlib",
    "random",
    "subprocess",
    "time",
    "unittest",
    "warnings",
    "asyncio",
    "concurrent",
    "datetime",
    "decimal",
    "django",
    "flask",
    "rest_framework",
    "pydantic",
    "sqlalchemy",
    "requests",
    "urllib3",
    "numpy",
    "pandas",
    "pytest",
    "mypy",
    "starlette",
    "fastapi",
    "click",
    "boto3",
    "botocore",
    "sentry_sdk",
)


def _is_framework_module(module: str) -> bool:
    """True if ``module`` is the stdlib or a well-known third-party that
    we never want to verify existence for."""
    if not module:
        return True
    root = module.split(".", 1)[0]
    return root in _FRAMEWORK_MODULE_PREFIXES


def _module_is_first_party(workspace_path: str, module: str) -> bool:
    """True when ``module`` resolves to a file inside the workspace.

    Principled complement to ``_is_framework_module`` — instead of an
    ever-growing blacklist of third-party libraries, we check whether
    the module's expected file-system path exists in the workspace.
    If not, it's an external package (installed via pip) and we should
    not flag its imports as missing — P13 has no way to verify external
    package symbols via workspace grep anyway.

    Checks for both layouts:
      * ``module/path/to/X.py``
      * ``module/path/to/X/__init__.py``

    Returns False on any error / missing workspace (fail-safe: skip).
    """
    import os as _os

    if not workspace_path or not module or module.startswith("."):
        return False
    try:
        candidate = module.replace(".", "/")
        for suffix in (".py", "/__init__.py"):
            if _os.path.exists(_os.path.join(workspace_path, candidate + suffix)):
                return True
        # Also try under common repo layouts: src/<module>, backend/<module>
        for root_prefix in ("src", "backend", "lib"):
            for suffix in (".py", "/__init__.py"):
                if _os.path.exists(_os.path.join(workspace_path, root_prefix, candidate + suffix)):
                    return True
    except Exception:
        return False
    return False


def _split_import_names(names_chunk: str) -> List[str]:
    """Parse the comma-separated tail of a ``from X import ...`` line.

    Handles parentheses, trailing commas, and ``as`` aliases. Filters
    wildcards and keeps only valid Python identifiers."""
    cleaned = names_chunk.strip().strip("()").rstrip(",")
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    out: List[str] = []
    for p in parts:
        # Drop the "as alias" portion; we want the imported name.
        primary = p.split(" as ", 1)[0].strip()
        if primary == "*" or not primary.isidentifier():
            continue
        out.append(primary)
    return out


def _python_symbol_defined_anywhere(
    workspace_path: str,
    name: str,
    *,
    timeout_s: float = 8.0,
) -> bool:
    """Grep the workspace for a Python definition of ``name``.

    Matches ``class name``, ``def name``, or ``name = ...`` at line
    start (with optional leading whitespace). Returns True on first
    match; False on zero matches; True on error (fail-safe — never
    report a symbol missing we couldn't verify)."""
    import subprocess

    # Anchor definitions at line start + optional indent only. This
    # avoids matching ``from X import name`` or ``foo(name=...)``.
    # Using extended regex: ^\s*(class|def)\s+name\b  OR
    # ^\s*name\s*=
    pattern = rf"^\s*(class|def)\s+{re.escape(name)}\b|" rf"^\s*{re.escape(name)}\s*="
    try:
        r = subprocess.run(
            [
                "grep",
                "-r",
                "-E",
                pattern,
                workspace_path,
                "--include=*.py",
                "--max-count=1",
                "-l",
                "--exclude-dir=.git",
                "--exclude-dir=.venv",
                "--exclude-dir=node_modules",
                "--exclude-dir=__pycache__",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        # exit 0 = found ≥1 match; exit 1 = no match; exit 2 = error
        if r.returncode == 0 and r.stdout.strip():
            return True
        # exit 1 = no match → symbol missing
        # any other non-zero = grep error → fail-safe "True" (don't flag)
        return r.returncode != 1
    except Exception:
        return True


# ---------------------------------------------------------------------------
# P13-Go — deterministic bare-identifier phantom detector for Go.
# ---------------------------------------------------------------------------
# Targets the "call to undefined function in same package" class of bug
# — a `go build` compile error the LLM worker routinely misses because
# the identifier name LOOKS plausible (e.g. `endpointQueryData`). This
# scanner reads the diff, extracts every bare call (no dot prefix) in
# newly-added lines, and greps the package directory for a definition.
# Zero matches → phantom; inject with severity=critical, conf=0.99.
#
# Scope-out by design (to avoid false positives):
#   * `pkg.Foo(...)` — requires import resolution; MVP skips
#   * `obj.Method(...)` — requires type inference; MVP skips
#   * Files using dot-imports (`import . "..."`) — can't disambiguate
# ---------------------------------------------------------------------------

# Matches a bare call `name(` where `name` is not preceded by `.`, not
# a function declaration, not a type keyword. Lookbehind for `.` is
# emulated via a character-class preceding context filter.
_GO_BARE_CALL_RE = re.compile(
    r"""
    (?:^|[\s=,;\[\]{}(+\-*/&|<>!])   # allowed preceding char (no `.`)
    (?!func\s+)(?!type\s+)            # not a decl keyword directly
    ([A-Za-z_][A-Za-z0-9_]*)          # the identifier
    \s*\(                             # followed by (
    """,
    re.VERBOSE,
)
# Bare identifier at an argument position: `(name,` / `,name,` / `,name)`.
# Captures identifiers passed as arguments that look substantial enough
# to be package-level constants/vars/functions (>=6 chars OR camelCase).
# Filters out obvious locals like `ctx`, `req`, `err`.
_GO_BARE_ARG_RE = re.compile(
    r"""
    (?:\(|,)\s*                       # preceded by ( or , + ws
    ([A-Za-z_][A-Za-z0-9_]*)          # the identifier
    \s*(?=,|\))                       # followed by , or ) — arg position
    """,
    re.VERBOSE,
)
# Dot-import marker. Any file containing this is skipped.
_GO_DOT_IMPORT_RE = re.compile(r'^\s*(?:import\s+)?\.\s+"[^"]+"\s*$', re.MULTILINE)
# Same-line `func name(` declaration — used to drop self-matches where
# the bare-call regex would fire on the function header itself.
_GO_FUNC_DECL_RE = re.compile(
    r"^\s*(?:func\s+(?:\(\s*\w+\s+\*?\w+(?:\[[^\]]*\])?\s*\)\s+)?)(\w+)\s*\(",
)
# Substantive identifier heuristic: >=6 chars, OR contains mixed case
# (camelCase), OR contains underscore (snake_case). Filters out short
# generic locals like `ctx`, `req`, `err`, `res`, `i`, `n`, `x`.
_GO_SUBSTANTIVE_IDENT_RE = re.compile(
    r"^(?:"
    r"[A-Za-z_][A-Za-z0-9_]{5,}|"  # >= 6 chars
    r"[a-z][a-z0-9]*[A-Z][A-Za-z0-9_]*|"  # camelCase
    r"[A-Z][A-Za-z0-9]*[_A-Z][A-Z_]*|"  # UPPER_SNAKE
    r"[A-Za-z]+_[A-Za-z_]+"  # snake_case (non-leading _)
    r")$"
)
# Matches `func (recv *T) Name(` or `func Name(` and captures the
# receiver name (group 1) and the func name (group 2).
_GO_FUNC_SIG_START_RE = re.compile(
    r"^\s*func\s+(?:\(\s*(\w+)\s+\*?[\w.\[\]]+(?:\[[^\]]*\])?\s*\)\s+)?(\w+)\s*\(",
)
# Matches a line that looks like a method signature: `Name(args) ret?`.
# Supports: interface method decls (`Foo(x int) string`), function type
# decls, and any "name + paren + optional return" shape-only lines.
# CALLS are distinguished by their args lacking typed params.
_GO_METHOD_SIG_RE = re.compile(
    r"""
    ^\s*
    \w+\s*                               # method name
    \(                                   # open paren
    [^)]*                                # params (no nested parens — MVP)
    \)                                   # close paren
    \s*
    (?:                                  # optional return type
        \([^)]*\)                        #   multi-return `(T1, T2)`
        |
        [\w*.<>\[\],\s]+                 #   single return type
    )?
    \s*$
    """,
    re.VERBOSE,
)
# Typed parameter pattern inside parens: `name Type` (lowercase-start
# identifier followed by whitespace followed by a type token). If
# present, the parens contain typed params → signature. If absent,
# the parens contain values/expressions → call.
_GO_TYPED_PARAM_RE = re.compile(r"[a-z_]\w*\s+(?:\*|\[\]|\.\.\.)*[\w.]")

# Go keywords + built-in identifiers + universe block. Covers every
# identifier a Go file can reference without a definition in user code.
_GO_BUILTINS: set[str] = {
    # keywords
    "break",
    "case",
    "chan",
    "const",
    "continue",
    "default",
    "defer",
    "else",
    "fallthrough",
    "for",
    "func",
    "go",
    "goto",
    "if",
    "import",
    "interface",
    "map",
    "package",
    "range",
    "return",
    "select",
    "struct",
    "switch",
    "type",
    "var",
    # pre-declared types
    "bool",
    "byte",
    "complex64",
    "complex128",
    "error",
    "float32",
    "float64",
    "int",
    "int8",
    "int16",
    "int32",
    "int64",
    "rune",
    "string",
    "uint",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "uintptr",
    "any",
    "comparable",
    # pre-declared values
    "true",
    "false",
    "iota",
    "nil",
    # built-in functions
    "append",
    "cap",
    "clear",
    "close",
    "complex",
    "copy",
    "delete",
    "imag",
    "len",
    "make",
    "max",
    "min",
    "new",
    "panic",
    "print",
    "println",
    "real",
    "recover",
}


def _go_dir_has_dot_import(file_path: str, workspace_path: str) -> bool:
    """True if the diff file uses dot-imports (skip entire file when so)."""
    import os as _os

    full = _os.path.join(workspace_path, file_path)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return bool(_GO_DOT_IMPORT_RE.search(f.read(16384)))
    except Exception:
        return True  # fail-safe: if we can't read, skip scanning


def _go_symbol_defined_anywhere(
    workspace_path: str,
    name: str,
    *,
    timeout_s: float = 8.0,
) -> bool:
    """Workspace-wide grep for ANY Go top-level definition of `name`,
    OR for `name` appearing as a function parameter anywhere.

    Matches:
      * `func NAME(` / `func (r R) NAME(` / `var NAME` / `const NAME`
        / `type NAME` / `NAME :=` / `NAME = ` (package level)
      * `(NAME Type` / `,NAME Type` — parameter position (handles the
        common case where NAME is a function param in one file and used
        as an argument in another file)

    Workspace-wide plus parameter-aware matches prevent false positives
    on (a) interface methods implemented in other packages and
    (b) parameters used across files in the same enclosing function.

    Returns True on any match; False on zero matches; True on error
    (fail-safe — never false-positive when grep errors)."""
    import subprocess

    # POSIX ERE (grep -E): non-capture `(?:...)` isn't supported;
    # use plain `(...)` groups. `\s` / `\w` work as GNU ERE extensions.
    pattern = (
        rf"^[[:space:]]*(func[[:space:]]+(\([^)]*\)[[:space:]]+)?{re.escape(name)}[[:space:]]*[(\[]|"
        rf"var[[:space:]]+{re.escape(name)}[[:space:]]|"
        rf"const[[:space:]]+{re.escape(name)}[[:space:]]|"
        rf"type[[:space:]]+{re.escape(name)}[[:space:]]|"
        rf"{re.escape(name)}[[:space:]]*:?=)|"
        # parameter position: `(name Type` or `, name Type`
        rf"[(,][[:space:]]*{re.escape(name)}[[:space:]]+(\*|\[\]|\.\.\.)*[[:alnum:]._]"
    )
    try:
        r = subprocess.run(
            [
                "grep",
                "-r",
                "-E",
                pattern,
                workspace_path,
                "--include=*.go",
                "--max-count=1",
                "-l",
                "--exclude-dir=.git",
                "--exclude-dir=vendor",
                "--exclude-dir=node_modules",
                "--exclude-dir=.venv",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
        return r.returncode != 1
    except Exception:
        return True


def _extract_go_locals_from_diff(diff_text: str) -> set[str]:
    """Scan `+` lines for names that are LOCAL (not package-level):
    method receivers, function parameters, and `:=` short-var decls.

    We use this as a skip-list when looking for phantom references —
    a reference to a local is not a compile error.
    """
    locals_set: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw.startswith("+"):
            continue
        body = raw[1:]
        stripped = body.lstrip()
        if stripped.startswith("//") or not stripped:
            continue
        if "//" in body:
            body = body.split("//", 1)[0]

        # Short-var decl: `name, name2 := ...`
        for m in re.finditer(
            r"(?:^|[\s;{}])([a-z_][\w]*(?:\s*,\s*[a-z_][\w]*)*)\s*:=",
            body,
        ):
            chunk = m.group(1)
            for n in chunk.split(","):
                n = n.strip()
                if n.isidentifier():
                    locals_set.add(n)

        # Function signature: capture receiver + param names
        sig_m = _GO_FUNC_SIG_START_RE.match(body)
        if sig_m:
            recv = sig_m.group(1)
            if recv:
                locals_set.add(recv)
            # Extract param list — everything between the opening `(`
            # of the signature's param list and the matching `)`.
            open_idx = body.find("(", sig_m.end() - 1)
            if open_idx != -1:
                depth = 0
                close_idx = -1
                for idx in range(open_idx, len(body)):
                    c = body[idx]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            close_idx = idx
                            break
                if close_idx != -1:
                    params = body[open_idx + 1 : close_idx]
                    # Each param is `name Type` or `name1, name2 Type`.
                    # Split on commas, extract leading identifier.
                    for seg in params.split(","):
                        seg = seg.strip()
                        # seg can be `name Type`, `name`, or just
                        # `Type` (unnamed result). Only keep first
                        # token if it's followed by whitespace + Type.
                        first = re.match(r"^([a-z_][\w]*)\s+", seg)
                        if first:
                            locals_set.add(first.group(1))
    return locals_set


def _extract_go_bare_references_from_diff(
    diff_text: str,
    *,
    skip_names: Optional[set[str]] = None,
) -> List[tuple[str, int]]:
    """Yield (name, new_line_number) for every substantive bare-identifier
    reference on `+` lines.

    Captured positions:
      * Function call: `name(`
      * Function argument: `,name,` / `(name,` / `,name)`

    Filters applied:
      * Go keywords + built-ins (`len`, `make`, etc.)
      * Method-on-obj / package-qualified (preceded by `.`)
      * Function declaration self-match (`func X(` — X is the decl name)
      * Local names (parameters / receivers / `:=` vars) via skip_names
      * Interface method signatures (whole-line matching `Name(args) ret?`)
      * Identifiers that are NOT substantive (locals like `ctx`, `req`,
        `err`, `i`) — filtered via `_GO_SUBSTANTIVE_IDENT_RE`
      * Comment / string / blank lines
    """
    skip_names = skip_names or set()
    results: List[tuple[str, int]] = []
    current_new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            m = _DIFF_HUNK_HEADER_RE.match(raw)
            if m:
                current_new_line = int(m.group(1))
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        is_addition = raw.startswith("+")
        if is_addition:
            body = raw[1:]
            stripped = body.strip()
            if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*") or not stripped:
                if not raw.startswith("-"):
                    current_new_line += 1
                continue
            if "//" in body:
                body = body.split("//", 1)[0]

            # Skip lines that look like a method signature (interface
            # method decl): `Foo(x int) string`. Distinguished from a
            # call (`foo("x")`) by having a TYPED param inside the
            # parens — `name Type` pattern.
            if not stripped.startswith("func ") and _GO_METHOD_SIG_RE.match(body):
                # Extract content between outer `(` and matching `)`
                open_idx = body.find("(")
                if open_idx >= 0:
                    depth = 0
                    close_idx = -1
                    for _idx in range(open_idx, len(body)):
                        _c = body[_idx]
                        if _c == "(":
                            depth += 1
                        elif _c == ")":
                            depth -= 1
                            if depth == 0:
                                close_idx = _idx
                                break
                    if close_idx > open_idx:
                        params = body[open_idx + 1 : close_idx]
                        if _GO_TYPED_PARAM_RE.search(params):
                            # Typed param → signature → skip line
                            if not raw.startswith("-"):
                                current_new_line += 1
                            continue
            # Skip the function declaration on this line to avoid
            # self-match against its own name.
            decl_m = _GO_FUNC_DECL_RE.match(body)
            decl_name = decl_m.group(1) if decl_m else None

            # Inline accept-filter. Uses loop-locals deliberately
            # (consumed within the same iteration; no closure capture
            # escapes the loop body).
            def _go_ident_accepted(name: str, start: int) -> bool:
                if name in _GO_BUILTINS:
                    return False
                if name in skip_names:
                    return False
                if decl_name and name == decl_name:  # noqa: B023
                    return False
                if start > 0 and body[start - 1] == ".":  # noqa: B023
                    return False
                return bool(_GO_SUBSTANTIVE_IDENT_RE.match(name))

            # Position 1: bare CALL sites (name followed by `(`)
            for m in _GO_BARE_CALL_RE.finditer(body):
                name = m.group(1)
                if _go_ident_accepted(name, m.start(1)):
                    results.append((name, current_new_line))
            # Position 2: bare ARGUMENT positions (name between `(|,`
            # and `,|)`). Captures constants/vars passed as arguments.
            for m in _GO_BARE_ARG_RE.finditer(body):
                name = m.group(1)
                if _go_ident_accepted(name, m.start(1)):
                    results.append((name, current_new_line))
        if not raw.startswith("-"):
            current_new_line += 1
    return results


# Backwards-compat alias for any in-tree callers / future re-use.
_extract_go_bare_calls_from_diff = _extract_go_bare_references_from_diff


def _scan_new_go_references_for_missing(
    workspace_path: str,
    file_diffs: Dict[str, str],
    *,
    max_symbols_checked: int = 24,
    grep_timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """P13-Go — Deterministic Go phantom bare-identifier detector.

    Scans `.go` file diffs for newly-added bare call sites (no `pkg.`
    prefix, not a method call) and verifies each name resolves to a
    top-level definition in the SAME PACKAGE DIRECTORY. Names that
    grep finds zero matches for are phantom — a `go build` compile
    error the LLM worker routinely misses.

    Guards:
      * Skips files using dot-imports (can't disambiguate)
      * Filters Go keywords + built-ins (`len`, `append`, `make`, …)
      * Caps `max_symbols_checked` grep calls per PR
      * `grep_timeout_s` subprocess timeout per symbol
      * Fails soft — any exception returns current findings unchanged
    """
    if not workspace_path or not file_diffs:
        return []

    found: List[Dict[str, str]] = []
    checked = 0
    seen_names: set = set()

    for file_path, diff_text in file_diffs.items():
        if not file_path.endswith(".go"):
            continue
        # Test files routinely reference _test helpers that live across
        # package boundaries; scope out to avoid noise.
        if file_path.endswith("_test.go"):
            continue
        if _go_dir_has_dot_import(file_path, workspace_path):
            continue
        locals_set = _extract_go_locals_from_diff(diff_text)
        for name, line in _extract_go_bare_references_from_diff(
            diff_text,
            skip_names=locals_set,
        ):
            if name in seen_names:
                continue
            if checked >= max_symbols_checked:
                break
            seen_names.add(name)
            checked += 1
            if _go_symbol_defined_anywhere(
                workspace_path,
                name,
                timeout_s=grep_timeout_s,
            ):
                continue
            found.append(
                {
                    "name": name,
                    "referenced_at": f"{file_path}:{line}",
                    "evidence": (
                        f"Deterministic workspace-wide grep for "
                        f"`func/var/const/type {name}` in any `.go` "
                        f"file → 0 matches. Bare identifier reference "
                        f"will fail `go build` with 'undefined: {name}'."
                    ),
                }
            )
        if checked >= max_symbols_checked:
            break
    return found


# ---------------------------------------------------------------------------
# P13-Java — deterministic phantom class reference detector for Java.
# ---------------------------------------------------------------------------
# Targets phantom class references (can't compile) introduced by a PR:
#   * `new Foo(...)`
#   * `Foo var = ...` (type declaration)
#   * `Foo.staticMethod(...)` (static entry)
#   * `<Foo>` (generic parameter)
# Verification: the class must be either
#   (a) imported in the file (read actual file content, not just diff);
#   (b) defined in same-package `.java` files; or
#   (c) a java.lang.* implicit import.
# ---------------------------------------------------------------------------

_JAVA_CLASS_REF_PATTERNS: List[re.Pattern[str]] = [
    # new ClassName(
    re.compile(r"\bnew\s+([A-Z][A-Za-z0-9_]*)\s*[(<]"),
    # ClassName.staticCall( — UPPER start disambiguates class from variable
    re.compile(r"(?:^|[\s=,(\[{;])([A-Z][A-Za-z0-9_]*)\.[a-z_][A-Za-z0-9_]*\s*\("),
    # <ClassName> or <ClassName, …> — generic parameters
    re.compile(r"<\s*([A-Z][A-Za-z0-9_]*)\s*(?:,|>)"),
    # extends/implements/throws ClassName
    re.compile(r"\b(?:extends|implements|throws)\s+([A-Z][A-Za-z0-9_]*)"),
]

_JAVA_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.(\*|\w+))?\s*;\s*$",
    re.MULTILINE,
)
_JAVA_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)

# java.lang.* classes are implicitly imported in every Java file.
# List covers the standard classes + common exceptions. If a rare one
# is missing (e.g. `ClassValue`) the scanner may over-flag — but these
# are rare enough in PR diffs that we prefer the cleaner filter.
_JAVA_LANG_CLASSES: set[str] = {
    # primitives wrappers
    "Boolean",
    "Byte",
    "Character",
    "Double",
    "Float",
    "Integer",
    "Long",
    "Short",
    "Void",
    # core
    "Object",
    "String",
    "StringBuilder",
    "StringBuffer",
    "Math",
    "System",
    "Thread",
    "ThreadGroup",
    "ThreadLocal",
    "Runtime",
    "Process",
    "ProcessBuilder",
    "Class",
    "ClassLoader",
    "Package",
    "Enum",
    "Record",
    "Number",
    "Comparable",
    "Iterable",
    "Readable",
    "CharSequence",
    "AutoCloseable",
    "Cloneable",
    "Runnable",
    # throwables
    "Throwable",
    "Exception",
    "Error",
    "RuntimeException",
    "NullPointerException",
    "IllegalArgumentException",
    "IllegalStateException",
    "UnsupportedOperationException",
    "ClassNotFoundException",
    "ClassCastException",
    "ArrayIndexOutOfBoundsException",
    "IndexOutOfBoundsException",
    "StringIndexOutOfBoundsException",
    "ArithmeticException",
    "NumberFormatException",
    "InterruptedException",
    "NoSuchMethodException",
    "NoSuchFieldException",
    "SecurityException",
    "OutOfMemoryError",
    "StackOverflowError",
    "AssertionError",
    "LinkageError",
    "NoClassDefFoundError",
    "VerifyError",
    "AbstractMethodError",
    "IncompatibleClassChangeError",
}
# Java primitive keywords (never class references)
_JAVA_PRIMITIVES: set[str] = {
    "void",
    "boolean",
    "byte",
    "char",
    "short",
    "int",
    "long",
    "float",
    "double",
}


def _parse_java_file_imports(workspace_path: str, file_path: str) -> tuple[set[str], set[str], str]:
    """Return (imported_simple_names, star_imports_prefixes, own_package).

    Reads the actual file on disk (head 16KB is enough — imports are at top).
    Handles both `import com.foo.Bar;` (adds `Bar` to the simple-name set)
    and `import com.foo.*;` (adds `com.foo` to the star set).
    """
    import os as _os

    full = _os.path.join(workspace_path, file_path)
    imported_simple: set[str] = set()
    star_imports: set[str] = set()
    own_package = ""
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            head = f.read(16384)
    except Exception:
        return (imported_simple, star_imports, own_package)

    pkg_m = _JAVA_PACKAGE_RE.search(head)
    if pkg_m:
        own_package = pkg_m.group(1)

    for m in _JAVA_IMPORT_RE.finditer(head):
        body = m.group(1)
        tail = m.group(2)
        if tail == "*":
            star_imports.add(body)  # e.g. `com.foo` from `import com.foo.*;`
        else:
            simple = tail if tail else body.rsplit(".", 1)[-1]
            imported_simple.add(simple)
    return (imported_simple, star_imports, own_package)


def _java_source_set_peers(package_dir: str) -> List[str]:
    """Return the package_dir plus its Maven/Gradle source-set peers.

    In Maven/Gradle conventions, ``src/main/java/com/foo`` and
    ``src/test/java/com/foo`` (plus less-common ``src/integrationTest/java``)
    hold the same Java package — classes in ``main`` are visible from
    ``test`` by the "same package" rule without an import statement.

    Returns the input dir first (unchanged), followed by any peer
    directories that exist logically (caller verifies on disk). If the
    input doesn't sit under a known source-set root, returns just
    the input unchanged.

    Regression target: PR #14161 flagged `PaymentController` as phantom
    from `src/test/java/.../controller/PaymentControllerTest.java`
    because the scanner only checked the test package-dir. The class
    lives in the peer `src/main/java/.../controller/PaymentController.java`.
    """
    peers: List[str] = [package_dir]
    known_roots = ("src/test/java/", "src/main/java/", "src/integrationTest/java/")
    matched_root = None
    for root in known_roots:
        if f"/{root}" in f"/{package_dir}/" or package_dir.startswith(root):
            matched_root = root
            break
    if not matched_root:
        return peers
    for other in known_roots:
        if other == matched_root:
            continue
        peer = package_dir.replace(matched_root, other, 1)
        if peer != package_dir and peer not in peers:
            peers.append(peer)
    return peers


def _java_class_defined_in_package(
    workspace_path: str,
    package_dir: str,
    name: str,
    *,
    timeout_s: float = 8.0,
) -> bool:
    """Grep package dir (+ its Maven source-set peers) for a top-level
    class/interface/enum/record definition.

    For a file in ``src/test/java/com/foo/``, also checks the peer
    ``src/main/java/com/foo/`` (same Java package, different source
    root). Without this check we false-positive every
    ``FooControllerTest`` that references ``FooController`` in the
    same package.
    """
    import os as _os
    import subprocess

    candidate_files: List[str] = []
    for peer in _java_source_set_peers(package_dir):
        full_dir = _os.path.join(workspace_path, peer)
        if not _os.path.isdir(full_dir):
            continue
        try:
            candidate_files.extend(_os.path.join(full_dir, f) for f in _os.listdir(full_dir) if f.endswith(".java"))
        except OSError:
            continue

    if not candidate_files:
        return True  # fail-safe when no peer resolves on disk

    esc = re.escape(name)
    # POSIX ERE (grep -E): inside [...] the \w/\s escapes are LITERAL, so use
    # [[:space:]] etc. Two declaration shapes:
    #   1. type decl — class/interface/enum/record/@interface Name
    #   2. field/constant decl — <modifier> ... NAME = ...  (e.g.
    #      `private static final Set<String> BRITISH_OR_IRISH_NATIONALITIES =`)
    # Shape 2 is the degraded-index backstop: P13's Java ref pattern mistakes
    # `CONST.method()` for a class, so without it a same-package constant gets
    # false-flagged as a phantom when find_symbol's index is unavailable. The
    # leading modifier keyword + trailing `=` keep it to declarations, not uses.
    pattern = (
        rf"^[[:space:]]*(public[[:space:]]+|private[[:space:]]+|protected[[:space:]]+)?"
        rf"(abstract[[:space:]]+|final[[:space:]]+|static[[:space:]]+|sealed[[:space:]]+)*"
        rf"(class|interface|enum|record|@interface)[[:space:]]+{esc}([[:space:]]|<|\{{|$)"
        rf"|^[[:space:]]*(public|private|protected|static|final|volatile|transient)[[:space:]].*"
        rf"[[:space:]]{esc}[[:space:]]*="
    )
    try:
        r = subprocess.run(
            ["grep", "-E", "-l", "--max-count=1", pattern, *candidate_files],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
        return r.returncode != 1
    except Exception:
        return True


def _extract_java_class_refs_from_diff(diff_text: str) -> List[tuple[str, int]]:
    """Yield (class_name, new_line_number) for every class reference
    in `+` lines. Best-effort: skips obvious comment/string-only lines."""
    results: List[tuple[str, int]] = []
    current_new_line = 0
    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            m = _DIFF_HUNK_HEADER_RE.match(raw)
            if m:
                current_new_line = int(m.group(1))
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw.startswith("+"):
            body = raw[1:]
            stripped = body.strip()
            if (
                stripped.startswith("//")
                or stripped.startswith("/*")
                or stripped.startswith("*")
                or stripped.startswith("import ")
                or stripped.startswith("package ")
                or not stripped
            ):
                if not raw.startswith("-"):
                    current_new_line += 1
                continue
            # Drop inline // comment tail
            if "//" in body:
                body = body.split("//", 1)[0]
            for pat in _JAVA_CLASS_REF_PATTERNS:
                for m in pat.finditer(body):
                    name = m.group(1)
                    if name in _JAVA_PRIMITIVES:
                        continue
                    results.append((name, current_new_line))
        if not raw.startswith("-"):
            current_new_line += 1
    return results


def _scan_new_java_references_for_missing(
    workspace_path: str,
    file_diffs: Dict[str, str],
    *,
    max_symbols_checked: int = 24,
    grep_timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """P13-Java — Deterministic Java phantom class reference detector.

    Scans `.java` file diffs for newly-referenced class names (via
    `new X(`, `X var =`, `X.staticMethod(`, `<X>`, `extends X`, etc.).
    A class name is a phantom if it is NOT:
      * imported in the file (parsed from actual file content);
      * covered by a `com.foo.*` star import (conservative: we skip
        these, cannot verify without FQN resolution);
      * defined in a same-package `.java` file;
      * a java.lang.* implicit import.

    Guards:
      * Caps `max_symbols_checked` grep calls per PR
      * `grep_timeout_s` subprocess timeout per symbol
      * Fails soft on any error
    """
    import os as _os

    if not workspace_path or not file_diffs:
        return []

    found: List[Dict[str, str]] = []
    checked = 0
    # Dedup globally across the PR — same (file, name) reported only
    # once even if referenced multiple times.
    seen: set = set()

    for file_path, diff_text in file_diffs.items():
        if not file_path.endswith(".java"):
            continue
        imported, star_imports, _pkg = _parse_java_file_imports(
            workspace_path,
            file_path,
        )
        # If the file uses star-imports, MVP skips the file to avoid
        # false positives. Star-imports hide which exact class names
        # are available.
        if star_imports:
            continue
        package_dir = _os.path.dirname(file_path)
        for name, line in _extract_java_class_refs_from_diff(diff_text):
            key = (file_path, name)
            if key in seen:
                continue
            if checked >= max_symbols_checked:
                break
            seen.add(key)
            # Filter: java.lang, explicitly imported, or same-package
            if name in _JAVA_LANG_CLASSES:
                continue
            if name in imported:
                continue
            checked += 1
            if _java_class_defined_in_package(
                workspace_path,
                package_dir,
                name,
                timeout_s=grep_timeout_s,
            ):
                continue
            found.append(
                {
                    "name": name,
                    "referenced_at": f"{file_path}:{line}",
                    "evidence": (
                        f"Deterministic grep for `class/interface/enum/"
                        f"record {name}` in Java package directory "
                        f"`{package_dir}/` → 0 matches; `{name}` is not "
                        f"imported in the file nor in `java.lang`. "
                        f"Compilation will fail with 'cannot find symbol: "
                        f"class {name}'."
                    ),
                }
            )
        if checked >= max_symbols_checked:
            break
    return found


# P14 — Mechanical stub-function detector (Python + Go).
# A "stub function" is one whose body unconditionally returns a
# "not implemented" sentinel. In a PR that ostensibly adds new
# functionality, every stub should either be TODO-tagged OR be
# obviously not called — anything else is a bug. We look for two
# shapes:
#   Go  : `return ..., errors.New("not implemented")` / `return errors.New("not implemented")`
#   Py  : `raise NotImplementedError`
# Then we scan the diff for callers of those functions. A call-site
# inside the diff is a strong signal the stub is live code path, not
# a TODO.
_GO_STUB_BODY_RE = re.compile(
    r"""^\s*return\s+                       # return statement
        (?:[^,]+,\s*)?                      # optional first tuple element
        errors\.New\(
        ["'](?:not\ implemented|Not\ Implemented|TODO:?\s*implement)["']
        \)\s*$""",
    re.VERBOSE | re.MULTILINE,
)
_PY_STUB_BODY_RE = re.compile(
    r"^\s*raise\s+NotImplementedError\b",
    re.MULTILINE,
)
# Java: `throw new UnsupportedOperationException(...)` is the canonical
# "stub" pattern. `NotImplementedException` is Apache Commons. For
# generic runtime exceptions we require the message to mention "not
# implemented" / "not supported" to avoid flagging legitimate errors.
_JAVA_STUB_BODY_RE = re.compile(
    r"""^\s*throw\s+new\s+
        (?:
            UnsupportedOperationException\s*\([^)]*\)
            |
            NotImplementedException\s*\([^)]*\)
            |
            (?:RuntimeException|AssertionError|IllegalStateException)
            \s*\(\s*["'][^"']*
            (?:not\s*implement|Not\s*Implement|not\s*supported|Not\s*Supported)
            [^"']*["']\s*\)
        )
        \s*;\s*$""",
    re.VERBOSE | re.MULTILINE,
)
_GO_FUNC_HEADER_RE = re.compile(
    r"^\+func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(\w+)\s*\(",
)
_PY_FUNC_HEADER_RE = re.compile(
    r"^\+\s*def\s+(\w+)\s*\(",
)
# Java method declaration: optional annotations on same line, one or
# more modifiers, optional generic-type parameter, a return type, the
# method name, open paren, closing paren, and opening brace — all on
# the same line. Multi-line signatures are rare in stubs; accept the
# single-line shape as sufficient.
_JAVA_FUNC_HEADER_RE = re.compile(
    r"""^\+\s*
        (?:@\w+(?:\([^)]*\))?\s+)*
        (?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)+
        (?:<[^>]+>\s+)?
        (?:[\w.<>\[\],\s?]+?\s+)?
        (\w+)
        \s*\(""",
    re.VERBOSE,
)
# Same-line marker that a Java code line is (probably) a method
# declaration — used during call-site scanning to exclude declarations
# from being counted as calls when the stub method is also declared in
# the diff (e.g. interface + impl both in scope).
_JAVA_METHOD_DECL_MARKER_RE = re.compile(
    r"^(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)+"
)


def _scan_for_stub_call_sites(
    file_diffs: Dict[str, str],
) -> List[Dict[str, str]]:
    """P14 — Detect stub functions introduced by the PR and match them
    against call sites also in the diff. Returns one dict per detected
    (stub_name, caller_site) pair.

    Operates purely on the diff text; no workspace read. Narrow by
    design: we only flag stubs whose function body in the diff
    contains a literal "not implemented" error return, and we only
    flag call sites that are also added by the diff. This avoids
    flagging legitimate TODO placeholders.
    """
    if not file_diffs:
        return []

    # Step 1: enumerate new stub functions.
    #   { name -> (file, line_in_new_file) }
    stubs: Dict[str, tuple] = {}
    for file_path, diff_text in file_diffs.items():
        is_go = file_path.endswith(".go")
        is_py = file_path.endswith(".py")
        is_java = file_path.endswith(".java")
        if not (is_go or is_py or is_java):
            continue
        # Walk diff line by line tracking hunks + function bodies.
        current_new_line = 0
        current_fn_name: Optional[str] = None
        current_fn_body_lines: List[str] = []
        current_fn_decl_line: int = 0
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = _DIFF_HUNK_HEADER_RE.match(raw)
                if m:
                    current_new_line = int(m.group(1))
                current_fn_name = None
                current_fn_body_lines = []
                continue
            if raw.startswith("---") or raw.startswith("+++"):
                continue
            is_addition = raw.startswith("+")
            if is_addition:
                if is_go:
                    header_re = _GO_FUNC_HEADER_RE
                elif is_py:
                    header_re = _PY_FUNC_HEADER_RE
                else:  # is_java
                    header_re = _JAVA_FUNC_HEADER_RE
                hm = header_re.match(raw)
                if hm:
                    # New function declaration — reset buffer.
                    current_fn_name = hm.group(1)
                    current_fn_decl_line = current_new_line
                    current_fn_body_lines = [raw]
                elif current_fn_name:
                    current_fn_body_lines.append(raw)
                    # Check for closing `}` (Go or Java) or a stub line
                    # body (Python). For Java the `}` is usually indented
                    # (method-in-class); for Go it's usually column 0.
                    # Accept either case.
                    is_brace_close = (is_go or is_java) and raw.startswith("+") and raw[1:].strip() == "}"
                    if is_brace_close:
                        body = "\n".join(ln.lstrip("+ \t") for ln in current_fn_body_lines)
                        body_re = _GO_STUB_BODY_RE if is_go else _JAVA_STUB_BODY_RE
                        if body_re.search(body):
                            stubs[current_fn_name] = (
                                file_path,
                                current_fn_decl_line,
                            )
                        current_fn_name = None
                        current_fn_body_lines = []
                    elif is_py:
                        # Strip leading `+` to match against clean code.
                        code_line = raw[1:] if raw.startswith("+") else raw
                        if _PY_STUB_BODY_RE.search(code_line):
                            stubs[current_fn_name] = (
                                file_path,
                                current_fn_decl_line,
                            )
                        # don't reset — Python fn may have more lines
            if not raw.startswith("-"):
                current_new_line += 1

    if not stubs:
        return []

    # Step 2: scan diff for callers of those stub names. Callers can
    # live on + lines (newly added calls) OR unchanged context lines
    # (pre-existing call sites that now hit a NEW stub because the
    # stub's definition was just introduced). We skip - lines (removed)
    # and function-declaration lines (avoid matching `func Name(` or
    # `def Name(` as a call of Name).
    findings: List[Dict[str, str]] = []
    # Call pattern: Name( not preceded by `func ` (Go), `def ` (Python),
    # `type `, `class `, or a period alone (to keep `obj.method(`).
    # Use a look-behind check via pre-filter.
    _DECL_PREFIXES = ("func ", "def ", "type ", "class ")
    for stub_name, (stub_file, stub_line) in stubs.items():
        call_re = re.compile(rf"\b{re.escape(stub_name)}\s*\(")
        for file_path, diff_text in file_diffs.items():
            current_new_line = 0
            seen_sites: set = set()
            for raw in diff_text.splitlines():
                if raw.startswith("@@"):
                    m = _DIFF_HUNK_HEADER_RE.match(raw)
                    if m:
                        current_new_line = int(m.group(1))
                    continue
                if raw.startswith("---") or raw.startswith("+++"):
                    continue
                # Skip removed lines entirely.
                if raw.startswith("-"):
                    continue
                is_code_line = raw.startswith(("+", " "))
                if is_code_line and call_re.search(raw):
                    # Strip leading +/space to examine the code.
                    code = raw[1:] if raw.startswith(("+", " ")) else raw
                    code_stripped = code.lstrip()
                    # Skip function/class declarations that happen to
                    # share the name (`func TablesList(...)` is NOT a
                    # call to TablesList, it's defining a same-name
                    # function in a different package/receiver).
                    is_decl = any(code_stripped.startswith(p) for p in _DECL_PREFIXES)
                    # Java method declarations start with annotations /
                    # modifier keywords (`public Foo(...)`, `@Override
                    # public <T> Foo()`). Treat any line whose stripped
                    # prefix matches that shape as a decl, not a call.
                    if not is_decl and file_path.endswith(".java"):
                        is_decl = bool(_JAVA_METHOD_DECL_MARKER_RE.match(code_stripped))
                    # Also skip the stub definition line itself.
                    is_self_site = file_path == stub_file and current_new_line == stub_line
                    if not is_decl and not is_self_site:
                        key = (file_path, current_new_line)
                        if key not in seen_sites:
                            seen_sites.add(key)
                            findings.append(
                                {
                                    "stub_name": stub_name,
                                    "stub_file": stub_file,
                                    "stub_line": str(stub_line),
                                    "caller_file": file_path,
                                    "caller_line": str(current_new_line),
                                }
                            )
                # Advance new-file counter for + and context lines.
                current_new_line += 1
    return findings


def _inject_stub_caller_findings(
    findings: List[Dict[str, Any]],
    file_diffs: Dict[str, str],
) -> tuple[List[Dict[str, Any]], int]:
    """P14 injection — turn (stub, caller) pairs into synthetic findings.

    Each finding points at the CALLER site with a high-confidence
    'calls a stub function that always returns not implemented'
    description. Skips injection if the coordinator already flagged
    the caller site at approximately the same line (±3).

    Returns (findings_with_injections, injected_count).
    """
    if not file_diffs:
        return (findings, 0)

    pairs = _scan_for_stub_call_sites(file_diffs)
    if not pairs:
        return (findings, 0)

    result = list(findings)
    injected = 0
    for p in pairs:
        caller_file = p["caller_file"]
        try:
            caller_line = int(p["caller_line"])
        except (ValueError, TypeError):
            continue
        stub_name = p["stub_name"]
        # Skip if an existing finding covers this (file, ±3 lines).
        covered = False
        for f in result:
            if f.get("file") != caller_file:
                continue
            fl = int(f.get("start_line") or 0)
            if abs(fl - caller_line) <= 3:
                # Also check the finding mentions the stub or concept.
                title = str(f.get("title", "") or "")
                if stub_name in title or "stub" in title.lower() or "not implemented" in title.lower():
                    covered = True
                    break
        if covered:
            continue
        synthetic = {
            "title": (
                f"Call to `{stub_name}()` hits a stub that always " f"returns 'not implemented' — runtime failure"
            ),
            "severity": "high",
            "confidence": 0.95,
            "file": caller_file,
            "start_line": caller_line,
            "end_line": caller_line,
            "evidence": [
                f"Stub definition at `{p['stub_file']}:{p['stub_line']}` "
                f"returns an error literal 'not implemented' / "
                f"NotImplementedError.",
                f"Call site at `{caller_file}:{caller_line}` invokes "
                f"the stub and does not guard against the failure.",
            ],
            "risk": (
                f"Every code path that reaches `{caller_file}:"
                f"{caller_line}` will surface the 'not implemented' "
                f"error to the user. The feature being implemented in "
                f"this PR is unshippable until the stub is filled in."
            ),
            "suggested_fix": (
                f"Either implement `{stub_name}` at "
                f"`{p['stub_file']}:{p['stub_line']}` with real logic, "
                f"or gate the caller behind a feature flag / explicit "
                f"'unsupported' error response until the implementation "
                f"lands."
            ),
            "category": "correctness",
            "_injected_from": "p14_stub_caller",
        }
        result.append(synthetic)
        injected += 1
    return (result, injected)


_EXISTS_NEGATION_MARKERS = (
    "does not exist",
    "doesn't exist",
    "not defined",
    "undefined",
    "is missing",
    "never defined",
    "no such symbol",
    "importerror",
    "nameerror",
    "not found in",
    "could not be found",
)


def _finding_claims_symbol_missing(finding: Dict[str, Any], symbol: str) -> bool:
    """Heuristic: does this finding claim `symbol` is missing/undefined?

    Used by the Phase 2 reflection pass to catch findings whose premise
    contradicts an `exists=True` fact. We match only when the finding
    BOTH mentions the symbol AND uses existence-negation phrasing —
    mentioning the symbol alone is not enough (many real bugs involve
    existing symbols)."""
    if not symbol:
        return False
    haystack_parts: List[str] = [
        str(finding.get("title", "") or ""),
        str(finding.get("risk", "") or ""),
        str(finding.get("suggested_fix", "") or ""),
    ]
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        haystack_parts.extend(str(e) for e in evidence)
    elif isinstance(evidence, str):
        haystack_parts.append(evidence)
    haystack = " ".join(haystack_parts)
    if symbol not in haystack:
        return False
    lowered = haystack.lower()
    return any(marker in lowered for marker in _EXISTS_NEGATION_MARKERS)


def _reflect_against_phase2_facts(
    findings: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """External-signal reflection (P8).

    Drops findings whose premise is contradicted by Phase 2 existence
    facts. The mechanical rule — deliberately narrow to avoid over-
    filtering — is:

        If the finding claims "symbol X doesn't exist / is undefined /
        will raise ImportError" AND Phase 2 recorded ``exists=True`` for
        X, drop the finding. Its premise is demonstrably wrong.

    Injected Phase 2 findings (``_injected_from`` set) are never dropped
    by this pass — they came FROM the facts, so they cannot contradict.

    Returns (kept_findings, dropped_count). Safe when no FactStore is
    active (returns input unchanged).
    """
    from app.scratchpad import current_factstore

    store = current_factstore()
    if store is None:
        return (findings, 0)

    try:
        present = list(store.iter_existence(exists=True))
    except Exception as exc:
        logger.warning(
            "[PR Brain v2] reflection pass skipped — iter_existence failed: %s",
            exc,
        )
        return (findings, 0)

    if not present:
        return (findings, 0)

    present_symbols = {p.symbol_name for p in present if p.symbol_name}
    if not present_symbols:
        return (findings, 0)

    kept: List[Dict[str, Any]] = []
    dropped = 0
    for f in findings:
        if f.get("_injected_from"):
            kept.append(f)
            continue
        contradicted = False
        for symbol in present_symbols:
            if _finding_claims_symbol_missing(f, symbol):
                logger.info(
                    "[PR Brain v2] Reflection drop: finding %r claims "
                    "`%s` is missing but Phase 2 confirmed exists=True",
                    f.get("title", "")[:80],
                    symbol,
                )
                contradicted = True
                break
        if contradicted:
            dropped += 1
        else:
            kept.append(f)
    return (kept, dropped)


def _filter_findings_to_diff_scope(
    findings: List[Dict[str, Any]],
    file_diffs: Dict[str, str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Per-finding diff-scope verification (P11 cheap).

    Inspired by Claude Code `/ultrareview`'s "every reported finding is
    independently reproduced and verified". This is the mechanical half
    of that pattern — an LLM-free check that a finding's file is actually
    touched by the diff. Findings that point at files the PR does not
    modify are almost always coordinator hallucinations (e.g. it confused
    a cross-file reference with a diff change).

    Kept injected findings untouched — Phase 2 may flag a diff file's
    reference to a symbol defined in an un-touched file, and that is
    legitimate scope.

    Returns (kept, demoted, demoted_count). Demoted findings are handed
    back so the caller can append them to the secondary-notes block.
    """
    if not file_diffs or not findings:
        return (list(findings), [], 0)

    touched_files = set(file_diffs.keys())
    # Allow trailing-slash / normalisation mismatches by also matching
    # basename when the coordinator reported a short path.
    touched_basenames = {p.rsplit("/", 1)[-1] for p in touched_files}

    kept: List[Dict[str, Any]] = []
    demoted: List[Dict[str, Any]] = []
    demoted_count = 0

    for f in findings:
        if f.get("_injected_from"):
            kept.append(f)
            continue
        file_claim = str(f.get("file", "") or "").strip()
        if not file_claim:
            kept.append(f)
            continue
        base = file_claim.rsplit("/", 1)[-1]
        in_diff = file_claim in touched_files or base in touched_basenames
        if in_diff:
            kept.append(f)
            continue
        logger.info(
            "[PR Brain v2] Diff-scope drop: finding %r targets `%s` " "which is not in the PR diff (touched: %d files)",
            f.get("title", "")[:80],
            file_claim,
            len(touched_files),
        )
        f = {**f, "_demoted_reason": "file_not_in_diff"}
        demoted.append(f)
        demoted_count += 1

    return (kept, demoted, demoted_count)


def _recompute_merge_recommendation(findings: List[Dict[str, Any]], fallback: str) -> str:
    """Recompute the merge vote from surviving dict findings after demotion.

    Mirrors ``code_review.shared.merge_recommendation`` (which needs ReviewFinding
    objects) on the dict shape used during precision filtering: any critical/high
    → request_changes; 3+ medium → request_changes; 1-2 medium →
    approve_with_followups; otherwise approve. Skips non-defect severities
    (nit/praise). Falls back to the prior value only if findings is malformed.
    """
    try:
        sev = [str(f.get("severity", "")).lower() for f in findings]
    except Exception:
        return fallback
    blocking = sum(1 for s in sev if s in ("critical", "high"))
    medium = sum(1 for s in sev if s in ("medium", "warning"))
    if blocking > 0 or medium >= 3:
        return "request_changes"
    if medium >= 1:
        return "approve_with_followups"
    return "approve"


def _filter_findings_describing_own_fix(
    findings: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Demote findings that describe the PR's OWN fix as a defect.

    Failure mode (PR 14442): the coordinator reads a bug that the diff *removes*
    (lives only on `-` lines), then reports it as an outstanding high-severity
    issue and votes Request Changes — telling the author to fix what they already
    fixed. The tell is a self-contradiction in the finding's own text: it both
    describes the old broken code AND states the PR fixes it.

    Conservative by design — only demotes on an explicit "the PR fixes/corrects
    this" phrase (optionally corroborated by old-vs-new framing). A genuine defect
    never says the PR already fixes it, so false-demotes are highly unlikely. The
    coordinator skill is the primary guard; this is the mechanical backstop.

    Injected findings (Phase 2 existence facts) are never touched. Returns
    (kept, demoted, demoted_count); demoted go to secondary observations.
    """
    if not findings:
        return ([], [], 0)

    import re as _re

    # "the PR (correctly) fixes/resolves/addresses this", "this PR fixes",
    # "new code (correctly) fixes", "is fixed by this change", etc.
    _fix_claim = _re.compile(
        r"\b(?:this\s+)?(?:pr|change|diff|commit|new\s+code)\b[^.]{0,60}"
        r"\b(?:correctly\s+)?(?:fix|fixes|fixed|resolv\w*|address\w*|correct\w*|eliminat\w*|avoid\w*)\b",
        _re.IGNORECASE,
    )
    # inverse phrasing: "fixed by this PR", "resolved by the new code"
    _fix_claim_rev = _re.compile(
        r"\b(?:fix\w*|resolv\w*|address\w*|correct\w*|eliminat\w*|avoid\w*)\b[^.]{0,40}"
        r"\bby\s+(?:this\s+)?(?:pr|change|diff|commit|the\s+new\s+code)\b",
        _re.IGNORECASE,
    )

    kept: List[Dict[str, Any]] = []
    demoted: List[Dict[str, Any]] = []
    count = 0
    for f in findings:
        if f.get("_injected_from"):
            kept.append(f)
            continue
        # Scan the finding's own narrative fields for a self-fix claim.
        blob = (
            " ".join(str(f.get(k, "") or "") for k in ("title", "risk", "suggested_fix", "reasoning"))
            + " "
            + " ".join(str(e) for e in (f.get("evidence") or []))
        )
        if _fix_claim.search(blob) or _fix_claim_rev.search(blob):
            logger.info(
                "[PR Brain v2] Fix-as-defect drop: finding %r asserts the PR "
                "already fixes the problem — not an outstanding defect",
                str(f.get("title", ""))[:80],
            )
            demoted.append({**f, "_demoted_reason": "describes_own_fix"})
            count += 1
        else:
            kept.append(f)
    return (kept, demoted, count)


def _extract_single_verdict(raw: str) -> str:
    """Parse the single-finding verifier's JSON. Returns one of
    confirmed / refuted / unclear. Defaults to unclear on parse failure."""
    import json as _json
    import re as _re

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = list(reversed(fenced)) if fenced else [raw[max(0, raw.rfind("{")) :]]
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, dict) and "verdict" in parsed:
                v = str(parsed["verdict"]).lower()
                if v in ("confirmed", "refuted", "unclear"):
                    return v
        except (ValueError, _json.JSONDecodeError):
            continue
    return "unclear"


def _extract_batch_verdicts(raw: str, expected_count: int) -> List[str]:
    """Parse the batch verifier's JSON. Returns verdict list aligned to
    input order. Missing / malformed entries default to 'unclear'."""
    import json as _json
    import re as _re

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = list(reversed(fenced))
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if not isinstance(parsed, dict):
                continue
            verdicts_list = parsed.get("verdicts") or []
            if not isinstance(verdicts_list, list):
                continue
            # Build index→verdict map first so out-of-order lists are handled.
            verdict_map: Dict[int, str] = {}
            for item in verdicts_list:
                if not isinstance(item, dict):
                    continue
                idx = item.get("finding_index")
                v = str(item.get("verdict", "")).lower()
                if isinstance(idx, int) and v in ("confirmed", "refuted", "unclear"):
                    verdict_map[idx] = v
            if verdict_map:
                return [verdict_map.get(i, "unclear") for i in range(expected_count)]
        except (ValueError, _json.JSONDecodeError):
            continue
    return ["unclear"] * expected_count


def _parse_existence_json(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of the existence-worker's JSON output.

    Accepts:
      * Fenced ```json {...} ``` blocks (prefer the LAST — models often
        restate near the end)
      * Bare JSON object with "symbols" key anywhere in the text

    Returns the dict on success, ``None`` on failure.
    """
    import json as _json
    import re as _re

    if not raw:
        return None

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates: list = list(reversed(fenced))
    if not candidates:
        # Fallback: find a top-level {..} with "symbols" key
        for start in range(len(raw) - 1, -1, -1):
            if raw[start] != "{":
                continue
            depth = 0
            for end in range(start, len(raw)):
                if raw[end] == "{":
                    depth += 1
                elif raw[end] == "}":
                    depth -= 1
                    if depth == 0:
                        snippet = raw[start : end + 1]
                        if '"symbols"' in snippet:
                            candidates.append(snippet)
                        break
            if candidates:
                break
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, dict) and "symbols" in parsed:
                return parsed
        except (ValueError, _json.JSONDecodeError):
            continue
    return None


def _finding_to_dict(f: ReviewFinding) -> dict:
    """Convert a ReviewFinding to a serializable dict."""
    return {
        "title": f.title,
        "category": f.category.value,
        "severity": f.severity.value,
        "confidence": f.confidence,
        "file": f.file,
        "start_line": f.start_line,
        "end_line": f.end_line,
        "evidence": f.evidence,
        "risk": f.risk,
        "suggested_fix": f.suggested_fix,
        "agent": f.agent,
    }


# Public surface = every scanner function + constant table defined above.
__all__ = [n for n in dir() if n.startswith("_") and not n.startswith("__")]
