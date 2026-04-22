"""Unit tests for PR splitter — LLM-backed split plan for oversized PRs.

Covers:
- Empty diff returns None
- LLM exception is swallowed (returns None)
- Empty LLM output returns None
- Fenced output is unwrapped
- Prompt construction includes title / description / diff stats
- Diff is truncated at budget_chars
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.code_review.splitter import generate_pr_split_plan


def _make_provider(text_out: str = "## Suggested split\n\nTest body.", raises=None):
    """Build a fake AIProvider whose call_model returns canned text."""
    provider = MagicMock()
    if raises is not None:
        provider.call_model.side_effect = raises
    else:
        provider.call_model.return_value = text_out
    return provider


class TestGeneratePRSplitPlan:
    @pytest.mark.asyncio
    async def test_empty_diff_returns_none(self):
        provider = _make_provider()
        out = await generate_pr_split_plan(
            diff_text="",
            pr_title="t",
            pr_description="d",
            total_lines=0,
            file_count=0,
            provider=provider,
        )
        assert out is None
        provider.call_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_returns_output(self):
        provider = _make_provider(
            "## Suggested split\n\nThe PR changes 3000 lines...\n\n### Chunk 1 — schema",
        )
        out = await generate_pr_split_plan(
            diff_text="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
            pr_title="Refactor auth",
            pr_description="Big cleanup",
            total_lines=3000,
            file_count=42,
            provider=provider,
        )
        assert out is not None
        assert out.startswith("## Suggested split")

    @pytest.mark.asyncio
    async def test_llm_exception_returns_none(self):
        provider = _make_provider(raises=RuntimeError("throttled"))
        out = await generate_pr_split_plan(
            diff_text="diff content",
            pr_title="t",
            pr_description="d",
            total_lines=3000,
            file_count=10,
            provider=provider,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_empty_llm_output_returns_none(self):
        provider = _make_provider(text_out="")
        out = await generate_pr_split_plan(
            diff_text="diff content",
            pr_title="t",
            pr_description="d",
            total_lines=3000,
            file_count=10,
            provider=provider,
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_fenced_output_is_unwrapped(self):
        fenced = "```markdown\n## Suggested split\n\nbody\n```"
        provider = _make_provider(text_out=fenced)
        out = await generate_pr_split_plan(
            diff_text="x" * 10,
            pr_title="t",
            pr_description="d",
            total_lines=3000,
            file_count=10,
            provider=provider,
        )
        assert out is not None
        assert not out.startswith("```")
        assert out.startswith("## Suggested split")

    @pytest.mark.asyncio
    async def test_prompt_includes_title_description_stats(self):
        """Verify the user-message carries pr_title / description / stats."""
        provider = _make_provider()
        await generate_pr_split_plan(
            diff_text="diff content",
            pr_title="Unique-Title-12345",
            pr_description="Unique-Description-67890",
            total_lines=1234,
            file_count=42,
            provider=provider,
        )
        called_args = provider.call_model.call_args
        # Positional: (user_message, max_tokens, system_prompt)
        user_msg = called_args.args[0]
        assert "Unique-Title-12345" in user_msg
        assert "Unique-Description-67890" in user_msg
        assert "1234" in user_msg  # total lines
        assert "42" in user_msg   # file count

    @pytest.mark.asyncio
    async def test_diff_truncated_at_budget(self):
        """Diff >40K chars should be truncated with a marker."""
        provider = _make_provider()
        big_diff = "a" * 50_000
        await generate_pr_split_plan(
            diff_text=big_diff,
            pr_title="t",
            pr_description="d",
            total_lines=5000,
            file_count=100,
            provider=provider,
        )
        user_msg = provider.call_model.call_args.args[0]
        assert "diff truncated" in user_msg
        # The diff excerpt is budget-bounded, not the full 50K
        assert user_msg.count("a") < 50_000
