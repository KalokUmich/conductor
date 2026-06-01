"""Tests for the USD-based budget controller (USD budget economy)."""

from __future__ import annotations

from app.agent_loop.budget import (
    BudgetConfig,
    BudgetController,
    BudgetSignal,
    IterationMetrics,
)

_MODEL = "eu.anthropic.claude-sonnet-4-6"  # EU sonnet: $3/$15 ×1.10

# Helper: 100K input on EU sonnet ≈ 100000 * 3 * 1.10 / 1e6 = $0.33
# 1M input ≈ $3.30.


def _ctl(**cfg) -> BudgetController:
    return BudgetController(BudgetConfig(**cfg), model=_MODEL)


# ---------------------------------------------------------------------------
# BudgetSignal tests (USD-gated)
# ---------------------------------------------------------------------------


class TestBudgetSignalNormal:
    def test_fresh_controller_returns_normal(self):
        assert _ctl(max_usd=5.0).get_signal() == BudgetSignal.NORMAL

    def test_low_usage_returns_normal(self):
        bc = _ctl(max_usd=5.0)
        bc.track(IterationMetrics(input_tokens=10_000, output_tokens=500))
        assert bc.get_signal() == BudgetSignal.NORMAL

    def test_multiple_iterations_under_threshold_normal(self):
        bc = _ctl(max_usd=5.0)
        for _ in range(5):
            bc.track(
                IterationMetrics(
                    input_tokens=20_000,
                    output_tokens=1_000,
                    new_files_accessed=1,
                    new_symbols_found=1,
                )
            )
        # ~$0.36 of $5 = 7%
        assert bc.get_signal() == BudgetSignal.NORMAL


class TestBudgetSignalWarnConverge:
    def test_warning_at_threshold(self):
        # max_usd $0.33 with 0.7 warn → 100K input ($0.33) is 100% > 70%.
        bc = _ctl(max_usd=0.40, warning_threshold=0.7)
        bc.track(IterationMetrics(input_tokens=100_000, output_tokens=0))
        assert bc.get_signal() == BudgetSignal.WARN_CONVERGE

    def test_diminishing_returns_triggers_warning(self):
        bc = _ctl(max_usd=100.0, diminishing_returns_window=3)
        for _ in range(3):
            bc.track(IterationMetrics(input_tokens=10_000, output_tokens=500))
        assert bc.get_signal() == BudgetSignal.WARN_CONVERGE

    def test_diminishing_returns_not_triggered_with_new_files(self):
        bc = _ctl(max_usd=100.0, diminishing_returns_window=3)
        for _ in range(3):
            bc.track(IterationMetrics(input_tokens=10_000, output_tokens=500, new_files_accessed=1))
        assert bc.get_signal() == BudgetSignal.NORMAL

    def test_diminishing_returns_not_triggered_below_window(self):
        bc = _ctl(max_usd=100.0, diminishing_returns_window=3)
        for _ in range(2):
            bc.track(IterationMetrics(input_tokens=10_000, output_tokens=500))
        assert bc.get_signal() == BudgetSignal.NORMAL


class TestBudgetSignalForceConclude:
    def test_critical_threshold_forces_conclude(self):
        # $0.33 input vs $0.35 cap → 94% > 90% critical
        bc = _ctl(max_usd=0.35, critical_threshold=0.9)
        bc.track(IterationMetrics(input_tokens=100_000, output_tokens=0))
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE

    def test_max_iterations_forces_conclude(self):
        bc = _ctl(max_usd=100.0, max_iterations=5)
        for _ in range(5):
            bc.track(IterationMetrics(input_tokens=1_000, output_tokens=100, new_files_accessed=1))
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE

    def test_iteration_limit_checked_before_usd_ratio(self):
        bc = _ctl(max_usd=100.0, max_iterations=3)
        for _ in range(3):
            bc.track(IterationMetrics(input_tokens=100, output_tokens=50, new_files_accessed=1))
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE


# ---------------------------------------------------------------------------
# Tracking tests
# ---------------------------------------------------------------------------


class TestTracking:
    def test_cumulative_tokens_and_usd(self):
        bc = _ctl(max_usd=5.0)
        bc.track(IterationMetrics(input_tokens=10_000, output_tokens=500))
        bc.track(IterationMetrics(input_tokens=20_000, output_tokens=1_000))
        assert bc.cumulative_input == 30_000
        assert bc.cumulative_output == 1_500
        assert bc.total_tokens == 31_500
        assert bc.cumulative_usd > 0  # priced, non-zero

    def test_iteration_count(self):
        bc = _ctl(max_usd=5.0)
        bc.track(IterationMetrics(input_tokens=100))
        bc.track(IterationMetrics(input_tokens=100))
        assert bc.iteration_count == 2

    def test_cache_read_is_cheaper_than_plain_input(self):
        cached = _ctl(max_usd=100.0)
        cached.track(IterationMetrics(input_tokens=0, cache_read_tokens=1_000_000))
        plain = _ctl(max_usd=100.0)
        plain.track(IterationMetrics(input_tokens=1_000_000))
        assert cached.cumulative_usd < plain.cumulative_usd

    def test_track_file_dedup(self):
        bc = _ctl(max_usd=5.0)
        assert bc.track_file("app/service.py") == 1
        assert bc.track_file("app/service.py") == 0

    def test_track_symbol_dedup(self):
        bc = _ctl(max_usd=5.0)
        assert bc.track_symbol("PaymentService") == 1
        assert bc.track_symbol("PaymentService") == 0


# ---------------------------------------------------------------------------
# Budget context & summary tests
# ---------------------------------------------------------------------------


class TestBudgetContext:
    def test_budget_context_shows_usd(self):
        bc = _ctl(max_usd=5.0, max_iterations=25)
        bc.track(IterationMetrics(input_tokens=100_000, output_tokens=5_000))
        ctx = bc.budget_context
        assert "$" in ctx
        assert "5.00" in ctx  # max
        assert "Iteration 1/25" in ctx

    def test_summary_dict_has_usd_and_tokens(self):
        bc = _ctl(max_usd=5.0)
        bc.track(IterationMetrics(input_tokens=50_000, output_tokens=2_000))
        s = bc.summary()
        assert s["total_input_tokens"] == 50_000
        assert s["total_output_tokens"] == 2_000
        assert s["total_tokens"] == 52_000
        assert s["total_cost_usd"] > 0
        assert "usd_usage_ratio" in s


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_max_usd_returns_full_ratio(self):
        bc = _ctl(max_usd=0.0)
        assert bc.usd_usage_ratio == 1.0
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE

    def test_default_config_values(self):
        cfg = BudgetConfig()
        assert cfg.max_usd == 5.0
        assert cfg.warning_threshold == 0.6
        assert cfg.critical_threshold == 0.9
        assert cfg.max_iterations == 50
        assert cfg.diminishing_returns_window == 3
