"""dispatch_sweep — full-diff one-lens cross-file scan.

PR Brain v2's dimension-sliced primitive (P12b). Where ``dispatch_verify``
decomposes by file-range (1-5 scoped files), this decomposes by bug class:
worker reads the entire PR diff through one lens (security, correctness,
concurrency, reliability, performance, api_contract, test_coverage) and
hunts that class of bug across every changed file.

Strong case: a changed function called from ≥3 sites or ≥2 files. File-range
dispatch can miss the cross-file contract break (caller A handles new return
shape, callers B and C don't). A sweep worker sees them all in one pass.

The handler lives in ``brain.py`` (``AgentToolExecutor._dispatch_sweep``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DispatchSweepParams(BaseModel):
    """PR Brain v2's dimension-sliced dispatch primitive (P12b).

    Where ``dispatch_verify`` decomposes a review by **file-range**
    (each worker sees 1-5 scoped files), this decomposes by **bug class**:
    the worker reads the *entire* PR diff through one lens (security,
    correctness, concurrency, reliability, performance, api_contract,
    test_coverage) and hunts that class of bug across every changed file.

    Strong case: a changed function is called from ≥3 call sites or
    ≥2 files. File-range dispatch gives each worker a narrow slice and
    can miss the cross-file contract break (caller A handles the new
    return shape, callers B and C don't). A dimension worker sees them
    all in one pass.

    Budget is higher than scoped dispatch (full diff + caller context in
    context window). ``model_tier='explorer'`` by default; escalate to
    ``'strong'`` only for genuinely cross-file invariant reasoning.

    Cap (enforced by Brain meta-skill):
      - PR < 5 files:   0 dimension workers (not worth the budget)
      - PR 5-14 files:  up to 1
      - PR ≥ 15 files:  up to 2
    """

    dimension: str = Field(
        ...,
        description=(
            "Lane name — must be one of the agent_factory roles: "
            "'security', 'correctness', 'concurrency', 'reliability', "
            "'performance', 'test_coverage', 'api_contract'. Brain "
            "composes the system prompt from the factory template fused "
            "with 'the whole PR diff' context."
        ),
    )
    direction_hint: Optional[str] = Field(
        default=None,
        max_length=500,
        description=(
            "1-2 sentences describing what to hunt across the diff. "
            "E.g. 'OAuth flow added PKCE; verify every redirect/callback "
            "path handles state-mismatch, and existing non-PKCE clients "
            "still work'. More guided than the role alone, but less "
            "prescriptive than a checks list — the whole point of "
            "dimension-sliced dispatch is to let the lens find what "
            "the coordinator didn't yet localise."
        ),
    )
    triggering_symbols: Optional[List[str]] = Field(
        default=None,
        max_length=20,
        description=(
            "Optional list of changed function / method names whose "
            "cross-file usage triggered this dispatch. Brain surfaces "
            "these to the worker so 'callers of foo' is already a "
            "concrete target instead of a general prowl. E.g. "
            "['TokenService.issue', 'PaymentGateway.charge']."
        ),
    )
    success_criteria: str = Field(
        ...,
        min_length=10,
        description=(
            "What 'done' looks like. E.g. 'For each changed function "
            "with ≥3 callers, verify every caller handles the new "
            "behaviour; flag the ones that don't with file:line "
            "evidence'."
        ),
    )
    budget_tokens: int = Field(
        default=150_000, ge=80_000, le=200_000,
        description=(
            "Token budget. Dimension dispatch reads the full diff + "
            "often traces callers across the repo, so budget is higher "
            "than scoped dispatch. 150K default leaves ~50K headroom "
            "in the explorer-tier context window for internal reasoning "
            "+ cache. Bump to 180K only when diff itself is >40K tokens."
        ),
    )
    model_tier: str = Field(
        default="explorer",
        description=(
            "'explorer' (default — dimension work is cross-file pattern "
            "matching, which the fast tier handles well) or 'strong' "
            "(only when cross-file logical inference is required — e.g. "
            "saga unwind, multi-step state machine invariant)."
        ),
    )


DISPATCH_SWEEP_TOOL_DEF: Dict[str, Any] = {
    "name": "dispatch_sweep",
    "description": (
        "PR Brain v2's dimension-sliced primitive (P12b). Dispatch a "
        "role-specialist worker that reads the ENTIRE PR diff through "
        "one lens and hunts its bug class across every changed file. "
        "Complement to `dispatch_verify` — use this when decomposition "
        "by file-range would split up a multi-site pattern.\n\n"
        "Use this when: a changed function has ≥3 callers or ≥2 distinct "
        "caller files, the PR introduces a new contract that multiple "
        "call sites must now honour, or a shared utility/middleware is "
        "modified. File-range dispatch can't see the cross-file break.\n\n"
        "Budget 150K default (fits in explorer-tier context with "
        "headroom). Escalate to `model_tier='strong'` ONLY for "
        "cross-file logical inference (saga unwind, multi-step "
        "state-machine invariant) — pattern-matching across files "
        "is the explorer tier's lane.\n\n"
        "Cap: 0 dispatches on PRs <5 files, 1 on 5-14, 2 on ≥15. "
        "Per-PR dimension-worker budget is separate from the scoped "
        "dispatch cap — these investigations don't compete for the "
        "same pool because they answer a different kind of question."
    ),
    "input_schema": DispatchSweepParams.model_json_schema(),
}
