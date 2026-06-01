"""Tests for the token→USD pricing table (USD budget economy, Phase 1a)."""

import pytest

from app.ai_provider import pricing


def test_tier_resolution_by_substring():
    assert pricing._resolve_tier("eu.anthropic.claude-haiku-4-5-20251001-v1:0") == "haiku"
    assert pricing._resolve_tier("eu.anthropic.claude-sonnet-4-6") == "sonnet"
    assert pricing._resolve_tier("eu.anthropic.claude-opus-4-8") == "opus"


def test_unknown_model_falls_back_to_sonnet():
    assert pricing._resolve_tier("some-unknown-model") == "sonnet"


def test_region_surcharge_applied():
    p = pricing.price_for("eu.anthropic.claude-sonnet-4-6")
    # base $3 input × 1.10 EU surcharge
    assert p.input_per_1m == pytest.approx(3.0 * 1.10)
    assert p.output_per_1m == pytest.approx(15.0 * 1.10)


def test_cost_usd_basic_sonnet():
    # 1M input + 1M output on sonnet, EU = (3 + 15) × 1.10 = $19.80
    c = pricing.cost_usd(
        "eu.anthropic.claude-sonnet-4-6",
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    )
    assert c == pytest.approx(19.80, abs=1e-4)


def test_cost_usd_cache_read_is_cheap():
    # 1M cache-read tokens on sonnet = 3 × 1.10 × 0.1 = $0.33 (vs $3.30 full input)
    c = pricing.cost_usd(
        "eu.anthropic.claude-sonnet-4-6",
        {"cache_read_input_tokens": 1_000_000},
    )
    assert c == pytest.approx(0.33, abs=1e-4)


def test_cost_usd_haiku_cheaper_than_sonnet():
    usage = {"input_tokens": 500_000, "output_tokens": 100_000}
    haiku = pricing.cost_usd("eu.anthropic.claude-haiku-4-5-20251001-v1:0", usage)
    sonnet = pricing.cost_usd("eu.anthropic.claude-sonnet-4-6", usage)
    assert haiku < sonnet


def test_cost_usd_empty_usage_is_zero():
    assert pricing.cost_usd("eu.anthropic.claude-sonnet-4-6", None) == 0.0
    assert pricing.cost_usd("eu.anthropic.claude-sonnet-4-6", {}) == 0.0


def test_budget_summary_prefers_sdk_actual_cost():
    # When the SDK gives total_cost_usd, use it verbatim (ignore token compute).
    c = pricing.cost_from_budget_summary(
        "eu.anthropic.claude-sonnet-4-6",
        {"total_cost_usd": 0.0517, "total_input_tokens": 999_999_999},
    )
    assert c == pytest.approx(0.0517)


def test_budget_summary_computes_when_no_actual():
    c = pricing.cost_from_budget_summary(
        "eu.anthropic.claude-sonnet-4-6",
        {"total_input_tokens": 1_000_000, "total_output_tokens": 0},
    )
    assert c == pytest.approx(3.0 * 1.10, abs=1e-4)
