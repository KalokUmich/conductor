"""dispatch_verify — scope-bounded structured verification.

PR Brain v2's core primitive. Brain dispatches a worker scoped to 1-5
files with either:

1. **Checks mode**: exactly 3 falsifiable yes/no questions about specific
   locations. Worker returns ``confirmed | violated | unclear`` per check.
2. **Role mode**: a role-specialist (security / correctness / concurrency
   / reliability / performance / test_coverage) composed from
   ``config/agent_factory/{role}.md``.

Workers return JSON: ``{checks: [...], findings: [...], unexpected_observations: [...]}``
with ``severity: null`` (Brain classifies severity, not the worker).

Counterpart to ``dispatch_explore`` (open prose) and ``dispatch_sweep``
(full-diff one-lens). The handler lives in ``brain.py`` (
``AgentToolExecutor._dispatch_verify``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class DispatchVerifyScope(BaseModel):
    """Single file slot in a dispatch_verify scope."""

    file: str = Field(..., description="Repo-relative path, e.g. 'src/auth/session.py'.")
    start: Optional[int] = Field(
        default=None, ge=1,
        description="1-indexed first line of the scope. Omit to cover the whole file.",
    )
    end: Optional[int] = Field(
        default=None, ge=1,
        description="1-indexed last line (inclusive). Omit to cover until EOF.",
    )


class DispatchVerifyParams(BaseModel):
    """PR Brain v2's core primitive — dispatch a scope-bounded sub-agent.

    Two dispatch modes:

    1. **Checks mode** (original): Brain has already localised a specific
       suspicion and hands the worker 3 falsifiable yes/no questions. Best
       when Survey found a concrete hotspot.
       Required: ``checks`` (exactly 3 questions). ``role`` unset.

    2. **Role mode** (new, P12): Brain has spotted a risk dimension but
       hasn't localised the invariant yet, and wants a role-specialist to
       do a scope-bounded deep-dive. Brain COMPOSES the sub-agent's system
       prompt by referencing ``config/agent_factory/{role}.md`` (lens,
       approach, finding-shape examples) and fusing in this PR's context.
       Required: ``role`` (one of the factory roles). Optional:
       ``direction_hint`` (1-2 sentences of what to investigate),
       ``checks`` (if Brain wants the role to also answer 3 specific
       questions — stronger dispatch).

    Must provide EITHER ``checks`` OR ``role``.

    The sub-agent returns verdicts (if checks) + findings with
    ``severity: null`` (Brain classifies severity) + optional
    unexpected_observations.
    """

    scope: List[DispatchVerifyScope] = Field(
        ...,
        min_length=1,
        max_length=5,
        description=(
            "1-5 file slots defining what the sub-agent may examine. Each slot "
            "is a file path with optional start/end line range. The sub-agent "
            "stays inside this scope unless a check explicitly requires "
            "cross-file verification (e.g. 'does symbol X exist elsewhere')."
        ),
    )
    role: Optional[str] = Field(
        default=None,
        description=(
            "Optional role reference from config/agent_factory/: 'security', "
            "'correctness', 'concurrency', 'reliability', 'performance', or "
            "'test_coverage'. When set, Brain composes the worker's system "
            "prompt from (a) the role's Lens/Concerns/Approach/Examples in "
            "the factory template and (b) this PR's scope + direction_hint "
            "+ Brain's Survey notes. The factory file is a REFERENCE — Brain "
            "does the composition, does not paste the template verbatim."
        ),
    )
    direction_hint: Optional[str] = Field(
        default=None,
        max_length=500,
        description=(
            "1-2 sentences describing what to investigate, used with 'role'. "
            "Brain emits this when it has identified a risk dimension but "
            "not yet localised specific invariants. E.g. 'OAuth flow gained "
            "PKCE support in this commit — look for token leaks, "
            "state-mismatch bypass, or incomplete migration of existing "
            "clients'. Less prescriptive than checks, more guided than "
            "'review for security'."
        ),
    )
    checks: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional: exactly 3 falsifiable predicates about specific "
            "locations. Each check must be answerable as confirmed | "
            "violated | unclear with file:line evidence. Combine with "
            "'role' for 'specialist + specific questions'. Leave None when "
            "dispatching with role + direction_hint only. Role-shaped "
            "tasks ('review for correctness') as checks are rejected — "
            "give concrete invariant-at-location questions."
        ),
    )
    success_criteria: str = Field(
        ...,
        min_length=10,
        description=(
            "What 'done' looks like for this investigation. E.g. 'Answer each "
            "check with a verdict + file:line evidence, and flag any bug "
            "whose presence would make the verdict violated'."
        ),
    )
    budget_tokens: int = Field(
        default=120_000, ge=40_000, le=400_000,
        description="Token budget for this sub-agent. 80–150K typical.",
    )
    model_tier: str = Field(
        default="explorer",
        description=(
            "'explorer' (Haiku, default for most investigations) or 'strong' "
            "(Sonnet, for hard verification where evidence is ambiguous). "
            "Brain's replan step may upgrade to 'strong' on a follow-up pass."
        ),
    )
    may_subdispatch: bool = Field(
        default=False,
        description=(
            "When True, the dispatched sub-agent may itself call "
            "dispatch_verify once (depth 2, hard wall). Use only when a "
            "check genuinely requires subdividing (e.g. 'for each of the 3 "
            "call sites of foo, verify the caller handles the new return'). "
            "Most investigations should leave this False."
        ),
    )
    context: Optional[str] = Field(
        default=None,
        description=(
            "Optional 1-3 sentences of Brain-composed context the sub-agent "
            "needs to interpret the task. E.g. 'This diff introduces a new "
            "AssignmentSource dataclass used by Celery workers'. Do NOT put "
            "the diff here — sub-agent gets the diff separately."
        ),
    )

    @model_validator(mode="after")
    def _require_checks_or_role(self) -> "DispatchVerifyParams":
        """Exactly-one-mode validator — must have at least one of {checks, role}."""
        if not self.checks and not self.role:
            raise ValueError(
                "dispatch_verify requires either 'checks' (3 specific "
                "questions) or 'role' (factory reviewer, e.g. 'security'). "
                "Got neither."
            )
        if self.checks is not None and len(self.checks) != 3:
            raise ValueError(
                f"When provided, 'checks' must have exactly 3 entries "
                f"(got {len(self.checks)})."
            )
        return self


DISPATCH_VERIFY_TOOL_DEF: Dict[str, Any] = {
    "name": "dispatch_verify",
    "description": (
        "PR Brain v2's core primitive — dispatch a scope-bounded sub-agent "
        "with exactly 3 falsifiable checks. The sub-agent returns per-check "
        "verdicts (confirmed | violated | unclear), findings with "
        "`severity: null`, and optional unexpected_observations. You (the "
        "Brain) classify severity yourself using cross-cutting context.\n\n"
        "Use when you've surveyed the PR and want a focused investigation "
        "on a specific slice of code. Each dispatch should target a "
        "single semantic unit — breadth (more focused dispatches) beats "
        "depth (one kitchen-sink dispatch).\n\n"
        "Good check shape: invariant-at-location (\"At line 138, is "
        "`amount` validated >0 before the INSERT?\"). Bad: role-shaped "
        "(\"Review for correctness\") — that's delegated synthesis.\n\n"
        "Set `may_subdispatch=true` only when a check genuinely requires "
        "subdivision (e.g. 'for each of the 3 call sites, verify…'). "
        "Depth 2 is a hard wall — sub-sub-agents cannot dispatch further."
    ),
    "input_schema": DispatchVerifyParams.model_json_schema(),
}
