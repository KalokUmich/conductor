"""Configuration dataclasses for the agent loop.

Replaces 20+ individual constructor parameters with two structured
dataclasses — ``AgentLoopConfig`` (for AgentLoopService) and
``BrainExecutorConfig`` (for AgentToolExecutor).

Usage::

    from app.agent_loop.config import AgentLoopConfig, BrainExecutorConfig
    from app.agent_loop.budget import BudgetConfig

    config = AgentLoopConfig(
        max_iterations=20,
        budget_config=BudgetConfig(max_input_tokens=300_000),
        is_sub_agent=True,
        forced_tools=["grep", "read_file"],
        agent_identity={"name": "explore_architecture", ...},
    )
    svc = AgentLoopService(provider=provider, config=config)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AgentLoopConfig:
    """Configuration for the agent loop service.

    Groups all behavioural and prompt-assembly settings so that
    AgentLoopService.__init__ receives a single structured object
    instead of 20+ individual keyword arguments.

    Attributes:
        max_iterations: Maximum number of LLM ↔ tool-call cycles
            before the loop forces a final answer.
        max_evidence_retries: How many times the evidence check may
            push back on an unsupported answer before accepting it.
        budget_config: Token-budget limits (input tokens, iterations).
            Uses a default BudgetConfig when not provided.
        interactive: Whether the agent may pause to ask the user for
            clarification via the ``ask_user`` tool.  Automatically
            forced to ``True`` in Brain mode.
        perspective: Agent role description used in legacy (non-4-layer)
            system-prompt and completeness-check personalisation.
        is_brain: Set ``True`` for the Brain orchestrator; causes the
            loop to use Brain tools and skip sub-agent quality checks.
        brain_system_prompt: Pre-built system prompt for Brain mode.
            Ignored when ``is_brain`` is ``False``.
        is_sub_agent: Set ``True`` for agents dispatched by Brain or
            by the workflow engine.  Disables interactive mode.
        forced_tools: Explicit tool allowlist supplied by Brain dispatch
            (bypasses classification).  ``None`` means "let the loop
            classify and select tools".
        agent_identity: Per-agent identity dict for the 4-layer prompt
            architecture.  Keys: ``name``, ``description``,
            ``instructions``, ``skill`` (optional).  When present the
            loop builds a per-agent system prompt (Layer 1) from these
            values.
        forced_strategy: Strategy key override (Layer 3, e.g.
            ``"code_review"``).  Overrides the classifier's choice.
        forced_skill: Investigation-skill key override (Layer 3, e.g.
            ``"business_flow"``).
        workflow_config: Workflow ``AgentConfig`` for workflow-driven
            agents.  Used together with ``workflow_route_name``.
        workflow_route_name: Route name that determines classification
            when the agent runs inside a workflow.
    """

    # Core loop limits
    max_iterations: int = 40
    max_evidence_retries: int = 2
    budget_config: Optional[Any] = None  # BudgetConfig — avoid circular import

    # Interaction mode
    interactive: bool = False
    perspective: str = ""

    # Brain-specific
    is_brain: bool = False
    brain_system_prompt: str = ""

    # Sub-agent specific
    is_sub_agent: bool = False
    forced_tools: Optional[List[str]] = None
    agent_identity: Optional[Dict[str, str]] = None
    forced_strategy: str = ""
    forced_skill: str = ""

    # Workflow
    workflow_config: Any = None
    workflow_route_name: str = ""


@dataclass
class BrainExecutorConfig:
    """Configuration for the Brain's agent tool executor.

    Controls where sub-agent dispatching happens in the depth/concurrency
    hierarchy and sets timeouts for individual agent runs.

    Attributes:
        workspace_path: Absolute path to the codebase root that all
            sub-agents will operate on.
        current_depth: Depth of *this* executor in the recursive
            dispatch tree.  ``0`` = Brain, ``1`` = first-level agent,
            ``2`` = second-level (max by default).
        max_depth: Maximum allowed dispatch depth.  Attempts to
            dispatch beyond this depth return an error result instead
            of spawning another agent.
        max_concurrent: Maximum number of sub-agents that may run in
            parallel (enforced via an ``asyncio.Semaphore`` inside
            ``_dispatch_swarm``).
        sub_agent_timeout: Seconds to wait for a single sub-agent
            before treating it as a timeout (partial success).
    """

    workspace_path: str = ""
    current_depth: int = 0
    max_depth: int = 2
    max_concurrent: int = 3
    sub_agent_timeout: float = 300.0
