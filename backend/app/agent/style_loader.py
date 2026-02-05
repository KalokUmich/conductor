"""Code style loader for AI agent.

Loads code style guidelines from .ai/code-style.md if it exists and is non-empty,
otherwise falls back to built-in Google style guidelines.
"""
import os
from pathlib import Path
from typing import Optional, Literal
from enum import Enum


class Language(str, Enum):
    """Supported programming languages."""
    JAVA = "java"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    GO = "go"
    JSON = "json"


# Built-in Google Style Guidelines
# Reference: https://google.github.io/styleguide/
GOOGLE_STYLE_GUIDELINES = {
    Language.JAVA: """# Java Code Style (Google Style)
Reference: https://google.github.io/styleguide/javaguide.html

## Source File Structure
- License/copyright info (if present)
- Package statement (not line-wrapped)
- Import statements (no wildcards, no line-wrapping)
- Exactly one top-level class

## Formatting
- Use 2 spaces for indentation (no tabs)
- Column limit: 100 characters
- One statement per line
- Line-wrapping: prefer to break at higher syntactic level
- Continuation lines indented at least +4 spaces

## Braces (K&R Style)
- Opening brace on same line (no line break before)
- Line break after opening brace
- Line break before closing brace
- Always use braces for if/else/for/do/while (even single statements)
- Empty blocks may be concise: `{}`

## Whitespace
- Single blank line between consecutive members of a class
- Space after keywords (if, for, catch), not before
- Space before opening brace `{`
- Spaces around binary/ternary operators

## Naming Conventions
- Classes/Interfaces: UpperCamelCase (e.g., `MyClass`, `Readable`)
- Methods: lowerCamelCase (e.g., `sendMessage`, `stop`)
- Constants: UPPER_SNAKE_CASE (e.g., `MAX_VALUE`, `EMPTY_ARRAY`)
- Non-constant fields: lowerCamelCase (e.g., `computedValues`)
- Parameters/Local variables: lowerCamelCase
- Type variables: single capital letter or name + T (e.g., `E`, `T`, `RequestT`)

## Imports
- No wildcard imports (static or otherwise)
- Static imports in one group, non-static in another
- Alphabetical order within groups (ASCII sort order)

## Javadoc
- Required for every public class, method, field
- Use `@param`, `@return`, `@throws` in that order
- First sentence is a summary fragment
- Block tags never empty

## Best Practices
- Always use @Override annotation
- Caught exceptions: never ignore silently
- Static members: qualify with class name, not instance
""",

    Language.JAVASCRIPT: """# JavaScript Code Style (Google Style)
Reference: https://google.github.io/styleguide/jsguide.html

## File Structure
- License/copyright (if present)
- @fileoverview JSDoc (if present)
- goog.module or ES imports
- The file's implementation

## Formatting
- Use 2 spaces for indentation (no tabs)
- Column limit: 80 characters
- Semicolons required at end of every statement
- Line-wrapping: break after operators (except `.`)
- Continuation lines indented at least +4 spaces

## Braces (K&R Style)
- Required for all control structures (if, else, for, do, while)
- No line break before opening brace
- Line break after opening brace and before closing brace

## Whitespace
- Single blank line between class methods
- Space after keywords (if, for, catch)
- Space before opening brace `{`
- Spaces around binary operators

## Naming Conventions
- Classes: UpperCamelCase (e.g., `MyClass`)
- Functions/methods: lowerCamelCase (e.g., `myFunction`)
- Constants: UPPER_SNAKE_CASE (e.g., `MAX_RETRY_COUNT`)
- Variables/parameters: lowerCamelCase (e.g., `myVariable`)
- Private properties: trailing underscore (e.g., `this.data_`)

## Declarations
- Use `const` by default
- Use `let` only when reassignment is needed
- Never use `var` (not block-scoped)
- One variable per declaration

## ES Modules
- Use named exports (not default exports)
- Import paths must include `.js` extension

## Strings
- Use single quotes for ordinary strings
- Use template literals for string interpolation
- Never use `eval()` or Function() constructor with strings

## Arrays and Objects
- Use trailing commas in multi-line literals
- Use literal syntax: `[]` not `new Array()`, `{}` not `new Object()`
- Prefer destructuring for accessing object properties

## JSDoc
- Use `/** */` for documentation
- Required for all public/exported functions
- Use `@param {Type} name`, `@return {Type}`, `@throws {Type}`
""",

    Language.PYTHON: """# Python Code Style (Google Style)
Reference: https://google.github.io/styleguide/pyguide.html

## Formatting
- Use 4 spaces for indentation (no tabs)
- Maximum line length: 80 characters
- Two blank lines between top-level definitions
- One blank line between method definitions in a class
- Use trailing commas in sequences when multi-line

## Naming Conventions
- Modules: lower_with_underscores (e.g., `my_module.py`)
- Classes/Exceptions: CapWords (e.g., `MyClass`, `MyError`)
- Functions/methods: lower_with_underscores (e.g., `my_function`)
- Constants: CAPS_WITH_UNDERSCORES (e.g., `MAX_VALUE`)
- Variables: lower_with_underscores (e.g., `my_variable`)
- Protected: single leading underscore (e.g., `_internal_var`)

## Imports
- One import per line (except `from x import a, b, c`)
- Group imports: stdlib, third-party, local application
- Absolute imports preferred over relative
- Avoid wildcard imports (`from x import *`)

## Docstrings (Google Style)
- Use triple double quotes
- First line: brief summary (imperative mood)
- Blank line after summary if more content
- Sections: Args, Returns, Raises, Yields, Examples

## Type Annotations
- Annotate function signatures (public APIs required)
- Use `Optional[X]` for parameters that can be None
- Use `Sequence`, `Mapping` over `list`, `dict` for params

## Exceptions
- Use built-in exceptions when appropriate
- Custom exceptions should inherit from `Exception`
- Never use bare `except:`, catch specific exceptions
- Minimize code in try block

## Best Practices
- Use `is None` or `is not None` for None checks
- Use implicit boolean evaluation for sequences (`if seq:`)
- Prefer list/dict comprehensions for simple cases
- Use `with` statement for resource management
- Avoid mutable default arguments
- Use f-strings for string formatting
""",

    Language.GO: """# Go Code Style (Google Style)
Reference: https://google.github.io/styleguide/go/

## Style Principles
1. Clarity: Code's purpose and rationale is clear to the reader
2. Simplicity: Accomplishes goals in the simplest way possible
3. Concision: High signal-to-noise ratio
4. Maintainability: Easy to modify correctly
5. Consistency: Consistent with broader codebase

## Formatting
- Use `gofmt` - all Go code must conform to its output
- Tabs for indentation (gofmt default)
- No fixed line length, but prefer refactoring over splitting

## Naming Conventions
- Use MixedCaps or mixedCaps, not underscores
- Exported names: UpperCamelCase (e.g., `MaxLength`)
- Unexported names: lowerCamelCase (e.g., `maxLength`)
- Package names: lowercase, single word, no underscores
- Interface names: method name + "er" suffix (e.g., `Reader`, `Writer`)
- Acronyms: consistent case (e.g., `URL` not `Url`)

## Package Design
- Short, lowercase names without underscores
- Package name should not repeat in exported names
  - Good: `http.Client`, Bad: `http.HTTPClient`
- Avoid meaningless names like `util`, `common`, `base`

## Error Handling
- Return errors as last return value
- Check errors immediately after function call
- Use `fmt.Errorf` with `%w` for error wrapping
- Error strings: lowercase, no punctuation at end

## Comments
- Package comment: precedes package clause
- Exported functions: start with function name
- Complete sentences with proper punctuation

## Declarations
- Group related declarations
- Prefer short variable names in small scopes
- Use `:=` for local variables, `var` for zero values
- Constants: use `const` block, iota for enums

## Best Practices
- Prefer returning early to reduce nesting
- Use named return values sparingly
- Use interfaces for abstraction, not implementation
- Keep interfaces small (1-3 methods)
- Accept interfaces, return concrete types
- Use context.Context for cancellation/timeouts
- Handle all error cases explicitly
- Prefer table-driven tests
""",

    Language.JSON: """# JSON Style Guide (Google Style)
Reference: https://google.github.io/styleguide/jsoncstyleguide.xml

## General Rules
- No comments in JSON (not valid JSON syntax)
- Use double quotes for all strings and property names
- Property values: boolean, number, string, object, array, or null
- Consider removing empty/null properties unless semantically required

## Property Names
- Use camelCase (e.g., `propertyName`, `firstName`)
- First character: letter, underscore, or dollar sign
- Avoid reserved JavaScript keywords
- Choose meaningful, descriptive names
- Plural names for arrays (e.g., `items`, `users`)
- Singular names for non-arrays (e.g., `user`, `address`)

## Data Structure
- Prefer flat structure over unnecessary nesting
- Group related data only when semantically meaningful

## Enum Values
- Represent enums as strings, not numbers
- Use UPPER_CASE for enum string values

## Date/Time Values
- Use RFC 3339 format for dates: `"2024-01-15T14:30:00.000Z"`
- Use ISO 8601 for durations

## Arrays
- Use for collections of similar items
- Items should be of consistent type
- Empty array `[]` preferred over null for missing collections

## Property Ordering
- `kind` property first (if present) - identifies object type
- `items` array last in data objects
- Other properties in logical order

## Reserved Properties (for APIs)
- `apiVersion`: API version string
- `data`: container for response data
- `error`: error information object
- `id`: unique identifier
- `items`: array of result items
- `kind`: object type identifier

## Best Practices
- Consistent property naming across entire API
- Document property types and constraints
- Use null for explicitly missing values, omit for optional
- Keep payloads reasonably sized
"""
}


