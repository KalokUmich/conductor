"""Tests for the code style loader."""
import os
import tempfile
import pytest
from pathlib import Path

from app.agent.style_loader import (
    CodeStyleLoader,
    Language,
    StyleSource,
    GOOGLE_STYLE_GUIDELINES,
)


class TestStyleLoaderFileMissing:
    """Test behavior when .ai/code-style.md is missing."""

    def test_missing_file_returns_builtin(self):
        """When file doesn't exist, should return built-in style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style()

            assert source == StyleSource.BUILTIN
            assert len(style) > 0

    def test_missing_file_returns_builtin_for_python(self):
        """When file doesn't exist, should return Python Google style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style(Language.PYTHON)

            assert source == StyleSource.BUILTIN
            assert "Python" in style
            assert "4 spaces" in style

    def test_missing_file_returns_builtin_for_java(self):
        """When file doesn't exist, should return Java Google style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style(Language.JAVA)

            assert source == StyleSource.BUILTIN
            assert "Java" in style
            assert "2 spaces" in style

    def test_missing_file_returns_builtin_for_javascript(self):
        """When file doesn't exist, should return JavaScript Google style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style(Language.JAVASCRIPT)

            assert source == StyleSource.BUILTIN
            assert "JavaScript" in style
            assert "const" in style

    def test_missing_file_returns_builtin_for_go(self):
        """When file doesn't exist, should return Go Google style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style(Language.GO)

            assert source == StyleSource.BUILTIN
            assert "Go" in style
            assert "gofmt" in style

    def test_missing_file_returns_builtin_for_json(self):
        """When file doesn't exist, should return JSON Google style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style(Language.JSON)

            assert source == StyleSource.BUILTIN
            assert "JSON" in style
            assert "camelCase" in style


class TestStyleLoaderEmptyFile:
    """Test behavior when .ai/code-style.md is empty."""

    def test_empty_file_returns_builtin(self):
        """When file is empty, should return built-in style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create empty .ai/code-style.md
            ai_dir = Path(tmpdir) / ".ai"
            ai_dir.mkdir()
            style_file = ai_dir / "code-style.md"
            style_file.write_text("")

            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style()

            assert source == StyleSource.BUILTIN

    def test_whitespace_only_file_returns_builtin(self):
        """When file contains only whitespace, should return built-in style."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir) / ".ai"
            ai_dir.mkdir()
            style_file = ai_dir / "code-style.md"
            style_file.write_text("   \n\t\n   ")

            loader = CodeStyleLoader(workspace_root=tmpdir)
            style, source = loader.get_style()

            assert source == StyleSource.BUILTIN


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
    """Test the built-in Google style guidelines."""

    def test_all_languages_have_builtin_styles(self):
        """All supported languages should have built-in styles."""
        for lang in Language:
            assert lang in GOOGLE_STYLE_GUIDELINES
            assert len(GOOGLE_STYLE_GUIDELINES[lang]) > 0

    def test_builtin_styles_contain_formatting_or_rules_section(self):
        """Built-in styles should contain formatting or rules guidelines."""
        for lang in Language:
            style = GOOGLE_STYLE_GUIDELINES[lang]
            # JSON uses "General Rules" instead of "Formatting"
            assert "Formatting" in style or "General Rules" in style

    def test_builtin_styles_contain_naming_section(self):
        """Built-in styles should contain naming conventions."""
        for lang in Language:
            style = GOOGLE_STYLE_GUIDELINES[lang]
            assert "Naming" in style or "naming" in style.lower()

