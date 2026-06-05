"""Smoke tests for the 4 backend-only code tools.

These tools don't have TS-extension counterparts (they're either git-based
analysis or require backend runtime resources) and were previously
untested. Each test runs the tool against either the parity_repo fixture
or the current git checkout and asserts the response shape + key
fields, so the next refactor can't silently break them.

Coverage target: git_hotspots, list_endpoints, extract_docstrings, db_schema.
run_test is covered elsewhere (metadata + cache-executor tests).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.code_tools.tools import execute_tool

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
FIXTURE_REPO = str(REPO_ROOT / "tests" / "fixtures" / "parity_repo")


# ---------------------------------------------------------------------------
# git_hotspots — needs a git repo
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with a couple of commits so hotspots has data."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    for i in range(3):
        (tmp_path / f"f{i}.py").write_text(f"x = {i}\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"c{i}"],
            cwd=tmp_path,
            check=True,
        )
    return str(tmp_path)


class TestGitHotspots:
    def test_returns_structured_response(self, git_repo):
        r = execute_tool("git_hotspots", git_repo, {"days": 365, "top_n": 5})
        assert r.success, r.error
        assert isinstance(r.data, dict)
        # Contract: at minimum both windows are present and a list of files.
        for field in ("hotspots", "recently_active"):
            assert field in r.data, f"Missing field: {field}"

    def test_rejects_oversized_top_n(self, git_repo):
        """top_n > 50 is rejected at the schema layer so the tool can't
        be asked for unbounded output."""
        r = execute_tool("git_hotspots", git_repo, {"days": 365, "top_n": 9999})
        assert not r.success
        assert "top_n" in (r.error or "")


# ---------------------------------------------------------------------------
# list_endpoints — scans source for route decorators
# ---------------------------------------------------------------------------


class TestListEndpoints:
    def test_scans_fixture_repo(self):
        r = execute_tool("list_endpoints", FIXTURE_REPO, {})
        assert r.success, r.error
        assert isinstance(r.data, dict)
        # Response should have a list of endpoints; may be empty for fixture
        assert "endpoints" in r.data
        assert isinstance(r.data["endpoints"], list)

    def test_nonexistent_path_error(self):
        r = execute_tool("list_endpoints", FIXTURE_REPO, {"path": "does/not/exist"})
        assert not r.success
        assert "not found" in (r.error or "").lower()


# ---------------------------------------------------------------------------
# extract_docstrings — file-level doc extraction
# ---------------------------------------------------------------------------


class TestExtractDocstrings:
    def test_python_file(self):
        r = execute_tool(
            "extract_docstrings",
            FIXTURE_REPO,
            {"path": "app/service.py"},
        )
        assert r.success, r.error
        assert isinstance(r.data, dict)
        assert "docstrings" in r.data
        assert isinstance(r.data["docstrings"], list)

    def test_file_not_found(self, tmp_path):
        r = execute_tool(
            "extract_docstrings",
            str(tmp_path),
            {"path": "missing.py"},
        )
        assert not r.success
        assert "not found" in (r.error or "").lower()


# ---------------------------------------------------------------------------
# db_schema — ORM model scanning
# ---------------------------------------------------------------------------


class TestDbSchema:
    def test_scans_fixture_repo(self):
        r = execute_tool("db_schema", FIXTURE_REPO, {})
        assert r.success, r.error
        assert isinstance(r.data, dict)
        assert "models" in r.data
        assert isinstance(r.data["models"], list)

    def test_detects_sqlalchemy_models(self, tmp_path):
        """Minimal SQLAlchemy model should be picked up so the regression
        doesn't go unnoticed if the detector patterns drift."""
        (tmp_path / "models.py").write_text(
            "from sqlalchemy.orm import DeclarativeBase\n"
            "class Base(DeclarativeBase): pass\n"
            "class User(Base):\n"
            "    __tablename__ = 'users'\n"
            "    id = Column(Integer, primary_key=True)\n"
            "    email = Column(String)\n"
        )
        r = execute_tool("db_schema", str(tmp_path), {})
        assert r.success, r.error
        names = [m.get("name") for m in r.data["models"]]
        assert "User" in names, f"SQLAlchemy User model not detected: {names}"
