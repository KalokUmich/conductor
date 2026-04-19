"""Shared test fixtures and configuration for backend tests.

Heavy dependencies are stubbed here so all test modules can import
application code without needing real installations.
"""

import sys
import types


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
