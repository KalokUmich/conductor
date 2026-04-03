"""Stack trace parser for Conductor chat.

Parses raw stack trace text (Python, JavaScript/TypeScript, Java, Go) into
structured StackFrame and ParsedStackTrace objects so the front-end can
render clickable, navigable frames.

Supported formats
-----------------
Python:
    Traceback (most recent call last):
      File "path/to/file.py", line 42, in function_name

JavaScript / TypeScript (V8 / Node):
    TypeError: Cannot read properties of null
        at functionName (path/to/file.ts:42:10)
        at path/to/file.ts:42:10          <- anonymous frame

Java:
    Exception in thread "main" java.lang.NullPointerException
        at com.example.App.process(App.java:42)

Go:
    goroutine 1 [running]:
    main.process(...)
            /path/to/file.go:42 +0x68
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

# ---------------------------------------------------------------------------
# Language enum
# ---------------------------------------------------------------------------


class StackTraceLanguage(str, Enum):
    """Detected language of the stack trace."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    JAVA = "java"
    GO = "go"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StackFrame:
    """A single frame extracted from a stack trace.

    Attributes:
        raw: Original text of the frame line.
        file_path: Raw file path as it appears in the trace (may be absolute).
        line_number: 1-based line number, or None if not present.
        column_number: 1-based column number (JS only), or None.
        function_name: Function / method name, or None.
        is_internal: True if the path looks like stdlib / node_modules / etc.
    """

    raw: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    column_number: Optional[int] = None
    function_name: Optional[str] = None
    is_internal: bool = False


@dataclass
class ParsedStackTrace:
    """A fully-parsed stack trace.

    Attributes:
        language: Detected language.
        error_type: Exception / error class name (e.g. ``AttributeError``).
        error_message: Human-readable error description.
        frames: Ordered list of stack frames (innermost last for Python/JS/Java,
                innermost first for Go).
        raw_text: The original, unmodified stack trace text.
    """

    language: StackTraceLanguage = StackTraceLanguage.UNKNOWN
    error_type: str = ""
    error_message: str = ""
    frames: List[StackFrame] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "language": self.language.value,
            "errorType": self.error_type,
            "errorMessage": self.error_message,
            "frames": [
                {
                    "raw": f.raw,
                    "filePath": f.file_path,
                    "lineNumber": f.line_number,
                    "columnNumber": f.column_number,
                    "functionName": f.function_name,
                    "isInternal": f.is_internal,
                }
                for f in self.frames
            ],
            "rawText": self.raw_text,
        }


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Python:  File "path/to/file.py", line 42, in my_func
_PY_FRAME = re.compile(r'\s*File\s+"([^"]+)",\s+line\s+(\d+),\s+in\s+(.+)')

# JS named:     at MyFunc (/abs/path/file.ts:42:10)
_JS_NAMED = re.compile(r"\s*at\s+(.+?)\s+\((.+?):(\d+):(\d+)\)")
# JS named without column:  at MyFunc (/abs/path/file.ts:42)
_JS_NAMED_NC = re.compile(r"\s*at\s+(.+?)\s+\((.+?):(\d+)\)")
# JS anonymous:     at /abs/path/file.ts:42:10
_JS_ANON = re.compile(r"\s*at\s+((?:\/|[A-Za-z]:\\|\.\.?\/|\w).+?):(\d+):(\d+)$")
# JS anonymous without column:  at /abs/path/file.ts:42
_JS_ANON_NC = re.compile(r"\s*at\s+((?:\/|[A-Za-z]:\\|\.\.?\/|\w).+?):(\d+)$")

# Java:     at com.example.App.method(FileName.java:42)
_JAVA_FRAME = re.compile(r"\s*at\s+([\w.$]+)\.([\w$<>[\]]+)\((.+?\.java):(\d+)\)")

# Go frame:   \t/path/to/file.go:42 +0x68
_GO_FILE = re.compile(r"^\t(.+\.go):(\d+)(?:\s+\+0x[0-9a-f]+)?$")
# Go function line (precedes file line):  main.process(...)
_GO_FUNC = re.compile(r"^(\S+)\(")


# ---------------------------------------------------------------------------
# Internal path detection
# ---------------------------------------------------------------------------

_INTERNAL_INDICATORS = (
    "node_modules",
    "/usr/lib/python",
    "/usr/local/lib/python",
    "<frozen ",
    "<string>",
    "/internal/",
    "site-packages",
    "/usr/local/go/src/",
    "/pkg/mod/",
    "java/",
    "sun/",
    "jdk/",
)


def _is_internal(path: str) -> bool:
    """Return True if the path looks like a standard-library or dependency file."""
    return any(ind in path for ind in _INTERNAL_INDICATORS)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def _detect_language(text: str) -> StackTraceLanguage:
    """Heuristically detect the language of a stack trace."""
    if "Traceback (most recent call last)" in text or re.search(r'File ".+", line \d+', text):
        return StackTraceLanguage.PYTHON

    if "goroutine" in text and re.search(r"\t.+\.go:\d+", text):
        return StackTraceLanguage.GO

    if re.search(r"\tat [\w.$]+\([\w]+\.java:\d+\)", text):
        return StackTraceLanguage.JAVA

    if re.search(r"\n?\s*at\s+.+?[:(]\d+", text):
        return StackTraceLanguage.JAVASCRIPT

    return StackTraceLanguage.UNKNOWN


# ---------------------------------------------------------------------------
# Per-language parsers
# ---------------------------------------------------------------------------