class StyleSource(str, Enum):
    """Source of the code style."""
    CUSTOM = "custom"  # From .ai/code-style.md
    BUILTIN = "builtin"  # Google style fallback


class CodeStyleLoader:
    """Loads code style guidelines from file or built-in defaults."""

    DEFAULT_STYLE_PATH = ".ai/code-style.md"

    def __init__(self, workspace_root: Optional[str] = None):
        """
        Initialize the style loader.

        Args:
            workspace_root: Root directory of the workspace. If None, uses current directory.
        """
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()

    def _get_style_file_path(self) -> Path:
        """Get the full path to the code style file."""
        return self.workspace_root / self.DEFAULT_STYLE_PATH

    def _read_custom_style(self) -> Optional[str]:
        """
        Read custom style from .ai/code-style.md.

        Returns:
            The content of the file if it exists and is non-empty, None otherwise.
        """
        style_path = self._get_style_file_path()

        if not style_path.exists():
            return None

        try:
            content = style_path.read_text(encoding="utf-8").strip()
            if not content:
                return None
            return content
        except (IOError, OSError):
            return None

    def get_style(
        self,
        language: Optional[Language] = None
    ) -> tuple[str, StyleSource]:
        """
        Get the code style guidelines.

        Args:
            language: The programming language. If None, returns general style or all languages.

        Returns:
            A tuple of (style_content, source) where source indicates if it's custom or built-in.
        """
        # Try to load custom style first
        custom_style = self._read_custom_style()

        if custom_style is not None:
            return (custom_style, StyleSource.CUSTOM)

        # Fallback to built-in Google style
        if language is not None:
            return (GOOGLE_STYLE_GUIDELINES[language], StyleSource.BUILTIN)

        # Return all languages combined
        all_styles = "\n\n---\n\n".join(GOOGLE_STYLE_GUIDELINES.values())
        return (all_styles, StyleSource.BUILTIN)

    def get_style_for_language(self, language: Language) -> tuple[str, StyleSource]:
        """
        Get code style for a specific language.

        Args:
            language: The programming language.

        Returns:
            A tuple of (style_content, source).
        """
        return self.get_style(language)

