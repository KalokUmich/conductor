"""Tests for Phase 9.18 step 1 — degraded-extraction signal in AST tools.

When tree-sitter times out on a file, the parser falls back to regex
and marks ``FileSymbols.extracted_via = "regex"``. The session Fact
Vault records the file on its skip list. AST tools should surface this
to the agent so it can route structural queries about those paths to
``grep`` / ``read_file`` instead of trusting degraded symbol data.
"""

from __future__ import annotations

import uuid

import pytest

from app.code_tools.tools import file_outline, find_symbol
from app.repo_graph.parser import FileSymbols, _extract_with_regex
from app.scratchpad import FactStore
from app.scratchpad.context import bind_factstore


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
    store = FactStore.open(f"deg-test-{uuid.uuid4().hex[:6]}", workspace="/ws")
    yield store
    store.delete()


class TestFileSymbolsExtractionMode:
    def test_regex_fallback_marks_extracted_via(self):
        """Direct call to the regex extractor — its output must carry the
        'regex' tag so any downstream caller can detect degradation."""
        result = _extract_with_regex("def foo(): pass\n", "python", "/tmp/foo.py")
        assert result.extracted_via == "regex"

    def test_default_extraction_mode_is_tree_sitter(self):
        fs = FileSymbols(file_path="/tmp/x.py")
        assert fs.extracted_via == "tree_sitter"


class TestFileOutlineDegradedSignal:
    def test_clean_parse_returns_plain_list(self, tmp_path):
        """Happy path — tree-sitter succeeds, data remains a plain list
        (back-compat shape)."""
        fp = tmp_path / "clean.py"
        fp.write_text("def hello():\n    return 1\n")
        result = file_outline(str(tmp_path), "clean.py")
        assert result.success
        assert isinstance(result.data, list)
        names = [d["name"] for d in result.data]
        assert "hello" in names

    def test_timeout_on_file_returns_dict_with_note(self, tmp_path, isolated_vault):
        """When the file is on the session skip list AND parse triggers
        regex fallback (the wrapper's pre-check routes to regex), the
        tool wraps data as a dict with a note for the agent."""
        fp = tmp_path / "slow.py"
        fp.write_text("def slow(): pass\n")
        # Pre-populate skip_facts so the wrapper's pre-check hits regex
        # directly, which sets extracted_via="regex" on the result.
        isolated_vault.put_skip(str(fp), reason="tree-sitter timeout after 60s", duration_ms=60100)
        with bind_factstore(isolated_vault):
            result = file_outline(str(tmp_path), "slow.py")
        assert result.success
        assert isinstance(result.data, dict)
        assert result.data["extracted_via"] == "regex"
        assert "grep" in result.data["note"].lower()
        assert any(d["name"] == "slow" for d in result.data["definitions"])


class TestFindSymbolDegradedSignal:
    def test_results_unannotated_when_no_vault(self, tmp_path):
        """No vault bound → tool must behave exactly like before."""
        fp = tmp_path / "mod.py"
        fp.write_text("def my_function(): pass\n")
        result = find_symbol(str(tmp_path), "my_function")
        assert result.success
        assert result.data
        for d in result.data:
            assert "extracted_via" not in d

    def test_degraded_file_annotated_in_results(self, tmp_path, isolated_vault):
        """find_symbol must tag each result from a skip-listed file so the
        agent can route to grep for authoritative info."""
        # Two files: one clean, one flagged.
        (tmp_path / "clean.py").write_text("def target_fn(): pass\n")
        flagged = tmp_path / "flagged.py"
        flagged.write_text("def target_fn(): pass\n")
        isolated_vault.put_skip(str(flagged), reason="tree-sitter timeout", duration_ms=60100)

        # Force a cold build of the symbol index within the vault binding
        # so _get_symbol_index sees both files — the skip tag is only
        # consulted later inside find_symbol's result-building loop.
        from app.code_tools.tools import invalidate_symbol_cache

        invalidate_symbol_cache(str(tmp_path))
        with bind_factstore(isolated_vault):
            result = find_symbol(str(tmp_path), "target_fn")
        assert result.success
        # At least one result per file — the flagged one must carry the tag.
        flagged_marked = [
            d for d in result.data if d["file_path"] == "flagged.py" and d.get("extracted_via") == "regex"
        ]
        clean_untagged = [d for d in result.data if d["file_path"] == "clean.py" and "extracted_via" not in d]
        assert flagged_marked, f"flagged.py result missing tag: {result.data}"
        assert clean_untagged, f"clean.py result unexpectedly tagged: {result.data}"
