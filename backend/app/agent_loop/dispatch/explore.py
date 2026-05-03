"""dispatch_explore — open-ended sub-agent investigation.

Brain dispatches one or more workers to explore the codebase when the
scope isn't known up front. Two modes:

1. **Template mode**: ``template=`` selects a pre-tuned agent from the
   registry (e.g. ``explore_implementation``, ``explore_usage``). These
   are tuned for specific perspectives.
2. **Dynamic mode**: ``tools=`` + optional ``perspective=``, ``skill=``,
   ``model=``, ``budget_tokens=`` composes an agent on the fly.

Workers return a prose answer; Brain synthesises across multiple worker
outputs (4-section structure for Domain Brain; freeform for General Brain).

Counterpart to ``dispatch_verify`` (scope-bounded structured checks) and
``dispatch_sweep`` (full-diff one-lens hunt).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DispatchExploreParams(BaseModel):
    query: str = Field(..., description="Focused question for the agent to investigate")

    # Mode 1: Template (pre-defined agent from registry)
    template: Optional[str] = Field(
        default=None,
        description="Pre-defined agent template name (e.g. 'correctness', "
        "'explore_implementation'). Use for Domain Brain templates and other "
        "pre-tuned investigators.",
    )

    # Mode 2: Dynamic composition (Brain assembles the agent)
    tools: Optional[List[str]] = Field(
        default=None,
        description="Tools for this agent (e.g. ['grep', 'read_file', "
        "'find_symbol']). Required when no template is specified.",
    )
    perspective: Optional[str] = Field(
        default=None,
        description="1-3 sentences defining the agent's investigation focus and what to look for.",
    )
    skill: Optional[str] = Field(
        default=None,
        description="Investigation skill key from the skill catalog "
        "(e.g. 'entry_point', 'root_cause', 'architecture', 'impact', "
        "'data_lineage', 'recent_changes', 'code_explanation', "
        "'config_analysis', 'issue_tracking').",
    )
    model: str = Field(
        default="explorer",
        description="'explorer' (Haiku, default) or 'strong' (Sonnet, for complex reasoning like root cause analysis).",
    )
    budget_tokens: Optional[int] = Field(
        default=None, ge=50000, le=800000,
        description="Token budget override. Defaults based on skill type.",
    )
    max_iterations: Optional[int] = Field(
        default=None, ge=5, le=30, description="Iteration limit override. Default: 20.",
    )

    # Shared
    budget_weight: float = Field(default=1.0, ge=0.3, le=2.0, description="Budget multiplier (1.0 = standard)")


DISPATCH_EXPLORE_TOOL_DEF: Dict[str, Any] = {
    "name": "dispatch_explore",
    "description": (
        "Dispatch a specialist agent to investigate the codebase. Two modes:\n"
        "1. Template mode: set template= to use a pre-defined agent from "
        "the template catalog (e.g. explore_implementation, explore_usage). "
        "These are pre-tuned for specific perspectives — pick one or "
        "dispatch several in PARALLEL (multiple tool calls in one turn) "
        "when a query benefits from complementary lenses.\n"
        "2. Dynamic mode: set tools= and optionally perspective=, skill=, "
        "model=, budget_tokens= to compose an agent on the fly. "
        "Use the skill catalog and tool catalog in your system prompt "
        "to select the right combination."
    ),
    "input_schema": DispatchExploreParams.model_json_schema(),
}
