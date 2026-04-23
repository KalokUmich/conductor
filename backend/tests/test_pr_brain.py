"""Tests for PRBrainOrchestrator and related components in app.agent_loop.pr_brain."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agent_loop.pr_brain import PRBrainOrchestrator
from app.code_review.models import (
    ChangedFile,
    FileCategory,
    FindingCategory,
    PRContext,
    ReviewFinding,
    RiskLevel,
    RiskProfile,
    Severity,
)
from app.code_tools.schemas import ToolResult
from app.workflow.models import PRBrainConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_scratchpad(monkeypatch):
    """Skip Fact Vault creation in PRBrainOrchestrator unit tests.

    These tests don't exercise `run_stream` end-to-end; they unit-test
    helper methods with mocked tool_executors. Creating a real SQLite
    file per test leaks ~40KB each into ~/.conductor/scratchpad/. Set
    the env var to 0 for the duration of this test module.
    """
    monkeypatch.setenv("CONDUCTOR_SCRATCHPAD_ENABLED", "0")


def _make_pr_brain(
    review_agents=None,
    max_findings=10,
) -> PRBrainOrchestrator:
    """Create a PRBrainOrchestrator with minimal mock dependencies."""
    config = PRBrainConfig(
        review_agents=review_agents or ["correctness", "concurrency", "security", "reliability", "test_coverage"],
        post_processing={"max_findings": max_findings},
    )
    return PRBrainOrchestrator(
        provider=MagicMock(),
        explorer_provider=MagicMock(),
        workspace_path="/tmp/repo",
        diff_spec="HEAD~1..HEAD",
        pr_brain_config=config,
        agent_registry={},
        tool_executor=MagicMock(),
    )


def _make_pr_context(
    total_changed_lines=300,
    file_count=3,
    diff_spec="HEAD~1..HEAD",
) -> PRContext:
    files = [
        ChangedFile(
            path=f"app/file{i}.py",
            additions=total_changed_lines // max(file_count, 1),
            deletions=0,
            category=FileCategory.BUSINESS_LOGIC,
        )
        for i in range(file_count)
    ]
    return PRContext(
        diff_spec=diff_spec,
        files=files,
        total_additions=total_changed_lines,
        total_deletions=0,
        total_changed_lines=total_changed_lines,
        file_count=file_count,
    )


def _make_finding(
    title="Test finding",
    severity=Severity.WARNING,
    confidence=0.85,
    file="app/service.py",
    start_line=10,
    category=FindingCategory.CORRECTNESS,
    agent="correctness",
    evidence=None,
) -> ReviewFinding:
    return ReviewFinding(
        title=title,
        category=category,
        severity=severity,
        confidence=confidence,
        file=file,
        start_line=start_line,
        end_line=start_line,
        evidence=evidence if evidence is not None else ["evidence item 1", "evidence item 2"],
        risk="some risk",
        suggested_fix="some fix",
        agent=agent,
    )


def _make_tool_result(agent_name: str, answer: str, tool_calls_made: int = 5) -> ToolResult:
    return ToolResult(
        tool_name="dispatch_agent",
        success=True,
        data={
            "agent_name": agent_name,
            "answer": answer,
            "tool_calls_made": tool_calls_made,
            "iterations": 3,
        },
    )


def _low_risk_profile() -> RiskProfile:
    return RiskProfile(
        correctness=RiskLevel.LOW,
        concurrency=RiskLevel.LOW,
        security=RiskLevel.LOW,
        reliability=RiskLevel.LOW,
        operational=RiskLevel.LOW,
    )


def _high_security_profile() -> RiskProfile:
    return RiskProfile(
        correctness=RiskLevel.LOW,
        concurrency=RiskLevel.LOW,
        security=RiskLevel.HIGH,
        reliability=RiskLevel.LOW,
        operational=RiskLevel.LOW,
    )


# _inject_missing_symbol_findings (Phase 2 post-pass)
# ---------------------------------------------------------------------------


class TestInjectMissingSymbolFindings:
    """The post-pass must guarantee one finding per missing symbol even
    when the coordinator LLM drops or merges them. See
    SENTRY_EVAL_SUMMARY_2026-04-20.md Priority 1."""

    def _fake_store(self, missing_symbols, sig_mismatches=None):
        """Build a stub FactStore yielding given missing symbols and/or
        signature-mismatch facts. ``sig_mismatches`` is a list of dicts
        with keys: name, referenced_at, actual_params, missing_params."""
        import time

        from app.scratchpad.store import ExistenceFact

        missing_facts = [
            ExistenceFact(
                symbol_name=sym["name"],
                symbol_kind=sym.get("kind", "class"),
                referenced_at=sym["referenced_at"],
                exists_flag=False,
                evidence=sym.get("evidence", "grep → 0 matches"),
                signature_info=None,
                ts_written=int(time.time()),
            )
            for sym in missing_symbols
        ]
        present_facts = [
            ExistenceFact(
                symbol_name=s["name"],
                symbol_kind=s.get("kind", "method"),
                referenced_at=s["referenced_at"],
                exists_flag=True,
                evidence=s.get("evidence", "defined"),
                signature_info={
                    "actual_params": s.get("actual_params", []),
                    "missing_params": s.get("missing_params", []),
                },
                ts_written=int(time.time()),
            )
            for s in (sig_mismatches or [])
        ]

        class _Store:
            def iter_existence(self, exists=None):
                if exists is False:
                    yield from missing_facts
                elif exists is True:
                    yield from present_facts
                else:
                    yield from (missing_facts + present_facts)

        return _Store()

    def test_injects_finding_when_symbol_absent(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/app.py:42"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 1
        assert len(findings) == 1
        synth = findings[0]
        assert synth["severity"] == "critical"
        assert synth["confidence"] == 0.99
        assert "FooBar" in synth["title"]
        assert synth["file"] == "src/app.py"
        assert synth["start_line"] == 42
        assert synth["_injected_from"] == "phase2_existence_missing"

    def test_skips_when_finding_already_mentions_symbol(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/app.py:42"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        existing = [{
            "title": "FooBar not defined — ImportError",
            "severity": "critical",
            "confidence": 0.95,
            "file": "src/app.py",
            "start_line": 42,
            "end_line": 42,
        }]
        findings, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0
        assert findings == existing

    def test_skips_when_evidence_mentions_symbol(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/app.py:42"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        existing = [{
            "title": "Broken import at module load",
            "evidence": ["class FooBar not found anywhere"],
            "file": "src/app.py",
            "start_line": 42,
        }]
        _, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0

    def test_injects_multiple_missing_symbols(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/a.py:10"},
            {"name": "BazQux", "referenced_at": "src/b.py:20"},
            {"name": "Quux", "referenced_at": "src/c.py:30"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 3
        names = sorted(f["title"] for f in findings)
        assert any("FooBar" in t for t in names)
        assert any("BazQux" in t for t in names)
        assert any("Quux" in t for t in names)

    def test_returns_input_when_no_factstore(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: None)
        existing = [{"title": "unrelated"}]
        findings, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0
        assert findings is existing or findings == existing

    def test_parse_reference_location(self):
        from app.agent_loop.pr_brain import _parse_reference_location

        assert _parse_reference_location("src/x.py:42") == ("src/x.py", 42)
        assert _parse_reference_location("src/x.py") == ("src/x.py", 0)
        assert _parse_reference_location("") == ("", 0)
        assert _parse_reference_location("src/x.py:abc") == ("src/x.py:abc", 0)

    def test_finding_covers_symbol_by_title(self):
        from app.agent_loop.pr_brain import _finding_covers_symbol

        assert _finding_covers_symbol(
            {"title": "ImportError: FooBar not defined"},
            "FooBar",
            "src/a.py:10",
        )

    def test_finding_covers_symbol_by_file_and_runtime_marker(self):
        from app.agent_loop.pr_brain import _finding_covers_symbol

        # Title doesn't mention the symbol by name, but same file and
        # title signals a runtime-load error — we accept it as coverage.
        assert _finding_covers_symbol(
            {
                "title": "ImportError at module load time",
                "file": "src/a.py",
            },
            "FooBar",
            "src/a.py:10",
        )

    def test_finding_does_not_cover_unrelated_file(self):
        from app.agent_loop.pr_brain import _finding_covers_symbol

        assert not _finding_covers_symbol(
            {
                "title": "TypeError in handler",
                "file": "src/other.py",
            },
            "FooBar",
            "src/a.py:10",
        )

    def test_injects_signature_mismatch_finding(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store(
            missing_symbols=[],
            sig_mismatches=[{
                "name": "paginate",
                "referenced_at": "src/api/list.py:82",
                "actual_params": ["self", "cursor", "on_results"],
                "missing_params": ["enable_batch_mode"],
            }],
        )
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 1
        synth = findings[0]
        assert "TypeError" in synth["title"]
        assert "enable_batch_mode" in synth["title"]
        assert synth["severity"] == "high"
        assert synth["file"] == "src/api/list.py"
        assert synth["start_line"] == 82
        assert synth["_injected_from"] == "phase2_existence_sigmismatch"

    def test_skips_sig_mismatch_when_kwarg_in_title(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store(
            missing_symbols=[],
            sig_mismatches=[{
                "name": "paginate",
                "referenced_at": "src/api/list.py:82",
                "actual_params": ["self", "cursor"],
                "missing_params": ["enable_batch_mode"],
            }],
        )
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        existing = [{
            "title": "paginate() doesn't accept enable_batch_mode kwarg",
            "file": "src/api/list.py",
            "start_line": 82,
            "severity": "high",
        }]
        _, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0

    def test_injects_both_missing_and_sigmismatch(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store(
            missing_symbols=[
                {"name": "FooBar", "referenced_at": "src/app.py:11"},
            ],
            sig_mismatches=[{
                "name": "paginate",
                "referenced_at": "src/api/list.py:82",
                "actual_params": ["self", "cursor"],
                "missing_params": ["enable_batch_mode"],
            }],
        )
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 2
        kinds = {f["_injected_from"] for f in findings}
        assert "phase2_existence_missing" in kinds
        assert "phase2_existence_sigmismatch" in kinds


# ---------------------------------------------------------------------------
# _reflect_against_phase2_facts (P8 — external-signal reflection)
# ---------------------------------------------------------------------------


class TestReflectAgainstPhase2Facts:
    """P8: drop findings whose premise ('X is missing') contradicts a
    Phase 2 exists=True fact. Deliberately narrow — requires both the
    symbol mention AND existence-negation phrasing."""

    def _store_with_present(self, present):
        import time

        from app.scratchpad.store import ExistenceFact

        facts = [
            ExistenceFact(
                symbol_name=p["name"],
                symbol_kind=p.get("kind", "class"),
                referenced_at=p.get("referenced_at", ""),
                exists_flag=True,
                evidence=p.get("evidence", "defined"),
                signature_info=None,
                ts_written=int(time.time()),
            )
            for p in present
        ]

        class _Store:
            def iter_existence(self, exists=None):
                if exists is True:
                    yield from facts
                elif exists is False:
                    return
                    yield  # pragma: no cover
                else:
                    yield from facts

        return _Store()

    def test_drops_finding_claiming_present_symbol_is_missing(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "ImportError: RealClass not defined in module",
            "severity": "critical",
            "confidence": 0.9,
            "file": "src/app.py",
            "start_line": 10,
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 1
        assert kept == []

    def test_keeps_real_bug_on_existing_symbol(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        # RealClass exists AND a real bug on RealClass.method does not
        # claim RealClass is missing — it should stay.
        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "RealClass.process mutates input without validation",
            "severity": "warning",
            "confidence": 0.85,
            "file": "src/app.py",
            "start_line": 50,
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_keeps_injected_phase2_findings_always(self, monkeypatch):
        """Injected findings came from the facts themselves — they can
        never contradict the facts, so reflection must never drop them."""
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "ImportError at runtime: RealClass not defined",
            "_injected_from": "phase2_existence_missing",
            "confidence": 0.99,
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_returns_unchanged_when_no_factstore(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: None)

        findings = [{"title": "whatever", "file": "x.py"}]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_returns_unchanged_when_no_present_facts(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{"title": "SomeSymbol not defined"}]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_requires_both_symbol_mention_and_negation_phrase(self, monkeypatch):
        """Finding that mentions the symbol without negation phrasing
        (just a real bug on the method) must be kept."""
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "RealClass uses stale cache key",
            "risk": "RealClass.compute stores key in mutable dict",
            "file": "src/a.py",
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings


# ---------------------------------------------------------------------------
# _filter_findings_to_diff_scope (P11 cheap — diff-scope verification)
# ---------------------------------------------------------------------------


class TestFilterFindingsToDiffScope:
    """P11 cheap: drop (demote) findings whose file is not touched by
    the PR diff. Inspired by UltraReview's independent-verification
    principle, implemented mechanically (no LLM)."""

    def test_keeps_finding_in_diff(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/app.py": "@@ -1,3 +1,4 @@\n+new line\n"}
        findings = [{"title": "bug", "file": "src/app.py", "start_line": 2}]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert kept == findings
        assert demoted == []

    def test_demotes_finding_outside_diff(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/app.py": "diff"}
        findings = [{"title": "phantom", "file": "src/other.py", "start_line": 1}]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 1
        assert kept == []
        assert len(demoted) == 1
        assert demoted[0]["_demoted_reason"] == "file_not_in_diff"

    def test_matches_on_basename_fallback(self):
        """Coordinator sometimes reports just the basename; tolerate it."""
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/deep/path/app.py": "diff"}
        findings = [{"title": "bug", "file": "app.py", "start_line": 1}]
        kept, _demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert len(kept) == 1

    def test_keeps_injected_phase2_findings_when_outside_diff(self):
        """Phase 2 may synthesize findings at the reference site of a
        symbol that itself lives in an un-touched file. Never demote."""
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/caller.py": "diff"}
        findings = [{
            "title": "ImportError",
            "file": "src/target.py",
            "_injected_from": "phase2_existence_missing",
        }]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert len(kept) == 1
        assert demoted == []

    def test_no_filter_when_no_file_diffs(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        findings = [{"title": "bug", "file": "x.py"}]
        kept, _demoted, n = _filter_findings_to_diff_scope(findings, {})
        assert n == 0
        assert kept == findings

    def test_no_filter_when_finding_has_no_file(self):
        """A finding without a file claim (synthesis-level note) is kept."""
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/app.py": "diff"}
        findings = [{"title": "general observation"}]
        kept, _demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert kept == findings

    def test_handles_mixed_kept_and_demoted(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/real.py": "diff"}
        findings = [
            {"title": "good", "file": "src/real.py"},
            {"title": "bad", "file": "src/ghost.py"},
            {"title": "also good", "file": "src/real.py"},
        ]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 1
        assert len(kept) == 2
        assert len(demoted) == 1
        assert demoted[0]["file"] == "src/ghost.py"


# ---------------------------------------------------------------------------
# _scan_new_python_imports_for_missing (P13 — deterministic import verifier)
# ---------------------------------------------------------------------------


class TestScanNewPythonImportsForMissing:
    """P13: belt-and-suspenders against LLM Phase 2 flakiness.

    We build a tiny workspace on tmp_path and verify the scan
    mechanically detects missing imports without any LLM dispatch."""

    def _diff(self, path: str, new_lines: list[str]) -> str:
        """Build a minimal unified diff adding the given lines to a file
        starting at line 1."""
        body_plus = "\n".join(f"+{ln}" for ln in new_lines)
        return (
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{len(new_lines)} @@\n"
            f"{body_plus}"
        )

    def test_detects_missing_imported_symbol(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        # workspace has my_mod but NOT FooBar defined
        (tmp_path / "my_mod.py").write_text(
            "class RealClass:\n    pass\n"
        )
        diff = self._diff("entry.py", ["from my_mod import FooBar"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert len(found) == 1
        assert found[0]["name"] == "FooBar"
        assert "my_mod import FooBar" in found[0]["evidence"]

    def test_accepts_existing_symbol(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text(
            "class RealClass:\n    pass\n"
        )
        diff = self._diff("entry.py", ["from my_mod import RealClass"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_multiple_names_in_one_import(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text(
            "class Alpha:\n    pass\n\n"
            "def Beta():\n    return 1\n"
        )
        diff = self._diff(
            "entry.py",
            ["from my_mod import Alpha, Beta, MissingGamma"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        names = [f["name"] for f in found]
        assert "MissingGamma" in names
        assert "Alpha" not in names
        assert "Beta" not in names

    def test_skips_framework_modules(self, tmp_path):
        """os / sys / typing / django etc. are never our responsibility."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff(
            "entry.py",
            [
                "from os import path",
                "from typing import Any",
                "from django.db import models",
            ],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_skips_relative_imports(self, tmp_path):
        """Relative imports need file-path resolution; skip for MVP."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff("entry.py", ["from .foo import NoneHere"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_skips_wildcard(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff("entry.py", ["from my_mod import *"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_respects_as_alias(self, tmp_path):
        """`from X import Foo as Bar` — verify Foo exists (the imported
        name), not Bar (the local alias)."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text(
            "class RealThing:\n    pass\n"
        )
        diff = self._diff(
            "entry.py", ["from my_mod import RealThing as RT"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_symbol_defined_as_assignment(self, tmp_path):
        """Module-level assignments count: `CONFIG = ...`."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text("CONFIG = {'a': 1}\n")
        diff = self._diff("entry.py", ["from my_mod import CONFIG"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_empty_workspace_or_diff(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        assert _scan_new_python_imports_for_missing("", {}) == []
        assert _scan_new_python_imports_for_missing(str(tmp_path), {}) == []

    def test_non_python_file_skipped(self, tmp_path):
        """A .ts diff should not trigger Python symbol checks."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff("entry.ts", ["import { Foo } from './bar';"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.ts": diff},
        )
        assert found == []

    def test_caps_symbols_checked(self, tmp_path):
        """Hard cap prevents runtime blow-up on giant diffs."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        # Create my_mod.py so _module_is_first_party passes (else scan
        # skips external modules entirely).
        (tmp_path / "my_mod.py").write_text("# empty module\n")
        many_names = [f"Missing{i}" for i in range(40)]
        diff = self._diff(
            "entry.py", [f"from my_mod import {', '.join(many_names)}"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff}, max_symbols_checked=5,
        )
        assert len(found) == 5

    def test_skips_non_first_party_modules(self, tmp_path):
        """External pip packages (arroyo, kombu, celery...) must NOT be
        flagged as missing. The workspace doesn't contain their source."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        # No my_mod.py in the workspace — it's an external package.
        # P13 used to false-positive on this (arroyo case on sentry-009).
        diff = self._diff(
            "entry.py",
            ["from arroyo.backends.kafka import KafkaPayload"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_first_party_under_src_directory(self, tmp_path):
        """Common repo layout: code under src/<module>/. First-party
        detection must resolve `from my_pkg import X` to `src/my_pkg/...`."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "my_pkg").mkdir()
        (tmp_path / "src" / "my_pkg" / "__init__.py").write_text(
            "class Real:\n    pass\n"
        )
        diff = self._diff(
            "entry.py",
            ["from my_pkg import Real, Missing"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        names = [f["name"] for f in found]
        assert "Missing" in names
        assert "Real" not in names


# ---------------------------------------------------------------------------
# _scan_new_go_references_for_missing (P13-Go — phantom bare-identifier)
# ---------------------------------------------------------------------------


class TestScanNewGoReferencesForMissing:
    """P13-Go: deterministic catch for `go build` compile errors caused
    by undefined bare identifiers in same-package diff additions.
    Targets the grafana-003 class (`endpointQueryData` etc.).
    """

    def _diff(self, path: str, new_lines: list[str]) -> str:
        body_plus = "\n".join(f"+{ln}" for ln in new_lines)
        return (
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{len(new_lines)} @@\n"
            f"{body_plus}"
        )

    def test_flags_phantom_bare_call(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "foo"
        pkg.mkdir(parents=True)
        (pkg / "existing.go").write_text(
            "package foo\nfunc DefinedFunc() {}\n"
        )
        (pkg / "new.go").write_text("package foo\n")
        diff = self._diff(
            "pkg/foo/new.go",
            ["package foo", "", "func UseIt() {",
             "\tendpointQueryData(\"x\")", "}"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/foo/new.go": diff},
        )
        names = [f["name"] for f in found]
        assert "endpointQueryData" in names
        ev = next(f for f in found if f["name"] == "endpointQueryData")["evidence"]
        assert "go build" in ev
        assert "undefined" in ev

    def test_defined_in_package_not_flagged(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "foo"
        pkg.mkdir(parents=True)
        (pkg / "helper.go").write_text(
            "package foo\nfunc DefinedFunc() string { return \"\" }\n"
        )
        (pkg / "new.go").write_text("package foo\n")
        diff = self._diff(
            "pkg/foo/new.go",
            ["package foo", "", "func UseIt() {",
             "\tDefinedFunc()", "}"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/foo/new.go": diff},
        )
        assert found == []

    def test_go_builtins_skipped(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "bar"
        pkg.mkdir(parents=True)
        (pkg / "new.go").write_text("package bar\n")
        diff = self._diff(
            "pkg/bar/new.go",
            ["package bar", "",
             "func Work(xs []int) int {",
             "\ts := make([]int, 0)",
             "\tfor _, x := range xs { s = append(s, x) }",
             "\treturn len(s)",
             "}"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/bar/new.go": diff},
        )
        # make, append, len all builtins — none flagged
        assert found == []

    def test_method_call_skipped(self, tmp_path):
        """`obj.Method()` is not a bare call — skip."""
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "baz"
        pkg.mkdir(parents=True)
        (pkg / "new.go").write_text("package baz\n")
        diff = self._diff(
            "pkg/baz/new.go",
            ["package baz", "",
             "func Work(c Client) {", "\tc.DoSomething()", "}"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/baz/new.go": diff},
        )
        # DoSomething is a method call, not bare — not flagged
        names = [f["name"] for f in found]
        assert "DoSomething" not in names

    def test_package_qualified_call_skipped(self, tmp_path):
        """`pkg.Foo()` is package-qualified — scope-out per MVP."""
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "qux"
        pkg.mkdir(parents=True)
        (pkg / "new.go").write_text("package qux\n")
        diff = self._diff(
            "pkg/qux/new.go",
            ["package qux", "", "import \"fmt\"", "",
             "func Work() { fmt.Println(\"hi\") }"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/qux/new.go": diff},
        )
        names = [f["name"] for f in found]
        assert "Println" not in names
        assert "fmt" not in names

    def test_func_declaration_not_self_flagged(self, tmp_path):
        """A function declared in the diff should not flag itself as
        a phantom caller."""
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "selfref"
        pkg.mkdir(parents=True)
        (pkg / "new.go").write_text("package selfref\n")
        diff = self._diff(
            "pkg/selfref/new.go",
            ["package selfref", "", "func NewOne() int { return 42 }"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/selfref/new.go": diff},
        )
        names = [f["name"] for f in found]
        assert "NewOne" not in names

    def test_test_files_skipped(self, tmp_path):
        """_test.go files get scope-out to avoid cross-package test helpers."""
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "a"
        pkg.mkdir(parents=True)
        (pkg / "new_test.go").write_text("package a\n")
        diff = self._diff(
            "pkg/a/new_test.go",
            ["package a", "", "func TestSomething(t *T) {",
             "\tsomeUndefinedHelper()", "}"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/a/new_test.go": diff},
        )
        assert found == []

    def test_symbol_cap(self, tmp_path):
        """Cap at max_symbols_checked (default 24)."""
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "many"
        pkg.mkdir(parents=True)
        (pkg / "new.go").write_text("package many\n")
        calls = [f"\tmissing{i}()" for i in range(30)]
        diff = self._diff(
            "pkg/many/new.go",
            ["package many", "", "func F() {"] + calls + ["}"],
        )
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/many/new.go": diff},
            max_symbols_checked=10,
        )
        assert len(found) <= 10

    def test_grafana_003_three_phantoms(self, tmp_path):
        """Reproducer for grafana-003: 3 phantom bare-call identifiers
        in a single newly-added Go file. In production the patched file
        exists on disk (eval runner materializes it before scan); we
        mirror that here."""
        from app.agent_loop.pr_brain import (
            _scan_new_go_references_for_missing,
        )
        pkg = tmp_path / "pkg" / "clientmiddleware"
        pkg.mkdir(parents=True)
        # No helpers define endpoint* symbols.
        (pkg / "logger_middleware.go").write_text(
            "package clientmiddleware\n\n"
            "type LoggerMiddleware struct {}\n"
        )
        # Materialise the patched file — the scanner reads it to check
        # for dot-imports.
        contextual = pkg / "contextual_logger.go"
        contextual.write_text(
            "package clientmiddleware\n\n"
            "type ContextualLoggerMiddleware struct{ next Handler }\n\n"
            "func (m *ContextualLoggerMiddleware) QueryData(ctx context.Context, req *QueryDataRequest) {\n"
            "\tendpointQueryData(req)\n"
            "}\n"
        )
        diff_lines = [
            "package clientmiddleware",
            "",
            "type ContextualLoggerMiddleware struct{ next Handler }",
            "",
            "func (m *ContextualLoggerMiddleware) QueryData(ctx context.Context, req *QueryDataRequest) {",
            "\tendpointQueryData(req)",
            "}",
            "",
            "func (m *ContextualLoggerMiddleware) CallResource(ctx context.Context, req *CallResourceRequest) {",
            "\tendpointCallResource(req)",
            "}",
            "",
            "func (m *ContextualLoggerMiddleware) CheckHealth(ctx context.Context, req *CheckHealthRequest) {",
            "\tendpointCheckHealth(req)",
            "}",
        ]
        diff = self._diff("pkg/clientmiddleware/contextual_logger.go", diff_lines)
        found = _scan_new_go_references_for_missing(
            str(tmp_path), {"pkg/clientmiddleware/contextual_logger.go": diff},
        )
        names = {f["name"] for f in found}
        assert "endpointQueryData" in names
        assert "endpointCallResource" in names
        assert "endpointCheckHealth" in names


# ---------------------------------------------------------------------------
# _scan_new_java_references_for_missing (P13-Java — phantom class refs)
# ---------------------------------------------------------------------------


class TestScanNewJavaReferencesForMissing:
    """P13-Java: deterministic catch for `cannot find symbol` compile
    errors caused by new class references (`new Foo(`, `Foo.static()`,
    etc.) with no matching import or same-package definition."""

    def _diff(self, path: str, new_lines: list[str]) -> str:
        body_plus = "\n".join(f"+{ln}" for ln in new_lines)
        return (
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{len(new_lines)} @@\n"
            f"{body_plus}"
        )

    def _make_pkg_file(self, tmp_path, rel_path: str, content: str) -> None:
        full = tmp_path / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def test_flags_phantom_new_class(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "public class UseIt {\n"
            "    void run() { new PhantomHelper(); }\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "public class UseIt {",
             "    void run() { new PhantomHelper(); }",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        names = [f["name"] for f in found]
        assert "PhantomHelper" in names
        ev = next(f for f in found if f["name"] == "PhantomHelper")["evidence"]
        assert "cannot find symbol" in ev

    def test_imported_class_not_flagged(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "import com.bar.RealHelper;\n\n"
            "public class UseIt {\n"
            "    void run() { new RealHelper(); }\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "import com.bar.RealHelper;",
             "",
             "public class UseIt {",
             "    void run() { new RealHelper(); }",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        assert found == []

    def test_same_package_class_not_flagged(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/SiblingClass.java",
            "package com.foo;\n\npublic class SiblingClass {}\n",
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "public class UseIt {\n"
            "    void run() { new SiblingClass(); }\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "public class UseIt {",
             "    void run() { new SiblingClass(); }",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        assert found == []

    def test_java_lang_implicit_import_not_flagged(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "public class UseIt {\n"
            "    void run() { new String(\"hi\"); "
            "throw new IllegalStateException(); }\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "public class UseIt {",
             "    void run() { new String(\"hi\"); "
             "throw new IllegalStateException(); }",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        # String + IllegalStateException are java.lang.* implicit
        assert found == []

    def test_star_import_file_skipped(self, tmp_path):
        """File with a `com.x.*` import is skipped to avoid false flags."""
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "import com.bar.*;\n\n"
            "public class UseIt {\n"
            "    void run() { new PhantomHelper(); }\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "import com.bar.*;",
             "",
             "public class UseIt {",
             "    void run() { new PhantomHelper(); }",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        # Star import hides class visibility; MVP skips entire file
        assert found == []

    def test_generic_parameter_phantom(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "import java.util.List;\n\n"
            "public class UseIt {\n"
            "    List<PhantomType> items;\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "import java.util.List;",
             "",
             "public class UseIt {",
             "    List<PhantomType> items;",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        names = [f["name"] for f in found]
        assert "PhantomType" in names

    def test_extends_phantom(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "public class UseIt extends PhantomBase {\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "public class UseIt extends PhantomBase {",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        names = [f["name"] for f in found]
        assert "PhantomBase" in names

    def test_primitive_type_skipped(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_java_references_for_missing,
        )
        self._make_pkg_file(
            tmp_path,
            "src/main/java/com/foo/UseIt.java",
            "package com.foo;\n\n"
            "public class UseIt {\n"
            "    int count = 0;\n"
            "    boolean flag = false;\n"
            "}\n",
        )
        diff = self._diff(
            "src/main/java/com/foo/UseIt.java",
            ["package com.foo;", "",
             "public class UseIt {",
             "    int count = 0;",
             "    boolean flag = false;",
             "}"],
        )
        found = _scan_new_java_references_for_missing(
            str(tmp_path), {"src/main/java/com/foo/UseIt.java": diff},
        )
        # int, boolean are primitives; even the UPPER-start matcher
        # shouldn't pick them up since the pattern requires UPPER prefix
        names = [f["name"] for f in found]
        assert "int" not in names
        assert "boolean" not in names


# ---------------------------------------------------------------------------
# _scan_for_stub_call_sites (P14 — mechanical stub detector)
# ---------------------------------------------------------------------------


class TestScanForStubCallSites:
    """P14: detect stub functions added in the diff whose body is a
    literal 'not implemented' error return, and flag call sites that
    also appear in the diff (on + lines or context lines). Pure
    diff-text scan — no workspace read. Aimed at grafana-009 class
    multi-site stub bugs."""

    def test_detects_go_stub_with_diff_caller(self):
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "--- a/pkg/sql/db.go\n"
            "+++ b/pkg/sql/db.go\n"
            "@@ -0,0 +1,6 @@\n"
            "+func (d *DB) RunCommands(cmds []string) (string, error) {\n"
            '+\treturn "", errors.New("not implemented")\n'
            "+}\n"
        )
        caller_diff = (
            "--- a/pkg/sql/parser.go\n"
            "+++ b/pkg/sql/parser.go\n"
            "@@ -10,5 +10,7 @@\n"
            " func Parse() {\n"
            "+\tduckDB := NewInMemoryDB()\n"
            "+\tduckDB.RunCommands([]string{\"SELECT 1\"})\n"
            " }\n"
        )
        file_diffs = {
            "pkg/sql/db.go": stub_diff,
            "pkg/sql/parser.go": caller_diff,
        }
        found = _scan_for_stub_call_sites(file_diffs)
        names = [f["stub_name"] for f in found]
        assert "RunCommands" in names
        caller_files = {f["caller_file"] for f in found}
        assert "pkg/sql/parser.go" in caller_files

    def test_detects_go_stub_on_unchanged_context_line(self):
        """A pre-existing `duckDB.RunCommands(...)` now hits a NEW stub —
        caller is on a context line, not a + line. Must still be flagged."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/pkg/sql/db.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func (d *DB) RunCommands() (string, error) {\n"
            '+\treturn "", errors.New("not implemented")\n'
            "+}\n"
        )
        caller_diff = (
            "+++ b/pkg/sql/parser.go\n"
            "@@ -20,3 +20,3 @@\n"
            " func Parse() {\n"
            " \tduckDB.RunCommands([]string{\"x\"})\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "pkg/sql/db.go": stub_diff,
            "pkg/sql/parser.go": caller_diff,
        })
        assert any(
            f["caller_file"] == "pkg/sql/parser.go" for f in found
        )

    def test_python_stub_detection(self):
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/svc.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def fetch_data():\n"
            "+    raise NotImplementedError\n"
        )
        caller_diff = (
            "+++ b/api.py\n"
            "@@ -5,3 +5,4 @@\n"
            " def handler():\n"
            "+    data = fetch_data()\n"
            "     return data\n"
        )
        found = _scan_for_stub_call_sites({
            "svc.py": stub_diff,
            "api.py": caller_diff,
        })
        assert any(f["stub_name"] == "fetch_data" for f in found)

    def test_does_not_flag_stub_without_caller(self):
        """A stub added with no caller in the diff is fine — may be
        a legitimate TODO placeholder."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/todo.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def later():\n"
            "+    raise NotImplementedError\n"
        )
        found = _scan_for_stub_call_sites({"todo.py": stub_diff})
        assert found == []

    def test_does_not_confuse_func_declaration_with_call(self):
        """`func Foo(` and `def Foo(` are declarations, not calls.
        Must not be flagged as call sites of Foo."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/a.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func (d *DB) Foo() error {\n"
            '+\treturn errors.New("not implemented")\n'
            "+}\n"
        )
        # Another file defines a same-named function. Not a call.
        other_diff = (
            "+++ b/b.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func Foo() error {\n"
            "+\treturn nil\n"
            "+}\n"
        )
        found = _scan_for_stub_call_sites({
            "a.go": stub_diff,
            "b.go": other_diff,
        })
        # Neither file should be flagged as a caller — b.go's `func Foo`
        # is a declaration, and a.go's is the stub itself.
        assert found == []

    def test_ignores_non_stub_functions(self):
        """Real function bodies (not just 'not implemented') must not
        trigger stub detection."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        real_diff = (
            "+++ b/a.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func (d *DB) Real() error {\n"
            "+\treturn d.actualWork()\n"
            "+}\n"
        )
        caller_diff = (
            "+++ b/b.go\n"
            "@@ -5,3 +5,4 @@\n"
            " func X() {\n"
            "+\td.Real()\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.go": real_diff,
            "b.go": caller_diff,
        })
        assert found == []

    def test_empty_diff(self):
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        assert _scan_for_stub_call_sites({}) == []

    def test_java_unsupported_operation_stub(self):
        """Canonical Java stub: `throw new UnsupportedOperationException`.
        Validates against the keycloak-003 / -005 pattern."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/impl/Foo.java\n"
            "@@ -0,0 +1,5 @@\n"
            "+    @Override\n"
            "+    public CertificateUtilsProvider getCertificateUtils() {\n"
            '+        throw new UnsupportedOperationException("Not supported yet.");\n'
            "+    }\n"
        )
        caller_diff = (
            "+++ b/api/Service.java\n"
            "@@ -10,3 +10,4 @@\n"
            " public class Service {\n"
            "+    CertificateUtilsProvider p = provider.getCertificateUtils();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "impl/Foo.java": stub_diff,
            "api/Service.java": caller_diff,
        })
        assert any(f["stub_name"] == "getCertificateUtils" for f in found)
        assert any(
            f["caller_file"] == "api/Service.java" for f in found
        )

    def test_java_notimplementedexception_stub(self):
        """Apache Commons pattern."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/svc.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public String fetch() {\n"
            "+        throw new NotImplementedException();\n"
            "+    }\n"
        )
        caller_diff = (
            "+++ b/api.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class Api {\n"
            "+    String data = svc.fetch();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "svc.java": stub_diff,
            "api.java": caller_diff,
        })
        assert any(f["stub_name"] == "fetch" for f in found)

    def test_java_runtime_exception_with_not_implemented_message(self):
        """`throw new RuntimeException("not implemented")` only flagged
        when the message mentions not-implemented / not-supported."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/a.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public void doThing() {\n"
            '+        throw new RuntimeException("feature not implemented");\n'
            "+    }\n"
        )
        caller_diff = (
            "+++ b/b.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class B {\n"
            "+    a.doThing();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.java": stub_diff,
            "b.java": caller_diff,
        })
        assert any(f["stub_name"] == "doThing" for f in found)

    def test_java_real_runtime_exception_not_flagged(self):
        """`throw new RuntimeException("db connection lost")` is a
        legitimate error, NOT a stub. Must not be flagged."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/a.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public void doThing() {\n"
            '+        throw new RuntimeException("db connection lost");\n'
            "+    }\n"
        )
        caller_diff = (
            "+++ b/b.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class B {\n"
            "+    a.doThing();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.java": stub_diff,
            "b.java": caller_diff,
        })
        assert found == []

    def test_java_does_not_treat_decl_as_call(self):
        """A same-named method declared in an interface alongside the
        impl stub must not be counted as a caller."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/impl/Foo.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public void render() {\n"
            "+        throw new UnsupportedOperationException();\n"
            "+    }\n"
        )
        # Interface also in the diff — declares the method with the same
        # name. This is a DECLARATION, not a call.
        iface_diff = (
            "+++ b/iface/IFoo.java\n"
            "@@ -0,0 +1,2 @@\n"
            "+public interface IFoo {\n"
            "+    public void render();\n"
            "+}\n"
        )
        found = _scan_for_stub_call_sites({
            "impl/Foo.java": stub_diff,
            "iface/IFoo.java": iface_diff,
        })
        # Expect 0 callers: the only mention outside the stub is a decl.
        assert found == []

    def test_java_non_stub_body_not_flagged(self):
        """Real method body must not trigger stub detection."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        real_diff = (
            "+++ b/a.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public int add(int a, int b) {\n"
            "+        return a + b;\n"
            "+    }\n"
        )
        caller_diff = (
            "+++ b/b.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class B {\n"
            "+    int x = calc.add(1, 2);\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.java": real_diff,
            "b.java": caller_diff,
        })
        assert found == []


# ---------------------------------------------------------------------------
# _inject_stub_caller_findings (P14 — finding injection from stub pairs)
# ---------------------------------------------------------------------------


class TestInjectStubCallerFindings:
    """P14 injection turns stub/caller pairs into synthetic findings.
    Caller sites not already covered by a coordinator finding must be
    flagged; covered sites must be skipped."""

    def _grafana_style_diffs(self):
        return {
            "pkg/sql/db.go": (
                "+++ b/pkg/sql/db.go\n"
                "@@ -0,0 +1,3 @@\n"
                "+func (d *DB) RunCommands() (string, error) {\n"
                '+\treturn "", errors.New("not implemented")\n'
                "+}\n"
            ),
            "pkg/sql/parser.go": (
                "+++ b/pkg/sql/parser.go\n"
                "@@ -20,3 +20,4 @@\n"
                " func Parse() {\n"
                "+\td.RunCommands()\n"
                " }\n"
            ),
        }

    def test_injects_when_coordinator_missed_caller(self):
        from app.agent_loop.pr_brain import _inject_stub_caller_findings

        kept, n = _inject_stub_caller_findings([], self._grafana_style_diffs())
        assert n == 1
        assert kept[0]["_injected_from"] == "p14_stub_caller"
        assert kept[0]["file"] == "pkg/sql/parser.go"
        assert kept[0]["severity"] == "high"

    def test_skips_when_coordinator_already_flagged(self):
        from app.agent_loop.pr_brain import _inject_stub_caller_findings

        existing = [{
            "title": "RunCommands call hits not-implemented stub",
            "file": "pkg/sql/parser.go",
            "start_line": 21,
            "severity": "high",
        }]
        kept, n = _inject_stub_caller_findings(
            existing, self._grafana_style_diffs(),
        )
        assert n == 0
        assert kept == existing

    def test_no_inject_when_no_stubs_in_diff(self):
        from app.agent_loop.pr_brain import _inject_stub_caller_findings

        kept, n = _inject_stub_caller_findings([], {})
        assert n == 0
        assert kept == []


# ---------------------------------------------------------------------------
# P9 — Java-aware Phase 2 hint injection
# ---------------------------------------------------------------------------


class TestPhase2LangHints:
    """The Phase 2 worker query should include a per-language
    `find_symbol`-preference hint for each of the 4 mainstream
    languages (Java, Python, Go, TS/JS) — and only when the diff
    actually touches that language."""

    def _make_ctx(self, paths):
        files = [
            ChangedFile(
                path=p, additions=10, deletions=0,
                category=FileCategory.BUSINESS_LOGIC,
            )
            for p in paths
        ]
        return PRContext(
            diff_spec="HEAD~1..HEAD",
            files=files,
            total_additions=10 * len(files),
            total_deletions=0,
            total_changed_lines=10 * len(files),
            file_count=len(files),
        )

    def _capture_phase2_query(self, brain, ctx, file_diffs):
        """Drive _run_v2_phase2_existence far enough to capture the query
        that gets passed to dispatch_agent, then short-circuit."""
        captured = {}

        class _FakeExecutor:
            async def execute(self, tool_name, params):
                captured["tool"] = tool_name
                captured["params"] = params
                # Return minimal success result so the generator completes.
                return ToolResult(
                    tool_name=tool_name,
                    success=True,
                    data={"answer": '{"symbols":[]}'},
                )

        async def _drive():
            gen = brain._run_v2_phase2_existence(_FakeExecutor(), ctx, file_diffs)
            async for _ in gen:
                pass

        import asyncio as _asyncio
        _asyncio.run(_drive())
        return captured

    def test_java_hint_present_when_java_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["src/main/java/Foo.java"])
        captured = self._capture_phase2_query(
            brain, ctx, {"src/main/java/Foo.java": "@@ -1 +1 @@\n+class Foo {}\n"},
        )
        assert captured["tool"] == "dispatch_agent"
        query = captured["params"]["query"]
        assert "Language-specific hints" in query
        assert "find_symbol" in query
        assert "Java" in query
        # Java-specific hint should NOT contain hints for other languages
        assert "Python (" not in query
        assert "Go (" not in query

    def test_python_hint_present_when_python_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["app/service.py", "app/model.py"])
        captured = self._capture_phase2_query(
            brain, ctx, {"app/service.py": "@@ -1 +1 @@\n+def foo(): pass\n"},
        )
        query = captured["params"]["query"]
        assert "Language-specific hints" in query
        assert "Python (`.py`)" in query
        assert "find_symbol" in query
        assert "MRO" in query
        # Python-only should not include Java/Go/TS hints
        assert "Java (`" not in query
        assert "Go (`.go`)" not in query
        assert "TypeScript" not in query

    def test_go_hint_present_when_go_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["pkg/handler.go"])
        captured = self._capture_phase2_query(
            brain, ctx, {"pkg/handler.go": "@@ -1 +1 @@\n+func Foo() {}\n"},
        )
        query = captured["params"]["query"]
        assert "Go (`.go`)" in query
        assert "receiver" in query

    def test_ts_hint_present_for_tsx_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["src/components/Button.tsx"])
        captured = self._capture_phase2_query(
            brain, ctx, {"src/components/Button.tsx": "@@ -1 +1 @@\n+export const Button = () => null\n"},
        )
        query = captured["params"]["query"]
        assert "TypeScript" in query
        assert "find_symbol" in query
        assert "overload" in query.lower()

    def test_no_hint_for_non_mainstream_only_diff(self):
        """Rust/C/C++ files parse via tree-sitter but don't get a tailored
        AST-prefer hint (lower signal:noise — those agents rarely run)."""
        brain = _make_pr_brain()
        ctx = self._make_ctx(["src/lib.rs"])
        captured = self._capture_phase2_query(
            brain, ctx, {"src/lib.rs": "@@ -1 +1 @@\n+fn foo() {}\n"},
        )
        query = captured["params"]["query"]
        assert "Language-specific hints" not in query

    def test_multiple_hints_for_mixed_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx([
            "app/service.py",
            "src/main/java/Bar.java",
            "pkg/handler.go",
            "web/app.tsx",
        ])
        captured = self._capture_phase2_query(
            brain, ctx, {
                "app/service.py": "@@ -1 +1 @@\n+pass\n",
                "src/main/java/Bar.java": "@@ -1 +1 @@\n+class Bar {}\n",
                "pkg/handler.go": "@@ -1 +1 @@\n+func H() {}\n",
                "web/app.tsx": "@@ -1 +1 @@\n+export const A = 1\n",
            },
        )
        query = captured["params"]["query"]
        assert "Language-specific hints" in query
        assert "Java (`.java`)" in query
        assert "Python (`.py`)" in query
        assert "Go (`.go`)" in query
        assert "TypeScript" in query


class TestPhase2ReorderP13First:
    """v2u — P13 deterministic scanners must run BEFORE the LLM
    existence worker, not after. When P13 finds missing symbols, they
    must appear in the 'Pre-verified by P13' block of the LLM query so
    the worker knows to skip them and focus on signature-level checks.
    """

    def _make_ctx(self, paths):
        files = [
            ChangedFile(
                path=p, additions=10, deletions=0,
                category=FileCategory.BUSINESS_LOGIC,
            )
            for p in paths
        ]
        return PRContext(
            diff_spec="HEAD~1..HEAD",
            files=files,
            total_changed_lines=10 * len(files),
            file_count=len(files),
        )

    def _capture_query(self, brain, ctx, file_diffs, monkeypatch,
                       python_missing=None, go_missing=None, java_missing=None):
        """Monkeypatch the 3 P13 scanners + the factstore + capture the LLM query."""
        from app.agent_loop import pr_brain as pb
        from app import scratchpad as sp

        monkeypatch.setattr(
            pb, "_scan_new_python_imports_for_missing",
            lambda *a, **kw: python_missing or [],
        )
        monkeypatch.setattr(
            pb, "_scan_new_go_references_for_missing",
            lambda *a, **kw: go_missing or [],
        )
        monkeypatch.setattr(
            pb, "_scan_new_java_references_for_missing",
            lambda *a, **kw: java_missing or [],
        )
        # P13 only runs when a FactStore is available. The autouse
        # _disable_scratchpad fixture sets the env var OFF so real
        # reviews don't leak SQLite files into ~/.conductor/. For this
        # test we need current_factstore() to return SOMETHING store-
        # shaped so the P13 branch executes.
        fake_store = MagicMock()
        fake_store.put_existence = MagicMock()
        monkeypatch.setattr(sp, "current_factstore", lambda: fake_store)

        captured = {}

        class _FakeExecutor:
            async def execute(self, tool_name, params):
                captured["tool"] = tool_name
                captured["params"] = params
                return ToolResult(
                    tool_name=tool_name, success=True,
                    data={"answer": '{"symbols":[]}'},
                )

        async def _drive():
            gen = brain._run_v2_phase2_existence(_FakeExecutor(), ctx, file_diffs)
            async for _ in gen:
                pass

        import asyncio as _asyncio
        _asyncio.run(_drive())
        return captured

    def test_p13_missing_appear_in_pre_verified_block(self, monkeypatch):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["app/service.py"])
        captured = self._capture_query(
            brain, ctx, {"app/service.py": "@@ -1 +1 @@\n+from x import Y\n"},
            monkeypatch,
            python_missing=[{
                "name": "Y",
                "referenced_at": "app/service.py:1",
                "evidence": "from x import Y",
            }],
        )
        query = captured["params"]["query"]
        assert "Pre-verified missing symbols" in query
        assert "`Y`" in query  # backtick-wrapped symbol name
        assert "app/service.py:1" in query

    def test_no_pre_verified_block_when_p13_empty(self, monkeypatch):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["app/service.py"])
        captured = self._capture_query(
            brain, ctx, {"app/service.py": "@@ -1 +1 @@\n+pass\n"},
            monkeypatch,
            python_missing=[],
        )
        query = captured["params"]["query"]
        assert "Pre-verified missing symbols" not in query

    def test_task_text_shifted_to_signature_focus(self, monkeypatch):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["app/service.py"])
        captured = self._capture_query(
            brain, ctx, {"app/service.py": "@@ -1 +1 @@\n+pass\n"},
            monkeypatch,
        )
        query = captured["params"]["query"]
        # New task focuses on signature-level checks
        assert "Method call signatures" in query
        assert "signature" in query.lower()
        # Explicitly tells the worker not to re-verify imports
        assert "Do NOT re-verify import-level existence" in query

    def test_multi_language_p13_aggregated(self, monkeypatch):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["a.py", "b.go", "c.java"])
        captured = self._capture_query(
            brain, ctx,
            {
                "a.py": "@@ -1 +1 @@\n+from x import Py\n",
                "b.go": "@@ -1 +1 @@\n+GoRef()\n",
                "c.java": "@@ -1 +1 @@\n+new JavaRef();\n",
            },
            monkeypatch,
            python_missing=[{"name": "Py", "referenced_at": "a.py:1", "evidence": "..."}],
            go_missing=[{"name": "GoRef", "referenced_at": "b.go:1", "evidence": "..."}],
            java_missing=[{"name": "JavaRef", "referenced_at": "c.java:1", "evidence": "..."}],
        )
        query = captured["params"]["query"]
        assert "`Py`" in query
        assert "`GoRef`" in query
        assert "`JavaRef`" in query
