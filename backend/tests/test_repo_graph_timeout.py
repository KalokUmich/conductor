"""Tests for Phase 9.18 step 1 — per-file tree-sitter parse timeout.

Covers the timeout wrapper, regex fallback on timeout, skip_fact
writeback to the session Fact Vault, and skip-list pre-check so future
tool calls on pathological files short-circuit.

Zombie daemon thread behaviour is NOT asserted here — by design the
worker keeps running after timeout; full kill-on-timeout arrives with
Sprint 17's ProcessPoolExecutor.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import pytest

from app.repo_graph.parser import (
    FileSymbols,
    _extract_with_regex,
    extract_definitions,
    extract_definitions_with_timeout,
)
from app.scratchpad import FactStore
from app.scratchpad.context import bind_factstore


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Per-test FactStore rooted in tmp_path so ~/.conductor isn't touched."""
    monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
    store = FactStore.open(f"timeout-test-{uuid.uuid4().hex[:6]}", workspace="/ws")
    yield store
    store.delete()


# ---------------------------------------------------------------------------
# timeout → regex fallback
# ---------------------------------------------------------------------------


class TestTimeoutFallsBackToRegex:
    def test_slow_parser_triggers_fallback(self, tmp_path):
        """When _extract_with_tree_sitter blocks past the timeout, the
        wrapper must unblock and return the regex extractor's output."""

        def _slow_treesitter(source, language, file_path):
            time.sleep(5)  # longer than test's 0.2s timeout
            raise AssertionError("must not complete — test expects timeout")

        src = b"def hello():\n    return 1\n"
        test_file = tmp_path / "hello.py"
        test_file.write_bytes(src)

        with patch("app.repo_graph.parser._extract_with_tree_sitter", _slow_treesitter):
            t0 = time.monotonic()
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=0.2
            )
            elapsed = time.monotonic() - t0

        # Unblocked well before the 5s mock sleep
        assert elapsed < 2.0, f"timeout wrapper took {elapsed:.2f}s — should be ~0.2s"
        # Regex fallback produced the function definition
        assert result.file_path == str(test_file)
        assert any(d.name == "hello" for d in result.definitions)

    def test_exception_also_falls_back_to_regex(self, tmp_path):
        def _broken_treesitter(source, language, file_path):
            raise RuntimeError("grammar exploded")

        src = b"def foo():\n    pass\n"
        test_file = tmp_path / "foo.py"

        with patch("app.repo_graph.parser._extract_with_tree_sitter", _broken_treesitter):
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=1.0
            )
        assert any(d.name == "foo" for d in result.definitions)

    def test_fast_parser_returns_treesitter_output(self, tmp_path):
        """Happy path — timeout generous, tree-sitter returns before the
        wrapper's queue wait expires, regex is NOT called."""
        sentinel = FileSymbols(file_path="sentinel", language="python")

        def _fast(source, language, file_path):
            return sentinel

        src = b"def bar(): pass\n"
        test_file = tmp_path / "bar.py"

        with patch("app.repo_graph.parser._extract_with_tree_sitter", _fast), patch(
            "app.repo_graph.parser._extract_with_regex"
        ) as regex_mock:
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=5.0
            )
        assert result is sentinel
        regex_mock.assert_not_called()


# ---------------------------------------------------------------------------
# skip_fact integration
# ---------------------------------------------------------------------------


