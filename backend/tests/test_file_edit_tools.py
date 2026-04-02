"""Unit tests for file_edit and file_write tools."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.code_tools.file_edit_tools import (
    FILE_EDIT_TOOL_REGISTRY,
    _file_read_state,
    clear_file_read_state,
    file_edit,
    file_write,
    record_file_read,
)


@pytest.fixture(autouse=True)
def clean_state():
    clear_file_read_state()
    yield
    clear_file_read_state()


# ---------------------------------------------------------------------------
# file_edit
# ---------------------------------------------------------------------------

class TestFileEdit:
    def test_basic_replacement(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello world\n")
        record_file_read(str(f), "hello world\n")

        result = file_edit(str(tmp_path), "test.py", "hello", "goodbye")
        assert result.success is True
        assert f.read_text() == "goodbye world\n"
        assert result.data["replacements"] == 1
        assert "diff" in result.data

    def test_replace_all(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa bbb aaa\n")
        record_file_read(str(f), "aaa bbb aaa\n")

        result = file_edit(str(tmp_path), "test.py", "aaa", "ccc", replace_all=True)
        assert result.success is True
        assert f.read_text() == "ccc bbb ccc\n"
        assert result.data["replacements"] == 2

    def test_multiple_matches_without_replace_all(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa bbb aaa\n")
        record_file_read(str(f), "aaa bbb aaa\n")

        result = file_edit(str(tmp_path), "test.py", "aaa", "ccc")
        assert result.success is False
        assert "2 matches" in result.error

    def test_old_string_not_found(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello world\n")
        record_file_read(str(f), "hello world\n")

        result = file_edit(str(tmp_path), "test.py", "nonexistent", "new")
        assert result.success is False
        assert "not found" in result.error

    def test_same_old_new(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        record_file_read(str(f), "hello\n")

        result = file_edit(str(tmp_path), "test.py", "hello", "hello")
        assert result.success is False
        assert "identical" in result.error

    def test_read_before_write_required(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        # NOT calling record_file_read

        result = file_edit(str(tmp_path), "test.py", "hello", "goodbye")
        assert result.success is False
        assert "not been read" in result.error

    def test_staleness_check(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        record_file_read(str(f), "hello\n")

        # Simulate external modification with enough time gap
        time.sleep(1.0)
        f.write_text("hello\n")  # same content but different mtime
        # Manually set mtime far in future to guarantee staleness
        os.utime(str(f), (time.time() + 10, time.time() + 10))

        result = file_edit(str(tmp_path), "test.py", "hello", "goodbye")
        assert result.success is False
        assert "modified since" in result.error

    def test_blocked_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        f = git_dir / "config"
        f.write_text("x")
        record_file_read(str(f), "x")

        result = file_edit(str(tmp_path), ".git/config", "x", "y")
        assert result.success is False
        assert ".git" in result.error

    def test_blocked_env_file(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("SECRET=abc")
        record_file_read(str(f), "SECRET=abc")

        result = file_edit(str(tmp_path), ".env", "SECRET=abc", "SECRET=xyz")
        assert result.success is False
        assert "protected" in result.error or ".env" in result.error

    def test_path_escape_blocked(self, tmp_path):
        result = file_edit(str(tmp_path), "../../../etc/passwd", "root", "hacked")
        assert result.success is False
        assert "escapes" in result.error

    def test_secret_warning(self, tmp_path):
        f = tmp_path / "config.py"
        f.write_text("key = 'old'\n")
        record_file_read(str(f), "key = 'old'\n")

        result = file_edit(str(tmp_path), "config.py", "key = 'old'", "api_key = 'sk-abcdefghijklmnopqrstuvwxyz123456'")
        assert result.success is True
        assert len(result.data["secret_warnings"]) > 0

    def test_diff_in_result(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        record_file_read(str(f), "line1\nline2\nline3\n")

        result = file_edit(str(tmp_path), "test.py", "line2", "CHANGED")
        assert result.success is True
        assert "-line2" in result.data["diff"]
        assert "+CHANGED" in result.data["diff"]


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------

class TestFileWrite:
    def test_create_new_file(self, tmp_path):
        result = file_write(str(tmp_path), "new.py", "print('hello')\n")
        assert result.success is True
        assert result.data["action"] == "created"
        assert (tmp_path / "new.py").read_text() == "print('hello')\n"

    def test_create_with_subdirs(self, tmp_path):
        result = file_write(str(tmp_path), "src/pkg/module.py", "# new module\n")
        assert result.success is True
        assert (tmp_path / "src" / "pkg" / "module.py").exists()

    def test_overwrite_requires_read(self, tmp_path):
        f = tmp_path / "existing.py"
        f.write_text("old content\n")
        # NOT calling record_file_read

        result = file_write(str(tmp_path), "existing.py", "new content\n")
        assert result.success is False
        assert "read_file first" in result.error

    def test_overwrite_after_read(self, tmp_path):
        f = tmp_path / "existing.py"
        f.write_text("old content\n")
        record_file_read(str(f), "old content\n")

        result = file_write(str(tmp_path), "existing.py", "new content\n")
        assert result.success is True
        assert result.data["action"] == "overwritten"
        assert f.read_text() == "new content\n"

    def test_blocked_paths(self, tmp_path):
        result = file_write(str(tmp_path), ".git/hooks/pre-commit", "#!/bin/sh\nexit 1")
        assert result.success is False

    def test_path_escape(self, tmp_path):
        result = file_write(str(tmp_path), "../../escape.txt", "hacked")
        assert result.success is False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestFileEditAdvanced:
    """Additional edge-case tests for file_edit."""

    def test_delete_text_empty_new_string(self, tmp_path):
        """Replacing with empty string should delete the matched text."""
        f = tmp_path / "test.py"
        f.write_text("hello world\n")
        record_file_read(str(f), "hello world\n")

        result = file_edit(str(tmp_path), "test.py", " world", "")
        assert result.success is True
        assert f.read_text() == "hello\n"

    def test_multiline_replacement(self, tmp_path):
        f = tmp_path / "test.py"
        original = "def foo():\n    pass\n\ndef bar():\n    return 1\n"
        f.write_text(original)
        record_file_read(str(f), original)

        result = file_edit(str(tmp_path), "test.py", "def foo():\n    pass", "def foo():\n    return 42")
        assert result.success is True
        assert "return 42" in f.read_text()

    def test_edit_then_edit_again(self, tmp_path):
        """After first edit, read state should be updated so second edit works."""
        f = tmp_path / "test.py"
        f.write_text("aaa bbb ccc\n")
        record_file_read(str(f), "aaa bbb ccc\n")

        r1 = file_edit(str(tmp_path), "test.py", "aaa", "xxx")
        assert r1.success is True

        # Second edit should work (read state was updated by first edit)
        r2 = file_edit(str(tmp_path), "test.py", "bbb", "yyy")
        assert r2.success is True
        assert f.read_text() == "xxx yyy ccc\n"

    def test_blocked_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        f = nm / "index.js"
        f.write_text("module.exports = {}")
        record_file_read(str(f), "module.exports = {}")

        result = file_edit(str(tmp_path), "node_modules/pkg/index.js", "module", "hacked")
        assert result.success is False
        assert "node_modules" in result.error

    def test_symlink_escape(self, tmp_path):
        """Symlink pointing outside workspace should be blocked."""
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret data")

        # Create symlink inside workspace pointing outside
        ws = tmp_path / "workspace"
        ws.mkdir()
        link = ws / "link.txt"
        try:
            link.symlink_to(secret)
        except OSError:
            return  # Skip if symlinks not supported

        record_file_read(str(link.resolve()), "secret data")
        result = file_edit(str(ws), "link.txt", "secret", "hacked")
        assert result.success is False
        assert "escapes" in result.error


class TestFileWriteAdvanced:
    """Additional edge-case tests for file_write."""

    def test_secret_warning(self, tmp_path):
        result = file_write(str(tmp_path), "config.py", "API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n")
        assert result.success is True
        assert len(result.data["secret_warnings"]) > 0

    def test_overwrite_staleness(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("old\n")
        record_file_read(str(f), "old\n")

        # Set mtime far in future
        os.utime(str(f), (time.time() + 10, time.time() + 10))

        result = file_write(str(tmp_path), "test.py", "new\n")
        assert result.success is False
        assert "modified since" in result.error

    def test_overwrite_generates_diff(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\n")
        record_file_read(str(f), "line1\nline2\n")

        result = file_write(str(tmp_path), "test.py", "line1\nCHANGED\n")
        assert result.success is True
        assert result.data["diff"]  # non-empty
        assert "-line2" in result.data["diff"]
        assert "+CHANGED" in result.data["diff"]

    def test_node_modules_blocked(self, tmp_path):
        result = file_write(str(tmp_path), "node_modules/pkg/evil.js", "hack")
        assert result.success is False


class TestExecuteToolDispatch:
    """Verify file_edit/file_write dispatch through execute_tool."""

    def test_file_edit_via_execute_tool(self, tmp_path):
        from app.code_tools.tools import execute_tool

        f = tmp_path / "hello.py"
        f.write_text("print('hello')\n")

        # First read (to register read state)
        r = execute_tool("read_file", str(tmp_path), {"path": "hello.py"})
        assert r.success is True

        # Now edit
        r = execute_tool("file_edit", str(tmp_path), {
            "path": "hello.py",
            "old_string": "hello",
            "new_string": "world",
        })
        assert r.success is True
        assert f.read_text() == "print('world')\n"

    def test_file_write_via_execute_tool(self, tmp_path):
        from app.code_tools.tools import execute_tool

        r = execute_tool("file_write", str(tmp_path), {
            "path": "new_file.py",
            "content": "# new file",
        })
        assert r.success is True
        assert "new file" in (tmp_path / "new_file.py").read_text()

    def test_read_file_records_state(self, tmp_path):
        from app.code_tools.tools import execute_tool

        f = tmp_path / "track.py"
        f.write_text("tracked content\n")

        # Read should register state
        execute_tool("read_file", str(tmp_path), {"path": "track.py"})

        # Now edit should work (state was tracked)
        r = execute_tool("file_edit", str(tmp_path), {
            "path": "track.py",
            "old_string": "tracked",
            "new_string": "modified",
        })
        assert r.success is True


class TestClearState:
    def test_clear_resets_tracking(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        record_file_read(str(f), "hello\n")
        assert str(f) in _file_read_state

        clear_file_read_state()
        assert str(f) not in _file_read_state


class TestRegistry:
    def test_tools_registered(self):
        assert "file_edit" in FILE_EDIT_TOOL_REGISTRY
        assert "file_write" in FILE_EDIT_TOOL_REGISTRY
