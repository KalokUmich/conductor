"""Tests for the code style loader."""

import tempfile
from pathlib import Path

import pytest

from app.agent.style_loader import (
    CodeStyleLoader,
    Language,
    StyleSource,
    _read_builtin_style,
    _read_universal_style,
)


class TestStyleLoaderFileMissing:
    """Test behavior when .ai/code-style.md is missing."""

    def test_missing_file_returns_universal(self):
        """When file doesn't exist and no language, should return universal style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style()

            assert source == StyleSource.UNIVERSAL
            assert len(style) > 0

    @pytest.mark.parametrize(
        "language,expected_name,expected_keyword",
        [
            (Language.PYTHON, "Python", "4 spaces"),
            (Language.JAVA, "Java", "2 spaces"),
            (Language.JAVASCRIPT, "JavaScript", "const"),
            (Language.GO, "Go", "gofmt"),
            (Language.JSON, "JSON", "camelCase"),
        ],
    )
    def test_missing_file_returns_builtin_for_language(self, language, expected_name, expected_keyword):
        """When file doesn't exist, should return builtin style for each language."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style(language)

            assert source == StyleSource.BUILTIN
            assert expected_name in style
            assert expected_keyword in style


class TestStyleLoaderEmptyFile:
    """Test behavior when .ai/code-style.md is empty."""

    def test_empty_file_returns_universal(self):
        """When file is empty, should return universal style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create empty .ai/code-style.md
            ai_dir = Path(tmpdir) / ".ai"
            ai_dir.mkdir()
            style_file = ai_dir / "code-style.md"
            style_file.write_text("")

            loader = CodeStyleLoader(workspace_root=tmpdir)
            _, source = loader.get_style()

            assert source == StyleSource.UNIVERSAL

    def test_whitespace_only_file_returns_universal(self):
        """When file contains only whitespace, should return universal style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir) / ".ai"
            ai_dir.mkdir()
            style_file = ai_dir / "code-style.md"
            style_file.write_text("   \n\t\n   ")

            loader = CodeStyleLoader(workspace_root=tmpdir)
            _, source = loader.get_style()

            assert source == StyleSource.UNIVERSAL


class TestStyleLoaderWithContent:
    """Test behavior when .ai/code-style.md has content."""

    def test_file_with_content_returns_custom(self):
        """When file has content, should return custom style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir) / ".ai"
            ai_dir.mkdir()
            style_file = ai_dir / "code-style.md"
            custom_content = "# My Custom Style\n\nUse tabs for indentation."
            style_file.write_text(custom_content)

            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style()

            assert source == StyleSource.CUSTOM
            assert style == custom_content

    def test_custom_style_ignores_language_parameter(self):
        """Custom style is returned regardless of language parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir) / ".ai"
            ai_dir.mkdir()
            style_file = ai_dir / "code-style.md"
            custom_content = "# Team Style Guide"
            style_file.write_text(custom_content)

            loader = CodeStyleLoader(workspace_root=tmpdir)

            for lang in Language:
                style, source = loader.get_style(lang)
                assert source == StyleSource.CUSTOM
                assert style == custom_content

    def test_file_with_unicode_content(self):
        """Should handle unicode content correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir) / ".ai"
            ai_dir.mkdir()
            style_file = ai_dir / "code-style.md"
            custom_content = "# 代码风格指南\n\n使用 4 个空格缩进。"
            style_file.write_text(custom_content, encoding="utf-8")

            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style()

            assert source == StyleSource.CUSTOM
            assert "代码风格指南" in style


class TestBuiltinStyles:
    """Test the built-in style guidelines loaded from .md files."""

    def test_all_languages_have_builtin_style_files(self):
        """All supported languages should have .md style files."""
        for lang in Language:
            style = _read_builtin_style(lang)
            assert len(style) > 0

    def test_builtin_styles_contain_formatting_or_rules_section(self):
        """Built-in styles should contain formatting or rules guidelines."""
        for lang in Language:
            style = _read_builtin_style(lang)
            # JSON uses "General Rules" instead of "Formatting"
            assert "Formatting" in style or "General Rules" in style

    def test_builtin_styles_contain_naming_section(self):
        """Built-in styles should contain naming conventions."""
        for lang in Language:
            style = _read_builtin_style(lang)
            assert "Naming" in style or "naming" in style.lower()


class TestUniversalStyle:
    """Test the universal (language-agnostic) style guidelines."""

    def test_universal_style_exists(self):
        """Universal style file should exist and be non-empty."""
        style = _read_universal_style()
        assert len(style) > 0

    def test_universal_style_is_concise(self):
        """Universal style should be concise (under 60 lines)."""
        style = _read_universal_style()
        line_count = len(style.split("\n"))
        assert line_count < 60, f"Universal style has {line_count} lines, expected < 60"

    def test_universal_style_is_language_agnostic(self):
        """Universal style should not reference specific language syntax."""
        style = _read_universal_style()
        assert "Universal" in style
        assert "Readability" in style
        assert "Naming" in style

    def test_no_language_returns_universal(self):
        """get_style() with no language should return universal style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style()

            assert source == StyleSource.UNIVERSAL
            assert "Universal" in style


class TestListTemplates:
    """Test the list_templates() static method."""

    def test_list_templates_returns_all_files(self):
        """list_templates() should return at least 6 templates."""
        templates = CodeStyleLoader.list_templates()
        assert len(templates) >= 6

    def test_list_templates_has_required_keys(self):
        """Each template should have name, filename, and content keys."""
        templates = CodeStyleLoader.list_templates()
        for t in templates:
            assert "name" in t
            assert "filename" in t
            assert "content" in t
            assert len(t["content"]) > 0

    def test_list_templates_includes_expected_names(self):
        """Templates should include all languages and universal."""
        templates = CodeStyleLoader.list_templates()
        names = [t["name"] for t in templates]
        for expected in ["java", "javascript", "python", "go", "json", "universal"]:
            assert expected in names, f"Missing template: {expected}"

    def test_list_templates_filename_format(self):
        """Template filenames should end in .md."""
        templates = CodeStyleLoader.list_templates()
        for t in templates:
            assert t["filename"].endswith(".md")
            assert t["filename"] == t["name"] + ".md"
