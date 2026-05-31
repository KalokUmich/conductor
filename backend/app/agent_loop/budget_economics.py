"""BudgetEconomics — pre-flight USD budget consultant for the Brain.

Before the Brain orchestrates a task, it can consult this service to get a
*budget plan*: a loose USD ceiling for the whole task and per-leaf caps for the
sub-agents it will dispatch. The plan is enforced downstream via
``ClaudeAgentOptions.max_budget_usd`` (SDK leaves) and ``BudgetConfig.max_usd``
(in-house coordinators).

Design principles (from the project owner):

* **Caps are loose safety circuit-breakers, not targets.** Real spend is
  single-digit dollars — a heavy PR review costs ~$1, one leaf ~$0.05–0.50. The
  caps exist so a runaway loop can't cost hundreds; a normal worker must finish
  far under its cap. (A live sentry_007 run cost $1.07 across 9 leaves.)
* **Hard anchor: a 2000-line PR gets a $50 total ceiling.** PRs above ~2200 lines
  aren't reviewed at all, so $50 is the top of the band.
* **Query classes, not just PR.** ``pr`` (size-driven) and ``business``
  (domain/business-logic understanding — multi-agent, deserves cost refinement)
  always merit a plan; ``generic`` code Q&A takes a cheap default and only
  consults when the handling Brain judges it will fan out.
* **Self-optimizing.** ``estimate`` accepts an optional ``history`` provider that
  returns measured cost percentiles per (query_class, agent); when present the
  deterministic policy is blended toward the data (Phase 3). Until then it is
  pure policy — identical behaviour when history is empty.

This module is dependency-light and fully unit-testable offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# --- Policy constants (USD) -------------------------------------------------

#: Total ceiling for a PR at/above the anchor size. PRs >~2200 lines aren't
#: reviewed, so this is the top of the band.
PR_CEILING_USD = 50.0
#: Floor so even a tiny PR gets workable headroom.
PR_FLOOR_USD = 5.0
#: Line count at which a PR hits the full ceiling.
PR_ANCHOR_LINES = 2000

#: Loose totals for non-PR multi-agent work.
BUSINESS_TOTAL_USD = 5.0
GENERIC_TOTAL_USD = 3.0

#: Cheap single-shot task sub-types (no fan-out) → tight, near-zero ceilings.
SUBTYPE_CAPS_USD: Dict[str, float] = {
    "jira_triage": 0.10,
    "summary": 0.05,
    "splitter": 0.20,
}

#: Per-leaf circuit-breaker bounds. The ceiling mirrors brain._SDK_LEAF_MAX_USD.
PER_LEAF_MAX_CEILING_USD = 8.0
PER_LEAF_MIN_USD = 0.25

VALID_QUERY_CLASSES = ("pr", "business", "generic")

#: Weight given to measured history (p80) vs deterministic policy when both
#: exist (Phase 3 self-optimization).
_HISTORY_BLEND = 0.5


# --- Data shapes ------------------------------------------------------------


@dataclass(frozen=True)
class TaskSignals:
    """Inputs the consultant reasons about. All optional — sensible fallbacks."""

    diff_lines: Optional[int] = None  # PR size
    expected_leaves: Optional[int] = None  # fan-out hint from the coordinator
    sub_type: Optional[str] = None  # 'jira_triage' | 'summary' | 'splitter'
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BudgetPlan:
    """A pre-flight budget plan. All caps are loose ceilings, not targets."""

    query_class: str
    total_cap_usd: float
    per_leaf_default_usd: float
    per_leaf_max_usd: float
    expected_leaves: int
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_class": self.query_class,
            "total_cap_usd": round(self.total_cap_usd, 4),
            "per_leaf_default_usd": round(self.per_leaf_default_usd, 4),
            "per_leaf_max_usd": round(self.per_leaf_max_usd, 4),
            "expected_leaves": self.expected_leaves,
            "rationale": self.rationale,
        }


#: A history provider returns the measured p80 cost (USD) for a
#: (query_class, agent_name) pair, or None when there's no data yet.
HistoryProvider = Callable[[str, Optional[str]], Optional[float]]


# --- Service ----------------------------------------------------------------


class BudgetEconomics:
    """Pre-flight budget estimator. Stateless apart from an optional history hook."""

    def __init__(self, history: Optional[HistoryProvider] = None):
        self._history = history

    # -- public API ----------------------------------------------------------

    def estimate(self, query_class: str, signals: Optional[TaskSignals] = None) -> BudgetPlan:
        """Return a :class:`BudgetPlan` for the given query class + signals."""
        qc = query_class if query_class in VALID_QUERY_CLASSES else "generic"
        sig = signals or TaskSignals()

        if qc == "pr":
            total, leaves, why = self._estimate_pr(sig)
        elif qc == "business":
            total, leaves, why = self._estimate_business(sig)
        else:
            total, leaves, why = self._estimate_generic(sig)

        leaves = max(1, leaves)
        per_leaf_default = total / leaves
        per_leaf_max = min(PER_LEAF_MAX_CEILING_USD, max(PER_LEAF_MIN_USD, total * 0.5))

        # Phase 3: blend toward measured p80 when history exists.
        if self._history is not None:
            measured = self._history(qc, sig.sub_type)
            if measured is not None and measured > 0:
                blended = (1 - _HISTORY_BLEND) * per_leaf_default + _HISTORY_BLEND * measured
                why += f"; blended per-leaf with measured p80 ${measured:.4f}"
                per_leaf_default = blended

        plan = BudgetPlan(
            query_class=qc,
            total_cap_usd=total,
            per_leaf_default_usd=per_leaf_default,
            per_leaf_max_usd=per_leaf_max,
            expected_leaves=leaves,
            rationale=why,
        )
        logger.info("[budget_economics] %s", plan.to_dict())
        return plan

    @staticmethod
    def classify(*, is_pr: bool = False, is_domain: bool = False) -> str:
        """Light query classifier. PR by construction; Domain handoff ⇒ business."""
        if is_pr:
            return "pr"
        if is_domain:
            return "business"
        return "generic"

    # -- per-class policy ----------------------------------------------------

    def _estimate_pr(self, sig: TaskSignals):
        lines = sig.diff_lines or 0
        total = min(PR_CEILING_USD, max(PR_FLOOR_USD, lines / PR_ANCHOR_LINES * PR_CEILING_USD))
        leaves = sig.expected_leaves or self._pr_leaves(lines)
        why = (
            f"PR {lines} lines → total ${total:.2f} "
            f"(linear to ${PR_CEILING_USD:.0f} ceiling at {PR_ANCHOR_LINES} lines, "
            f"${PR_FLOOR_USD:.0f} floor); ~{leaves} leaves"
        )
        return total, leaves, why

    def _estimate_business(self, sig: TaskSignals):
        leaves = sig.expected_leaves or 4
        why = f"business/domain task → loose ${BUSINESS_TOTAL_USD:.2f} total across ~{leaves} leaves"
        return BUSINESS_TOTAL_USD, leaves, why

    def _estimate_generic(self, sig: TaskSignals):
        st = sig.sub_type
        if st in SUBTYPE_CAPS_USD:
            cap = SUBTYPE_CAPS_USD[st]
            return cap, 1, f"single-shot '{st}' → tight ${cap:.2f} cap"
        leaves = sig.expected_leaves or 2
        why = f"generic task → ${GENERIC_TOTAL_USD:.2f} total across ~{leaves} leaves"
        return GENERIC_TOTAL_USD, leaves, why

    @staticmethod
    def _pr_leaves(lines: int) -> int:
        """Rough leaf count from PR size: more diff → more parallel workers."""
        return max(3, min(12, lines // 400))


#: Process-wide default instance (no history wired yet — pure policy).
_default = BudgetEconomics()


def get_budget_economics() -> BudgetEconomics:
    return _default
