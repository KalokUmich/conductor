"""Agent loop service — drives code navigation via LLM + tools.

The loop sends the user query to the LLM along with tool definitions.
The LLM decides which tools to call, the loop executes them and feeds
results back, repeating until the LLM produces a final answer or the
iteration limit is reached.

Concurrency notes:
  * ``chat_with_tools()`` is synchronous — each LLM call is offloaded to
    a thread via ``asyncio.to_thread()`` so it never blocks the event loop.
  * When the LLM returns multiple tool calls in one turn, the tools are
    executed concurrently via ``asyncio.gather()``.
  * ``run_stream()`` is an async generator that yields ``AgentEvent`` objects
    suitable for SSE streaming.  ``run()`` is a thin wrapper that collects
    the stream into a single ``AgentResult``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.ai_provider.base import AIProvider, ToolUseResponse
from app.code_tools.executor import LocalToolExecutor, ToolExecutor
from app.code_tools.output_policy import apply_policy
from app.code_tools.schemas import TOOL_DEFINITIONS, filter_tools

from .budget import BudgetConfig, BudgetController, BudgetSignal, IterationMetrics
# completeness check removed — Brain handles via need_brain_review
from .evidence import check_evidence
from .prompts import _read_key_docs, build_system_prompt, scan_workspace_layout, scan_workspace_risk
from .query_classifier import QUERY_TYPES, QueryClassification, classify_query, classify_query_with_llm
from .trace import IterationTrace, SessionTrace, ToolCallTrace, TraceWriter
from app.workflow.observability import observe

logger = logging.getLogger(__name__)


@dataclass
class ContextChunk:
    """A piece of code context collected during the agent loop."""
    file_path: str
    content: str
    start_line: int = 0
    end_line: int = 0
    relevance: str = ""
    source_tool: str = ""


@dataclass
class AgentEvent:
    """A streaming event emitted during the agent loop.

    ``kind`` is one of:
      * ``thinking``      — LLM reasoning text (mid-loop, before tool calls)
      * ``tool_call``     — tool invocation about to start
      * ``tool_result``   — tool execution completed
      * ``context_chunk`` — a piece of code context collected
      * ``done``          — final answer produced
      * ``error``         — unrecoverable error
    """
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ThinkingStep:
    """A single step in the agent's thinking/investigation process."""
    kind: str          # "thinking", "tool_call", "tool_result"
    iteration: int = 0
    text: str = ""     # thinking text or action description
    tool: str = ""     # tool name (for tool_call / tool_result)
    params: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""  # tool result summary
    success: bool = True


@dataclass
class AgentResult:
    """Result of an agent loop run."""
    answer: str = ""
    context_chunks: List[ContextChunk] = field(default_factory=list)
    thinking_steps: List[ThinkingStep] = field(default_factory=list)
    tool_calls_made: int = 0
    iterations: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None
    budget_summary: Optional[Dict[str, Any]] = None


