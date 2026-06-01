"""Tests for the BudgetAnalyzer self-optimization layer (P3)."""

from __future__ import annotations

from app.agent_loop.budget_analyzer import (
    BudgetAnalyzer,
    _percentile,
    compute_stats,
)
from app.agent_loop.budget_economics import (
    BudgetEconomics,
    TaskSignals,
    get_budget_economics,
)


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 0.8) == 0.0

    def test_single(self):
        assert _percentile([0.5], 0.8) == 0.5

    def test_median_odd(self):
        assert _percentile([1, 2, 3], 0.5) == 2.0

    def test_p80_interpolates(self):
        # 0..10 step 1: p80 ≈ 8.0
        assert abs(_percentile(list(range(11)), 0.8) - 8.0) < 1e-9

    def test_monotone(self):
        vals = sorted([0.1, 0.2, 0.3, 0.4, 0.9])
        assert _percentile(vals, 0.5) <= _percentile(vals, 0.8) <= _percentile(vals, 0.95)


class TestComputeStats:
    def test_drops_under_min_samples(self):
        # 4 rows < _MIN_SAMPLES (5) → dropped
        stats = compute_stats({"explore_usage": [0.1, 0.2, 0.3, 0.4]})
        assert stats == {}

    def test_keeps_with_enough_samples(self):
        stats = compute_stats({"correctness": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]})
        assert "correctness" in stats
        s = stats["correctness"]
        assert s.samples == 6
        assert s.p50 <= s.p80 <= s.p95

    def test_filters_zero_and_negative(self):
        # zeros (in-house no-cost rows) must not drag the percentile to 0
        stats = compute_stats({"a": [0.0, 0.0, 0.5, 0.5, 0.5, 0.5, 0.6]})
        # 5 positive values remain → kept, p50 ~0.5 not ~0
        assert "a" in stats
        assert stats["a"].p50 >= 0.5

    def test_filtered_below_threshold_dropped(self):
        # only 3 positive after filtering → dropped
        stats = compute_stats({"a": [0.0, 0.0, 0.0, 0.0, 0.1, 0.2, 0.3]})
        assert stats == {}

    def test_to_dict(self):
        stats = compute_stats({"sec": [0.1, 0.2, 0.3, 0.4, 0.5]})
        d = stats["sec"].to_dict()
        assert d["agent_name"] == "sec"
        assert "p80" in d and d["samples"] == 5


class TestHistoryProvider:
    def test_unknown_agent_returns_none(self):
        a = BudgetAnalyzer()
        prov = a.history_provider()
        assert prov("pr", "never_seen") is None

    def test_none_agent_returns_none(self):
        a = BudgetAnalyzer()
        assert a.history_provider()("pr", None) is None

    def test_known_agent_returns_p80(self):
        a = BudgetAnalyzer()
        a._stats = compute_stats({"correctness": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]})
        prov = a.history_provider()
        assert prov("pr", "correctness") == a._stats["correctness"].p80

    def test_feeds_budget_economics_blend(self):
        # End-to-end: analyzer history shifts the estimate's per-leaf default.
        a = BudgetAnalyzer()
        a._stats = compute_stats({"x": [9.0, 9.0, 9.0, 9.0, 9.0, 9.0]})  # high measured
        econ_policy = BudgetEconomics()
        econ_hist = BudgetEconomics(history=a.history_provider())
        base = econ_policy.estimate("business", TaskSignals(sub_type="x")).per_leaf_default_usd
        blended = econ_hist.estimate("business", TaskSignals(sub_type="x")).per_leaf_default_usd
        # measured p80 (=9) >> policy → blended default rises
        assert blended > base


class TestInstallSelfOptimization:
    def test_install_idempotent_and_wires_history(self):
        import app.agent_loop.budget_analyzer as ba
        from app.agent_loop.lifecycle import _HOOKS, unregister_hook

        ba._analyzer = None  # clean slate
        try:
            a1 = ba.install_self_optimization()
            a2 = ba.install_self_optimization()
            assert a1 is a2  # idempotent — same analyzer instance
            # callback registered exactly once despite two install calls
            assert _HOOKS["on_task_end"].count(ba._on_task_end_refresh) == 1
            # singleton history now points at the analyzer
            assert get_budget_economics()._history is not None
        finally:
            unregister_hook("on_task_end", ba._on_task_end_refresh)
            ba._analyzer = None
            get_budget_economics().set_history(None)

    def test_on_task_end_refresh_noop_without_analyzer_or_loop(self):
        import app.agent_loop.budget_analyzer as ba

        ba._analyzer = None
        # no analyzer + no running loop → must be a silent no-op (no raise)
        ba._on_task_end_refresh(object())
