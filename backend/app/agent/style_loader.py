"""Code style loader for AI agent.

Loads code style guidelines from .ai/code-style.md if it exists and is non-empty,
otherwise falls back to built-in style guidelines stored as markdown files.
"""

from enum import Enum
from pathlib import Path
from typing import List, Optional


class Language(str, Enum):
    """Supported programming languages."""

    JAVA = "java"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    GO = "go"
    JSON = "json"


STYLES_DIR = Path(__file__).parent / "styles"


class StyleSource(str, Enum):
    """Source of the code style."""

    CUSTOM = "custom"  # From .ai/code-style.md
    BUILTIN = "builtin"  # Language-specific Google style fallback
    UNIVERSAL = "universal"  # Language-agnostic universal style


def _read_builtin_style(language: Language) -> str:
    """Read a language-specific built-in style from the styles directory.

    Args:
        language: The programming language.

    Returns:
        The content of the style markdown file.

    Raises:
        FileNotFoundError: If the style file does not exist.
    """
    style_path = STYLES_DIR / f"{language.value}.md"
    return style_path.read_text(encoding="utf-8").strip()


def _read_universal_style() -> str:
    """Read the universal (language-agnostic) style guidelines.

    Returns:
        The content of the universal style markdown file.

    Raises:
        FileNotFoundError: If the universal style file does not exist.
    """
    style_path = STYLES_DIR / "universal.md"
    return style_path.read_text(encoding="utf-8").strip()


class CodeStyleLoader:
    """Loads code style guidelines from file or built-in defaults.

    This class implements a priority chain for loading code style guidelines:

    1. **Custom style** (.ai/code-style.md in workspace root)
       - If this file exists and is non-empty, it takes precedence over all built-in styles
       - Allows teams to define their own coding standards

    2. **Built-in language-specific style** (agent/styles/{language}.md)
       - If no custom style is found, loads the built-in style for the specified language
       - Available built-in styles:
         * python.md - Google Python Style Guide
         * java.md - Google Java Style Guide
         * javascript.md - Google JavaScript Style Guide
         * go.md - Effective Go
         * json.md - JSON formatting conventions

    3. **Universal style** (agent/styles/universal.md)
       - If no language is specified or no language-specific style exists
       - Contains language-agnostic best practices (naming, comments, error handling, etc.)

    Example usage:
        loader = CodeStyleLoader(workspace_root="/path/to/workspace")

        # Try custom style first, fallback to Python built-in
        style, source = loader.get_style(language=Language.PYTHON)

        # Try custom style first, fallback to universal
        style, source = loader.get_style()
    """

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
        except OSError:
            return None

    def get_style(self, language: Optional[Language] = None) -> tuple[str, StyleSource]:
        """
        Get the code style guidelines.

        Args:
            language: The programming language. If None, returns universal style.

        Returns:
            A tuple of (style_content, source) where source indicates if it's custom,
            builtin (language-specific), or universal.
        """
        # Try to load custom style first
        custom_style = self._read_custom_style()

        if custom_style is not None:
            return (custom_style, StyleSource.CUSTOM)

        # Fallback to built-in style
        if language is not None:
            return (_read_builtin_style(language), StyleSource.BUILTIN)

        # Return universal style when no language specified
        return (_read_universal_style(), StyleSource.UNIVERSAL)

    @staticmethod
    def list_templates() -> List[dict]:
        """List all available built-in style templates.

        Returns:
            A list of dicts with keys: name, filename, content.
        """
        templates = []
        for md_file in sorted(STYLES_DIR.glob("*.md")):
            content = md_file.read_text(encoding="utf-8").strip()
            templates.append(
                {
                    "name": md_file.stem,
                    "filename": md_file.name,
                    "content": content,
                }
            )
        return templates
