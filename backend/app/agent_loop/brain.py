"""Brain orchestrator — AgentToolExecutor and budget management.

The Brain is the central coordinator that dispatches specialist agents
(agent-as-tool) and parallel swarms (subagent swarm). It wraps a real
ToolExecutor and intercepts ``dispatch_agent`` / ``dispatch_swarm`` calls,
running sub-agents in isolated contexts and returning condensed findings.

Design principles:
  - Only Brain talks to the user (via ask_user)
  - Sub-agents return condensed AgentFindings, not full traces
  - Recursive depth control: Brain(0) → agent(1) → sub-agent(2) max
  - Concurrency limited via semaphore (default: 3 concurrent sub-agents)
  - Partial failure in swarms: succeeded agents' findings are still returned
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.code_tools.executor import ToolExecutor
from app.code_tools.schemas import ToolResult

from .config import BrainExecutorConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_SUMMARY_TRUNCATE_LEN = 120  # max chars per tool-call summary in condense_result
_MAX_CONTEXT_CHUNKS = 10  # cap on context chunks returned to Brain (prevents bloat)
_MAX_TOOLS_SUMMARY = 15  # cap on tool-call summary lines returned to Brain
_MAX_BRAIN_RESERVE = 100_000  # upper bound on tokens Brain reserves for its own calls
_MIN_AGENT_BUDGET = 50_000  # floor budget allocated to any sub-agent
_MAX_AGENT_BUDGET = 800_000  # ceiling budget allocated to any sub-agent
_DEFAULT_AGENT_BUDGET = 100_000  # minimum guaranteed budget even when pool is generous

# ---------------------------------------------------------------------------
# Role-factory template loader (P12 — role-based dispatch)
# ---------------------------------------------------------------------------

_VALID_FACTORY_ROLES = frozenset({
    "security",
    "correctness",
    "concurrency",
    "reliability",
    "performance",
    "test_coverage",
})


def _load_role_template(role: str) -> Optional[Dict[str, Any]]:
    """Load a role template from config/agent_factory/{role}.md.

    Returns ``{"frontmatter": {...}, "body": "..."}`` on success, ``None``
    when the template is missing or malformed. Deliberately tolerant —
    a missing factory file should not break the Brain; we fall back to
    a minimal generic prompt.
    """
    if role not in _VALID_FACTORY_ROLES:
        return None

    import yaml

    try:
        from app.workflow.loader import _find_config_dir
    except Exception:
        return None

    try:
        config_dir = _find_config_dir()
        template_path = config_dir / "agent_factory" / f"{role}.md"
        if not template_path.exists():
            return None
        content = template_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read role template %s: %s", role, exc)
        return None

    import re as _re
    match = _re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)", content, _re.DOTALL)
    if not match:
        logger.warning("Role template %s missing YAML frontmatter", role)
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.warning("Role template %s has invalid YAML: %s", role, exc)
        return None

    return {
        "frontmatter": frontmatter,
        "body": match.group(2).strip(),
    }


def _compose_role_system_prompt(
    role: str,
    role_template: Dict[str, Any],
    scope_block: str,
    direction_hint: Optional[str],
    checks: Optional[List[str]],
    brain_context: Optional[str],
    may_subdispatch: bool,
) -> str:
    """Compose a role-specialist system prompt.

    **Reference, not copy**: the factory template (Lens / Concerns /
    Approach / Examples) teaches the mindset; this function fuses that
    with the PR-specific context Brain has already gathered. The
    resulting prompt is unique to this dispatch, not a paste of the
    factory file.
    """
    frontmatter = role_template.get("frontmatter", {})
    role_body = role_template.get("body", "").strip()
    description = frontmatter.get("description", f"{role} reviewer")

    parts: List[str] = []
    parts.append(f"# You are a {role} reviewer for this PR")
    parts.append("")
    parts.append(f"**Role identity**: {description}")
    parts.append("")
    parts.append(
        "Your lens, typical concerns, investigation approach, and "
        "finding-shape examples are below. Treat these as how you "
        "*think*; do not copy their specific examples into your output."
    )
    parts.append("")
    parts.append(role_body)
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append("# Your task in THIS PR (composed by the PR Brain)")
    parts.append("")
    parts.append("## Scope — stay inside these files")
    parts.append("")
    parts.append(scope_block)
    parts.append("")
    if direction_hint:
        parts.append("## Brain's direction hint")
        parts.append("")
        parts.append(direction_hint)
        parts.append("")
    if brain_context:
        parts.append("## Context from Brain's Survey")
        parts.append("")
        parts.append(brain_context)
        parts.append("")
    if checks:
        parts.append("## Specific checks Brain wants answered")
        parts.append("")
        parts.append("\n".join(f"{i+1}. {c}" for i, c in enumerate(checks)))
        parts.append("")
        parts.append(
            "For each check, emit `{id, question, verdict, evidence}`."
        )
        parts.append("")

    parts.append("## Output contract — MUST follow")
    parts.append("")
    parts.append(
        "Emit a JSON block at end of turn with this shape:"
    )
    parts.append("")
    parts.append("```json")
    parts.append("{")
    parts.append(
        '  "summary": "≤3 sentences. Overall verdict from your lens.",'
    )
    if checks:
        parts.append('  "checks": [/* verdict per check, in order */],')
    parts.append(
        '  "findings": ['
    )
    parts.append(
        '    {"title": "...", "file": "...", "line": N, '
        '"description": "...", "severity": null, '
        '"severity_hint": "critical|high|medium|low|nit", '
        '"confidence": 0.0-1.0}'
    )
    parts.append("  ]")
    parts.append("}")
    parts.append("```")
    parts.append("")
    parts.append(
        "**Severity rules**: `severity` MUST be `null` — Brain classifies "
        "severity, not you. `severity_hint` is a HINT Brain may override. "
        "At most 5 findings; quality > quantity. Every finding MUST have "
        "file:line evidence quoted from code."
    )
    parts.append("")
    parts.append("## Hard boundaries")
    parts.append("")
    parts.append(
        "- Stay within the scope files above. Cross-file grep only if "
        "verifying existence of a symbol referenced by your finding."
    )
    parts.append(
        "- No style / naming nits. No pre-existing issues. No speculative "
        '"potential concern" without a concrete trigger path in THIS diff.'
    )
    parts.append(
        "- If nothing in your lens fires, emit an empty findings array + "
        "a summary explaining what you verified and why nothing rose."
    )

    if may_subdispatch:
        parts.append("")
        parts.append("## Sub-dispatch permitted (depth 2 hard wall)")
        parts.append("")
        parts.append(
            "Brain set may_subdispatch=true. You may call "
            "`dispatch_subagent` ONCE to delegate a narrower investigation. "
            "Sub-sub-agents cannot dispatch further."
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Condensed findings returned by sub-agents
# ---------------------------------------------------------------------------


@dataclass
class AgentFindings:
    """Condensed result from a sub-agent, returned to Brain as a tool result.

    Contains enough for Brain to judge quality and suggest new directions,
    without the full tool call history or intermediate LLM reasoning.
    """

    answer: str = ""
    context_chunks: List[Dict[str, Any]] = field(default_factory=list)
    files_accessed: List[str] = field(default_factory=list)
    tools_summary: List[str] = field(default_factory=list)
    gaps_identified: List[str] = field(default_factory=list)
    confidence: str = "medium"  # high | medium | low
    iterations: int = 0
    tool_calls_made: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None


def _parse_subagent_json(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a PR Brain v2 sub-agent's final answer.

    Accepts:
      * A plain JSON object with {checks, findings, unexpected_observations}.
      * JSON wrapped in a ```json ... ``` fenced block (one or more blocks —
        the last fenced block is preferred since models often restate their
        answer near the end).
      * JSON embedded in prose — falls back to finding the last balanced
        ``{...}`` that contains the "checks" key.

    Returns the dict on success, ``None`` if no usable JSON was found.
    """
    import json as _json
    import re as _re

    if not raw:
        return None

    # Prefer the LAST ```json fenced block — models tend to restate at end.
    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates: list = list(reversed(fenced))

    # Fallback: any top-level {...} that contains "checks".
    if not candidates:
        # Greedy match from last `{` backwards to first `}` containing "checks"
        for start in range(len(raw) - 1, -1, -1):
            if raw[start] != "{":
                continue
            depth = 0
            for end in range(start, len(raw)):
                if raw[end] == "{":
                    depth += 1
                elif raw[end] == "}":
                    depth -= 1
                    if depth == 0:
                        snippet = raw[start : end + 1]
                        if '"checks"' in snippet:
                            candidates.append(snippet)
                        break
            if candidates:
                break

    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, dict) and "checks" in parsed:
                return parsed
        except (ValueError, _json.JSONDecodeError):
            continue
    return None