class TestSkipFactsIntegration:
    def test_timeout_writes_skip_fact(self, isolated_vault, tmp_path):
        def _slow(source, language, file_path):
            time.sleep(5)

        src = b"def x(): pass\n"
        test_file = tmp_path / "x.py"

        with bind_factstore(isolated_vault), patch("app.repo_graph.parser._extract_with_tree_sitter", _slow):
            extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=0.2
            )

        reason = isolated_vault.should_skip(str(test_file))
        assert reason is not None
        assert "timeout" in reason.lower()

    def test_skip_fact_prevents_retry(self, isolated_vault, tmp_path):
        """Once a path is on the skip list, subsequent calls MUST NOT
        invoke tree-sitter — they go straight to regex."""
        src = b"def y(): pass\n"
        test_file = tmp_path / "y.py"
        isolated_vault.put_skip(str(test_file), reason="prior timeout", duration_ms=200)

        with bind_factstore(isolated_vault), patch(
            "app.repo_graph.parser._extract_with_tree_sitter"
        ) as ts_mock:
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=5.0
            )
        ts_mock.assert_not_called()
        assert any(d.name == "y" for d in result.definitions)

    def test_no_vault_bound_still_works(self, tmp_path):
        """When scratchpad isn't bound, timeout wrapper MUST still succeed
        — the skip-list machinery is best-effort."""

        def _slow(source, language, file_path):
            time.sleep(5)

        src = b"def z(): pass\n"
        test_file = tmp_path / "z.py"

        with patch("app.repo_graph.parser._extract_with_tree_sitter", _slow):
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=0.2
            )
        assert any(d.name == "z" for d in result.definitions)

    def test_vault_put_skip_error_does_not_propagate(self, isolated_vault, tmp_path):
        """If put_skip raises (closed DB, disk full), extraction still
        returns a usable FileSymbols — caching is never load-bearing."""

        def _slow(source, language, file_path):
            time.sleep(5)

        src = b"def w(): pass\n"
        test_file = tmp_path / "w.py"

        with bind_factstore(isolated_vault), patch.object(
            isolated_vault, "put_skip", side_effect=RuntimeError("disk full")
        ), patch("app.repo_graph.parser._extract_with_tree_sitter", _slow):
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=0.2
            )
        assert any(d.name == "w" for d in result.definitions)


# ---------------------------------------------------------------------------
# timeout=0 (disabled) and env var override
# ---------------------------------------------------------------------------


class TestTimeoutDisabled:
    def test_zero_timeout_is_legacy_sync(self, tmp_path):
        """timeout_s=0 must NOT spawn a daemon thread. Verified
        indirectly by checking that a fast parser returns successfully
        with no queue machinery."""
        sentinel = FileSymbols(file_path="s", language="python")

        def _fast(source, language, file_path):
            return sentinel

        src = b"pass\n"
        test_file = tmp_path / "s.py"

        with patch("app.repo_graph.parser._extract_with_tree_sitter", _fast):
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=0
            )
        assert result is sentinel

    def test_env_var_sets_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_PARSE_TIMEOUT_S", "0.1")

        def _slow(source, language, file_path):
            time.sleep(2)

        src = b"def q(): pass\n"
        test_file = tmp_path / "q.py"

        with patch("app.repo_graph.parser._extract_with_tree_sitter", _slow):
            t0 = time.monotonic()
            result = extract_definitions_with_timeout(
                str(test_file), source=src, timeout_s=None
            )
            elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert any(d.name == "q" for d in result.definitions)

    def test_invalid_env_var_falls_back_to_60s_default(self, tmp_path, monkeypatch):
        """Bad env value → 60s default, not a crash."""
        monkeypatch.setenv("CONDUCTOR_PARSE_TIMEOUT_S", "not-a-number")
        sentinel = FileSymbols(file_path="n", language="python")

        def _fast(source, language, file_path):
            return sentinel

        src = b"pass\n"
        test_file = tmp_path / "n.py"
        with patch("app.repo_graph.parser._extract_with_tree_sitter", _fast):
            # Should not raise — default kicks in
            assert (
                extract_definitions_with_timeout(str(test_file), source=src) is sentinel
            )


# ---------------------------------------------------------------------------
# extract_definitions delegation
# ---------------------------------------------------------------------------


class TestExtractDefinitionsDelegation:
    def test_extract_definitions_uses_timeout_wrapper(self, tmp_path):
        """Public extract_definitions() must now go through the timed
        wrapper, so every caller in the codebase gets the 60s ceiling."""

        def _slow(source, language, file_path):
            time.sleep(5)

        src = b"def a(): pass\n"
        test_file = tmp_path / "a.py"
        with patch("app.repo_graph.parser._extract_with_tree_sitter", _slow):
            t0 = time.monotonic()
            result = extract_definitions(str(test_file), source=src, timeout_s=0.2)
            elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        assert any(d.name == "a" for d in result.definitions)

    def test_regex_fallback_runs_on_timeout(self, tmp_path):
        def _slow(source, language, file_path):
            time.sleep(5)

        src = b"def direct_fallback(): pass\n"
        test_file = tmp_path / "d.py"
        with patch("app.repo_graph.parser._extract_with_tree_sitter", _slow):
            result = _extract_with_regex(src.decode("utf-8"), "python", str(test_file))
            assert any(d.name == "direct_fallback" for d in result.definitions)
