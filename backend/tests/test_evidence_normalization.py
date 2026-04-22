"""Unit tests for ``_normalize_evidence`` — defensive coercion of the
LLM's raw ``evidence`` field into a clean ``List[str]``.

Regression target: PR #14227 rendered a finding's evidence block one
character per bullet::

    - @
    - V
    - a
    - l
    - u
    - e
    ...

because a re-parsed string round-tripped through ``list(s)`` upstream,
producing ``["@", "V", "a", ...]`` which the dataclass ``evidence: List[str]``
accepted as-is. The markdown renderer then iterated char-by-char.
"""

from __future__ import annotations

from app.code_review.shared import _normalize_evidence


class TestNormalizeEvidence:
    def test_list_of_strings_passthrough(self):
        assert _normalize_evidence(["a", "b", "c"]) == ["a", "b", "c"]

    def test_single_string_wrapped_in_list(self):
        """LLM sometimes emits evidence as a sentence, not an array."""
        assert _normalize_evidence("cache key missing user_id at line 45") == [
            "cache key missing user_id at line 45",
        ]

    def test_empty_string_drops(self):
        assert _normalize_evidence("") == []

    def test_whitespace_only_string_drops(self):
        assert _normalize_evidence("   \n  ") == []

    def test_none_returns_empty(self):
        assert _normalize_evidence(None) == []

    def test_missing_key_via_dict_get_returns_empty(self):
        """Simulates ``raw.get("evidence")`` when key is absent."""
        raw = {}
        assert _normalize_evidence(raw.get("evidence")) == []

    def test_char_list_rejoins_into_one_string(self):
        """The PR #14227 bug — a re-parsed string showed up as chars."""
        chars = list('@Value("abound.auth.username")')
        out = _normalize_evidence(chars)
        assert len(out) == 1
        assert out[0] == '@Value("abound.auth.username")'

    def test_short_single_char_list_not_rejoined(self):
        """Heuristic threshold: below 6 single-char items, keep as-is."""
        # Authors might legitimately have 3 single-char bullets
        # (e.g. status codes ["A", "B", "C"]). Don't over-collapse.
        out = _normalize_evidence(["A", "B", "C"])
        assert out == ["A", "B", "C"]

    def test_whitespace_only_items_are_filtered_out(self):
        """Whitespace-only entries are dropped before the char-list
        heuristic runs — a char list containing spaces will shrink
        below the rejoin threshold. Document this behaviour."""
        # 2 whitespace items + 5 real chars → 5 kept → below 6 threshold
        out = _normalize_evidence([" ", "h", "i", " ", "w", "o", "r"])
        # 5 items, each single char — not rejoined (< 6 threshold)
        assert out == ["h", "i", "w", "o", "r"]

    def test_mostly_single_char_with_one_fragment_rejoins(self):
        """Real PR #14227 shape — first element is ``@Value("`` (8 chars),
        rest are single chars. ``≥ 80% single-char`` heuristic still
        fires and rejoins."""
        chars = ['@Value("', "a", "b", "o", "u", "n", "d", '")']
        out = _normalize_evidence(chars)
        assert len(out) == 1
        assert "abound" in out[0]
        assert "@Value" in out[0]

    def test_mixed_str_and_non_str_drops_non_str(self):
        out = _normalize_evidence(["valid", 42, None, {"not": "str"}, [1, 2]])
        # Numeric coerced to str; dict + None + nested list dropped
        assert out == ["valid", "42"]

    def test_float_and_bool_coerced(self):
        out = _normalize_evidence(["text", 3.14, True])
        assert out == ["text", "3.14", "True"]

    def test_empty_list_passthrough(self):
        assert _normalize_evidence([]) == []

    def test_list_with_leading_trailing_whitespace_trimmed(self):
        out = _normalize_evidence(["  hit  ", "   second   "])
        assert out == ["hit", "second"]

    def test_non_iterable_returns_empty(self):
        """Defensive — an unexpected shape shouldn't raise."""
        assert _normalize_evidence(42) == []
        assert _normalize_evidence({"a": "b"}) == []
        assert _normalize_evidence(True) == []

    def test_integration_parse_findings_with_malformed_evidence(self):
        """End-to-end: ``parse_findings`` must render a clean evidence
        list even when the LLM emitted char-list evidence."""
        from app.code_review.models import FindingCategory
        from app.code_review.shared import parse_findings

        answer = (
            'Here are my findings:\n\n'
            '```json\n'
            '[{"title":"Plaintext creds",'
            '"severity":"high",'
            '"confidence":0.9,'
            '"file":"Svc.java",'
            '"start_line":40,'
            '"end_line":45,'
            # Evidence is a CHAR LIST (the PR #14227 bug shape)
            '"evidence":["@","V","a","l","u","e","(\\"x\\")"],'
            '"risk":"leak",'
            '"suggested_fix":"hash"}]'
            '\n```\n'
        )
        findings = parse_findings(
            answer, agent_name="test",
            category=FindingCategory.SECURITY,
        )
        assert len(findings) == 1
        # Evidence should be a SINGLE rejoined string, not 7 char bullets
        assert len(findings[0].evidence) == 1
        assert findings[0].evidence[0] == '@Value("x")'