def condense_result(result) -> Dict[str, Any]:
    """Condense an AgentResult into a dict suitable for Brain's tool result.

    Extracts key information while discarding full tool outputs and
    intermediate reasoning that would pollute Brain's context.

    Args:
        result: An AgentResult (or duck-typed equivalent) with ``answer``,
            ``thinking_steps``, ``context_chunks``, ``tool_calls_made``,
            ``iterations``, ``duration_ms``, and ``error`` attributes.

    Returns:
        A dict with keys: answer, context_chunks, files_accessed,
        tools_summary, gaps_identified, confidence, iterations,
        tool_calls_made, duration_ms, error.
    """

    # Build tools summary from thinking steps
    tools_summary = []
    files_accessed: set[str] = set()
    gaps = []

    # Prefer the agent's authoritative read set (from BudgetController) — that's
    # every file the agent opened via read_file/file_outline/compressed_view.
    # Falls back to chunk-derived paths for older AgentResult instances.
    if hasattr(result, "files_accessed") and result.files_accessed:
        files_accessed.update(result.files_accessed)

    if hasattr(result, "thinking_steps"):
        for step in result.thinking_steps:
            if hasattr(step, "kind"):
                kind = step.kind
                tool = step.tool if hasattr(step, "tool") else ""
                summary = step.summary if hasattr(step, "summary") else ""
            else:
                kind = step.get("kind", "")
                tool = step.get("tool", "")
                summary = step.get("summary", "")

            if kind == "tool_result" and tool:
                # Truncate long summaries
                short = summary[:_SUMMARY_TRUNCATE_LEN] + "..." if len(summary) > _SUMMARY_TRUNCATE_LEN else summary
                tools_summary.append(f"{tool}: {short}")

    # Extract files from context chunks (union with budget-tracked set above)
    chunks_data = []
    if hasattr(result, "context_chunks"):
        for chunk in result.context_chunks:
            if hasattr(chunk, "file_path"):
                fp = chunk.file_path
                files_accessed.add(fp)
                chunks_data.append(
                    {
                        "file_path": fp,
                        "start_line": getattr(chunk, "start_line", 0),
                        "end_line": getattr(chunk, "end_line", 0),
                        "content": getattr(chunk, "content", "")[:500],
                    }
                )

    # Determine confidence from evidence quality
    answer = result.answer or ""
    confidence = "high"
    if not answer or len(answer) < 50 or result.tool_calls_made < 2:
        confidence = "low"
    elif "not found" in answer.lower() or "unable to" in answer.lower():
        confidence = "medium"

    # Budget exhaustion is not a real error — the agent produced results,
    # just ran out of budget. Only propagate actual failures.
    error = result.error
    if error and ("budget" in error.lower() or "token" in error.lower()):
        error = None  # budget exhaustion is expected, not an error

    return {
        "answer": answer,
        "context_chunks": chunks_data[:_MAX_CONTEXT_CHUNKS],  # cap to prevent context bloat
        "files_accessed": sorted(files_accessed),
        "tools_summary": tools_summary[:_MAX_TOOLS_SUMMARY],  # cap
        "gaps_identified": gaps,
        "confidence": confidence,
        "iterations": result.iterations,
        "tool_calls_made": result.tool_calls_made,
        "duration_ms": result.duration_ms,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Budget manager for Brain + sub-agents
# ---------------------------------------------------------------------------


class BrainBudgetManager:
    """Manages token budget across Brain and its sub-agents.

    Brain reserves a portion for its own LLM calls (thinking, synthesis).
    Remaining budget is allocated to sub-agents on demand.

    Allocations are pre-deducted from the pool at ``allocate()`` time so
    that parallel sub-agent dispatches see a correctly draining pool. The
    reservation is moved to ``used`` when ``report()`` is called with the
    actual token consumption — overruns and underruns both reconcile via
    the ``used`` total.
    """

    def __init__(self, total_tokens: int, brain_reserve_ratio: float = 0.15):
        self.total = total_tokens
        self.brain_reserve = min(_MAX_BRAIN_RESERVE, int(total_tokens * brain_reserve_ratio))
        self.used: Dict[str, int] = {}  # agent_name → actual tokens consumed (post-report)
        self.reserved: Dict[str, int] = {}  # agent_name → tokens held at allocate() time
        self._lock = asyncio.Lock()

    @property
    def remaining(self) -> int:
        committed = sum(self.used.values()) + sum(self.reserved.values())
        return max(0, self.total - committed - self.brain_reserve)

    async def allocate(self, agent_name: str, weight: float = 1.0) -> int:
        """Allocate tokens for a sub-agent.

        Pre-deducts the allocation from the pool so concurrent dispatches
        see the pool drain correctly. Guarantees at least
        ``_MIN_AGENT_BUDGET`` tokens even when the pool is nearly
        exhausted, to prevent agents from starting with too small a budget.

        Args:
            agent_name: Name of the sub-agent requesting tokens (used for logging).
            weight: Relative budget multiplier (e.g. 1.5 for a heavyweight agent).

        Returns:
            Number of input tokens allocated to the agent.
        """
        async with self._lock:
            available = self.remaining
            if available < _MIN_AGENT_BUDGET:
                allocated = _MIN_AGENT_BUDGET
                logger.warning(
                    "Budget low (%d remaining), allocating minimum %d to %s",
                    available,
                    allocated,
                    agent_name,
                )
            else:
                # Give sub-agents enough budget to work properly.
                # Old system: ~460K per agent. Don't starve them.
                allocated = min(int(available * 0.6 * weight), _MAX_AGENT_BUDGET)
                allocated = max(allocated, _DEFAULT_AGENT_BUDGET)
            # Pre-deduct: hold this allocation in reserved until report() arrives.
            # Cumulative per-agent so a single agent dispatched twice gets summed.
            self.reserved[agent_name] = self.reserved.get(agent_name, 0) + allocated
            logger.info(
                "Budget allocated %d tokens to %s (remaining: %d)",
                allocated,
                agent_name,
                self.remaining,
            )
            return allocated

    async def report(self, agent_name: str, tokens_used: int) -> None:
        """Record actual token usage after a sub-agent completes.

        Releases the agent's reservation and moves the actual usage into
        ``used``. Underruns return budget to the pool; overruns are recorded
        as-is and consume future capacity. Cumulative per-agent — calling
        this multiple times for the same agent name adds to the previously
        reported total.

        Args:
            agent_name: Name of the sub-agent that completed.
            tokens_used: Number of input tokens consumed by that run.
        """
        async with self._lock:
            self.used[agent_name] = self.used.get(agent_name, 0) + tokens_used
            # Release the reservation — the actual usage in `used` now
            # represents this agent's pool consumption.
            self.reserved.pop(agent_name, None)


# ---------------------------------------------------------------------------
# AgentToolExecutor — wraps a real executor, intercepts Brain meta-tools
# ---------------------------------------------------------------------------


class AgentToolExecutor(ToolExecutor):
    """Tool executor that intercepts ``dispatch_agent`` and ``dispatch_swarm``.

    Used by both Brain (meta-tools + dispatch) and explorer agents
    (code tools + optional dispatch when depth < max).
    """

    def __init__(
        self,
        inner_executor: ToolExecutor,
        agent_registry: Dict[str, Any],  # name → AgentConfig
        swarm_registry: Dict[str, Any],  # name → SwarmConfig
        agent_provider,  # AIProvider for sub-agents (explorer/Haiku)
        strong_provider=None,  # AIProvider for strong model (Sonnet)
        config: Optional[BrainExecutorConfig] = None,
        brain_config: Optional[Any] = None,  # BrainConfig
        trace_writer=None,
        event_sink: Optional[asyncio.Queue] = None,
        budget_manager: Optional[BrainBudgetManager] = None,
        qa_cache: Optional[Dict[str, str]] = None,
        llm_semaphore: Optional[asyncio.Semaphore] = None,
        # Legacy individual params — kept for backward compatibility.
        # When ``config`` is provided these are ignored.
        workspace_path: str = "",
        current_depth: int = 0,
        max_depth: int = 2,
        max_concurrent: int = 3,
        sub_agent_timeout: float = 300.0,
    ):
        self._inner = inner_executor
        self._agent_registry = agent_registry
        self._swarm_registry = swarm_registry
        self._agent_provider = agent_provider
        self._strong_provider = strong_provider or agent_provider
        self._brain_config = brain_config
        self._trace_writer = trace_writer
        self._event_sink = event_sink
        self._budget_manager = budget_manager
        self._qa_cache = qa_cache or {}
        self._llm_semaphore = llm_semaphore

        # Build config from individual params when not supplied directly
        if config is None:
            config = BrainExecutorConfig(
                workspace_path=workspace_path,
                current_depth=current_depth,
                max_depth=max_depth,
                max_concurrent=max_concurrent,
                sub_agent_timeout=sub_agent_timeout,
            )
        self._executor_config = config

        # Convenience accessors (read from config)
        self._workspace_path = config.workspace_path
        self._current_depth = config.current_depth
        self._max_depth = config.max_depth
        self._max_concurrent = config.max_concurrent
        self._sub_agent_timeout = config.sub_agent_timeout

        self._code_context: Optional[Dict[str, Any]] = None
        self._plan: Optional[Dict[str, Any]] = None

    async def execute(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        """Execute a tool. Intercepts create_plan, dispatch_agent, dispatch_swarm, transfer_to_brain, and dispatch_subagent."""
        if tool_name == "create_plan":
            return await self._create_plan(params)
        elif tool_name == "dispatch_agent":
            return await self._dispatch_agent(params)
        elif tool_name == "dispatch_swarm":
            return await self._dispatch_swarm(params)
        elif tool_name == "transfer_to_brain":
            return await self._transfer_to_brain(params)
        elif tool_name == "dispatch_subagent":
            return await self._dispatch_subagent(params)
        # All other tools (grep, read_file, ask_user, etc.) pass through
        return await self._inner.execute(tool_name, params)

    # -----------------------------------------------------------------
    # create_plan — record the Brain's investigation plan
    # -----------------------------------------------------------------

    async def _create_plan(self, params: Dict[str, Any]) -> ToolResult:
        """Record the Brain's investigation plan and emit it for UI display."""
        self._plan = {
            "mode": params.get("mode", "simple"),
            "reasoning": params.get("reasoning", ""),
            "agents": params.get("agents", []),
            "query_decomposition": params.get("query_decomposition", []),
            "risk": params.get("risk", ""),
            "fallback": params.get("fallback", ""),
        }
        logger.info(
            "[Brain] Plan created: mode=%s, agents=%s",
            self._plan["mode"],
            self._plan["agents"],
        )

        if self._event_sink:
            from app.workflow.engine import WorkflowEvent

            await self._event_sink.put(WorkflowEvent("plan_created", self._plan))

        return ToolResult(
            tool_name="create_plan",
            success=True,
            data={"status": "plan_recorded", **self._plan},
        )

    # -----------------------------------------------------------------
    # transfer_to_brain — hand off to a specialized brain
    # -----------------------------------------------------------------

    async def _transfer_to_brain(self, params: Dict[str, Any]) -> ToolResult:
        """Transfer control to a specialized Brain orchestrator (one-way handoff)."""
        brain_name = params.get("brain_name", "")
        valid_brains = {"pr_review"}

        if brain_name not in valid_brains:
            available = ", ".join(sorted(valid_brains))
            return ToolResult(
                tool_name="transfer_to_brain",
                success=False,
                error=f"Unknown brain '{brain_name}'. Available: {available}",
            )

        logger.info("[Brain] Transferring to specialized brain '%s'", brain_name)

        if self._event_sink:
            from app.workflow.engine import WorkflowEvent

            await self._event_sink.put(
                WorkflowEvent(
                    "transfer_initiated",
                    {
                        "brain": brain_name,
                        "params": params,
                    },
                )
            )

        return ToolResult(
            tool_name="transfer_to_brain",
            success=True,
            data={
                "transfer": True,
                "brain": brain_name,
                "params": params,
            },
        )

    # -----------------------------------------------------------------
    # dispatch_agent — run one agent-as-tool
    # -----------------------------------------------------------------

    def _build_dynamic_config(self, params: Dict[str, Any]) -> Any:
        """Build an ephemeral AgentConfig from dynamic dispatch params."""
        from app.workflow.models import AgentConfig, AgentLimits

        skill = params.get("skill", "")
        return AgentConfig(
            name=f"dynamic_{skill or 'explorer'}",
            description=params.get("perspective", "Dynamic investigation agent"),
            model=params.get("model", "explorer"),
            instructions=params.get("perspective", ""),
            skill=skill,
            tools=params.get("tools", []),  # list format → tool_list property returns it directly
            limits=AgentLimits(
                max_iterations=params.get("max_iterations", 20),
                budget_tokens=params.get("budget_tokens", 300_000),
                evidence_retries=1,
            ),
        )

    # -----------------------------------------------------------------
    # dispatch_subagent — PR Brain v2's coordinator primitive
    # -----------------------------------------------------------------

    async def _dispatch_subagent(self, params: Dict[str, Any]) -> ToolResult:
        """Dispatch a scope-bounded sub-agent with exactly 3 falsifiable checks.

        The sub-agent uses the ``pr_subagent_checks`` template (see
        ``config/agents/pr_subagent_checks.md``) and returns a structured
        JSON response: per-check verdicts + findings with ``severity: null``
        + optional unexpected_observations.

        Depth tracking: a ContextVar tracks how deep we are in the dispatch
        tree. Brain = depth 0, sub-agent = depth 1, sub-sub-agent = depth 2
        (only allowed if parent set ``may_subdispatch=true``). Depth-2
        agents cannot sub-dispatch further — hard wall returned as an error
        the Brain can see.
        """

        # Depth wall — hard. Brain is depth 0; sub-agent at depth 1 may
        # subdispatch only if its parent set may_subdispatch=true; a sub-
        # sub-agent at depth 2 is never allowed to subdispatch.
        if self._current_depth >= 2:
            return ToolResult(
                tool_name="dispatch_subagent",
                success=False,
                error=(
                    "dispatch_subagent rejected: you are at recursion depth "
                    f"{self._current_depth} (≥2 is the hard wall). Answer "
                    "the checks directly without further sub-dispatch."
                ),
            )

        scope = params.get("scope", [])
        checks = params.get("checks") or []
        role = (params.get("role") or "").strip().lower() or None
        direction_hint = params.get("direction_hint") or ""
        success_criteria = params.get("success_criteria", "")
        context = params.get("context", "")
        may_subdispatch = params.get("may_subdispatch", False)
        model_tier = params.get("model_tier", "explorer")

        # Validate the two dispatch modes: at least one of {checks, role}.
        if not checks and not role:
            return ToolResult(
                tool_name="dispatch_subagent",
                success=False,
                error=(
                    "dispatch_subagent requires either 'checks' (3 specific "
                    "questions) OR 'role' (factory reviewer e.g. 'security'). "
                    "Got neither."
                ),
            )
        if checks and len(checks) != 3:
            return ToolResult(
                tool_name="dispatch_subagent",
                success=False,
                error=f"dispatch_subagent requires exactly 3 checks when 'checks' is set (got {len(checks)}).",
            )
        if role and role not in _VALID_FACTORY_ROLES:
            return ToolResult(
                tool_name="dispatch_subagent",
                success=False,
                error=(
                    f"Unknown role '{role}'. Available roles in "
                    f"config/agent_factory/: "
                    f"{sorted(_VALID_FACTORY_ROLES)}"
                ),
            )
        if not 1 <= len(scope) <= 5:
            return ToolResult(
                tool_name="dispatch_subagent",
                success=False,
                error=f"dispatch_subagent scope must have 1-5 files (got {len(scope)}).",
            )

        # Build scope block (shared by both modes).
        scope_lines = []
        for s in scope:
            if isinstance(s, dict):
                f = s.get("file", "")
                start = s.get("start")
                end = s.get("end")
                if start and end:
                    scope_lines.append(f"- {f}:{start}-{end}")
                else:
                    scope_lines.append(f"- {f}")
            else:
                scope_lines.append(f"- {s}")
        scope_block = "\n".join(scope_lines)

        mode_label = (
            "combined" if (role and checks) else ("role" if role else "checks")
        )
        logger.info(
            "[dispatch_subagent] mode=%s role=%s checks=%d scope_files=%d "
            "direction_hint=%r depth=%d",
            mode_label,
            role or "-",
            len(checks) if checks else 0,
            len(scope),
            (direction_hint[:60] + "...") if direction_hint and len(direction_hint) > 60 else direction_hint,
            self._current_depth,
        )

        # P4 — Persist this dispatch decision into the scratchpad's plan
        # memory. The coordinator's in-context plan tends to drift across
        # replan rounds as the conversation grows; the plan recap we weave
        # into later tool-result returns (see below) restores it from an
        # authoritative source. Depth-0 dispatches only — sub-agent-level
        # dispatches (depth 1) would pollute the coordinator's recap.
        plan_dispatch_index: Optional[int] = None
        if self._current_depth == 0:
            try:
                from app.scratchpad import current_factstore

                _store = current_factstore()
                if _store is not None:
                    plan_dispatch_index = _store.count_plan_entries() + 1
                    _store.put_plan_entry(
                        dispatch_index=plan_dispatch_index,
                        mode=mode_label,
                        role=role,
                        scope=scope_block.replace("\n", " | ")[:500],
                        success_criteria=success_criteria,
                        reason=(direction_hint or context or None),
                    )
            except Exception as exc:
                logger.debug("[P4] plan_memory put failed (non-fatal): %s", exc)
                plan_dispatch_index = None

        if role:
            # Role mode: compose bespoke system prompt from factory +
            # PR-specific context, dispatch via dynamic-compose
            # _dispatch_agent (not template mode).
            role_template = _load_role_template(role)
            if role_template is None:
                return ToolResult(
                    tool_name="dispatch_subagent",
                    success=False,
                    error=(
                        f"Role '{role}' template not found at "
                        f"config/agent_factory/{role}.md or failed to "
                        f"parse. Falling back to checks mode is the "
                        f"coordinator's responsibility."
                    ),
                )

            composed_perspective = _compose_role_system_prompt(
                role=role,
                role_template=role_template,
                scope_block=scope_block,
                direction_hint=direction_hint or None,
                checks=checks or None,
                brain_context=context or None,
                may_subdispatch=(may_subdispatch and self._current_depth == 0),
            )

            # Derive tools — use role's tools_hint if provided, else a safe
            # read-only default.
            tools_hint = (
                role_template["frontmatter"].get("tools_hint")
                or role_template["frontmatter"].get("tools")
                or [
                    "grep", "read_file", "find_symbol", "find_references",
                    "git_diff", "git_show", "file_outline",
                ]
            )
            # P10 — Coordinator's explicit `model_tier="strong"` overrides
            # the role template's default (most roles default to explorer
            # to keep cost low). Otherwise, honour the role's model_hint.
            if model_tier == "strong":
                model_hint = "strong"
            else:
                model_hint = role_template["frontmatter"].get(
                    "model_hint", model_tier
                )

            # Dispatch in dynamic mode. The role lens lives in the
            # perspective; we pass a terse task query since the
            # perspective already frames the task.
            task_query = (
                f"Review the code in your scope through your {role} lens. "
                f"{success_criteria}"
            )
            delegated_params = {
                "perspective": composed_perspective,
                "tools": tools_hint,
                "model": model_hint,
                "query": task_query,
                "budget_tokens": params.get("budget_tokens", 120_000),
                "budget_weight": 1.0,
                "_subagent_kind": f"pr_role_{role}",
            }
            result = await self._dispatch_agent(delegated_params)
        else:
            # Checks mode (original v2 behaviour): generic pr_subagent_checks.
            template = self._agent_registry.get("pr_subagent_checks")
            if template is None:
                return ToolResult(
                    tool_name="dispatch_subagent",
                    success=False,
                    error=(
                        "Agent template 'pr_subagent_checks' missing from "
                        "registry. Ensure config/agents/pr_subagent_checks.md "
                        "exists and load_agent_registry() picked it up."
                    ),
                )

            check_lines = "\n".join(f"{i+1}. {c}" for i, c in enumerate(checks))
            sub_query_parts = [
                "## Investigation scope",
                "",
                scope_block,
                "",
                "## The 3 checks (answer all, in order)",
                "",
                check_lines,
                "",
                "## Success criteria",
                "",
                success_criteria,
            ]
            if context:
                sub_query_parts.extend(["", "## Context from the Brain", "", context])

            sub_query_parts.extend([
                "",
                "## Your output",
                "",
                "Emit the JSON schema from your system prompt as your final "
                "message (wrapped in a ```json block or as the turn body). "
                "`severity` MUST be null in every finding — the Brain classifies "
                "severity, not you.",
            ])

            if may_subdispatch and self._current_depth == 0:
                sub_query_parts.extend([
                    "",
                    "## Sub-dispatch permitted (depth 2 hard wall)",
                    "",
                    "The Brain set `may_subdispatch=true`. If a check truly "
                    "requires subdivision, you may call `dispatch_subagent` ONCE "
                    "to delegate a narrower investigation. Its result must fold "
                    "into your own 3 verdicts. Your sub-agents cannot sub-dispatch.",
                ])

            sub_query = "\n".join(sub_query_parts)

            delegated_params = {
                "template": "pr_subagent_checks",
                "query": sub_query,
                "model": model_tier,
                "budget_tokens": params.get("budget_tokens", 150_000),
                "budget_weight": 1.0,
            }
            result = await self._dispatch_agent(delegated_params)

        if not result.success:
            return ToolResult(
                tool_name="dispatch_subagent",
                success=False,
                error=f"sub-agent dispatch failed: {result.error}",
            )

        # Parse the sub-agent's final answer as JSON. Tolerant to ```json fences.
        condensed = result.data or {}
        raw_answer = condensed.get("answer") or condensed.get("final_answer") or ""
        parsed = _parse_subagent_json(raw_answer)

        # Shape guard — if the sub-agent didn't produce the expected shape,
        # return the raw answer with a warning so the Brain can still act.
        if parsed is None:
            logger.warning(
                "dispatch_subagent: worker did not emit parseable JSON. "
                "Returning raw answer (%d chars).",
                len(raw_answer),
            )
            return ToolResult(
                tool_name="dispatch_subagent",
                success=True,
                data={
                    "checks": [],
                    "findings": [],
                    "unexpected_observations": [],
                    "raw_answer": raw_answer[:4000],
                    "shape_warning": (
                        "Sub-agent did not return structured JSON — raw answer "
                        "included for Brain inspection."
                    ),
                    "iterations": condensed.get("iterations", 0),
                    "total_input_tokens": condensed.get("total_input_tokens", 0),
                    "total_output_tokens": condensed.get("total_output_tokens", 0),
                },
            )

        # Enforce severity=null on findings (contract).
        for f in parsed.get("findings", []) or []:
            if isinstance(f, dict):
                f["severity"] = None
                # Tag role-mode findings for downstream dedup/synthesis.
                # Allows coordinator + synthesis to group by role lens,
                # track which specialist surfaced which finding, and
                # attribute severity_hint to the right source.
                if role:
                    f.setdefault("_dispatched_by", f"role={role}")

        parsed.setdefault("checks", [])
        parsed.setdefault("findings", [])
        parsed.setdefault("unexpected_observations", [])
        parsed["iterations"] = condensed.get("iterations", 0)
        parsed["total_input_tokens"] = condensed.get("total_input_tokens", 0)
        parsed["total_output_tokens"] = condensed.get("total_output_tokens", 0)
        parsed["files_accessed"] = condensed.get("files_accessed", [])

        # P4 — From dispatch #3 onward, surface a compact plan recap in the
        # tool result. Coordinator's in-context memory of "what I already
        # dispatched and why" degrades as the loop grows; recap pins it.
        if plan_dispatch_index is not None and plan_dispatch_index >= 3:
            try:
                from app.scratchpad import current_factstore

                _store = current_factstore()
                if _store is not None:
                    entries = _store.iter_plan_entries()
                    recap_lines: List[str] = [
                        f"Plan recap — {len(entries)} dispatches so far:",
                    ]
                    for e in entries:
                        role_label = f" role={e.role}" if e.role else ""
                        reason_snippet = (
                            f" — {e.reason[:80]}" if e.reason else ""
                        )
                        recap_lines.append(
                            f"  #{e.dispatch_index} [{e.mode}{role_label}] "
                            f"{e.scope[:120]}{reason_snippet}"
                        )
                    parsed["_plan_recap"] = "\n".join(recap_lines)
            except Exception as exc:
                logger.debug("[P4] plan recap failed (non-fatal): %s", exc)

        return ToolResult(
            tool_name="dispatch_subagent",
            success=True,
            data=parsed,
        )

    async def _dispatch_agent(self, params: Dict[str, Any]) -> ToolResult:
        """Run a single specialist agent and return condensed findings.

        Supports two modes:
        - Template mode: ``template`` (or legacy ``agent_name``) looks up a
          pre-defined agent from the registry.
        - Dynamic mode: ``tools`` + optional ``perspective``, ``skill``,
          ``model``, ``budget_tokens`` compose an agent on the fly.
        """
        query = params.get("query", "")
        weight = params.get("budget_weight", 1.0)

        # Depth check
        if self._current_depth >= self._max_depth:
            return ToolResult(
                tool_name="dispatch_agent",
                success=False,
                error=f"Max agent depth ({self._max_depth}) reached. "
                f"Use your available code tools to investigate directly.",
            )

        # Resolve agent config: template mode vs dynamic mode
        template = params.get("template") or params.get("agent_name")
        if template:
            # Template mode: lookup in registry (existing behavior)
            agent_config = self._agent_registry.get(template)
            if agent_config is None:
                available = ", ".join(sorted(self._agent_registry.keys()))
                return ToolResult(
                    tool_name="dispatch_agent",
                    success=False,
                    error=f"Unknown agent template '{template}'. Available: {available}",
                )
            agent_name = template
            resolved_model = getattr(agent_config, "model", "explorer") or "explorer"
        elif params.get("tools"):
            # Dynamic mode: Brain composes the agent on the fly
            agent_config = self._build_dynamic_config(params)
            agent_name = agent_config.name
            resolved_model = params.get("model", "explorer")
        else:
            return ToolResult(
                tool_name="dispatch_agent",
                success=False,
                error="Either 'template' or 'tools' must be provided. "
                "Use template= for pre-defined agents, or tools= to "
                "compose an agent dynamically.",
            )

        logger.info(
            "[Brain] Dispatching agent '%s' (depth=%d, mode=%s, model=%s, query='%s')",
            agent_name,
            self._current_depth + 1,
            "template" if template else "dynamic",
            resolved_model,
            query[:80],
        )

        # Emit dispatch event for UI
        if self._event_sink:
            from app.workflow.engine import WorkflowEvent

            await self._event_sink.put(
                WorkflowEvent(
                    "agent_dispatched",
                    {
                        "agent_name": agent_name,
                        "query": query,
                        "depth": self._current_depth + 1,
                        "mode": "template" if template else "dynamic",
                    },
                )
            )

        # Select provider based on resolved model
        provider = self._strong_provider if resolved_model == "strong" else self._agent_provider

        # Allocate budget — respect agent's own budget_tokens as cap
        from .budget import BudgetConfig  # lazy: avoids circular import (brain ↔ budget)

        if self._budget_manager:
            pool_tokens = await self._budget_manager.allocate(agent_name, weight)
            agent_cap = agent_config.limits.budget_tokens
            budget_tokens = min(pool_tokens, agent_cap) if agent_cap else pool_tokens
        else:
            budget_tokens = agent_config.limits.budget_tokens

        # Build sub-executor (recursive: depth + 1)
        sub_executor = AgentToolExecutor(
            inner_executor=self._inner,
            agent_registry=self._agent_registry,
            swarm_registry=self._swarm_registry,
            agent_provider=self._agent_provider,
            strong_provider=self._strong_provider,
            config=BrainExecutorConfig(
                workspace_path=self._workspace_path,
                current_depth=self._current_depth + 1,
                max_depth=self._max_depth,
                max_concurrent=self._max_concurrent,
                sub_agent_timeout=self._sub_agent_timeout,
            ),
            trace_writer=self._trace_writer,
            event_sink=self._event_sink,
            budget_manager=self._budget_manager,
            qa_cache=self._qa_cache,
        )
        # Propagate code_context to sub-executors
        sub_executor._code_context = self._code_context

        # Build the agent's tool list: core_tools + agent-specific + signal_blocker.
        # This bypasses keyword classification (Brain already decided which
        # agent to dispatch — we don't want the sub-agent re-classifying).
        agent_tool_names = list(agent_config.tool_list)
        if self._brain_config:
            core = getattr(self._brain_config, "core_tools", [])
            agent_tool_names = list(set(core + agent_tool_names))
        # Sub-agents can signal Brain for direction mid-execution
        agent_tool_names.append("signal_blocker")

        # Build and run sub-agent (4-layer prompt architecture)
        from .config import AgentLoopConfig
        from .service import AgentLoopService  # lazy: avoids circular import (brain ↔ service)

        svc = AgentLoopService(
            provider=provider,
            config=AgentLoopConfig(
                max_iterations=agent_config.limits.max_iterations,
                max_evidence_retries=1,
                budget_config=BudgetConfig(max_input_tokens=budget_tokens),
                is_sub_agent=True,
                perspective=agent_config.instructions,
                forced_tools=agent_tool_names,
                agent_identity={
                    "name": agent_config.name,
                    "description": getattr(agent_config, "description", "") or "",
                    "instructions": agent_config.instructions,
                    "skill": getattr(agent_config, "skill", "") or "",
                },
                forced_skill=getattr(agent_config, "skill", "") or "",
            ),
            tool_executor=sub_executor,
            trace_writer=self._trace_writer,
            llm_semaphore=self._llm_semaphore,
        )
        # Per-agent overrides from template
        if agent_config.limits.temperature is not None:
            svc._temperature = agent_config.limits.temperature
        if hasattr(agent_config, "quality"):
            svc._quality_config = agent_config.quality

        # 4-layer: query stays clean — agent identity is in system prompt (Layer 1),
        # not in the user message (Layer 4).

        start = time.monotonic()
        try:
            # Stream events to event_sink for real-time UI updates
            if self._event_sink:
                result = None
                from .service import AgentResult  # lazy: avoids circular import (brain ↔ service)

                agent_result = AgentResult()
                async for event in svc.run_stream(
                    query=query,
                    workspace_path=self._workspace_path,
                    code_context=self._code_context,
                ):
                    # Handle signal_blocker: respond from Brain's Q&A cache or with guidance
                    if event.kind == "signal_blocker":
                        from .signal_blocker import respond_to_signal

                        sig_session = event.data.get("session_id", "")
                        sig_reason = event.data.get("reason", "")
                        sig_options = event.data.get("options", [])
                        # Check Q&A cache first
                        response = None
                        for key, val in self._qa_cache.items():
                            if key.lower() in sig_reason.lower():
                                response = val
                                break
                        if not response and sig_options:
                            response = f"Choose the first option: {sig_options[0]}"
                        elif not response:
                            response = "Continue with your best judgment based on the evidence."
                        respond_to_signal(sig_session, response)
                        logger.info(
                            "[Brain] Responded to signal from %s: %s → %s", agent_name, sig_reason[:50], response[:50]
                        )
                        continue  # don't forward signal_blocker to UI

                    # Forward agent events with agent_name tag
                    await self._event_sink.put(
                        __import__("app.workflow.engine", fromlist=["WorkflowEvent"]).WorkflowEvent(
                            event.kind,
                            {"agent_name": agent_name, **event.data},
                        )
                    )
                    if event.kind in ("done", "error"):
                        agent_result.answer = event.data.get("answer", "")
                        agent_result.tool_calls_made = event.data.get("tool_calls_made", 0)
                        agent_result.iterations = event.data.get("iterations", 0)
                        agent_result.duration_ms = event.data.get("duration_ms", 0)
                        agent_result.budget_summary = event.data.get("budget_summary")
                        agent_result.error = event.data.get("error")
                        # Collect thinking steps
                        raw_steps = event.data.get("thinking_steps", [])
                        from .service import ThinkingStep

                        agent_result.thinking_steps = [
                            ThinkingStep(**s) if isinstance(s, dict) else s for s in raw_steps
                        ]
                        if event.kind == "context_chunk":
                            from .service import ContextChunk

                            agent_result.context_chunks.append(ContextChunk(**event.data))
                result = agent_result
            else:
                result = await asyncio.wait_for(
                    svc.run(query=query, workspace_path=self._workspace_path, code_context=self._code_context),
                    timeout=self._sub_agent_timeout,
                )

            elapsed = (time.monotonic() - start) * 1000

            # Report budget usage
            if self._budget_manager and result.budget_summary:
                tokens = result.budget_summary.get("total_input_tokens", 0)
                await self._budget_manager.report(agent_name, tokens)

            # Emit completion event
            if self._event_sink:
                from app.workflow.engine import WorkflowEvent

                findings = condense_result(result)
                # Budget exhaustion = agent finished with useful data, not a failure
                has_answer = bool(result.answer and result.answer.strip())
                status = "done" if has_answer else ("error" if result.error else "done")
                await self._event_sink.put(
                    WorkflowEvent(
                        "agent_complete",
                        {
                            "agent_name": agent_name,
                            "status": status,
                            "confidence": findings["confidence"],
                            "duration_ms": elapsed,
                        },
                    )
                )

            logger.info(
                "[Brain] Agent '%s' completed in %.0fms (iterations=%d, tools=%d)",
                agent_name,
                elapsed,
                result.iterations,
                result.tool_calls_made,
            )

            condensed = condense_result(result)
            # Add quality metadata from agent template
            condensed["need_brain_review"] = agent_config.quality.need_brain_review
            # Add token usage for upstream reporting
            if result.budget_summary:
                condensed["total_input_tokens"] = result.budget_summary.get("total_input_tokens", 0)
                condensed["total_output_tokens"] = result.budget_summary.get("total_output_tokens", 0)
            return ToolResult(tool_name="dispatch_agent", success=True, data=condensed)

        except TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("[Brain] Agent '%s' timed out after %.0fms", agent_name, elapsed)
            if self._event_sink:
                from app.workflow.engine import WorkflowEvent

                await self._event_sink.put(
                    WorkflowEvent(
                        "agent_complete",
                        {
                            "agent_name": agent_name,
                            "status": "timeout",
                            "confidence": "low",
                            "duration_ms": elapsed,
                        },
                    )
                )
            return ToolResult(
                tool_name="dispatch_agent",
                success=True,  # partial success — Brain can still use whatever was found
                data={
                    "answer": "",
                    "error": "Agent timed out",
                    "confidence": "low",
                    "files_accessed": [],
                    "tools_summary": [],
                    "gaps_identified": [],
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("[Brain] Agent '%s' failed: %s", agent_name, exc)
            if self._event_sink:
                from app.workflow.engine import WorkflowEvent

                await self._event_sink.put(
                    WorkflowEvent(
                        "agent_complete",
                        {
                            "agent_name": agent_name,
                            "status": "error",
                            "confidence": "low",
                            "duration_ms": elapsed,
                        },
                    )
                )
            return ToolResult(
                tool_name="dispatch_agent",
                success=False,
                error=f"Agent '{agent_name}' failed: {exc}",
            )

    # -----------------------------------------------------------------
    # dispatch_swarm — run multiple agents in parallel
    # -----------------------------------------------------------------

    async def _dispatch_swarm(self, params: Dict[str, Any]) -> ToolResult:
        """Run a predefined group of agents in parallel."""
        swarm_name = params.get("swarm_name", "")
        query = params.get("query", "")

        # Only predefined swarms allowed
        preset = self._swarm_registry.get(swarm_name)
        if preset is None:
            available = ", ".join(sorted(self._swarm_registry.keys()))
            return ToolResult(
                tool_name="dispatch_swarm",
                success=False,
                error=f"Unknown swarm '{swarm_name}'. Available: {available}. "
                f"For single-agent tasks, use dispatch_agent instead.",
            )
        agent_names = preset.agents
        logger.info("[Brain] Dispatching swarm '%s' (%d agents): %s", swarm_name, len(agent_names), agent_names)

        if not agent_names:
            return ToolResult(
                tool_name="dispatch_swarm",
                success=False,
                error="No agents specified for swarm dispatch.",
            )

        # Emit swarm dispatch event for UI
        if self._event_sink:
            from app.workflow.engine import WorkflowEvent

            await self._event_sink.put(
                WorkflowEvent(
                    "swarm_dispatched",
                    {
                        "swarm_name": swarm_name or "custom",
                        "agents": agent_names,
                        "query": query,
                    },
                )
            )

        # Run agents in parallel with concurrency limit
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def run_one(name: str) -> Dict[str, Any]:
            # Prepend agent's focus directive to differentiate exploration paths
            agent_config = self._agent_registry.get(name)
            agent_focus = getattr(agent_config, "focus", "") if agent_config else ""
            agent_query = f"{agent_focus}\n\n{query}" if agent_focus else query
            async with semaphore:
                result = await self._dispatch_agent(
                    {
                        "agent_name": name,
                        "query": agent_query,
                    }
                )
                return {"agent": name, **(result.data if result.data else {"error": result.error})}

        raw_results = await asyncio.gather(
            *[run_one(name) for name in agent_names],
            return_exceptions=True,
        )

        # Process results, handling partial failures
        findings = []
        for name, result in zip(agent_names, raw_results):
            if isinstance(result, Exception):
                logger.warning("[Brain] Swarm agent '%s' raised: %s", name, result)
                findings.append(
                    {
                        "agent": name,
                        "answer": "",
                        "error": str(result),
                        "confidence": "low",
                    }
                )
            else:
                findings.append(result)

        # Budget exhaustion is not a real failure — agent still produced useful findings.
        # Only count as failed if there's no answer at all.
        succeeded = sum(1 for f in findings if f.get("answer"))
        logger.info("[Brain] Swarm complete: %d/%d agents succeeded", succeeded, len(findings))

        # Include synthesis guide so Brain knows how to combine findings
        synthesis_guide = getattr(preset, "synthesis_guide", "") if preset else ""

        return ToolResult(
            tool_name="dispatch_swarm",
            success=succeeded > 0,
            data={
                "agents": findings,
                "swarm_name": swarm_name or "custom",
                "synthesis_guide": synthesis_guide,
            },
        )
