"""Tests for the token-based budget controller."""
from __future__ import annotations

import pytest

from app.agent_loop.budget import (
    BudgetConfig,
    BudgetController,
    BudgetSignal,
    IterationMetrics,
)


# ---------------------------------------------------------------------------
# BudgetSignal tests
# ---------------------------------------------------------------------------


class TestBudgetSignalNormal:
    def test_fresh_controller_returns_normal(self):
        bc = BudgetController(BudgetConfig(max_input_tokens=500_000))
        assert bc.get_signal() == BudgetSignal.NORMAL

    def test_low_usage_returns_normal(self):
        bc = BudgetController(BudgetConfig(max_input_tokens=500_000))
        bc.track(IterationMetrics(input_tokens=10_000, output_tokens=500))
        assert bc.get_signal() == BudgetSignal.NORMAL

    def test_multiple_iterations_under_threshold_normal(self):
        bc = BudgetController(BudgetConfig(max_input_tokens=500_000))
        for _ in range(5):
            bc.track(IterationMetrics(
                input_tokens=20_000, output_tokens=1_000,
                new_files_accessed=1, new_symbols_found=1,
            ))
        # 100K / 500K = 20%
        assert bc.get_signal() == BudgetSignal.NORMAL


class TestBudgetSignalWarnConverge:
    def test_warning_at_threshold(self):
        bc = BudgetController(BudgetConfig(
            max_input_tokens=100_000,
            warning_threshold=0.7,
        ))
        bc.track(IterationMetrics(input_tokens=75_000, output_tokens=1_000))
        assert bc.get_signal() == BudgetSignal.WARN_CONVERGE

    def test_diminishing_returns_triggers_warning(self):
        bc = BudgetController(BudgetConfig(
            max_input_tokens=1_000_000,
            diminishing_returns_window=3,
        ))
        # 3 iterations with no new files or symbols
        for _ in range(3):
            bc.track(IterationMetrics(
                input_tokens=10_000, output_tokens=500,
                new_files_accessed=0, new_symbols_found=0,
            ))
        assert bc.get_signal() == BudgetSignal.WARN_CONVERGE

    def test_diminishing_returns_not_triggered_with_new_files(self):
        bc = BudgetController(BudgetConfig(
            max_input_tokens=1_000_000,
            diminishing_returns_window=3,
        ))
        for _ in range(3):
            bc.track(IterationMetrics(
                input_tokens=10_000, output_tokens=500,
                new_files_accessed=1, new_symbols_found=0,
            ))
        assert bc.get_signal() == BudgetSignal.NORMAL

    def test_diminishing_returns_not_triggered_below_window(self):
        bc = BudgetController(BudgetConfig(
            max_input_tokens=1_000_000,
            diminishing_returns_window=3,
        ))
        # Only 2 iterations — below window
        for _ in range(2):
            bc.track(IterationMetrics(
                input_tokens=10_000, output_tokens=500,
                new_files_accessed=0, new_symbols_found=0,
            ))
        assert bc.get_signal() == BudgetSignal.NORMAL


class TestBudgetSignalForceConclude:
    def test_critical_threshold_forces_conclude(self):
        bc = BudgetController(BudgetConfig(
            max_input_tokens=100_000,
            critical_threshold=0.9,
        ))
        bc.track(IterationMetrics(input_tokens=95_000, output_tokens=1_000))
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE

    def test_max_iterations_forces_conclude(self):
        bc = BudgetController(BudgetConfig(
            max_input_tokens=1_000_000,
            max_iterations=5,
        ))
        for _ in range(5):
            bc.track(IterationMetrics(
                input_tokens=1_000, output_tokens=100,
                new_files_accessed=1,
            ))
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE

    def test_iteration_limit_checked_before_token_ratio(self):
        """Even with low token usage, hitting max_iterations forces conclude."""
        bc = BudgetController(BudgetConfig(
            max_input_tokens=1_000_000,
            max_iterations=3,
        ))
        for _ in range(3):
            bc.track(IterationMetrics(
                input_tokens=100, output_tokens=50,
                new_files_accessed=1,
            ))
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE


# ---------------------------------------------------------------------------
# Tracking tests
# ---------------------------------------------------------------------------


class TestTracking:
    def test_cumulative_tokens(self):
        bc = BudgetController()
        bc.track(IterationMetrics(input_tokens=10_000, output_tokens=500))
        bc.track(IterationMetrics(input_tokens=20_000, output_tokens=1_000))
        assert bc.cumulative_input == 30_000
        assert bc.cumulative_output == 1_500
        assert bc.total_tokens == 31_500

    def test_iteration_count(self):
        bc = BudgetController()
        bc.track(IterationMetrics(input_tokens=100))
        bc.track(IterationMetrics(input_tokens=100))
        assert bc.iteration_count == 2

    def test_track_file_new(self):
        bc = BudgetController()
        assert bc.track_file("app/service.py") == 1
        assert len(bc.files_accessed) == 1

    def test_track_file_duplicate(self):
        bc = BudgetController()
        bc.track_file("app/service.py")
        assert bc.track_file("app/service.py") == 0
        assert len(bc.files_accessed) == 1

    def test_track_symbol_new(self):
        bc = BudgetController()
        assert bc.track_symbol("PaymentService") == 1
        assert len(bc.symbols_resolved) == 1

    def test_track_symbol_duplicate(self):
        bc = BudgetController()
        bc.track_symbol("PaymentService")
        assert bc.track_symbol("PaymentService") == 0
        assert len(bc.symbols_resolved) == 1


# ---------------------------------------------------------------------------
# Budget context & summary tests
# ---------------------------------------------------------------------------


class TestBudgetContext:
    def test_budget_context_format(self):
        bc = BudgetController(BudgetConfig(max_input_tokens=500_000, max_iterations=25))
        bc.track(IterationMetrics(input_tokens=100_000, output_tokens=5_000))
        bc.track_file("app/a.py")
        bc.track_symbol("Foo")
        ctx = bc.budget_context
        assert "100,000" in ctx
        assert "500,000" in ctx
        assert "20%" in ctx
        assert "Files: 1" in ctx
        assert "Symbols: 1" in ctx

    def test_summary_dict(self):
        bc = BudgetController(BudgetConfig(max_input_tokens=100_000))
        bc.track(IterationMetrics(input_tokens=50_000, output_tokens=2_000))
        bc.track_file("x.py")
        s = bc.summary()
        assert s["total_input_tokens"] == 50_000
        assert s["total_output_tokens"] == 2_000
        assert s["total_tokens"] == 52_000
        assert s["iterations"] == 1
        assert s["input_usage_ratio"] == 0.5
        assert s["files_accessed"] == 1
        assert s["symbols_resolved"] == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_max_tokens_returns_full_ratio(self):
        bc = BudgetController(BudgetConfig(max_input_tokens=0))
        assert bc.input_usage_ratio == 1.0
        assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE

    def test_default_config_values(self):
        cfg = BudgetConfig()
        assert cfg.max_input_tokens == 880_000
        assert cfg.warning_threshold == 0.7
        assert cfg.critical_threshold == 0.9
        assert cfg.max_iterations == 40
        assert cfg.diminishing_returns_window == 3
