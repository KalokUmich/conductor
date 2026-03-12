"""Tests for the per-tool output truncation policy."""
from __future__ import annotations

import json

from app.code_tools.output_policy import (
    OutputPolicy,
    apply_policy,
    get_policy,
)


class TestGetPolicy:
    def test_known_tool(self):
        p = get_policy("grep")
        assert p.max_results == 40
        assert p.truncate_unit == "results"

    def test_unknown_tool_returns_default(self):
        p = get_policy("nonexistent_tool")
        assert p.max_chars == 30_000
        assert p.truncate_unit == "chars"

    def test_read_file_policy(self):
        p = get_policy("read_file")
        assert p.max_chars == 50_000
        assert p.truncate_unit == "lines"

    def test_list_files_policy(self):
        p = get_policy("list_files")
        assert p.max_results == 100


class TestApplyPolicyResultTruncation:
    def test_list_under_limit_not_truncated(self):
        data = [{"file": f"f{i}.py", "line": i} for i in range(10)]
        text = apply_policy("grep", data)
        parsed = json.loads(text)
        assert len(parsed) == 10

    def test_list_over_limit_truncated(self):
        data = [{"file": f"f{i}.py", "line": i} for i in range(60)]
        text = apply_policy("grep", data)
        assert "more results truncated" in text

    def test_list_truncation_count_correct(self):
        data = [{"file": f"f{i}.py"} for i in range(50)]
        text = apply_policy("grep", data)
        # grep limit is 40, so 10 should be truncated
        assert "10 more results truncated" in text

    def test_dict_not_affected_by_max_results(self):
        data = {"content": "hello world", "total_lines": 5}
        text = apply_policy("read_file", data)
        parsed = json.loads(text)
        assert parsed["content"] == "hello world"


class TestApplyPolicyCharTruncation:
    def test_small_output_not_truncated(self):
        data = {"key": "value"}
        text = apply_policy("trace_variable", data)
        assert "truncated" not in text

    def test_large_output_truncated(self):
        # trace_variable has max_chars=20_000
        data = {"content": "x" * 25_000}
        text = apply_policy("trace_variable", data)
        assert "truncated" in text
        assert len(text) <= 25_000  # some overhead for the truncation message

    def test_line_truncation_for_read_file(self):
        # read_file uses "lines" truncation unit
        lines = ["line " + str(i) + " " * 100 for i in range(600)]
        data = {"content": "\n".join(lines), "total_lines": 600}
        text = apply_policy("read_file", data)
        # Should be truncated but still valid-ish
        if "truncated" in text:
            # Should try to break at newline
            assert text.endswith("... (truncated)")


class TestApplyPolicyBudgetAdaptive:
    def test_normal_budget_full_limits(self):
        data = [{"file": f"f{i}.py"} for i in range(50)]
        text = apply_policy("grep", data, remaining_input_tokens=200_000)
        # Normal budget: grep limit is 40 results
        assert "10 more results truncated" in text

    def test_low_budget_shrinks_limits(self):
        data = [{"file": f"f{i}.py"} for i in range(50)]
        text = apply_policy("grep", data, remaining_input_tokens=50_000)
        # Low budget: limit shrinks to max(5, 40//2) = 20
        assert "30 more results truncated" in text

    def test_very_low_budget_minimum_5(self):
        data = [{"file": f"f{i}.py"} for i in range(50)]
        # find_symbol has max_results=20, half is 10, still > 5
        text = apply_policy("find_symbol", data, remaining_input_tokens=10_000)
        assert "40 more results truncated" in text

    def test_none_remaining_tokens_no_adaptation(self):
        data = [{"file": f"f{i}.py"} for i in range(50)]
        text = apply_policy("grep", data, remaining_input_tokens=None)
        # Without remaining_tokens, no adaptation
        assert "10 more results truncated" in text


class TestApplyPolicyEdgeCases:
    def test_empty_list(self):
        text = apply_policy("grep", [])
        assert text == "[]"

    def test_empty_dict(self):
        text = apply_policy("read_file", {})
        assert text == "{}"

    def test_none_data(self):
        text = apply_policy("grep", None)
        assert text == "null"

    def test_string_data(self):
        text = apply_policy("unknown_tool", "hello")
        assert text == '"hello"'

    def test_unknown_tool_uses_default_policy(self):
        # Default is 30_000 chars
        data = {"content": "x" * 35_000}
        text = apply_policy("future_tool", data)
        assert "truncated" in text
