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
from unittest.mock import MagicMock  # noqa: E402

_stub("tree_sitter_languages")
_stub("networkx", DiGraph=MagicMock, pagerank=MagicMock, PowerIterationFailedConvergence=Exception)
_stub("litellm", completion=MagicMock(), drop_params=False)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def api_client():
    """Provide a TestClient for the main FastAPI app.

    Named api_client (not client) to avoid shadowing the module-level
    `client = TestClient(app)` pattern used in existing test files.
    """
    return TestClient(app)
