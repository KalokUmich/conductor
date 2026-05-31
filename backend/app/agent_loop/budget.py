"""USD-based budget controller for the agent loop.

The budget economy is denominated in **USD** (not tokens). Checked after each
LLM call to signal convergence or forced conclusion. A USD gate is the unit we
actually care about and the only cumulative-spend lever the Claude Agent SDK can
enforce on the leaf path (``max_budget_usd``); a token gate is both
SDK-unenforceable and price-fragile.

Token counts are still tracked (for telemetry + the cost computation), but the
*gate* is dollars. ``max_iterations`` remains an independent hard cap.

Reference: "How Do Coding Agents Spend Your Money?" (ICLR 2026)
https://openreview.net/forum?id=1bUeVB3fov
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from app.ai_provider import pricing


class BudgetSignal(Enum):
    NORMAL = "normal"
    WARN_CONVERGE = "warn_converge"
    FORCE_CONCLUDE = "force_conclude"


@dataclass
class BudgetConfig:
    max_usd: float = 5.0  # Total USD budget per session (loose safety ceiling)
    warning_threshold: float = 0.6  # 60% — inject warning into prompt (gives ~30% buffer to converge before FORCE_CONCLUDE)
    critical_threshold: float = 0.9  # 90% — force conclusion (reserves ~10% headroom for the wrap-up LLM call)
    max_iterations: int = 50  # Hard iteration cap (independent of the USD gate)
    diminishing_returns_window: int = 3  # N iterations with no new info


@dataclass
class IterationMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    tool_names: List[str] = field(default_factory=list)
    new_files_accessed: int = 0
    new_symbols_found: int = 0


class BudgetController:
    """USD-aware budget controller embedded in AgentLoopService.

    Tracks cumulative USD spend per session (priced from per-iteration token
    counts via :mod:`app.ai_provider.pricing`), detects diminishing returns, and
    emits signals that the agent loop uses to inject convergence guidance or
    force conclusion. Token totals are retained for telemetry.
    """

    def __init__(self, config: Optional[BudgetConfig] = None, model: str = "") -> None:
        self.config = config or BudgetConfig()
        self.model = model
        self.cumulative_input = 0
        self.cumulative_output = 0
        self.cumulative_usd = 0.0
        self.iteration_count = 0
        self.iteration_history: List[IterationMetrics] = []
        self.files_accessed: set = set()
        self.symbols_resolved: set = set()

    @property
    def total_tokens(self) -> int:
        return self.cumulative_input + self.cumulative_output

    @property
    def usd_usage_ratio(self) -> float:
        if self.config.max_usd <= 0:
            return 1.0
        return self.cumulative_usd / self.config.max_usd

    def track(self, metrics: IterationMetrics) -> None:
        """Call after each LLM response with token counts; accrues USD spend."""
        self.cumulative_input += metrics.input_tokens
        self.cumulative_output += metrics.output_tokens
        self.cumulative_usd += pricing.cost_usd(
            self.model,
            {
                "input_tokens": metrics.input_tokens,
                "output_tokens": metrics.output_tokens,
                "cache_read_input_tokens": metrics.cache_read_tokens,
                "cache_creation_input_tokens": metrics.cache_creation_tokens,
            },
        )
        self.iteration_count += 1
        self.iteration_history.append(metrics)

    def track_file(self, file_path: str) -> int:
        """Track a file access, return 1 if new, 0 if already seen."""
        if file_path in self.files_accessed:
            return 0
        self.files_accessed.add(file_path)
        return 1

    def track_symbol(self, symbol_name: str) -> int:
        """Track a symbol resolution, return 1 if new, 0 if already seen."""
        if symbol_name in self.symbols_resolved:
            return 0
        self.symbols_resolved.add(symbol_name)
        return 1

    def get_signal(self) -> BudgetSignal:
        """Determine current budget signal for the agent."""
        # Hard iteration cap
        if self.iteration_count >= self.config.max_iterations:
            return BudgetSignal.FORCE_CONCLUDE
        # USD critical threshold
        if self.usd_usage_ratio >= self.config.critical_threshold:
            return BudgetSignal.FORCE_CONCLUDE
        # USD warning threshold
        if self.usd_usage_ratio >= self.config.warning_threshold:
            return BudgetSignal.WARN_CONVERGE
        # Diminishing returns
        if self._detect_diminishing_returns():
            return BudgetSignal.WARN_CONVERGE
        return BudgetSignal.NORMAL

    def _detect_diminishing_returns(self) -> bool:
        """If last N iterations found no new files or symbols."""
        window = self.config.diminishing_returns_window
        if len(self.iteration_history) < window:
            return False
        recent = self.iteration_history[-window:]
        return all(m.new_files_accessed == 0 and m.new_symbols_found == 0 for m in recent)

    @property
    def budget_context(self) -> str:
        """Text injected into the LLM prompt for budget awareness."""
        remaining = max(0.0, self.config.max_usd - self.cumulative_usd)
        return (
            f"[Budget: ${self.cumulative_usd:.2f}/${self.config.max_usd:.2f} "
            f"({self.usd_usage_ratio:.0%}). "
            f"Iteration {self.iteration_count}/{self.config.max_iterations}. "
            f"Remaining: ~${remaining:.2f}. "
            f"Tokens: {self.cumulative_input:,} in / {self.cumulative_output:,} out. "
            f"Files: {len(self.files_accessed)}, Symbols: {len(self.symbols_resolved)}]"
        )

    def summary(self) -> dict:
        """Export summary for AgentResult / logging."""
        return {
            "total_input_tokens": self.cumulative_input,
            "total_output_tokens": self.cumulative_output,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.cumulative_usd, 6),
            "iterations": self.iteration_count,
            "usd_usage_ratio": round(self.usd_usage_ratio, 3),
            "files_accessed": len(self.files_accessed),
            "symbols_resolved": len(self.symbols_resolved),
        }
