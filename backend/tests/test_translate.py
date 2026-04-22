"""Unit tests for app.code_review.translate.translate_pr_summary."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.code_review.models import FindingCategory, ReviewFinding, ReviewResult, Severity
from app.code_review.translate import (
    AVAILABLE_PLATFORMS,
    translate_pr_summary,
)


def _finding(
    title: str = "Null check missing",
    severity: Severity = Severity.WARNING,
    file: str = "app/service.py",
    start_line: int = 42,
) -> ReviewFinding:
    return ReviewFinding(
        title=title,
        category=FindingCategory.CORRECTNESS,
        severity=severity,
        confidence=0.9,
        file=file,
        start_line=start_line,
        end_line=start_line,
        evidence=[f"Line {start_line}: missing null check"],
        risk="NPE at runtime",
        suggested_fix="Add guard",
        agent="pr_brain_v2",
    )


def _sync_call(return_text: str) -> MagicMock:
    """Build a fake AIProvider whose sync ``call_model`` returns `return_text`."""
    provider = MagicMock()
    provider.call_model = MagicMock(return_value=return_text)
    return provider


class TestTranslatePrSummary:
    """Drop-in replacement for the raw synthesis in PR-level comments."""

    @pytest.mark.asyncio
    async def test_empty_synthesis_returns_empty(self):
        provider = _sync_call("SHOULD NOT BE CALLED")
        out = await translate_pr_summary(
            synthesis="",
            findings=[],
            platform="azure",
            provider=provider,
        )
        assert out == ""
        provider.call_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_platform_returns_original(self):
        """Platform that has no style rule — passthrough, no LLM call."""
        provider = _sync_call("SHOULD NOT BE CALLED")
        original = "Original synthesis here."
        out = await translate_pr_summary(
            synthesis=original,
            findings=[],
            platform="unsupported_platform",
            provider=provider,
        )
        assert out == original
        provider.call_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_azure_platform_calls_provider_with_system_prompt(self):
        """When platform=azure, provider receives the Azure-shaped system prompt."""
        fake_out = "## Summary\nThis PR adds retry logic."
        provider = _sync_call(fake_out)
        out = await translate_pr_summary(
            synthesis="Long Google-style synthesis.",
            findings=[_finding()],
            platform="azure",
            provider=provider,
            pr_title="Add retry on timeout",
            pr_description="Fixes flaky webhook deliveries.",
        )
        assert out == fake_out
        provider.call_model.assert_called_once()
        # Positional args: (prompt, max_tokens, system_prompt)
        args = provider.call_model.call_args.args
        assert len(args) == 3
        user_msg, max_tokens, system = args
        assert max_tokens == 800
        assert "Azure DevOps" in system
        assert "NO code snippets" in system
        # User message should include title + description + findings themes
        assert "Add retry on timeout" in user_msg
        assert "Fixes flaky webhook deliveries." in user_msg
        assert "Null check missing" in user_msg  # finding title as theme

    @pytest.mark.asyncio
    async def test_llm_exception_fails_soft_to_original(self):
        """LLM error → return original synthesis, don't raise."""
        provider = MagicMock()
        provider.call_model = MagicMock(side_effect=RuntimeError("bedrock throttled"))
        original = "Original coordinator output."
        out = await translate_pr_summary(
            synthesis=original,
            findings=[],
            platform="azure",
            provider=provider,
        )
        assert out == original

    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_original(self):
        """Empty / whitespace-only LLM output → fall back to original."""
        provider = _sync_call("    \n   ")
        original = "Original synthesis."
        out = await translate_pr_summary(
            synthesis=original,
            findings=[],
            platform="azure",
            provider=provider,
        )
        assert out == original

    @pytest.mark.asyncio
    async def test_strips_code_fence_wrapper(self):
        """If LLM accidentally wraps output in ```...```, fences get stripped."""
        fenced = "```markdown\n## Summary\nShort one.\n```"
        provider = _sync_call(fenced)
        out = await translate_pr_summary(
            synthesis="raw synthesis",
            findings=[],
            platform="azure",
            provider=provider,
        )
        assert "```" not in out
        assert out.startswith("## Summary")

    @pytest.mark.asyncio
    async def test_findings_surfaced_as_themes_not_quoted_verbatim(self):
        """The user message should list finding titles only — NOT risk / evidence."""
        fake_out = "## Summary\nOK."
        provider = _sync_call(fake_out)
        findings = [
            _finding(title="Title One"),
            _finding(title="Title Two", severity=Severity.CRITICAL),
        ]
        await translate_pr_summary(
            synthesis="ignored",
            findings=findings,
            platform="azure",
            provider=provider,
        )
        user_msg = provider.call_model.call_args.args[0]
        # Both titles + severities should be present
        assert "Title One" in user_msg
        assert "Title Two" in user_msg
        assert "warning" in user_msg
        assert "critical" in user_msg
        # But evidence / risk / fix should NOT be embedded
        assert "NPE at runtime" not in user_msg
        assert "Add guard" not in user_msg

    def test_available_platforms_contains_azure(self):
        assert "azure" in AVAILABLE_PLATFORMS


class TestFormatSummaryMarkdownWithOverride:
    """format_summary_markdown should delegate long-form to
    overall_summary_override when provided, skipping the raw synthesis
    path and the "Detailed Analysis" header."""

    def _result(self, synthesis: str = "## Coordinator\nRaw output.") -> ReviewResult:
        return ReviewResult(
            diff_spec="HEAD~1..HEAD",
            findings=[_finding()],
            files_reviewed=["app/service.py"],
            merge_recommendation="approve",
            synthesis=synthesis,
            total_tokens=0,
            total_iterations=0,
            total_duration_ms=0,
        )

    def test_no_override_uses_synthesis_under_detailed_header(self):
        from app.integrations.azure_devops.formatter import format_summary_markdown

        md = format_summary_markdown(self._result())
        assert "### Detailed Analysis" in md
        assert "Raw output." in md

    def test_override_replaces_synthesis_section(self):
        from app.integrations.azure_devops.formatter import format_summary_markdown

        override = "## Summary\nThis PR refactors retry."
        md = format_summary_markdown(
            self._result(),
            overall_summary_override=override,
        )
        # Translator output owns its own header — no "Detailed Analysis" wrap
        assert "### Detailed Analysis" not in md
        assert "## Summary" in md
        assert "This PR refactors retry." in md
        # Raw synthesis should NOT leak through
        assert "Raw output." not in md

    def test_empty_override_uses_it_not_synthesis(self):
        """Explicit empty string suppresses the synthesis section entirely."""
        from app.integrations.azure_devops.formatter import format_summary_markdown

        md = format_summary_markdown(
            self._result(synthesis="Raw output."),
            overall_summary_override="",
        )
        assert "### Detailed Analysis" not in md
        assert "Raw output." not in md
