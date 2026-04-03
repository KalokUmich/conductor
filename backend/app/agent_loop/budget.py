"""Token-based budget controller for the agent loop.

Replaces iteration-only budget management with token tracking.
Checked after each LLM call to signal convergence or forced conclusion.

Reference: "How Do Coding Agents Spend Your Money?" (ICLR 2026)
https://openreview.net/forum?id=1bUeVB3fov
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class BudgetSignal(Enum):
    NORMAL = "normal"
    WARN_CONVERGE = "warn_converge"
    FORCE_CONCLUDE = "force_conclude"


@dataclass
class BudgetConfig:
    max_input_tokens: int = 1_000_000  # Total input token budget per session
    warning_threshold: float = 0.7  # 70% — inject warning into prompt
    critical_threshold: float = 0.9  # 90% — force conclusion
    max_iterations: int = 50  # Hard iteration cap
    diminishing_returns_window: int = 3  # N iterations with no new info


@dataclass
class IterationMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_names: List[str] = field(default_factory=list)
    new_files_accessed: int = 0
    new_symbols_found: int = 0


class BudgetController:
    """Token-aware budget controller embedded in AgentLoopService.

    Tracks cumulative token usage per session, detects diminishing returns,
    and emits signals that the agent loop uses to inject convergence guidance
    or force conclusion.
    """

    def __init__(self, config: Optional[BudgetConfig] = None) -> None:
        self.config = config or BudgetConfig()
        self.cumulative_input = 0
        self.cumulative_output = 0
        self.iteration_count = 0
        self.iteration_history: List[IterationMetrics] = []
        self.files_accessed: set = set()
        self.symbols_resolved: set = set()

    @property
    def total_tokens(self) -> int:
        return self.cumulative_input + self.cumulative_output

    @property
    def input_usage_ratio(self) -> float:
        if self.config.max_input_tokens == 0:
            return 1.0
        return self.cumulative_input / self.config.max_input_tokens

    def track(self, metrics: IterationMetrics) -> None:
        """Call after each LLM response with token counts."""
        self.cumulative_input += metrics.input_tokens
        self.cumulative_output += metrics.output_tokens
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
        # Token-based critical threshold
        if self.input_usage_ratio >= self.config.critical_threshold:
            return BudgetSignal.FORCE_CONCLUDE
        # Token-based warning threshold
        if self.input_usage_ratio >= self.config.warning_threshold:
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
        remaining = self.config.max_input_tokens - self.cumulative_input
        return (
            f"[Budget: {self.cumulative_input:,}/{self.config.max_input_tokens:,} "
            f"input tokens ({self.input_usage_ratio:.0%}). "
            f"Iteration {self.iteration_count}/{self.config.max_iterations}. "
            f"Remaining: ~{remaining:,} input tokens. "
            f"Files: {len(self.files_accessed)}, Symbols: {len(self.symbols_resolved)}]"
        )

    def summary(self) -> dict:
        """Export summary for AgentResult / logging."""
        return {
            "total_input_tokens": self.cumulative_input,
            "total_output_tokens": self.cumulative_output,
            "total_tokens": self.total_tokens,
            "iterations": self.iteration_count,
            "input_usage_ratio": round(self.input_usage_ratio, 3),
            "files_accessed": len(self.files_accessed),
            "symbols_resolved": len(self.symbols_resolved),
        }