class AgentLoopService:
    """Runs the LLM agent loop for code intelligence queries."""

    def __init__(
        self,
        provider: AIProvider,
        max_iterations: int = 40,
        tool_executor: Optional[ToolExecutor] = None,
        budget_config: Optional[BudgetConfig] = None,
        trace_writer: Optional[TraceWriter] = None,
        classifier_provider: Optional[AIProvider] = None,
        explorer_provider: Optional[AIProvider] = None,
        _is_sub_agent: bool = False,
        llm_semaphore: Optional[asyncio.Semaphore] = None,
        max_evidence_retries: int = 2,
        workflow_config=None,
        workflow_route_name: str = "",
        verifier_provider: Optional[AIProvider] = None,
        perspective: str = "",
        interactive: bool = False,
        _is_brain: bool = False,
        brain_system_prompt: str = "",
        forced_tools: Optional[List[str]] = None,
        agent_identity: Optional[Dict[str, str]] = None,
    ) -> None:
        self._provider = provider
        self._max_iterations = max_iterations
        self._tool_executor = tool_executor
        self._budget_config = budget_config
        self._trace_writer = trace_writer
        self._classifier_provider = classifier_provider
        self._explorer_provider = explorer_provider
        self._is_sub_agent = _is_sub_agent
        self._llm_semaphore = llm_semaphore
        self._max_evidence_retries = max_evidence_retries
        self._workflow_config = workflow_config
        self._workflow_route_name = workflow_route_name
        self._verifier_provider = verifier_provider
        self._perspective = perspective
        self._interactive = interactive and not _is_sub_agent
        self._is_brain = _is_brain
        self._brain_system_prompt = brain_system_prompt
        self._forced_tools = forced_tools
        self._agent_identity = agent_identity  # 4-layer: per-agent identity from .md
        self._temperature = None  # set per-agent via forced_tools dispatch
        self._quality_config = None  # set per-agent via brain dispatch
        self._forced_strategy = ""  # strategy key override (Layer 3 strategy)
        self._forced_skill = ""    # investigation skill override (Layer 3 skill)

    @observe(name="agent_loop")
    async def run(
        self,
        query: str,
        workspace_path: str,
        code_context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """Execute the agent loop (non-streaming).

        Args:
            query:          Natural language question about the codebase.
            workspace_path: Absolute path to the workspace root.
            code_context:   Optional code snippet dict for snippet-based queries.

        Returns:
            AgentResult with the answer and collected context.
        """
        result = AgentResult()
        async for event in self.run_stream(query, workspace_path, code_context=code_context):
            if event.kind == "context_chunk":
                result.context_chunks.append(ContextChunk(**event.data))
            elif event.kind in ("done", "error"):
                result.answer = event.data.get("answer", result.answer)
                result.tool_calls_made = event.data.get("tool_calls_made", 0)
                result.iterations = event.data.get("iterations", 0)
                result.duration_ms = event.data.get("duration_ms", 0.0)
                result.budget_summary = event.data.get("budget_summary")
                # Collect thinking steps
                raw_steps = event.data.get("thinking_steps", [])
                result.thinking_steps = [
                    ThinkingStep(
                        kind=s.get("kind", ""),
                        iteration=s.get("iteration", 0),
                        text=s.get("text", ""),
                        tool=s.get("tool", ""),
                        params=s.get("params", {}),
                        summary=s.get("summary", ""),
                        success=s.get("success", True),
                    )
                    for s in raw_steps
                ]
                if event.kind == "error" or event.data.get("error"):
                    result.error = event.data.get("error")
        return result

    @observe(name="agent_loop_stream")
    async def run_stream(
        self,
        query: str,
        workspace_path: str,
        code_context: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute the agent loop, yielding events for SSE streaming.

        Yields ``AgentEvent`` instances as the loop progresses so callers
        can forward them to clients via Server-Sent Events.

        Parameters
        ----------
        code_context:
            Optional code snippet dict (code, file_path, language, start_line, end_line).
            When present, injected prominently into the system prompt.
        """
        start = time.monotonic()

        # Session trace — records per-iteration metrics for offline analysis
        trace = SessionTrace(
            session_id=uuid.uuid4().hex[:16],
            query=query,
            workspace_path=workspace_path,
        )
        trace.begin()

        # Emit session ID so the client can correlate ask_user answers
        if self._interactive:
            yield AgentEvent(kind="session", data={"session_id": trace.session_id})

        # Scan workspace for context (layout, docs, risk) so the LLM
        # knows the project structure from iteration 1.
        layout_task = asyncio.to_thread(scan_workspace_layout, workspace_path)
        docs_task = asyncio.to_thread(_read_key_docs, workspace_path)
        risk_task = asyncio.to_thread(scan_workspace_risk, workspace_path)
        layout, project_docs, risk_context = await asyncio.gather(
            layout_task, docs_task, risk_task,
        )

        # Branch 0: Brain mode — skip classification, use Brain tools + prompt
        if self._is_brain:
            from app.code_tools.schemas import get_brain_tool_definitions
            system = self._brain_system_prompt
            # Inject code_context so Brain sees the snippet when deciding dispatch
            if code_context:
                lang = code_context.get("language", "")
                system += (
                    "\n\n## Code Under Discussion\n\n"
                    "The user is asking about this specific code snippet. "
                    "Pass the full query (including this context) to the dispatched agent.\n\n"
                    f"`{code_context['file_path']}` "
                    f"(lines {code_context.get('start_line', '?')}–{code_context.get('end_line', '?')}):\n\n"
                    f"```{lang}\n{code_context['code']}\n```\n"
                )
            active_tools = get_brain_tool_definitions()
            classification = QueryClassification(
                query_type="brain",
                budget_level="high",
                suggested_token_budget=self._budget_config.max_input_tokens if self._budget_config else 100_000,
                tool_set=[],
            )
            logger.info("Brain mode: %d meta-tools, prompt=%d chars",
                        len(active_tools), len(system))

            # Emit classify event for UI
            yield AgentEvent(kind="classify", data={
                "query_type": "brain",
                "budget_level": "high",
                "tools_active": len(active_tools),
                "tools_total": len(active_tools),
            })

            # Brain is always interactive
            self._interactive = True

            messages = self._initial_messages(query, classification)

        # Branch 0.5: Dispatched by Brain with forced_tools — skip classification entirely
        elif self._forced_tools:
            classification = QueryClassification(
                query_type="brain_dispatched",
                budget_level="medium",
                suggested_token_budget=self._budget_config.max_input_tokens if self._budget_config else 300_000,
                tool_set=self._forced_tools,
            )
            logger.info("Brain-dispatched agent: forced_tools=%d, skipping classification",
                        len(self._forced_tools))

        # Branch 1: Running under WorkflowEngine — use the route's classification directly
        elif self._workflow_route_name:
            route_type = (
                self._workflow_route_name
                or getattr(self._workflow_config, "category", None)
                or getattr(self._workflow_config, "name", "general")
            )
            if route_type in QUERY_TYPES:
                spec = QUERY_TYPES[route_type]
                classification = QueryClassification(
                    query_type=route_type,
                    budget_level=spec["budget_level"],
                    suggested_token_budget=spec["suggested_token_budget"],
                    tool_set=spec["tools"],
                )
            else:
                classification = classify_query(query)
                classification.query_type = route_type
            logger.info("Using workflow-driven classification: type=%s (route=%s)",
                        classification.query_type, self._workflow_route_name)

        # Branch 2: Standalone mode — full classification (LLM preferred, keyword fallback)
        else:
            if self._classifier_provider:
                try:
                    classification = await classify_query_with_llm(
                        query, self._classifier_provider,
                    )
                except Exception as exc:
                    logger.warning("LLM classifier failed, falling back to keywords: %s", exc)
                    classification = classify_query(query)
            else:
                classification = classify_query(query)
        is_high_level = classification.query_type in ("architecture_question", "business_flow_tracing")

        # Build system prompt and tool list (skip for Brain — already set above)
        if not self._is_brain:
            if self._agent_identity:
                # 4-layer path: Brain-dispatched sub-agent with per-agent identity
                from .prompts import build_sub_agent_system_prompt
                system = build_sub_agent_system_prompt(
                    agent_name=self._agent_identity["name"],
                    agent_description=self._agent_identity["description"],
                    agent_instructions=self._agent_identity["instructions"],
                    workspace_path=workspace_path,
                    workspace_layout=layout,
                    project_docs=project_docs,
                    max_iterations=self._max_iterations,
                    risk_context=risk_context,
                    code_context=code_context,
                    strategy_key=self._forced_strategy or None,
                    skill_key=self._forced_skill or self._agent_identity.get("skill") or None,
                    has_signal_blocker=bool(self._forced_tools),
                )
            else:
                # Legacy path: standalone / old workflow mode
                effective_query_type = self._forced_strategy or classification.query_type
                system = build_system_prompt(
                    workspace_path,
                    workspace_layout=layout,
                    project_docs=project_docs,
                    max_iterations=self._max_iterations,
                    query_type=effective_query_type,
                    risk_context=risk_context,
                    code_context=code_context,
                    interactive=self._interactive,
                    has_signal_blocker=bool(self._forced_tools),
                )

            # Dynamic tool set: forced_tools (from Brain dispatch) > classification > all
            if self._forced_tools:
                active_tools = filter_tools(self._forced_tools)
                logger.info("Using forced tools from Brain dispatch: %d tools", len(active_tools))
            elif classification.tool_set:
                active_tools = filter_tools(classification.tool_set)
            else:
                active_tools = TOOL_DEFINITIONS

            # In interactive mode, append the ask_user tool so the LLM can
            # request clarification from the user mid-loop.
            if self._interactive:
                from app.code_tools.schemas import get_ask_user_tool_def
                active_tools = list(active_tools) + [get_ask_user_tool_def()]

        logger.info(
            "Query classified: type=%s, budget=%s, tools=%d",
            classification.query_type, classification.budget_level,
            len(active_tools),
        )

        # Emit classify event (Brain already emitted its own above)
        if not self._is_brain:
            yield AgentEvent(kind="classify", data={
                "query_type": classification.query_type,
                "budget_level": classification.budget_level,
                "tools_active": len(active_tools),
                "tools_total": len(TOOL_DEFINITIONS),
            })
            messages = self._initial_messages(query, classification)
        total_tool_calls = 0
        evidence_retries = 0
        # completeness_checked removed — Brain handles via need_brain_review
        response: Optional[ToolUseResponse] = None

        # Token budget controller — tracks cumulative token usage
        budget = BudgetController(self._budget_config or BudgetConfig(
            max_iterations=self._max_iterations,
        ))

        # Accumulate LLM text throughout the loop so we have a fallback
        # if the final answer is empty (e.g. max_tokens hit, model quirk).
        accumulated_text: List[str] = []

        # Accumulate thinking steps for the final response
        thinking_steps: List[Dict[str, Any]] = []

        # Track files already read (path → line count) to detect redundant reads
        files_read: Dict[str, int] = {}
        # Track grep patterns already used to detect redundant searches
        greps_used: List[str] = []
        # Track distinct top-level directories accessed to detect scatter
        dirs_accessed: Dict[str, int] = {}  # dir → count of files read
        # Track all tool calls to detect exact duplicate calls
        _tool_call_history: Dict[str, int] = {}  # "tool|params_json" → count
        # Structured tool history for completeness verifier
        _tool_history_for_verifier: List[Dict[str, Any]] = []

        # Build executor: prefer the injected one, fall back to local
        executor = self._tool_executor or LocalToolExecutor(workspace_path)

        # Cache for tool results to serve duplicates without re-execution
        _tool_result_cache: Dict[str, "ToolResult"] = {}

        # Closure executed in a thread for each tool call (with timing)
        async def _exec_tool(tc_arg):
            import json as _json

            # Build a cache key from tool name + sorted params
            try:
                cache_key = f"{tc_arg.name}|{_json.dumps(tc_arg.input, sort_keys=True)}"
            except (TypeError, ValueError):
                cache_key = f"{tc_arg.name}|{str(tc_arg.input)}"

            dup_count = _tool_call_history.get(cache_key, 0)
            _tool_call_history[cache_key] = dup_count + 1

            # Exact duplicate — return cached result without re-execution
            if dup_count > 0 and cache_key in _tool_result_cache:
                return tc_arg, _tool_result_cache[cache_key], 0.0

            t0 = time.monotonic()
            result = await executor.execute(tc_arg.name, tc_arg.input)
            elapsed = (time.monotonic() - t0) * 1000
            _tool_result_cache[cache_key] = result
            return tc_arg, result, elapsed

        # LLM call timeout — prevents hanging when context is too large
        _LLM_TIMEOUT_SECONDS = 300

        # Short session tag for log correlation across parallel agents
        _sid = trace.session_id[:8]

        # Max retries for throttled LLM calls before giving up
        _LLM_THROTTLE_RETRIES = 3
        _LLM_THROTTLE_BACKOFF = [5, 15, 30]  # seconds

        for iteration in range(self._max_iterations):
            # LLM call — offload to thread to avoid blocking the event loop.
            # When a shared semaphore is provided (e.g. multi-agent review),
            # acquire it first to limit concurrent Bedrock API calls and
            # avoid throttling.
            llm_start = time.monotonic()
            n_msgs = len(messages)
            logger.info(
                "[%s] iter=%d/%d LLM call starting (msgs=%d)",
                _sid, iteration + 1, self._max_iterations, n_msgs,
            )
            response = None
            for attempt in range(_LLM_THROTTLE_RETRIES + 1):
                try:
                    if self._llm_semaphore:
                        sem_wait_start = time.monotonic()
                        logger.info(
                            "[%s] iter=%d waiting for LLM semaphore...",
                            _sid, iteration + 1,
                        )
                        async with self._llm_semaphore:
                            sem_wait_ms = (time.monotonic() - sem_wait_start) * 1000
                            logger.info(
                                "[%s] iter=%d semaphore acquired (waited %.0fms), "
                                "calling LLM...",
                                _sid, iteration + 1, sem_wait_ms,
                            )
                            response = await asyncio.wait_for(
                                asyncio.to_thread(
                                    self._provider.chat_with_tools,
                                    messages=messages,
                                    tools=active_tools,
                                    max_tokens=8192,
                                    system=system,
                                    temperature=self._temperature,
                                ),
                                timeout=_LLM_TIMEOUT_SECONDS,
                            )
                    else:
                        response = await asyncio.wait_for(
                            asyncio.to_thread(
                                self._provider.chat_with_tools,
                                messages=messages,
                                tools=active_tools,
                                max_tokens=8192,
                                system=system,
                                temperature=self._temperature,
                            ),
                            timeout=_LLM_TIMEOUT_SECONDS,
                        )
                    break  # success — exit retry loop
                except asyncio.TimeoutError:
                    exc = TimeoutError(
                        f"LLM call timed out after {_LLM_TIMEOUT_SECONDS}s at iteration "
                        f"{iteration + 1} (context may be too large)"
                    )
                    logger.error("%s", exc)
                    answer = "\n\n".join(accumulated_text) if accumulated_text else ""
                    trace.finish(answer=answer, error=str(exc), budget_summary=budget.summary())
                    self._save_trace(trace)
                    yield AgentEvent(kind="error", data={
                        "error": str(exc),
                        "answer": answer,
                        "tool_calls_made": total_tool_calls,
                        "iterations": iteration + 1,
                        "duration_ms": (time.monotonic() - start) * 1000,
                        "thinking_steps": thinking_steps,
                        "budget_summary": budget.summary(),
                    })
                    return
                except Exception as exc:
                    exc_name = type(exc).__name__
                    is_throttle = (
                        "Throttling" in exc_name
                        or "throttl" in str(exc).lower()
                        or "Too many requests" in str(exc)
                        or "rate" in str(exc).lower()
                    )
                    if is_throttle and attempt < _LLM_THROTTLE_RETRIES:
                        backoff = _LLM_THROTTLE_BACKOFF[attempt]
                        logger.warning(
                            "[%s] iter=%d THROTTLED (attempt %d/%d): %s. "
                            "Backing off %ds before retry...",
                            _sid, iteration + 1, attempt + 1,
                            _LLM_THROTTLE_RETRIES + 1, exc, backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue  # retry
                    # Non-throttle error, or retries exhausted — fail
                    logger.error(
                        "[%s] iter=%d LLM call failed: [%s] %s",
                        _sid, iteration + 1, exc_name, exc,
                    )
                    trace.finish(
                        answer="\n\n".join(accumulated_text) if accumulated_text else "",
                        error=str(exc),
                        budget_summary=budget.summary(),
                    )
                    self._save_trace(trace)
                    yield AgentEvent(kind="error", data={
                        "error": str(exc),
                        "answer": "\n\n".join(accumulated_text) if accumulated_text else "",
                        "tool_calls_made": total_tool_calls,
                        "iterations": iteration + 1,
                        "duration_ms": (time.monotonic() - start) * 1000,
                        "thinking_steps": thinking_steps,
                        "budget_summary": budget.summary(),
                    })
                    return
            llm_elapsed_ms = (time.monotonic() - llm_start) * 1000
            _in_tok = response.usage.input_tokens if response.usage else 0
            _out_tok = response.usage.output_tokens if response.usage else 0
            _n_tc = len(response.tool_calls) if response.tool_calls else 0
            logger.info(
                "[%s] iter=%d LLM call done in %.0fms "
                "(in=%d out=%d tool_calls=%d stop=%s)",
                _sid, iteration + 1, llm_elapsed_ms,
                _in_tok, _out_tok, _n_tc,
                response.stop_reason,
            )

            # Track token usage from LLM response
            iter_metrics = IterationMetrics(
                input_tokens=response.usage.input_tokens if response.usage else 0,
                output_tokens=response.usage.output_tokens if response.usage else 0,
            )

            # Build iteration trace
            iter_trace = IterationTrace(
                iteration=iteration + 1,
                input_tokens=iter_metrics.input_tokens,
                output_tokens=iter_metrics.output_tokens,
                llm_latency_ms=llm_elapsed_ms,
            )

            # Track all LLM text for fallback (keep last 3 to limit context growth)
            if response.text:
                accumulated_text.append(response.text)
                if len(accumulated_text) > 3:
                    accumulated_text = accumulated_text[-3:]

            # Emit thinking text when the LLM also requests tool calls
            if response.text and response.tool_calls:
                thinking_steps.append({
                    "kind": "thinking",
                    "iteration": iteration + 1,
                    "text": response.text,
                })
                yield AgentEvent(kind="thinking", data={
                    "text": response.text,
                    "iteration": iteration + 1,
                })

            # Final answer — no tool calls requested
            if response.stop_reason == "end_turn" or not response.tool_calls:
                budget.track(iter_metrics)
                answer = response.text or ""
                # Fallback: if final answer is empty, use accumulated thinking
                if not answer.strip() and accumulated_text:
                    answer = accumulated_text[-1]
                    logger.warning(
                        "Agent final answer was empty at iteration %d; "
                        "falling back to last thinking text (%d chars)",
                        iteration + 1, len(answer),
                    )

                # Quality checks driven by agent template config.
                # Brain skips all checks (it dispatches agents, doesn't explore).
                qc = self._quality_config  # QualityConfig or None

                if self._is_brain:
                    duration = (time.monotonic() - start) * 1000
                    trace.finish(answer=answer, budget_summary=budget.summary())
                    self._save_trace(trace)
                    yield AgentEvent(kind="done", data={
                        "answer": answer,
                        "context_chunks": [],
                        "thinking_steps": thinking_steps,
                        "tool_calls_made": total_tool_calls,
                        "iterations": iteration + 1,
                        "duration_ms": duration,
                        "budget_summary": budget.summary(),
                    })
                    return

                # Evidence check — skip if template says evidence_check: false
                skip_evidence = qc and not qc.evidence_check
                effective_files = len(budget.files_accessed)
                if self._forced_tools and effective_files == 0 and total_tool_calls >= 3:
                    effective_files = 1  # grep/list_files count as investigation

                remaining = self._max_iterations - (iteration + 1)
                if skip_evidence:
                    # Create a passing evidence result
                    from .evidence import EvidenceCheck
                    ev = EvidenceCheck(passed=True, file_refs=0, code_blocks=0, tool_calls_made=total_tool_calls, guidance="")
                else:
                    ev = check_evidence(
                        answer=answer,
                        tool_calls_made=total_tool_calls,
                        files_accessed=effective_files,
                        remaining_iterations=remaining,
                        min_file_refs=qc.min_file_refs if qc else 1,
                        min_tool_calls=qc.min_tool_calls if qc else 2,
                    )
                if not ev.passed:
                    evidence_retries += 1
                    if evidence_retries > self._max_evidence_retries:
                        logger.warning(
                            "Evidence check failed %d times (max %d) — "
                            "enriching answer with collected file refs",
                            evidence_retries, self._max_evidence_retries,
                        )
                        # Enrich the answer with files the agent already accessed
                        answer = _enrich_answer_with_refs(
                            answer, budget.files_accessed,
                        )
                    else:
                        logger.info(
                            "Evidence check failed at iteration %d "
                            "(refs=%d, tools=%d, files=%d, retry=%d/%d) — requesting retry",
                            iteration + 1, ev.file_refs, ev.tool_calls_made,
                            len(budget.files_accessed),
                            evidence_retries, self._max_evidence_retries,
                        )
                        # Push back: re-add the answer as assistant, then
                        # inject evidence guidance as a user message
                        messages.append({
                            "role": "assistant",
                            "content": [{"text": answer}],
                        })
                        messages.append({
                            "role": "user",
                            "content": [{"text": ev.guidance}],
                        })
                        iter_trace.budget_signal = budget.get_signal().value
                        trace.add_iteration(iter_trace)
                        continue  # back to the loop for another LLM call

                # Completeness check removed — Brain handles quality evaluation
                # via need_brain_review flag. Sub-agents only do evidence check.

                iter_trace.budget_signal = budget.get_signal().value
                trace.add_iteration(iter_trace)
                trace.finish(answer=answer, budget_summary=budget.summary())
                self._save_trace(trace)
                yield AgentEvent(kind="done", data={
                    "answer": answer,
                    "tool_calls_made": total_tool_calls,
                    "iterations": iteration + 1,
                    "duration_ms": (time.monotonic() - start) * 1000,
                    "thinking_steps": thinking_steps,
                    "budget_summary": budget.summary(),
                })
                return

            # Append the assistant's response to the conversation
            messages.append(self._assistant_message(response))

            # Emit tool_call events before execution starts
            for tc in response.tool_calls:
                thinking_steps.append({
                    "kind": "tool_call",
                    "iteration": iteration + 1,
                    "tool": tc.name,
                    "params": tc.input,
                })
                yield AgentEvent(kind="tool_call", data={
                    "iteration": iteration + 1,
                    "tool": tc.name,
                    "params": tc.input,
                })

            # Separate special tools from regular tool calls
            _special = {"ask_user", "signal_blocker"}
            regular_calls = [tc for tc in response.tool_calls if tc.name not in _special]
            ask_user_calls = [tc for tc in response.tool_calls if tc.name == "ask_user"]
            signal_calls = [tc for tc in response.tool_calls if tc.name == "signal_blocker"]

            # Execute regular tool calls concurrently
            tool_outputs: list = []
            if regular_calls:
                tool_outputs = list(await asyncio.gather(
                    *[_exec_tool(tc) for tc in regular_calls]
                ))

            # Check for brain transfer (one-way handoff to specialized brain)
            for tc, result, _lat in tool_outputs:
                if (tc.name == "transfer_to_brain" and result.success
                        and isinstance(result.data, dict) and result.data.get("transfer")):
                    logger.info("Brain transfer to '%s' — exiting agent loop", result.data.get("brain"))
                    yield AgentEvent(kind="transfer", data=result.data)
                    return

            # Handle ask_user (at most one per turn)
            if ask_user_calls and self._interactive:
                from app.agent_loop.interactive import (
                    ASK_USER_TIMEOUT,
                    register_question,
                    cleanup as cleanup_question,
                )
                from app.code_tools.schemas import ToolResult

                tc = ask_user_calls[0]
                question_text = tc.input.get("question", "")
                question_ctx = tc.input.get("context", "")

                pq = register_question(trace.session_id, question_text, question_ctx)

                yield AgentEvent(kind="ask_user", data={
                    "session_id": trace.session_id,
                    "question": question_text,
                    "context": question_ctx,
                    "tool_use_id": tc.id,
                })

                # Wait for user answer with 15s keepalive heartbeats
                ask_start = time.monotonic()
                try:
                    while not pq.event.is_set():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(pq.event.wait()), timeout=15.0,
                            )
                        except asyncio.TimeoutError:
                            elapsed = time.monotonic() - ask_start
                            if elapsed >= ASK_USER_TIMEOUT:
                                pq.timed_out = True
                                break
                            yield AgentEvent(kind="ask_user_waiting", data={
                                "session_id": trace.session_id,
                                "elapsed_seconds": int(elapsed),
                            })
                except asyncio.CancelledError:
                    cleanup_question(trace.session_id)
                    raise

                if pq.timed_out:
                    answer_text = (
                        "(The user did not respond within the time limit. "
                        "Continue with your best judgment based on available evidence.)"
                    )
                else:
                    answer_text = pq.answer or "(No answer provided)"

                cleanup_question(trace.session_id)

                tool_outputs.append((tc, ToolResult(
                    tool_name="ask_user",
                    success=True,
                    data={"answer": answer_text, "timed_out": pq.timed_out},
                ), 0.0))

                # Record the Q&A in thinking steps
                thinking_steps.append({
                    "kind": "ask_user",
                    "iteration": iteration + 1,
                    "text": f"Q: {question_text}\nA: {answer_text}",
                    "tool": "ask_user",
                })

            # Handle extra ask_user calls beyond the first (return guidance)
            for extra_tc in ask_user_calls[1:]:
                from app.code_tools.schemas import ToolResult
                tool_outputs.append((extra_tc, ToolResult(
                    tool_name="ask_user",
                    success=False,
                    error="Only one question per turn. Continue with the answer you received.",
                ), 0.0))

            # Handle signal_blocker — sub-agent asks Brain for direction
            if signal_calls and self._forced_tools:
                from app.agent_loop.signal_blocker import (
                    SIGNAL_TIMEOUT,
                    register_signal,
                    cleanup_signal,
                )
                from app.code_tools.schemas import ToolResult

                tc = signal_calls[0]
                reason = tc.input.get("reason", "")
                options = tc.input.get("options", [])
                sig_ctx = tc.input.get("context", "")

                ps = register_signal(trace.session_id, reason, options, sig_ctx)

                yield AgentEvent(kind="signal_blocker", data={
                    "session_id": trace.session_id,
                    "reason": reason,
                    "options": options,
                    "context": sig_ctx,
                    "tool_use_id": tc.id,
                })

                # Wait for Brain's response (with timeout)
                try:
                    await asyncio.wait_for(ps.event.wait(), timeout=SIGNAL_TIMEOUT)
                except asyncio.TimeoutError:
                    ps.timed_out = True
                except asyncio.CancelledError:
                    cleanup_signal(trace.session_id)
                    raise

                if ps.timed_out:
                    response_text = "(Brain did not respond. Continue with your best judgment.)"
                else:
                    response_text = ps.response or "(No direction provided)"

                cleanup_signal(trace.session_id)

                tool_outputs.append((tc, ToolResult(
                    tool_name="signal_blocker",
                    success=True,
                    data={"response": response_text, "timed_out": ps.timed_out},
                ), 0.0))

                thinking_steps.append({
                    "kind": "signal_blocker",
                    "iteration": iteration + 1,
                    "text": f"Signal: {reason}\nBrain: {response_text}",
                    "tool": "signal_blocker",
                })

            # Process results and collect context
            tool_results_content = []
            guidance_notes: List[str] = []

            # Detect duplicate tool calls and warn the LLM
            import json as _json
            dup_tools = []
            for tc_arg in response.tool_calls:
                try:
                    ck = f"{tc_arg.name}|{_json.dumps(tc_arg.input, sort_keys=True)}"
                except (TypeError, ValueError):
                    ck = f"{tc_arg.name}|{str(tc_arg.input)}"
                cnt = _tool_call_history.get(ck, 0)
                if cnt > 1:
                    dup_tools.append((tc_arg.name, cnt))
            if dup_tools:
                dup_desc = ", ".join(f"{name} ({cnt}x)" for name, cnt in dup_tools)
                guidance_notes.append(
                    f"⚠ DUPLICATE TOOL CALLS DETECTED: {dup_desc}. "
                    f"You are re-calling tools with identical parameters — the result "
                    f"will be the same. Stop repeating and either: (1) use the result "
                    f"you already have, (2) try a different approach, or (3) provide "
                    f"your final answer now."
                )
                # After 3+ repeats of any single call, force conclude
                max_repeats = max(cnt for _, cnt in dup_tools)
                if max_repeats >= 3:
                    guidance_notes.append(
                        "🛑 STOP: You have repeated the same tool call 3+ times. "
                        "You MUST provide your answer NOW based on what you already know. "
                        "Do NOT call any more tools."
                    )

            # Guard: warn if too many heavy tools in one turn
            diff_calls_this_turn = sum(
                1 for tc_arg in response.tool_calls
                if tc_arg.name in ("git_diff", "read_file") and not tc_arg.input.get("start_line")
            )
            if diff_calls_this_turn > 3:
                guidance_notes.append(
                    f"⚠ You called {diff_calls_this_turn} heavy tools (git_diff/read_file) "
                    f"in a single turn. This wastes context budget. "
                    f"Review at most 2 files per turn: call git_diff on 1-2 files, "
                    f"analyze them, then proceed to the next batch."
                )

            for tc, tool_result, tool_elapsed_ms in tool_outputs:
                total_tool_calls += 1
                logger.info(
                    "Agent tool call #%d: %s(%s)",
                    total_tool_calls, tc.name, _truncate_json(tc.input),
                )

                # Emit tool_result summary
                result_summary = _summarize_result(tc.name, tool_result)
                _tool_history_for_verifier.append({
                    "tool": tc.name,
                    "params": tc.input,
                    "summary": result_summary,
                })
                thinking_steps.append({
                    "kind": "tool_result",
                    "iteration": iteration + 1,
                    "tool": tc.name,
                    "success": tool_result.success,
                    "summary": result_summary,
                })
                yield AgentEvent(kind="tool_result", data={
                    "iteration": iteration + 1,
                    "tool": tc.name,
                    "success": tool_result.success,
                    "summary": result_summary,
                })

                # Record tool call in the iteration trace
                result_chars = len(json.dumps(tool_result.data, default=str)) if tool_result.data else 0
                tc_trace = ToolCallTrace(
                    tool_name=tc.name,
                    params=tc.input,
                    success=tool_result.success,
                    result_chars=result_chars,
                    latency_ms=tool_elapsed_ms,
                )

                # Track files read and detect redundant reads
                if tc.name == "read_file" and tool_result.success:
                    fpath = tc.input.get("path", "")
                    total_lines = (
                        tool_result.data.get("total_lines", 0)
                        if isinstance(tool_result.data, dict) else 0
                    )
                    has_range = tc.input.get("start_line") or tc.input.get("end_line")
                    if fpath in files_read and not has_range:
                        guidance_notes.append(
                            f"⚠ You already read '{fpath}' ({files_read[fpath]} lines) "
                            f"earlier. Do NOT re-read entire files. Use start_line/end_line "
                            f"to read only the specific section you need, or reference "
                            f"what you already learned."
                        )
                    files_read[fpath] = total_lines

                    # Warn about large files without outline
                    if total_lines > 200 and not has_range:
                        guidance_notes.append(
                            f"⚠ '{fpath}' is {total_lines} lines. For large files, "
                            f"use file_outline first to find the relevant methods, "
                            f"then read only those specific line ranges."
                        )

                    # Track directory for scatter detection
                    top_dir = _top_directory(fpath)
                    if top_dir:
                        dirs_accessed[top_dir] = dirs_accessed.get(top_dir, 0) + 1

                # Track grep patterns and detect too many results / zero results
                if tc.name == "grep":
                    pattern = tc.input.get("pattern", "")
                    greps_used.append(pattern)
                    if tool_result.success:
                        n_results = (
                            len(tool_result.data)
                            if isinstance(tool_result.data, list) else 0
                        )
                        if n_results >= 40:
                            guidance_notes.append(
                                f"⚠ grep('{pattern}') returned {n_results} results — "
                                f"too broad. Narrow your search: use a more specific pattern, "
                                f"set 'path' to a subdirectory, or use 'include_glob' to "
                                f"filter by file type."
                            )
                        elif n_results == 0:
                            guidance_notes.append(
                                f"⚠ grep('{pattern}') returned 0 results. "
                                f"Try: (1) find_symbol with the key class/function name, "
                                f"(2) a simpler substring pattern, "
                                f"(3) explore the relevant directory you already identified "
                                f"with file_outline on its files. "
                                f"Do NOT widen to a catch-all pattern."
                            )

                # Track files and symbols for budget controller
                iter_metrics.tool_names.append(tc.name)
                if tc.name == "read_file" and tool_result.success:
                    fpath = tc.input.get("path", "")
                    if fpath:
                        new_f = budget.track_file(fpath)
                        iter_metrics.new_files_accessed += new_f
                        tc_trace.new_files = new_f
                elif tc.name == "find_symbol" and tool_result.success:
                    if isinstance(tool_result.data, list):
                        for sym in tool_result.data:
                            sname = sym.get("name", "")
                            if sname:
                                new_s = budget.track_symbol(sname)
                                iter_metrics.new_symbols_found += new_s
                                tc_trace.new_symbols += new_s
                elif tc.name in ("file_outline", "compressed_view") and tool_result.success:
                    fpath = tc.input.get("path", tc.input.get("file_path", ""))
                    if fpath:
                        new_f = budget.track_file(fpath)
                        iter_metrics.new_files_accessed += new_f
                        tc_trace.new_files = new_f

                # Collect context chunks from relevant tools
                for chunk in _extract_context(tc, tool_result, query):
                    yield AgentEvent(kind="context_chunk", data={
                        "file_path": chunk.file_path,
                        "content": chunk.content,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "relevance": chunk.relevance,
                        "source_tool": chunk.source_tool,
                    })

                remaining_tokens = budget.config.max_input_tokens - budget.cumulative_input
                tool_results_content.append(
                    self._tool_result_block(tc.id, tool_result, tc.name, remaining_tokens)
                )
                iter_trace.tool_calls.append(tc_trace)

            # Commit iteration metrics to budget controller
            budget.track(iter_metrics)
            budget_signal = budget.get_signal()

            # Record iteration trace
            iter_trace.budget_signal = budget_signal.value
            if response.text and response.tool_calls:
                iter_trace.thinking_text = response.text[:500]
            trace.add_iteration(iter_trace)

            # Budget-driven convergence
            if budget_signal == BudgetSignal.FORCE_CONCLUDE:
                answer = "\n\n".join(accumulated_text) if accumulated_text else ""
                conclude_reason = (
                    "Max iterations reached"
                    if budget.iteration_count >= budget.config.max_iterations
                    else "Token budget exhausted"
                )
                # Enrich with collected file refs so the answer is still useful
                answer = _enrich_answer_with_refs(answer, budget.files_accessed)
                logger.warning(
                    "Budget FORCE_CONCLUDE at iteration %d: %s "
                    "(input tokens: %s/%s)",
                    iteration + 1, conclude_reason,
                    budget.cumulative_input, budget.config.max_input_tokens,
                )
                trace.finish(answer=answer, error=conclude_reason, budget_summary=budget.summary())
                self._save_trace(trace)
                yield AgentEvent(kind="done", data={
                    "answer": answer,
                    "tool_calls_made": total_tool_calls,
                    "iterations": iteration + 1,
                    "duration_ms": (time.monotonic() - start) * 1000,
                    "error": conclude_reason,
                    "thinking_steps": thinking_steps,
                    "budget_summary": budget.summary(),
                })
                return

            if budget_signal == BudgetSignal.WARN_CONVERGE:
                guidance_notes.append(
                    f"⚠ BUDGET WARNING: {budget.budget_context} "
                    f"You MUST start converging NOW. Only verification tool calls "
                    f"are allowed (expand_symbol on already-identified symbols, "
                    f"read_file with specific line ranges). "
                    f"Do NOT start new searches (grep, find_symbol, module_summary). "
                    f"Summarize your findings and provide your answer."
                )

            # Scatter detection: tighter threshold for high-level queries
            scatter_limit = 3 if is_high_level else 5
            if len(dirs_accessed) >= scatter_limit:
                top_dirs = sorted(dirs_accessed.keys())
                guidance_notes.append(
                    f"⚠ SCATTER WARNING: You have read files from {len(dirs_accessed)} "
                    f"different directories ({', '.join(top_dirs[:6])}). "
                    f"This suggests unfocused exploration. STOP exploring new directories. "
                    f"Go back to the most relevant module you found and follow its call "
                    f"chain. If you cannot find the answer, provide what you know so far."
                )

            # Convergence checkpoints — earlier for high-level queries
            if is_high_level and iteration + 1 >= 3 and iteration > 1:
                guidance_notes.append(
                    f"⚠ HIGH-LEVEL QUERY CHECKPOINT: You've used {iteration + 1} of "
                    f"{self._max_iterations} iterations on a high-level question. "
                    f"By now you should have found the orchestration layer. "
                    f"STOP and write your answer using what you've found. "
                    f"List the steps/flow with file paths and line numbers. "
                    f"Do NOT keep exploring implementation details."
                )
            elif not is_high_level:
                half_budget = self._max_iterations // 2
                if iteration + 1 == half_budget and iteration > 2:
                    guidance_notes.append(
                        f"⚠ HALFWAY CHECKPOINT: You've used {iteration + 1} of "
                        f"{self._max_iterations} iterations. Summarize what you've "
                        f"learned so far, identify what's missing, and decide: "
                        f"can you answer now? If not, make a focused 2-3 step plan "
                        f"for the remaining budget. Do not keep exploring broadly."
                    )

            # Inject budget context + guidance notes into the conversation
            remaining = self._max_iterations - (iteration + 1)
            budget_note = budget.budget_context
            low_threshold = 5 if is_high_level else 3
            if remaining <= low_threshold:
                if is_high_level:
                    budget_note += (
                        " ⚠ URGENT: You are running low on iterations for a high-level question. "
                        "You MUST provide your answer NOW. Summarize the flow/steps you've found "
                        "with file paths and line numbers. Do NOT make any more exploratory tool calls."
                    )
                else:
                    budget_note += (
                        " ⚠ Running low on iterations. "
                        "Wrap up your investigation and provide your answer soon."
                    )
            if guidance_notes:
                budget_note += "\n" + "\n".join(guidance_notes)

            messages.append(
                self._tool_results_message_with_note(tool_results_content, budget_note)
            )

        # Exhausted iterations — make one final LLM call WITHOUT tools
        # to force a proper answer based on everything collected so far.
        logger.info(
            "[%s] Iterations exhausted (%d). Making final judge call (no tools).",
            _sid, self._max_iterations,
        )

        messages.append({
            "role": "user",
            "content": [{
                "text": (
                    "⚠ You have exhausted all tool-calling iterations. "
                    "You MUST provide your final answer NOW based on everything "
                    "you have learned so far. Synthesize your findings into a "
                    "clear, complete explanation. Do NOT say you need more information "
                    "or request additional tool calls — just give your best answer "
                    "with what you have."
                ),
            }],
        })

        try:
            final_response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._provider.chat_with_tools,
                    messages=messages,
                    tools=[],  # no tools — force text-only response
                    system=system,
                ),
                timeout=_LLM_TIMEOUT_SECONDS,
            )
            answer = (final_response.text or "").strip()
            if final_response.usage:
                budget.record_usage(final_response.usage.input_tokens, final_response.usage.output_tokens)
            logger.info(
                "[%s] Final judge call produced %d chars",
                _sid, len(answer),
            )
        except Exception as exc:
            logger.warning("[%s] Final judge call failed: %s", _sid, exc)
            answer = ""

        # Fallback if final judge also produced nothing
        if not answer and accumulated_text:
            answer = accumulated_text[-1]
            logger.warning(
                "Final judge call empty; falling back to last thinking text (%d chars)",
                len(answer),
            )

        trace.finish(answer=answer, error=None, budget_summary=budget.summary())
        self._save_trace(trace)
        yield AgentEvent(kind="done", data={
            "answer": answer,
            "tool_calls_made": total_tool_calls,
            "iterations": self._max_iterations,
            "duration_ms": (time.monotonic() - start) * 1000,
            "thinking_steps": thinking_steps,
            "budget_summary": budget.summary(),
        })

    def _save_trace(self, trace: SessionTrace) -> None:
        """Persist the session trace if a writer is configured."""
        if self._trace_writer:
            self._trace_writer.save(trace)

    # ------------------------------------------------------------------
    # Message formatting — provider-agnostic (Bedrock Converse format)
    #
    # We use the Bedrock Converse message format as the canonical format
    # because it's the most structured. Provider adapters in
    # (Legacy multi-agent review and multi-perspective methods removed —
    #  now handled by Brain dispatch_swarm)

    @staticmethod
    def _initial_messages(
        query: str,
        classification: Optional[QueryClassification] = None,
    ) -> List[Dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [{"text": query}],
            }
        ]

    @staticmethod
    def _assistant_message(response: ToolUseResponse) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = []
        if response.text:
            content.append({"text": response.text})
        for tc in response.tool_calls:
            content.append({
                "toolUse": {
                    "toolUseId": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                }
            })
        return {"role": "assistant", "content": content}

    @staticmethod
    def _tool_result_block(
        tool_use_id: str,
        result,
        tool_name: str = "",
        remaining_input_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        if result.success:
            text = apply_policy(tool_name, result.data, remaining_input_tokens)
        else:
            text = f"ERROR: {result.error}"

        return {
            "toolUseId": tool_use_id,
            "content": [{"text": text}],
        }

    @staticmethod
    def _tool_results_message(results: List[Dict]) -> Dict[str, Any]:
        return {
            "role": "user",
            "content": [{"toolResult": r} for r in results],
        }

    @staticmethod
    def _tool_results_message_with_note(
        results: List[Dict], note: str,
    ) -> Dict[str, Any]:
        """Build tool results message with an appended system guidance note."""
        content: List[Dict[str, Any]] = [{"toolResult": r} for r in results]
        content.append({"text": note})
        return {"role": "user", "content": content}


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _top_directory(fpath: str, depth: int = 2) -> str:
    """Extract the top-level directory from a relative file path.

    For ``services/render/client.py`` with depth=2, returns ``services/render``.
    For ``api.py`` (no directory), returns ``(root)``.
    """
    parts = fpath.replace("\\", "/").split("/")
    if len(parts) <= 1:
        return "(root)"
    return "/".join(parts[:depth])


def _truncate_json(obj: Any, max_len: int = 200) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= max_len else s[:max_len] + "..."


def _summarize_result(tool_name: str, result) -> str:
    """Create a brief human-readable summary of a tool result."""
    if not result.success:
        return f"Error: {result.error}"
    data = result.data
    if isinstance(data, list):
        n = len(data)
        return f"{n} result{'s' if n != 1 else ''}"
    if isinstance(data, dict):
        if "content" in data:
            return f"{data.get('total_lines', '?')} lines"
        if "diff" in data:
            return f"{len(data['diff'])} chars of diff"
    return "ok"


def _enrich_answer_with_refs(answer: str, files_accessed: set) -> str:
    """Append a 'Files examined' section when the LLM omitted citations.

    Called when the evidence-check retry cap is hit — the agent DID
    investigate (tool calls, file reads) but the LLM never formatted
    citations in the expected ``file:line`` style.  Rather than
    accepting a bare answer, we append the evidence the agent already
    collected so the reader can trace what was examined.
    """
    if not files_accessed:
        return answer
    refs = sorted(files_accessed)
    section = "\n\n---\n**Files examined during analysis:**\n"
    section += "\n".join(f"- `{f}`" for f in refs)
    return answer + section


def _extract_context(tc, result, query: str) -> List[ContextChunk]:
    """Extract context chunks from tool results.

    Not every tool produces meaningful context for the client.
    This function selects the tools whose output is valuable as
    visible "evidence" of what the agent examined.
    """
    if not result.success or not result.data:
        return []

    chunks: List[ContextChunk] = []
    name = tc.name

    if name == "read_file":
        chunks.append(ContextChunk(
            file_path=result.data.get("path", ""),
            content=result.data.get("content", ""),
            start_line=tc.input.get("start_line", 0),
            end_line=tc.input.get("end_line", 0),
            relevance=query,
            source_tool="read_file",
        ))

    elif name in ("grep", "find_references"):
        # Group matches by file — one chunk per file
        if isinstance(result.data, list) and result.data:
            by_file: Dict[str, list] = {}
            for m in result.data:
                by_file.setdefault(m.get("file_path", ""), []).append(m)
            for fp, matches in by_file.items():
                lines = [
                    f"{m.get('line_number', 0):>4} | {m.get('content', '')}"
                    for m in matches
                ]
                chunks.append(ContextChunk(
                    file_path=fp,
                    content="\n".join(lines),
                    start_line=matches[0].get("line_number", 0),
                    end_line=matches[-1].get("line_number", 0),
                    relevance=query,
                    source_tool=name,
                ))

    elif name == "find_symbol":
        if isinstance(result.data, list):
            for sym in result.data:
                sig = sym.get("signature", "")
                desc = f"{sym.get('kind', '')} {sym.get('name', '')}"
                if sig:
                    desc += f": {sig}"
                chunks.append(ContextChunk(
                    file_path=sym.get("file_path", ""),
                    content=desc,
                    start_line=sym.get("start_line", 0),
                    end_line=sym.get("end_line", 0),
                    relevance=query,
                    source_tool="find_symbol",
                ))

    elif name == "file_outline":
        if isinstance(result.data, list) and result.data:
            fp = result.data[0].get("file_path", "")
            lines = [
                f"  {d.get('kind', '')} {d.get('name', '')} L{d.get('start_line', 0)}"
                for d in result.data
            ]
            chunks.append(ContextChunk(
                file_path=fp,
                content="\n".join(lines),
                relevance=query,
                source_tool="file_outline",
            ))

    elif name == "ast_search":
        if isinstance(result.data, list):
            for m in result.data:
                chunks.append(ContextChunk(
                    file_path=m.get("file_path", ""),
                    content=m.get("text", ""),
                    start_line=m.get("start_line", 0),
                    end_line=m.get("end_line", 0),
                    relevance=query,
                    source_tool="ast_search",
                ))

    elif name == "git_diff":
        if isinstance(result.data, dict) and result.data.get("diff"):
            chunks.append(ContextChunk(
                file_path="(diff)",
                content=result.data["diff"][:10_000],
                relevance=query,
                source_tool="git_diff",
            ))

    elif name == "git_blame":
        if isinstance(result.data, list) and result.data:
            lines = [
                f"L{e.get('line_number', '?')} | {e.get('commit_hash', '?')} "
                f"({e.get('author', '?')}, {e.get('date', '?')}) {e.get('content', '')}"
                for e in result.data[:50]
            ]
            chunks.append(ContextChunk(
                file_path=tc.input.get("file", ""),
                content="\n".join(lines),
                start_line=tc.input.get("start_line", 0),
                end_line=tc.input.get("end_line", 0),
                relevance=query,
                source_tool="git_blame",
            ))

    elif name == "git_show":
        if isinstance(result.data, dict):
            info = (
                f"Commit: {result.data.get('commit_hash', '?')}\n"
                f"Author: {result.data.get('author', '?')}\n"
                f"Date: {result.data.get('date', '?')}\n"
                f"Message:\n{result.data.get('message', '')}\n"
            )
            diff = result.data.get("diff", "")
            if diff:
                info += f"\nDiff:\n{diff[:10_000]}"
            chunks.append(ContextChunk(
                file_path=f"(commit {result.data.get('commit_hash', '?')})",
                content=info,
                relevance=query,
                source_tool="git_show",
            ))

    elif name == "find_tests":
        if isinstance(result.data, list):
            by_file: Dict[str, list] = {}
            for m in result.data:
                by_file.setdefault(m.get("test_file", ""), []).append(m)
            for fp, matches in by_file.items():
                lines = [
                    f"  {m.get('test_function', '?')} L{m.get('line_number', '?')}: "
                    f"{m.get('context', '')}"
                    for m in matches
                ]
                chunks.append(ContextChunk(
                    file_path=fp,
                    content="\n".join(lines),
                    relevance=query,
                    source_tool="find_tests",
                ))

    elif name == "test_outline":
        if isinstance(result.data, list) and result.data:
            fp = tc.input.get("path", "")
            lines = []
            for entry in result.data:
                desc = f"  {entry.get('kind', '')} {entry.get('name', '')} L{entry.get('line_number', 0)}"
                mocks = entry.get("mocks", [])
                if mocks:
                    desc += f" mocks=[{', '.join(mocks[:5])}]"
                asserts = entry.get("assertions", [])
                if asserts:
                    desc += f" asserts=[{', '.join(a[:40] for a in asserts[:3])}]"
                lines.append(desc)
            chunks.append(ContextChunk(
                file_path=fp,
                content="\n".join(lines),
                relevance=query,
                source_tool="test_outline",
            ))

    elif name == "trace_variable":
        if isinstance(result.data, dict):
            d = result.data
            parts = [f"Variable: {d.get('variable', '?')} in {d.get('function', '?')} ({d.get('direction', '?')})"]
            for a in d.get("aliases", []):
                parts.append(f"  alias: {a.get('name', '?')} L{a.get('line', '?')}")
            for f in d.get("flows_to", []):
                parts.append(
                    f"  → {f.get('callee_function', '?')}(as {f.get('as_parameter', '?')}) "
                    f"L{f.get('call_line', '?')} [{f.get('confidence', '?')}]"
                )
            for s in d.get("sinks", []):
                parts.append(f"  ⊳ {s.get('kind', '?')}: {s.get('expression', '')[:80]} L{s.get('line', '?')}")
            for f in d.get("flows_from", []):
                parts.append(
                    f"  ← {f.get('caller_function', '?')}({f.get('arg_expression', '?')}) "
                    f"L{f.get('call_line', '?')}"
                )
            for s in d.get("sources", []):
                parts.append(f"  ⊲ {s.get('kind', '?')}: {s.get('expression', '')[:80]} L{s.get('line', '?')}")
            chunks.append(ContextChunk(
                file_path=d.get("file", ""),
                content="\n".join(parts),
                relevance=query,
                source_tool="trace_variable",
            ))

    return chunks
