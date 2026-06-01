"""Shared test fixtures and configuration for backend tests.

Heavy dependencies are stubbed here so all test modules can import
application code without needing real installations.
"""

import sys
import types
from pathlib import Path


def _stub(name: str, **attrs) -> types.ModuleType:
    """Register a stub module in sys.modules to prevent real imports."""
    if name in sys.modules:
        return sys.modules[name]
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# Stub heavy optional dependencies before any app code is imported
from unittest.mock import MagicMock

_stub("tree_sitter_language_pack")  # Phase 9.18 step 3: replaced tree_sitter_languages
_stub("networkx", DiGraph=MagicMock, pagerank=MagicMock, PowerIterationFailedConvergence=Exception)

# Playwright stubs — browser tools tests mock the service layer, so we only
# need the module structure to exist for import resolution.
_pw_sync = _stub(
    "playwright.sync_api",
    sync_playwright=MagicMock,
    Browser=MagicMock,
    BrowserContext=MagicMock,
    Page=MagicMock,
)
_stub("playwright", sync_api=_pw_sync)
_stub("playwright.sync_api", **{k: getattr(_pw_sync, k) for k in dir(_pw_sync) if not k.startswith("_")})

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def api_client():
    """Provide a TestClient for the main FastAPI app.

    Named api_client (not client) to avoid shadowing the module-level
    `client = TestClient(app)` pattern used in existing test files.
    """
    return TestClient(app)


# ---------------------------------------------------------------------------
# Database fixtures (async SQLAlchemy with aiosqlite for unit tests)
# ---------------------------------------------------------------------------

import pytest_asyncio


@pytest_asyncio.fixture
async def db_engine():
    """Create an async in-memory SQLite engine for tests.

    Uses aiosqlite so tests don't need a real Postgres instance.
    Tables are created automatically and dropped after the test.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.models import Base

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def redis_mock():
    """Provide a fakeredis async client for tests.

    Falls back to None if fakeredis is not installed.
    """
    try:
        import fakeredis.aioredis

        return fakeredis.aioredis.FakeRedis(decode_responses=True)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Git-enabled parity fixture
# ---------------------------------------------------------------------------
# A git-initialized copy of tests/fixtures/parity_repo with a deterministic
# 2-commit history, so the git_* code tools (git_log / git_diff / git_diff_files
# / git_blame / git_show) can be exercised against a small, stable repo instead
# of the real conductor source tree. The static parity_repo has no .git of its
# own (it's tracked by the outer conductor repo), so we materialise a throwaway
# git workdir per session.

_PARITY_REPO_SRC = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "parity_repo"


def _parity_git(args: "list[str]", cwd, env) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def git_parity_repo(tmp_path_factory) -> str:
    """Git-initialised copy of parity_repo with a deterministic 2-commit history.

    Hermetic: global/system gitconfig, hooks, and signing are bypassed, and the
    author/committer identity + dates are pinned so git_log / git_blame output is
    reproducible across machines.
    """
    import os
    import shutil

    dst = tmp_path_factory.mktemp("parity_git") / "repo"
    shutil.copytree(
        _PARITY_REPO_SRC,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".conductor", ".git"),
    )

    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Parity Fixture",
        "GIT_AUTHOR_EMAIL": "parity@example.com",
        "GIT_COMMITTER_NAME": "Parity Fixture",
        "GIT_COMMITTER_EMAIL": "parity@example.com",
        "GIT_AUTHOR_DATE": "2024-01-01T00:00:00 +0000",
        "GIT_COMMITTER_DATE": "2024-01-01T00:00:00 +0000",
    }

    _parity_git(["init", "-b", "main"], dst, env)
    _parity_git(["config", "user.email", "parity@example.com"], dst, env)
    _parity_git(["config", "user.name", "Parity Fixture"], dst, env)
    _parity_git(["config", "commit.gpgsign", "false"], dst, env)

    # Commit 1: all files.
    _parity_git(["add", "-A"], dst, env)
    _parity_git(["commit", "--no-gpg-sign", "--no-verify", "-m", "initial fixture import"], dst, env)

    # Commit 2: a small edit so HEAD~1..HEAD diffs / blame have real content.
    svc = dst / "app" / "service.py"
    svc.write_text(svc.read_text() + "\n# parity fixture edit\n")
    env2 = {
        **env,
        "GIT_AUTHOR_DATE": "2024-01-02T00:00:00 +0000",
        "GIT_COMMITTER_DATE": "2024-01-02T00:00:00 +0000",
    }
    _parity_git(["add", "app/service.py"], dst, env2)
    _parity_git(["commit", "--no-gpg-sign", "--no-verify", "-m", "tweak service"], dst, env2)

    return str(dst)