def _parse_python(lines: List[str], result: ParsedStackTrace) -> None:
    """Extract frames and error from a Python traceback."""
    for line in lines:
        m = _PY_FRAME.match(line)
        if m:
            file_path = m.group(1)
            result.frames.append(
                StackFrame(
                    raw=line.rstrip(),
                    file_path=file_path,
                    line_number=int(m.group(2)),
                    function_name=m.group(3).strip(),
                    is_internal=_is_internal(file_path),
                )
            )

    # Error is the last non-blank, non-frame line
    for line in reversed(lines):
        s = line.strip()
        if s and not s.startswith("File ") and not s.startswith("Traceback") and not s.startswith("During handling"):
            if ":" in s:
                etype, _, emsg = s.partition(":")
                result.error_type = etype.strip()
                result.error_message = emsg.strip()
            else:
                result.error_message = s
            break


def _parse_javascript(lines: List[str], result: ParsedStackTrace) -> None:
    """Extract frames and error from a Node.js / V8 stack trace."""
    for line in lines:
        # Try patterns in decreasing specificity
        m = _JS_NAMED.match(line)
        if m:
            file_path = m.group(2)
            result.frames.append(
                StackFrame(
                    raw=line.rstrip(),
                    file_path=file_path,
                    line_number=int(m.group(3)),
                    column_number=int(m.group(4)),
                    function_name=m.group(1).strip(),
                    is_internal=_is_internal(file_path),
                )
            )
            continue

        m = _JS_NAMED_NC.match(line)
        if m:
            file_path = m.group(2)
            result.frames.append(
                StackFrame(
                    raw=line.rstrip(),
                    file_path=file_path,
                    line_number=int(m.group(3)),
                    function_name=m.group(1).strip(),
                    is_internal=_is_internal(file_path),
                )
            )
            continue

        m = _JS_ANON.match(line)
        if m:
            file_path = m.group(1)
            result.frames.append(
                StackFrame(
                    raw=line.rstrip(),
                    file_path=file_path,
                    line_number=int(m.group(2)),
                    column_number=int(m.group(3)),
                    is_internal=_is_internal(file_path),
                )
            )
            continue

        m = _JS_ANON_NC.match(line)
        if m:
            file_path = m.group(1)
            result.frames.append(
                StackFrame(
                    raw=line.rstrip(),
                    file_path=file_path,
                    line_number=int(m.group(2)),
                    is_internal=_is_internal(file_path),
                )
            )

    # Error type is typically the very first line
    for line in lines:
        s = line.strip()
        if s and not s.startswith("at "):
            if ":" in s:
                etype, _, emsg = s.partition(":")
                result.error_type = etype.strip()
                result.error_message = emsg.strip()
            break


def _parse_java(lines: List[str], result: ParsedStackTrace) -> None:
    """Extract frames and error from a Java stack trace."""
    for line in lines:
        m = _JAVA_FRAME.match(line)
        if m:
            class_path, method, _file_name, line_no = (m.group(1), m.group(2), m.group(3), int(m.group(4)))
            # Convert com.example.App → com/example/App.java
            guessed_path = class_path.replace(".", "/") + ".java"
            result.frames.append(
                StackFrame(
                    raw=line.rstrip(),
                    file_path=guessed_path,
                    line_number=line_no,
                    function_name=f"{class_path.split('.')[-1]}.{method}",
                    is_internal=_is_internal(class_path),
                )
            )

    # Error: first line containing "Exception" or "Error"
    for line in lines:
        s = line.strip()
        if ("Exception" in s or "Error" in s) and not s.startswith("at "):
            if ":" in s:
                etype, _, emsg = s.partition(":")
                # Take just the last component of the exception class name
                result.error_type = etype.strip().split()[-1].split(".")[-1]
                result.error_message = emsg.strip()
            else:
                result.error_type = s.split()[-1].split(".")[-1]
            break


def _parse_go(lines: List[str], result: ParsedStackTrace) -> None:
    """Extract frames and error from a Go panic / goroutine dump."""
    pending_func: Optional[str] = None

    for line in lines:
        m = _GO_FILE.match(line)
        if m:
            file_path = m.group(1)
            result.frames.append(
                StackFrame(
                    raw=line.rstrip(),
                    file_path=file_path,
                    line_number=int(m.group(2)),
                    function_name=pending_func,
                    is_internal=_is_internal(file_path),
                )
            )
            pending_func = None
            continue

        fm = _GO_FUNC.match(line.strip())
        if fm and not line.startswith("goroutine"):
            pending_func = fm.group(1)

    # panic: <message>  or  runtime error: <message>
    for line in lines:
        s = line.strip()
        if s.startswith("panic:"):
            result.error_type = "panic"
            result.error_message = s[6:].strip()
            break
        if s.startswith("runtime error:"):
            result.error_type = "runtime error"
            result.error_message = s[14:].strip()
            break


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_stack_trace(text: str) -> ParsedStackTrace:
    """Parse a raw stack trace string into a :class:`ParsedStackTrace`.

    The language is auto-detected.  If detection fails, all parsers are tried
    in sequence until one produces frames.

    Args:
        text: Raw stack trace text pasted by the user.

    Returns:
        A :class:`ParsedStackTrace` instance (frames may be empty if the
        text is not a recognised stack trace format).
    """
    result = ParsedStackTrace(raw_text=text)
    result.language = _detect_language(text)
    lines = text.splitlines()

    dispatch = {
        StackTraceLanguage.PYTHON: _parse_python,
        StackTraceLanguage.JAVASCRIPT: _parse_javascript,
        StackTraceLanguage.JAVA: _parse_java,
        StackTraceLanguage.GO: _parse_go,
    }

    parser = dispatch.get(result.language)
    if parser:
        parser(lines, result)
    else:
        # Try all parsers, stop at first successful parse
        for lang, fn in dispatch.items():
            fn(lines, result)
            if result.frames:
                result.language = lang
                break

    return result
