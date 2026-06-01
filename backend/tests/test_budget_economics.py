"""Tests for the BudgetEconomics pre-flight consultant."""

from __future__ import annotations

from app.agent_loop.budget_economics import (
    PER_LEAF_MAX_CEILING_USD,
    PR_CEILING_USD,
    BudgetEconomics,
    TaskSignals,
    get_budget_economics,
)


def _be() -> BudgetEconomics:
    return BudgetEconomics()


# --- PR sizing (hard anchors) ----------------------------------------------


class TestPrAnchors:
    def test_2000_line_pr_hits_50_ceiling(self):
        plan = _be().estimate("pr", TaskSignals(diff_lines=2000))
        assert plan.total_cap_usd == PR_CEILING_USD  # exactly $50 at the anchor

    def test_large_pr_capped_at_50(self):
        plan = _be().estimate("pr", TaskSignals(diff_lines=10_000))
        assert plan.total_cap_usd == PR_CEILING_USD  # never exceeds the ceiling

    def test_1000_line_pr_about_half(self):
        plan = _be().estimate("pr", TaskSignals(diff_lines=1000))
        assert abs(plan.total_cap_usd - 25.0) < 0.01

    def test_tiny_pr_gets_floor(self):
        plan = _be().estimate("pr", TaskSignals(diff_lines=50))
        assert plan.total_cap_usd == 5.0  # floor, not 50/2000*50 ≈ $1.25

    def test_pr_leaf_count_scales_with_size(self):
        small = _be().estimate("pr", TaskSignals(diff_lines=400))
        big = _be().estimate("pr", TaskSignals(diff_lines=4000))
        assert big.expected_leaves >= small.expected_leaves


# --- per-leaf caps are loose, never hard-stopping ---------------------------


class TestPerLeafCaps:
    def test_per_leaf_max_never_exceeds_ceiling(self):
        for lines in (50, 1000, 2000, 50_000):
            plan = _be().estimate("pr", TaskSignals(diff_lines=lines))
            assert plan.per_leaf_max_usd <= PER_LEAF_MAX_CEILING_USD

    def test_per_leaf_default_is_total_over_leaves(self):
        plan = _be().estimate("pr", TaskSignals(diff_lines=2000, expected_leaves=10))
        assert abs(plan.per_leaf_default_usd - PR_CEILING_USD / 10) < 0.01

    def test_per_leaf_max_far_above_typical_spend(self):
        # A real leaf spends ~$0.05-0.50; the cap must dwarf that so it never
        # hard-stops a normal worker.
        plan = _be().estimate("business")
        assert plan.per_leaf_max_usd >= 1.0


# --- business / generic / sub-types ----------------------------------------


class TestOtherClasses:
    def test_business_is_loose_default(self):
        plan = _be().estimate("business")
        assert plan.total_cap_usd == 5.0
        assert plan.expected_leaves >= 2

    def test_generic_default(self):
        plan = _be().estimate("generic")
        assert plan.total_cap_usd == 3.0

    def test_jira_triage_is_cheap(self):
        plan = _be().estimate("generic", TaskSignals(sub_type="jira_triage"))
        assert plan.total_cap_usd == 0.10
        assert plan.expected_leaves == 1

    def test_summary_is_cheapest(self):
        plan = _be().estimate("generic", TaskSignals(sub_type="summary"))
        assert plan.total_cap_usd == 0.05

    def test_unknown_class_falls_back_to_generic(self):
        plan = _be().estimate("nonsense")
        assert plan.query_class == "generic"
        assert plan.total_cap_usd == 3.0


# --- classification helper --------------------------------------------------


class TestClassify:
    def test_pr_by_construction(self):
        assert BudgetEconomics.classify(is_pr=True) == "pr"

    def test_domain_handoff_is_business(self):
        assert BudgetEconomics.classify(is_domain=True) == "business"

    def test_default_generic(self):
        assert BudgetEconomics.classify() == "generic"


# --- Phase 3 history blend --------------------------------------------------


class TestHistoryBlend:
    def test_no_history_is_pure_policy(self):
        plan = _be().estimate("business")
        assert "blended" not in plan.rationale

    def test_history_blends_per_leaf(self):
        # Measured p80 far above policy → blended default rises.
        be = BudgetEconomics(history=lambda qc, st: 99.0)
        base = _be().estimate("business").per_leaf_default_usd
        blended = be.estimate("business").per_leaf_default_usd
        assert blended > base
        assert "blended" in be.estimate("business").rationale

    def test_empty_history_returns_none_no_blend(self):
        be = BudgetEconomics(history=lambda qc, st: None)
        plan = be.estimate("business")
        assert "blended" not in plan.rationale


def test_singleton_accessor():
    assert isinstance(get_budget_economics(), BudgetEconomics)
