"""Dispatch primitives — schemas + tool definitions.

The Brain orchestrator dispatches sub-agents via three tools, each with a
distinct contract:

| Tool | Use case | Output |
|---|---|---|
| ``dispatch_explore`` | Open exploration; scope unknown to Brain | Prose answer |
| ``dispatch_verify`` | Scope-bounded verification; 3 falsifiable checks or role lens | JSON: verdicts + findings (severity=null) |
| ``dispatch_sweep`` | Full-diff scan through one role lens (cross-file pattern hunt) | JSON: findings (severity=null) |

This package holds the Pydantic param schemas + the tool-definition dicts
that get included in ``BRAIN_TOOL_DEFINITIONS``. The dispatch HANDLERS
(``_dispatch_explore``, ``_dispatch_verify``, ``_dispatch_sweep``) live on
``AgentToolExecutor`` in ``brain.py`` because they consume ~12 instance
fields (provider, agent_registry, budget_manager, scratchpad, …); moving
them to standalone functions would require either a giant arg list or a
context object that's effectively the executor itself.

Imported by ``code_tools/schemas.py`` to compose ``BRAIN_TOOL_DEFINITIONS``
and re-exported for the rest of the codebase.
"""

from .explore import DispatchExploreParams, DISPATCH_EXPLORE_TOOL_DEF
from .verify import (
    DispatchVerifyParams,
    DispatchVerifyScope,
    DISPATCH_VERIFY_TOOL_DEF,
)
from .sweep import DispatchSweepParams, DISPATCH_SWEEP_TOOL_DEF

__all__ = [
    "DispatchExploreParams",
    "DispatchVerifyParams",
    "DispatchVerifyScope",
    "DispatchSweepParams",
    "DISPATCH_EXPLORE_TOOL_DEF",
    "DISPATCH_VERIFY_TOOL_DEF",
    "DISPATCH_SWEEP_TOOL_DEF",
]
