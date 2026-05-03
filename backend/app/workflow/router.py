"""Brain swarms API.

Exposes the agent swarm composition that the Agent Swarm UI tab in the
extension visualizes (handoff targets reachable via Brain's
``transfer_to_brain`` and ``dispatch_swarm`` meta-tools).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Brain swarms API
# ---------------------------------------------------------------------------

brain_router = APIRouter(prefix="/api/brain", tags=["brain"])


class AgentInfo(BaseModel):
    name: str
    description: str = ""
    model: str = "explorer"
    tools: List[str] = []
    category: Optional[str] = None


class SpecializedBrainInfo(BaseModel):
    name: str
    description: str = ""
    model: str = "strong"
    type: str = "brain"  # "brain" for transfer_to_brain, "swarm" for dispatch_swarm
    mode: str = ""  # "parallel" | "sequential" | "pipeline"
    agents: List[AgentInfo] = []
    arbitrator: Optional[AgentInfo] = None
    trigger: str = ""  # how Brain activates this (e.g. "transfer_to_brain('pr_review')")


class BrainSwarmsResponse(BaseModel):
    brain_model: str = "strong"
    core_tools: List[str] = []
    specialized_brains: List[SpecializedBrainInfo] = []
    swarms: List[SpecializedBrainInfo] = []


@brain_router.get("/swarms", response_model=BrainSwarmsResponse)
async def get_brain_swarms():
    """Return all specialized brains and swarms with their agent compositions.

    Used by the Agent Swarm UI tab to visualize the Brain's handoff targets.
    """
    from .loader import (
        load_agent_registry,
        load_brain_config,
        load_pr_brain_config,
    )

    brain_cfg = load_brain_config()
    agent_registry = load_agent_registry()

    def _agent_info(name: str) -> AgentInfo:
        ac = agent_registry.get(name)
        if ac:
            return AgentInfo(
                name=ac.name,
                description=ac.description,
                model=ac.model,
                tools=list(ac.tool_list),
                category=ac.category,
            )
        return AgentInfo(name=name)

    # Specialized brains (transfer_to_brain targets)
    specialized = []
    try:
        pr_cfg = load_pr_brain_config()
        pr_agents = [_agent_info(n) for n in pr_cfg.review_agents]
        pr_arb = _agent_info(pr_cfg.arbitrator) if pr_cfg.arbitrator else None
        specialized.append(
            SpecializedBrainInfo(
                name=pr_cfg.name,
                description=pr_cfg.description,
                model=pr_cfg.model,
                type="brain",
                mode="pipeline",
                agents=pr_agents,
                arbitrator=pr_arb,
                trigger=f'transfer_to_brain("{pr_cfg.name}")',
            )
        )
    except Exception as exc:
        logger.warning("Failed to load PR Brain config: %s", exc)

    # Swarm presets retired 2026-05-03 — Domain Brain replaces dispatch_swarm.
    # Field kept on response for back-compat with the Extension's Agent Swarm
    # tab (it iterates an empty list cleanly). Future: surface Domain + PR
    # Brain workers under specialized_brains instead.
    swarms: List[SpecializedBrainInfo] = []

    return BrainSwarmsResponse(
        brain_model=brain_cfg.model,
        core_tools=brain_cfg.core_tools,
        specialized_brains=specialized,
        swarms=swarms,
    )
