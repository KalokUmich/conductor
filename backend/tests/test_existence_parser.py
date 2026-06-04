"""Tests for existence-worker JSON parsing hardening (Fix-C tolerance).

The old ``_parse_existence_json`` matched fenced blocks with a NON-GREEDY
``\\{[\\s\\S]*?\\}`` regex, which truncates a nested envelope at the first
``}`` and then fails — the dominant cause of the existence worker's
``parse_failed``. The rewrite uses a string-aware balanced-span scan.
"""

from __future__ import annotations

from app.agent_loop.existence_scanners import (
    _balanced_json_spans,
    _parse_existence_json,
)


def test_nested_json_in_fence_no_longer_truncates():
    """A nested {"symbols":[{...}]} inside a fence must parse (was parse_failed)."""
    raw = """Here is my analysis.

```json
{"symbols": [{"name": "Foo", "exists": true}, {"name": "Bar", "exists": false}]}
```
"""
    parsed = _parse_existence_json(raw)
    assert parsed is not None
    assert len(parsed["symbols"]) == 2
    assert parsed["symbols"][0]["name"] == "Foo"


def test_prose_embedded_json_with_brace_in_string():
    """A finding text containing '}' must not truncate the span."""
    raw = 'The verdict is: {"symbols": [{"name": "x", "note": "uses map[string]bool{} literal"}]} ' "— done."
    parsed = _parse_existence_json(raw)
    assert parsed is not None
    assert parsed["symbols"][0]["name"] == "x"


def test_last_object_with_symbols_wins():
    """Models restate near the end — prefer the last valid symbols envelope."""
    raw = (
        '{"symbols": [{"name": "stale"}]}\n'
        "After reconsidering:\n"
        '```json\n{"symbols": [{"name": "final", "exists": true}]}\n```'
    )
    parsed = _parse_existence_json(raw)
    assert parsed is not None
    assert parsed["symbols"][0]["name"] == "final"


def test_object_without_symbols_key_is_rejected():
    raw = '{"checks": [], "findings": []}'
    assert _parse_existence_json(raw) is None


def test_empty_and_garbage_return_none():
    assert _parse_existence_json("") is None
    assert _parse_existence_json("no json here at all") is None
    assert _parse_existence_json("{ broken json") is None


def test_balanced_spans_are_string_aware():
    spans = _balanced_json_spans('{"a": "}{"} tail {"b": 1}')
    assert spans == ['{"a": "}{"}', '{"b": 1}']
