# Conducator 强化实施方案

> **Agentic Code Intelligence — Detailed Implementation Specification**
>
> - Project: `github.com/KalokUmich/conducator`
> - Branch: `feature/git-workspace-code-search`
> - Date: 2026-03-12
> - Version: 1.0

---

## Table of Contents

1. [项目概述与现状分析](#1-项目概述与现状分析)
2. [核心设计原则](#2-核心设计原则)
3. [总体架构](#3-总体架构)
4. [Phase 1 — 基础强化 (Weeks 1-2)](#4-phase-1--基础强化-weeks-1-2)
5. [Phase 2 — Context Intelligence (Weeks 3-5)](#5-phase-2--context-intelligence-weeks-3-5)
6. [Phase 3 — Advanced Intelligence (Weeks 6-9)](#6-phase-3--advanced-intelligence-weeks-6-9)
7. [Phase 4 — 长期能力 (Week 10+)](#7-phase-4--长期能力-week-10)
8. [实施路线图](#8-实施路线图)
9. [测试策略](#9-测试策略)
10. [参考文献](#10-参考文献)

---

## 1. 项目概述与现状分析

### 1.1 Architecture Overview

The project migrated from a traditional RAG pipeline (CocoIndex + LiteLLM + Postgres) to an **agentic code intelligence** system. Current core components:

| Component | File(s) | Description |
|-----------|---------|-------------|
| AgentLoopService | `agent_loop/service.py` (717 lines) | While-loop + tool-calling agent, SSE streaming, Bedrock Converse format, max 25 iterations, scatter detection, convergence checkpoint at 50% |
| System Prompt | `agent_loop/prompts.py` (415 lines) | Hypothesis-driven exploration, guidance system, redundant read detection |
| 18 Code Tools | `code_tools/tools.py` (1967 lines) | grep, read_file, list_files, find_symbol, find_references, file_outline, get_dependencies, get_dependents, git_log, git_diff, ast_search, get_callees, get_callers, git_blame, git_show, find_tests, test_outline, trace_variable |
| Tool Schemas | `code_tools/schemas.py` (387 lines) | Pydantic schemas + TOOL_DEFINITIONS for LLM protocol |
| RepoMap | `repo_graph/` | tree-sitter AST → networkx dependency graph → PageRank ranking |
| 3 AI Providers | `ai_provider/` | ClaudeBedrockProvider, ClaudeDirectProvider, OpenAIProvider + ProviderResolver with health checks |
| LangExtract | `langextract/` | Multi-vendor Bedrock integration with dynamic catalog |
| Config | `config.py` (639 lines) | Dual system: AppSettings (server/git) + ConductorConfig (AI/auth/policy) |
| RAG Router | `rag/router.py` | Deprecated, returns 503 with deprecation message |
| Settings | `conductor.settings.yaml` | 8 AI models, 3 providers enabled |

### 1.2 Key Strengths (retain these)

- **While-loop + tools architecture** aligns with industry consensus — see [Braintrust](https://www.braintrust.dev/blog/agent-while-loop), [Anthropic](https://www.anthropic.com/research/building-effective-agents), [Letta v1](https://www.letta.com/blog/letta-v1-agent)
- **18 code tools**, especially `trace_variable` (data flow tracing with alias detection, argument→parameter mapping, sink/source patterns for ORM/SQL/HTTP/return/log) and PageRank-based `find_symbol`
- **3-tier symbol index cache**: in-memory → disk (`.conductor/symbol_index.json`) → full AST scan
- **Multi-provider** with health checks and model selection
- **Workspace reconnaissance**: auto-scan directory layout (depth≤3) + read key docs (README, CLAUDE.md, ARCHITECTURE.md)

### 1.3 Critical Issues to Fix

| # | Issue | Severity | Current State |
|---|-------|----------|--------------|
| 1 | Tool output truncation | 🔴 High | Uniform 30K char hard cutoff regardless of tool type |
| 2 | Budget management | 🔴 High | Iteration-based (not token-based); ROADMAP says 15, code says 25 |
| 3 | Config system | 🔴 High | Two overlapping systems; RAG remnants (embedding_model, reranking) still present |
| 4 | System prompt | 🟡 Medium | 415 lines — "lost in the middle" effect risk |
| 5 | No observability | 🟡 Medium | No structured traces or metrics |
| 6 | Cache invalidation | 🟡 Medium | 120s TTL for dependency graph; no git-based invalidation |
| 7 | Provider failover | 🟡 Medium | Failover chain and circuit breaker not clearly defined |

---

## 2. 核心设计原则

### Principle 1: Simple Loop, Smart Tools

Do NOT build complex pipelines. All capabilities exposed as tools within the existing agent loop. The while-loop + tool-calling pattern is the dominant architecture for successful agents (Claude Code, OpenAI Agents SDK, etc.).

> "Surprisingly, many of the most popular and successful agents share a common, straightforward architecture: a while loop that makes tool calls." — [Braintrust](https://www.braintrust.dev/blog/agent-while-loop)

> "The agent is just a system prompt and a handful of well-crafted tools." — [Braintrust](https://www.braintrust.dev/blog/agent-while-loop)

> Newer models (GPT-5, Claude 4.5+) benefit from simpler loops that stay "in-distribution" with model training. — [Letta v1](https://www.letta.com/blog/letta-v1-agent)

References:
- https://www.braintrust.dev/blog/agent-while-loop
- https://www.anthropic.com/research/building-effective-agents
- https://www.letta.com/blog/letta-v1-agent

### Principle 2: Token is Currency

Every token has cost. Input tokens dominate in coding agents. Same task can vary 10x in consumption across runs.

> "More complex tasks tend to consume more tokens, yet token usage also exhibits large variance across runs (some runs use up to 10× more tokens than others). Input tokens dominate overall consumption and cost, even with token caching." — [ICLR 2026](https://openreview.net/forum?id=1bUeVB3fov)

References:
- https://openreview.net/forum?id=1bUeVB3fov

### Principle 3: Compress First, Expand on Demand

Default to compressed views (signatures + calls + side effects). Only expand to full source when agent needs details. Less than 5% of context can match full-repo performance.

> MutaGReP "plans use less than 5% of the 128K context window for GPT-4o but rival the coding performance of GPT-4o with a context window filled with the repo." — [arXiv:2502.15872](https://arxiv.org/abs/2502.15872)

References:
- https://arxiv.org/abs/2502.15872

### Principle 4: Evidence Before Conclusion

Every answer must cite specific code lines and file locations. No claims without traced evidence.

> "Use the smallest set of tokens that maximize desired outcomes; prioritize informativeness over volume." — [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

References:
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

### Principle 5: Observable by Default

Every session produces a structured trace JSON for offline analysis.

> Process supervision can significantly improve agent search efficiency. — [RAG-Gym, arXiv:2502.13957](https://arxiv.org/abs/2502.13957)

References:
- https://arxiv.org/abs/2502.13957

### Principle 6: Cache What's Expensive

Module summaries, symbol index, dependency graph cached with git-based invalidation. Pre-condense repository knowledge and load on demand at runtime.

> LingmaAgent "introduces a top-down method to condense critical repository information into a knowledge graph, reducing complexity." — [arXiv:2406.01422](https://arxiv.org/abs/2406.01422)

References:
- https://arxiv.org/abs/2406.01422

### Principle 7: Enhance, Don't Replace

Build on existing 18 tools + RepoMap + agent loop. Do not rebuild from scratch.

---

## 3. 总体架构

```
                      ┌──────────────────────┐
                      │     User Question     │
                      └──────────┬───────────┘
                                 │
                      ┌──────────▼───────────┐
                      │   Query Classifier    │  ← Lightweight LLM or keyword rules
                      │  (strategy + budget)  │
                      └──────────┬───────────┘
                                 │
                      ┌──────────▼───────────┐
                      │  Workspace Priming    │  ← Enhanced initialization
                      │ (module localization) │     reuses existing RepoMap
                      └──────────┬───────────┘
                                 │
             ┌───────────────────▼────────────────────┐
             │          Agent Loop (existing)           │
             │  ┌─────────────────────────────────┐   │
             │  │  LLM + Tools (18 existing)       │   │
             │  │  + 3 new tools:                  │   │
             │  │    • compressed_view             │   │
             │  │    • module_summary              │   │
             │  │    • expand_symbol               │   │
             │  └───────────┬─────────────────────┘   │
             │              │                          │
             │  ┌───────────▼─────────────────────┐   │
             │  │  Context Budget Controller       │   │
             │  │  (token tracking + adaptive      │   │
             │  │   truncation + diminishing        │   │
             │  │   returns detection)              │   │
             │  └───────────┬─────────────────────┘   │
             │              │                          │
             │  ┌───────────▼─────────────────────┐   │
             │  │  Guidance Engine (runtime)        │   │
             │  │  • symbol role filtering          │   │
             │  │  • scatter detection              │   │
             │  │  • evidence completeness check    │   │
             │  └─────────────────────────────────┘   │
             └───────────────────┬────────────────────┘
                                 │
                      ┌──────────▼───────────┐
                      │   Answer + Evidence   │
                      └──────────────────────┘
```

Three intelligence layers — all enhancing the existing agent loop, NOT replacing it:

| Layer | Components | When |
|-------|-----------|------|
| Pre-loop | Query Classification + Workspace Priming | Before first LLM call |
| In-loop | 3 new tools + BudgetController + Guidance Engine | Each iteration |
| Post-loop | Evidence Verification (Phase 3) | Before returning answer |

---

## 4. Phase 1 — 基础强化 (Weeks 1-2)

### 4.1 Token-Based Budget Controller

**File:** `backend/app/agent_loop/budget.py`

**Why:** Iteration count is a poor budget unit — one iteration can consume 500 tokens or 50,000 tokens. The [ICLR 2026 study](https://openreview.net/forum?id=1bUeVB3fov) found input tokens dominate cost and same-task variance reaches 10x.

**Integration point:** In `AgentLoopService.run_stream()`, after each LLM response:
1. Call `budget.track(metrics)` with token counts from API response
2. Call `budget.get_signal()` — if `WARN_CONVERGE`, inject `budget_context` into next message; if `FORCE_CONCLUDE`, break loop
3. At session end, call `budget.to_trace()` for SessionTrace

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BudgetSignal(Enum):
    NORMAL = "normal"
    WARN_CONVERGE = "warn_converge"
    FORCE_CONCLUDE = "force_conclude"


@dataclass
class BudgetConfig:
    max_tokens: int = 500_000        # Total token budget per session
    warning_threshold: float = 0.7    # 70% — inject warning into prompt
    critical_threshold: float = 0.9   # 90% — force conclusion
    max_iterations: int = 25          # Hard iteration cap (keep existing)
    diminishing_returns_window: int = 3  # N iterations with no new info → converge


@dataclass
class IterationMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list = field(default_factory=list)
    new_files_accessed: int = 0
    new_symbols_found: int = 0


class BudgetController:
    """Embedded in AgentLoopService, checked after each iteration.

    Reference: "How Do Coding Agents Spend Your Money?" (ICLR 2026)
    https://openreview.net/forum?id=1bUeVB3fov
    """

    def __init__(self, config: BudgetConfig):
        self.config = config
        self.cumulative_input = 0
        self.cumulative_output = 0
        self.iteration_count = 0
        self.tool_token_breakdown: dict[str, int] = {}
        self.iteration_history: list[IterationMetrics] = []
        self.files_accessed: set[str] = set()
        self.symbols_resolved: set[str] = set()

    @property
    def total_tokens(self) -> int:
        return self.cumulative_input + self.cumulative_output

    @property
    def usage_ratio(self) -> float:
        if self.config.max_tokens == 0:
            return 1.0
        return self.total_tokens / self.config.max_tokens

    def track(self, metrics: IterationMetrics) -> None:
        """Call after each LLM iteration with token counts from API response."""
        self.cumulative_input += metrics.input_tokens
        self.cumulative_output += metrics.output_tokens
        self.iteration_count += 1
        self.iteration_history.append(metrics)
        for tc in metrics.tool_calls:
            self.tool_token_breakdown[tc.name] = (
                self.tool_token_breakdown.get(tc.name, 0) + tc.result_tokens
            )

    def get_signal(self) -> BudgetSignal:
        """Determine current budget signal for the agent."""
        if self.usage_ratio >= self.config.critical_threshold:
            return BudgetSignal.FORCE_CONCLUDE
        if self.usage_ratio >= self.config.warning_threshold:
            return BudgetSignal.WARN_CONVERGE
        if self.iteration_count >= self.config.max_iterations:
            return BudgetSignal.FORCE_CONCLUDE
        if self._detect_diminishing_returns():
            return BudgetSignal.WARN_CONVERGE
        return BudgetSignal.NORMAL

    def _detect_diminishing_returns(self) -> bool:
        """If last N iterations found no new files or symbols, signal convergence."""
        window = self.config.diminishing_returns_window
        if len(self.iteration_history) < window:
            return False
        recent = self.iteration_history[-window:]
        return all(
            m.new_files_accessed == 0 and m.new_symbols_found == 0
            for m in recent
        )

    @property
    def budget_context(self) -> str:
        """Text injected into the LLM prompt so it knows its budget status."""
        remaining = self.config.max_tokens - self.total_tokens
        return (
            f"[Budget: {self.total_tokens:,}/{self.config.max_tokens:,} tokens "
            f"({self.usage_ratio:.0%}). "
            f"Iterations: {self.iteration_count}/{self.config.max_iterations}. "
            f"Remaining: ~{remaining:,} tokens]"
        )

    def to_trace(self) -> dict:
        """Export for SessionTrace."""
        return {
            "total_input_tokens": self.cumulative_input,
            "total_output_tokens": self.cumulative_output,
            "iterations": self.iteration_count,
            "tool_breakdown": dict(self.tool_token_breakdown),
            "files_accessed": sorted(self.files_accessed),
            "symbols_resolved": sorted(self.symbols_resolved),
        }
```

### 4.2 Differentiated Tool Output Processing

**File:** `backend/app/code_tools/output_policy.py`

**Why:** Different tools produce fundamentally different output types. A uniform 30K char cutoff is wasteful for `file_outline` (small output) and lossy for `read_file` (cuts mid-function). Adaptive truncation based on remaining budget further optimizes token usage.

**Reference:** [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — "Use the smallest set of tokens that maximize desired outcomes."

```python
from dataclasses import dataclass, field
from typing import Optional, Literal


@dataclass
class OutputPolicy:
    max_results: Optional[int] = None       # For list-type outputs
    max_chars: Optional[int] = None         # For text-type outputs
    truncate_unit: Literal["char", "line", "ast_boundary"] = "char"
    sort_by: Optional[str] = None           # "relevance", "pagerank", None
    dedup: bool = False
    include_meta: bool = True               # Add "[Truncated: X/Y chars]" footer
    boost_roles: list[str] = field(default_factory=list)
    downweight_roles: list[str] = field(default_factory=list)


TOOL_OUTPUT_POLICIES: dict[str, OutputPolicy] = {
    "grep": OutputPolicy(
        max_results=80,
        sort_by="relevance",
        dedup=True,
        truncate_unit="line",
    ),
    "read_file": OutputPolicy(
        max_chars=50_000,
        truncate_unit="ast_boundary",   # Truncate at function/class boundary
        include_meta=True,
    ),
    "file_outline": OutputPolicy(
        max_chars=None,                 # Rarely needs truncation
    ),
    "find_symbol": OutputPolicy(
        max_results=20,
        sort_by="pagerank",
        boost_roles=["route_entry", "business_logic", "domain_model", "state_machine"],
        downweight_roles=["utility", "infrastructure", "generated", "test_only"],
    ),
    "find_references": OutputPolicy(
        max_results=30,
        boost_roles=["route_entry", "business_logic", "domain_model"],
        downweight_roles=["utility", "infrastructure", "generated", "test_only"],
    ),
    "trace_variable": OutputPolicy(max_chars=40_000),
    "get_callees": OutputPolicy(max_results=30),
    "get_callers": OutputPolicy(max_results=30),
    "get_dependencies": OutputPolicy(max_results=20),
    "get_dependents": OutputPolicy(max_results=20),
    "git_log": OutputPolicy(max_results=50),
    "git_diff": OutputPolicy(max_chars=40_000),
    "git_blame": OutputPolicy(max_chars=30_000),
    "git_show": OutputPolicy(max_chars=40_000),
    "ast_search": OutputPolicy(max_results=20),
    "list_files": OutputPolicy(max_results=200),
    "find_tests": OutputPolicy(max_results=20),
    "test_outline": OutputPolicy(max_chars=None),
}


def apply_policy(tool_name: str, raw_output: str, budget_remaining: int = 500_000) -> str:
    """Apply output policy with adaptive adjustment based on remaining budget.

    If budget is running low (<100K tokens remaining), reduce limits by 50%
    to conserve context space for the agent's reasoning.
    """
    policy = TOOL_OUTPUT_POLICIES.get(tool_name, OutputPolicy(max_chars=30_000))

    effective_max_chars = policy.max_chars
    effective_max_results = policy.max_results

    # Adaptive: shrink limits when budget is low
    if budget_remaining < 100_000:
        if effective_max_chars:
            effective_max_chars = effective_max_chars // 2
        if effective_max_results:
            effective_max_results = effective_max_results // 2

    result = raw_output

    # Apply character-level truncation
    if effective_max_chars and len(result) > effective_max_chars:
        if policy.truncate_unit == "ast_boundary":
            result = _truncate_at_ast_boundary(result, effective_max_chars)
        elif policy.truncate_unit == "line":
            lines = result.split("\n")
            result = "\n".join(lines[: effective_max_results or 80])
        else:
            result = result[:effective_max_chars]

        if policy.include_meta:
            result += (
                f"\n\n[Truncated: showing {len(result):,}/{len(raw_output):,} chars. "
                f"Use line_start/line_end parameters to read specific sections.]"
            )

    return result


def _truncate_at_ast_boundary(text: str, max_chars: int) -> str:
    """Truncate at the nearest function/class boundary before max_chars.

    Looks backward from max_chars for common AST boundary patterns:
    - 'def ' at start of line
    - 'class ' at start of line
    - blank line between top-level blocks
    """
    if len(text) <= max_chars:
        return text

    search_region = text[max(0, max_chars - 500) : max_chars]
    # Look for last function/class definition boundary
    for marker in ["\ndef ", "\nclass ", "\n\n"]:
        idx = search_region.rfind(marker)
        if idx >= 0:
            cut_point = max(0, max_chars - 500) + idx
            return text[:cut_point]

    return text[:max_chars]
```

### 4.3 Symbol Role Classification

**File:** `backend/app/code_tools/symbol_roles.py`

**Why:** The most common efficiency loss in code agents is wasting iterations on utility/infrastructure noise. Symbol role classification, integrated into `find_symbol` and `find_references` output ranking, filters noise at the source.

```python
from enum import Enum
from typing import Optional


class SymbolRole(str, Enum):
    ROUTE_ENTRY = "route_entry"
    BUSINESS_LOGIC = "business_logic"
    DOMAIN_MODEL = "domain_model"
    STATE_MACHINE = "state_machine"
    CONFIGURATION = "configuration"
    EXTERNAL_CLIENT = "external_client"
    INFRASTRUCTURE = "infrastructure"
    UTILITY = "utility"
    TEST_ONLY = "test_only"
    GENERATED = "generated"


# Higher weight = shown first in tool results
ROLE_WEIGHTS: dict[SymbolRole, float] = {
    SymbolRole.ROUTE_ENTRY: 1.0,
    SymbolRole.BUSINESS_LOGIC: 0.9,
    SymbolRole.STATE_MACHINE: 0.85,
    SymbolRole.DOMAIN_MODEL: 0.8,
    SymbolRole.EXTERNAL_CLIENT: 0.7,
    SymbolRole.CONFIGURATION: 0.6,
    SymbolRole.INFRASTRUCTURE: 0.3,
    SymbolRole.UTILITY: 0.2,
    SymbolRole.TEST_ONLY: 0.1,
    SymbolRole.GENERATED: 0.05,
}

HTTP_DECORATORS = frozenset({
    "app.route", "app.get", "app.post", "app.put", "app.delete", "app.patch",
    "router.get", "router.post", "router.put", "router.delete", "router.patch",
    "api_view", "action", "RequestMapping", "GetMapping", "PostMapping",
    "PutMapping", "DeleteMapping", "PatchMapping",
})

ORM_BASES = frozenset({
    "Base", "BaseModel", "Model", "Document", "Schema", "Table",
    "DeclarativeBase", "SQLModel",
})

INFRA_PATHS = frozenset({
    "/utils/", "/util/", "/infra/", "/common/", "/helpers/",
    "/lib/", "/shared/", "/middleware/",
})


def classify_symbol_role(
    symbol_name: str,
    file_path: str,
    decorators: list[str] | None = None,
    base_classes: list[str] | None = None,
    body_keywords: set[str] | None = None,
) -> SymbolRole:
    """Classify a symbol's role based on AST metadata and heuristics.

    Uses tree-sitter AST information already available in existing tools.
    Integrated into find_symbol/find_references output for ranking.
    """
    decorators = decorators or []
    base_classes = base_classes or []

    # Route entry: HTTP decorators
    if any(d in HTTP_DECORATORS for d in decorators):
        return SymbolRole.ROUTE_ENTRY

    # Domain model: inherits from ORM/Pydantic bases
    if any(b in ORM_BASES for b in base_classes):
        return SymbolRole.DOMAIN_MODEL

    # Test: test file patterns
    fname = file_path.split("/")[-1]
    if (
        fname.startswith("test_")
        or fname.endswith("_test.py")
        or "/tests/" in file_path
        or "/test/" in file_path
    ):
        return SymbolRole.TEST_ONLY

    # Generated code
    if "/generated/" in file_path or "/gen/" in file_path:
        return SymbolRole.GENERATED

    # Infrastructure: utility directories
    if any(p in file_path for p in INFRA_PATHS):
        return SymbolRole.INFRASTRUCTURE

    # External client: naming patterns
    lower_name = symbol_name.lower()
    if any(k in lower_name for k in ("client", "connector", "adapter", "gateway", "proxy")):
        return SymbolRole.EXTERNAL_CLIENT

    # Configuration
    if any(k in lower_name for k in ("config", "settings", "options", "env")):
        return SymbolRole.CONFIGURATION

    # State machine: body keyword patterns
    if body_keywords and body_keywords & {"state", "transition", "FSM", "StateMachine", "status"}:
        return SymbolRole.STATE_MACHINE

    # Default: business logic
    return SymbolRole.BUSINESS_LOGIC


def rank_symbols_by_role(symbols: list, boost: list[str], downweight: list[str]) -> list:
    """Re-rank a list of symbol results by their role weight.

    Used inside find_symbol and find_references to surface
    business-relevant results and suppress noise.
    """
    def score(sym):
        role = classify_symbol_role(
            sym.name, sym.file_path, sym.decorators, sym.base_classes
        )
        weight = ROLE_WEIGHTS.get(role, 0.5)
        # Apply boost/downweight from OutputPolicy
        if role.value in boost:
            weight *= 1.5
        elif role.value in downweight:
            weight *= 0.3
        return weight

    return sorted(symbols, key=score, reverse=True)
```

### 4.4 Structured Session Trace

**File:** `backend/app/agent_loop/trace.py`

**Why:** Without structured observability data, you cannot know which queries perform well/poorly, which tools are most used, or where the agent wastes tokens. This data is also the foundation for offline evaluation and prompt optimization (see [RAG-Gym](https://arxiv.org/abs/2502.13957)).

```python
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class SessionTrace:
    session_id: str
    query: str
    query_type: str = ""
    iterations: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    tools_used: dict[str, int] = field(default_factory=dict)
    tool_latencies_ms: dict[str, list[int]] = field(default_factory=dict)
    files_accessed: list[str] = field(default_factory=list)
    symbols_resolved: list[str] = field(default_factory=list)
    budget_signals: list[str] = field(default_factory=list)
    outcome: str = ""       # success | budget_exceeded | max_iterations | error
    wall_time_ms: int = 0
    answer_has_evidence: bool = False
    error_message: Optional[str] = None

    _start_time: float = field(default_factory=time.time, repr=False)

    def record_tool_call(self, tool_name: str, latency_ms: int) -> None:
        self.tools_used[tool_name] = self.tools_used.get(tool_name, 0) + 1
        self.tool_latencies_ms.setdefault(tool_name, []).append(latency_ms)

    def record_budget_signal(self, signal: str) -> None:
        self.budget_signals.append(f"iter={self.iterations}:{signal}")

    def finalize(self, outcome: str, has_evidence: bool = False) -> None:
        self.outcome = outcome
        self.answer_has_evidence = has_evidence
        self.wall_time_ms = int((time.time() - self._start_time) * 1000)

    def save(self, trace_dir: str = ".conductor/session_traces") -> Path:
        Path(trace_dir).mkdir(parents=True, exist_ok=True)
        path = Path(trace_dir) / f"{self.session_id}.json"
        data = asdict(self)
        data.pop("_start_time", None)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return path

    def summary(self) -> str:
        top_tools = sorted(self.tools_used.items(), key=lambda x: -x[1])[:5]
        total_tokens = self.total_input_tokens + self.total_output_tokens
        return (
            f"Session {self.session_id}: {self.outcome}\n"
            f"  Query type: {self.query_type}\n"
            f"  Iterations: {self.iterations}, Tokens: {total_tokens:,}\n"
            f"  Top tools: {', '.join(f'{t}({c})' for t, c in top_tools)}\n"
            f"  Files: {len(self.files_accessed)}, Symbols: {len(self.symbols_resolved)}\n"
            f"  Wall time: {self.wall_time_ms:,}ms, Evidence: {self.answer_has_evidence}"
        )
```

### 4.5 Config Unification + RAG Cleanup

**File:** Modify `backend/app/config.py`

Steps:
1. Merge `AppSettings` and `ConductorConfig` into a single `ConductorConfig` class with sub-sections: `server`, `git`, `ai`, `tools`, `auth`, `code_search`
2. Remove all RAG remnants: `embedding_model`, `reranking`, `chunk_size`, `vector_store`, `cocoindex_*`
3. Delete `embeddings/__init__.py` (38-byte stub)
4. Update `rag/router.py`: add removal timeline to deprecation message
5. Clean `requirements.txt`: confirm CocoIndex, LiteLLM RAG dependencies removed
6. Add startup validation: check required fields, print clear error on missing config

---

## 5. Phase 2 — Context Intelligence (Weeks 3-5)

### 5.1 Query Classifier

**File:** `backend/app/agent_loop/query_classifier.py`

**Why:** Different question types need fundamentally different search strategies. An entry point question needs 3-5 iterations; a root cause analysis might need 10-15. Classifying upfront lets us set appropriate budgets and suggest the right initial tools.

**Implementation:** Start with keyword matching (zero latency, no LLM cost). Upgrade to lightweight LLM classification (Haiku) only if accuracy is insufficient.

```python
from dataclasses import dataclass


@dataclass
class QueryClassification:
    query_type: str
    strategy: str
    initial_tools: list[str]
    budget_level: str                # "low" | "medium" | "high"
    suggested_token_budget: int


QUERY_TYPES = {
    "entry_point_discovery": {
        "description": "Find the entry point for a feature or endpoint",
        "strategy": "grep routes/endpoints → trace inward to handlers",
        "initial_tools": ["grep", "find_symbol"],
        "budget_level": "low",
        "suggested_token_budget": 200_000,
        "keywords": ["entry", "endpoint", "route", "handler", "where does", "where is"],
    },
    "business_flow_tracing": {
        "description": "Trace a complete business process path",
        "strategy": "find entry → trace callees → trace data flow",
        "initial_tools": ["find_symbol", "get_callees", "trace_variable"],
        "budget_level": "medium",
        "suggested_token_budget": 400_000,
        "keywords": ["flow", "process", "trace", "how does", "what happens", "step by step"],
    },
    "root_cause_analysis": {
        "description": "Analyze the root cause of a bug or error",
        "strategy": "find error location → trace callers → check data flow",
        "initial_tools": ["grep", "find_references", "get_callers"],
        "budget_level": "high",
        "suggested_token_budget": 500_000,
        "keywords": ["bug", "error", "fail", "why", "root cause", "debug", "crash", "exception"],
    },
    "impact_analysis": {
        "description": "Assess the impact of modifying code",
        "strategy": "find dependents → trace forward → check tests",
        "initial_tools": ["get_dependents", "find_references", "find_tests"],
        "budget_level": "medium",
        "suggested_token_budget": 350_000,
        "keywords": ["impact", "affect", "break", "blast radius", "change", "modify", "refactor"],
    },
    "architecture_question": {
        "description": "Understand overall architecture or module relationships",
        "strategy": "module_summary top dirs → get_dependencies → compressed_view key files",
        "initial_tools": ["list_files", "module_summary", "get_dependencies"],
        "budget_level": "medium",
        "suggested_token_budget": 300_000,
        "keywords": ["architecture", "structure", "organized", "overview", "design", "modules"],
    },
    "config_analysis": {
        "description": "Understand the impact of configuration settings",
        "strategy": "grep config key → trace where it's used → check consumers",
        "initial_tools": ["grep", "find_references", "trace_variable"],
        "budget_level": "low",
        "suggested_token_budget": 200_000,
        "keywords": ["config", "setting", "flag", "environment", "variable", "option"],
    },
    "data_lineage": {
        "description": "Trace how data flows through the system",
        "strategy": "find data source → trace_variable forward → find sinks",
        "initial_tools": ["trace_variable", "find_references", "grep"],
        "budget_level": "high",
        "suggested_token_budget": 450_000,
        "keywords": ["data", "lineage", "transform", "input", "flows to", "passed to", "stored"],
    },
}


def classify_query(question: str) -> QueryClassification:
    """Classify user question using keyword matching.

    Upgrade path: Replace with lightweight LLM call (Haiku) if
    keyword accuracy proves insufficient in production traces.
    """
    q_lower = question.lower()
    best_type = "business_flow_tracing"  # safe default
    best_score = 0

    for qtype, spec in QUERY_TYPES.items():
        score = sum(1 for kw in spec["keywords"] if kw in q_lower)
        if score > best_score:
            best_score = score
            best_type = qtype

    spec = QUERY_TYPES[best_type]
    return QueryClassification(
        query_type=best_type,
        strategy=spec["strategy"],
        initial_tools=spec["initial_tools"],
        budget_level=spec["budget_level"],
        suggested_token_budget=spec["suggested_token_budget"],
    )
```

### 5.2 Enhanced Workspace Priming

**File:** `backend/app/agent_loop/workspace_priming.py`

**Why:** The existing workspace reconnaissance (directory scan + key docs) gives the agent a starting map. By adding RepoMap PageRank data and cached module summaries, we give it a much richer initial understanding — avoiding blind exploration.

**Reference:** [LingmaAgent](https://arxiv.org/abs/2406.01422) — top-down knowledge graph condensation reduces complexity.

```python
import json
from pathlib import Path
from typing import Optional


async def enhanced_workspace_priming(
    repo_path: str,
    user_query: str,
    repo_map_service,       # existing RepoMap service instance
    max_depth: int = 3,
) -> str:
    """Enhanced workspace initialization — gives agent a high-level map.

    Builds on existing reconnaissance by adding:
    - Top modules by PageRank importance
    - Cached module summaries (if available)
    - Query-relevant directory hints
    """
    sections = []

    # 1. Directory structure (existing behavior, keep as-is)
    dir_tree = _scan_directory(repo_path, max_depth)
    sections.append(f"## Repository Structure\n```\n{dir_tree}\n```")

    # 2. NEW: Top modules by PageRank
    try:
        top_modules = repo_map_service.get_top_modules(k=10)
        if top_modules:
            lines = [
                f"  {i+1}. **{mod.name}** (importance: {mod.pagerank:.3f}, "
                f"{mod.file_count} files, {mod.loc:,} LOC)"
                for i, mod in enumerate(top_modules)
            ]
            sections.append("## Key Modules (ranked by importance)\n" + "\n".join(lines))
    except Exception:
        pass  # RepoMap may not be initialized

    # 3. NEW: Cached module summaries
    summaries_dir = Path(repo_path) / ".conductor" / "summaries"
    if summaries_dir.exists():
        cached = []
        for f in sorted(summaries_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                if not _is_stale(data, repo_path):
                    cached.append(
                        f"  - **{data['module']}**: {data.get('responsibilities', 'N/A')}"
                    )
            except (json.JSONDecodeError, KeyError):
                continue
        if cached:
            sections.append("## Module Summaries (cached)\n" + "\n".join(cached))

    # 4. NEW: Query-relevant directory hints
    relevant = _match_query_to_dirs(user_query, dir_tree)
    if relevant:
        hints = "\n".join(f"  - `{d}`" for d in relevant[:5])
        sections.append(f"## Likely Relevant Directories\n{hints}")

    # 5. Key docs (existing behavior: README, CLAUDE.md, ARCHITECTURE.md)
    for doc_name in ["README.md", "CLAUDE.md", "ARCHITECTURE.md"]:
        doc_path = Path(repo_path) / doc_name
        if doc_path.exists():
            content = doc_path.read_text()[:3000]
            sections.append(f"## {doc_name}\n```\n{content}\n```")

    return "\n\n".join(sections)


def _scan_directory(repo_path: str, max_depth: int) -> str:
    """Scan directory tree up to max_depth. Reuse existing implementation."""
    # This should delegate to the existing scan_directory function
    # in AgentLoopService workspace reconnaissance
    pass


def _match_query_to_dirs(query: str, dir_tree: str) -> list[str]:
    """Simple keyword matching to find query-relevant directories."""
    query_terms = set(query.lower().replace("_", " ").split())
    dirs = [line.strip() for line in dir_tree.split("\n") if "/" in line]
    scored = []
    for d in dirs:
        dir_terms = set(d.lower().replace("/", " ").replace("_", " ").split())
        overlap = len(query_terms & dir_terms)
        if overlap > 0:
            scored.append((overlap, d))
    return [d for _, d in sorted(scored, reverse=True)]


def _is_stale(cache_data: dict, repo_path: str) -> bool:
    """Check if cached summary is stale based on git commit hash.

    Compare cache_data["commit_hash"] with current HEAD.
    If any source files changed since cached commit, return True.
    """
    import subprocess

    cached_commit = cache_data.get("commit_hash", "")
    if not cached_commit:
        return True

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", cached_commit, "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        changed_files = set(result.stdout.strip().split("\n"))
        source_files = set(cache_data.get("source_files", []))
        return bool(changed_files & source_files)
    except Exception:
        return True  # Assume stale on error
```

### 5.3 New Tool: compressed_view

**File:** `backend/app/code_tools/compressed_view.py`

**Reference:** [MutaGReP](https://arxiv.org/abs/2502.15872) — <5% of context achieves full-repo performance.

```python
"""Compressed file view: signatures + calls + side effects.

Saves ~80% tokens vs read_file while preserving structural information.
Agent uses this FIRST to understand a file, then expand_symbol for details.
"""

from typing import Optional


SIDE_EFFECT_PATTERNS = {
    "db_write": [
        "session.add", "session.commit", ".save()", ".create(", ".update(",
        ".delete(", "bulk_create", ".objects.create", "INSERT", "UPDATE",
        "db.add", "db.flush", "db.execute",
    ],
    "http_call": [
        "requests.", "httpx.", "aiohttp.", "fetch(", "urllib",
        "ClientSession", ".get(", ".post(",
    ],
    "event_publish": [
        "publish(", "emit(", "send_event(", "dispatch(", "notify(",
        "event_bus.", "broker.",
    ],
    "file_write": [
        "open(", ".write(", "Path(", "mkdir(", "shutil.", "copyfile",
    ],
    "cache_write": [
        "cache.set", "redis.", "memcached.", ".cache(",
    ],
}


def compressed_view(
    file_path: str,
    focus: Optional[str] = None,
    ast_parser=None,
) -> str:
    """Return compressed view: signatures + call relationships + side effects.

    Args:
        file_path: Target file path
        focus: Optional — focus on a specific symbol name

    Returns:
        Compressed representation (~80% smaller than full source)

    Example output:
        ## payment/service.py (245 lines, 8 symbols)

        class PaymentService:
            process_payment(user_id: str, amount: Decimal) -> PaymentResult
                calls: RiskService.validate(), LedgerClient.charge()
                side_effects: db write, event publish
                raises: InsufficientFundsError, RiskCheckFailedError

            refund(payment_id: str) -> RefundResult
                calls: LedgerClient.reverse(), NotificationService.send()
                side_effects: db write, event publish
    """
    tree = ast_parser.parse_file(file_path)
    symbols = ast_parser.extract_symbols(tree)

    if focus:
        symbols = [s for s in symbols if focus.lower() in s.name.lower()]

    header = f"## {file_path} ({tree.line_count} lines, {len(symbols)} symbols)\n"
    lines = [header]

    for sym in symbols:
        indent = "    " if sym.parent else ""
        # Signature
        lines.append(f"{indent}{sym.kind} {sym.signature}")

        # Callees
        callees = ast_parser.extract_callees(sym)
        if callees:
            callee_strs = [f"{c.target}()" for c in callees[:8]]
            if len(callees) > 8:
                callee_strs.append(f"... +{len(callees) - 8} more")
            lines.append(f"{indent}    calls: {', '.join(callee_strs)}")

        # Side effects
        effects = _detect_side_effects(sym.body_text)
        if effects:
            lines.append(f"{indent}    side_effects: {', '.join(effects)}")

        # Exceptions raised
        exceptions = ast_parser.extract_exceptions(sym)
        if exceptions:
            lines.append(f"{indent}    raises: {', '.join(exceptions)}")

        lines.append("")

    return "\n".join(lines)


def _detect_side_effects(body_text: str) -> list[str]:
    """Detect side effects by pattern matching in function body."""
    if not body_text:
        return []
    effects = []
    for effect_type, markers in SIDE_EFFECT_PATTERNS.items():
        if any(m in body_text for m in markers):
            effects.append(effect_type.replace("_", " "))
    return effects


# TOOL_DEFINITION for LLM protocol — add to TOOL_DEFINITIONS in schemas.py
COMPRESSED_VIEW_TOOL = {
    "name": "compressed_view",
    "description": (
        "Return a compressed view of a file showing function/class signatures, "
        "call relationships, side effects, and exceptions. Saves ~80% tokens vs "
        "read_file. Use this FIRST to understand a file's structure, then use "
        "read_file or expand_symbol only for specific symbols you need in detail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to analyze",
            },
            "focus": {
                "type": "string",
                "description": "Optional: focus on a specific symbol name",
            },
        },
        "required": ["file_path"],
    },
}
```

### 5.4 New Tool: module_summary

**File:** `backend/app/code_tools/module_summary.py`

**Reference:** [LingmaAgent](https://arxiv.org/abs/2406.01422) — condense repository into knowledge graph.

```python
"""Module-level summary: responsibilities, key services, dependencies.

Saves ~95% tokens vs reading all files in a module.
Results are cached in .conductor/summaries/ with git-based invalidation.
"""

import json
import subprocess
from pathlib import Path
from typing import Optional


def module_summary(
    module_path: str,
    repo_map_service=None,
    repo_root: str = ".",
    cache_dir: str = ".conductor/summaries",
) -> str:
    """Return high-level module summary with caching.

    Args:
        module_path: Path to the module directory

    Returns:
        Summary: responsibilities, services, models, deps, entry points

    Example output:
        ## Module: payment/ (12 files, 3,400 LOC)

        Key Services: PaymentService, RefundService
        Key Models: Payment, Refund, PaymentMethod
        External Dependencies: StripeClient, LedgerClient, RiskService

        Entry Points:
          - POST /api/payments → PaymentController.create
          - POST /api/refunds → RefundController.create

        Depends On: risk/, ledger/, notification/
        Depended By: order/, subscription/
    """
    # Check cache
    cached = _load_cache(module_path, cache_dir, repo_root)
    if cached:
        return cached

    # Build summary from AST analysis
    module = Path(module_path)
    py_files = list(module.rglob("*.py"))
    if not py_files:
        return f"## Module: {module_path}\nNo Python files found."

    total_loc = 0
    all_symbols = []
    for f in py_files:
        try:
            content = f.read_text()
            total_loc += len(content.splitlines())
            symbols = repo_map_service.get_file_symbols(str(f))
            all_symbols.extend(symbols)
        except Exception:
            continue

    # Classify symbols
    services = [s for s in all_symbols if s.kind == "class" and "Service" in s.name]
    models = [s for s in all_symbols if s.kind == "class" and _is_model(s)]
    controllers = [s for s in all_symbols if s.kind == "class" and "Controller" in s.name]
    routes = [s for s in all_symbols if _has_route_decorator(s)]

    # Dependencies from RepoMap
    deps = repo_map_service.get_module_dependencies(module_path) if repo_map_service else []
    dependents = repo_map_service.get_module_dependents(module_path) if repo_map_service else []

    # Format
    lines = [f"## Module: {module_path} ({len(py_files)} files, {total_loc:,} LOC)\n"]

    if services:
        lines.append(f"Key Services: {', '.join(s.name for s in services)}")
    if models:
        lines.append(f"Key Models: {', '.join(m.name for m in models)}")
    if controllers:
        lines.append(f"Controllers: {', '.join(c.name for c in controllers)}")

    if routes:
        lines.append("\nEntry Points:")
        for r in routes[:10]:
            lines.append(f"  - {r.method} {r.path} → {r.handler}")

    if deps:
        lines.append(f"\nDepends On: {', '.join(str(d) for d in deps[:10])}")
    if dependents:
        lines.append(f"Depended By: {', '.join(str(d) for d in dependents[:10])}")

    summary_text = "\n".join(lines)

    # Cache result
    _save_cache(module_path, summary_text, py_files, cache_dir, repo_root)

    return summary_text


def _is_model(symbol) -> bool:
    return any(
        b in ("Base", "BaseModel", "Model", "Document", "SQLModel", "Table")
        for b in getattr(symbol, "base_classes", [])
    )


def _has_route_decorator(symbol) -> bool:
    return any("route" in d.lower() or "mapping" in d.lower()
               for d in getattr(symbol, "decorators", []))


def _load_cache(module_path: str, cache_dir: str, repo_root: str) -> Optional[str]:
    cache_file = Path(cache_dir) / f"{module_path.replace('/', '_')}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        # Check staleness
        cached_commit = data.get("commit_hash", "")
        if cached_commit:
            result = subprocess.run(
                ["git", "diff", "--name-only", cached_commit, "HEAD"],
                cwd=repo_root, capture_output=True, text=True, timeout=5,
            )
            changed = set(result.stdout.strip().split("\n"))
            sources = set(data.get("source_files", []))
            if changed & sources:
                return None  # stale
        return data.get("summary", None)
    except Exception:
        return None


def _save_cache(
    module_path: str, summary: str, source_files: list[Path],
    cache_dir: str, repo_root: str,
) -> None:
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        )
        commit_hash = result.stdout.strip()
    except Exception:
        commit_hash = ""

    cache_file = Path(cache_dir) / f"{module_path.replace('/', '_')}.json"
    cache_file.write_text(json.dumps({
        "module": module_path,
        "summary": summary,
        "commit_hash": commit_hash,
        "source_files": [str(f) for f in source_files],
    }, indent=2))


# TOOL_DEFINITION
MODULE_SUMMARY_TOOL = {
    "name": "module_summary",
    "description": (
        "Return a high-level summary of a module/directory: key services, models, "
        "dependencies, and entry points. Saves ~95% tokens vs reading all files. "
        "Use this when deciding which part of the codebase to explore, or when "
        "answering architecture questions. Results are cached for performance."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "module_path": {
                "type": "string",
                "description": "Path to the module directory (e.g. 'backend/app/payment')",
            },
        },
        "required": ["module_path"],
    },
}
```

### 5.5 New Tool: expand_symbol

**File:** `backend/app/code_tools/expand_symbol.py`

**Reference:** [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — progressive disclosure pattern.

```python
"""Lazy expansion: expand a symbol from compressed view to full source.

Agent workflow: compressed_view → identify symbol → expand_symbol
This implements the "compress first, expand on demand" principle.
"""


def expand_symbol(
    symbol_name: str,
    depth: int = 1,
    repo_map_service=None,
) -> str:
    """Expand a symbol to its full source code.

    Args:
        symbol_name: Full symbol name (e.g. "PaymentService.process_payment")
        depth: 1 = this symbol only; 2 = also expand direct callees

    Returns:
        Full source code with file location metadata
    """
    location = repo_map_service.resolve_symbol(symbol_name)
    if not location:
        # Try fuzzy match
        candidates = repo_map_service.fuzzy_find_symbol(symbol_name, limit=3)
        if candidates:
            suggestion = "\n".join(f"  - {c.name} ({c.file_path})" for c in candidates)
            return (
                f"Symbol '{symbol_name}' not found. Did you mean:\n{suggestion}\n"
                f"Use the exact name from above to expand."
            )
        return f"Symbol '{symbol_name}' not found in the codebase."

    source = _extract_source(location.file_path, location.start_line, location.end_line)

    result = [
        f"## {symbol_name} [FULL SOURCE]",
        f"File: {location.file_path}:{location.start_line}-{location.end_line}",
        f"Role: {location.role}",
        "",
        source,
    ]

    if depth >= 2:
        callees = repo_map_service.get_callees(symbol_name)
        for callee in callees[:5]:
            callee_loc = repo_map_service.resolve_symbol(callee.target)
            if callee_loc:
                callee_source = _extract_source(
                    callee_loc.file_path, callee_loc.start_line, callee_loc.end_line
                )
                result.extend([
                    "",
                    f"## {callee.target} [CALLEE]",
                    f"File: {callee_loc.file_path}:{callee_loc.start_line}-{callee_loc.end_line}",
                    "",
                    callee_source,
                ])
        if len(callees) > 5:
            result.append(f"\n[{len(callees) - 5} more callees not shown. "
                          f"Use get_callees for the full list.]")

    return "\n".join(result)


def _extract_source(file_path: str, start_line: int, end_line: int) -> str:
    """Extract source lines from a file."""
    try:
        with open(file_path) as f:
            lines = f.readlines()
        return "".join(lines[start_line - 1 : end_line])
    except Exception as e:
        return f"Error reading {file_path}: {e}"


# TOOL_DEFINITION
EXPAND_SYMBOL_TOOL = {
    "name": "expand_symbol",
    "description": (
        "Expand a symbol from compressed_view to its full source code. "
        "Use after compressed_view when you need the complete implementation "
        "of a specific function or class. Set depth=2 to also see the source "
        "of direct callees (up to 5)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol_name": {
                "type": "string",
                "description": "Full symbol name, e.g. 'PaymentService.process_payment'",
            },
            "depth": {
                "type": "integer",
                "description": "1 = this symbol only (default), 2 = also expand direct callees",
                "default": 1,
            },
        },
        "required": ["symbol_name"],
    },
}
```

### 5.6 System Prompt Restructuring

**File:** `backend/app/agent_loop/prompts.py`

**Why:** The current 415-line system prompt risks the "lost in the middle" effect. Restructuring into 3 layers reduces the initial prompt size and only injects guidance when needed.

**Reference:** [Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — "Start minimal, test with best model; progressive disclosure via exploration."

```python
# ═══════════════════════════════════════════════════════
# LAYER 1: Core Identity (~100 lines, ALWAYS included)
# ═══════════════════════════════════════════════════════

CORE_IDENTITY = """You are a code intelligence agent specialized in repository-level code understanding. You navigate large codebases to answer questions with precision and evidence.

## Core Behavior

1. HYPOTHESIS-DRIVEN: Before exploring, state your hypothesis about where the answer lies.
2. EVIDENCE-BASED: Every claim must reference a specific file and line number.
3. EFFICIENT: Use compressed_view first to understand structure. Only use read_file or expand_symbol when you need implementation details.
4. BUDGET-AWARE: Monitor your token budget shown in [Budget: ...] tags. Converge toward an answer when budget runs low.

## Tool Usage Priority (most efficient first)

1. **module_summary** — understand a directory's purpose (~95% token savings)
2. **compressed_view** — see a file's structure: signatures, calls, side effects (~80% savings)
3. **expand_symbol** — read the full source of ONE specific symbol
4. **find_symbol / grep** — search across the codebase for specific names or patterns
5. **get_callees / get_callers** — follow call chains
6. **trace_variable** — trace data flow forward/backward
7. **read_file** — last resort for raw text (configs, data files, non-code)

## Output Format

Structure every answer with:
- **Direct answer** to the question (1-3 sentences)
- **Evidence**: file paths, line numbers, relevant code snippets
- **Call chain or data flow** (if applicable): Entry → A → B → C
- **Caveats**: uncertainties, areas not fully traced
"""


# ═══════════════════════════════════════════════════════
# LAYER 2: Strategy (~50 lines each, selected by query_type)
# ═══════════════════════════════════════════════════════

STRATEGIES = {
    "entry_point_discovery": """## Strategy: Entry Point Discovery
1. grep for route/endpoint patterns matching the query terms
2. Use find_symbol to locate handler functions
3. Use compressed_view on the handler file to understand structure
4. Trace inward using get_callees if the handler delegates
Target: 3-6 iterations.""",

    "business_flow_tracing": """## Strategy: Business Flow Tracing
1. Find the entry point (grep routes or find_symbol)
2. Use compressed_view to understand the entry handler
3. Follow the call chain using get_callees, building a flow diagram
4. For data transformations, use trace_variable to follow values
5. Summarize the complete flow: Entry → Service → Repository → External
Target: 6-12 iterations.""",

    "root_cause_analysis": """## Strategy: Root Cause Analysis
1. Find the error location (grep for error messages, exception types)
2. Use expand_symbol to read the error context in detail
3. Trace callers using get_callers — how do we reach this error?
4. Check data flow using trace_variable — what input causes the failure?
5. Check recent changes using git_log/git_diff for regression clues
Target: 8-15 iterations.""",

    "impact_analysis": """## Strategy: Impact Analysis
1. Find all dependents using get_dependents (who depends on this code?)
2. Use find_references to find all call sites
3. Use find_tests to identify test coverage
4. For each affected module, use compressed_view to assess severity
5. Summarize: affected modules, affected APIs, risk level
Target: 6-12 iterations.""",

    "architecture_question": """## Strategy: Architecture Overview
1. Use module_summary on top-level directories to understand responsibilities
2. Use get_dependencies to map module relationships
3. Use compressed_view on key service files for interface details
4. Build a dependency diagram: Module → depends on → Module
Target: 5-10 iterations.""",

    "config_analysis": """## Strategy: Config Analysis
1. grep for the config key/setting name
2. Use find_references to find all consumers
3. Use trace_variable to understand how the config value flows
4. Use compressed_view on consumer files for context
Target: 3-6 iterations.""",

    "data_lineage": """## Strategy: Data Lineage Tracing
1. Find the data origin point (grep or find_symbol)
2. Use trace_variable with forward direction to follow the data
3. At each step, note transformations and side effects
4. Identify all sinks (DB writes, API calls, event publishes)
5. Build a lineage diagram: Source → Transform → Transform → Sink
Target: 8-15 iterations.""",
}


# ═══════════════════════════════════════════════════════
# LAYER 3: Runtime Guidance (injected dynamically)
# ═══════════════════════════════════════════════════════

RUNTIME_GUIDANCE = {
    "budget_warning": (
        "[WARNING: You have used {usage}% of your token budget. "
        "Start converging. Summarize findings and identify remaining gaps.]"
    ),
    "budget_critical": (
        "[CRITICAL: Token budget nearly exhausted ({usage}%). "
        "Provide your best answer NOW with evidence collected so far.]"
    ),
    "scatter_warning": (
        "[WARNING: You've accessed {count} files across {dirs} different "
        "directories. This suggests unfocused exploration. Re-evaluate your "
        "hypothesis and narrow your search.]"
    ),
    "redundant_read": (
        "[NOTE: You've already read {file} in this session. The content "
        "is in your context. Use the information you already have.]"
    ),
    "evidence_reminder": (
        "[REMINDER: Before concluding, verify you have: "
        "(1) specific file:line references, "
        "(2) relevant code snippets, "
        "(3) traced the complete path.]"
    ),
}


def build_system_prompt(query_type: str, budget_context: str = "") -> str:
    """Assemble system prompt from layers."""
    parts = [CORE_IDENTITY]
    if query_type in STRATEGIES:
        parts.append(STRATEGIES[query_type])
    if budget_context:
        parts.append(budget_context)
    return "\n\n".join(parts)


def inject_guidance(key: str, **kwargs) -> str:
    """Generate a runtime guidance message for injection into conversation."""
    template = RUNTIME_GUIDANCE.get(key, "")
    return template.format(**kwargs) if template else ""
```

---

## 6. Phase 3 — Advanced Intelligence (Weeks 6-9)

### 6.1 RepoMap v2: Dataflow-Enhanced Graph

**Files:** Enhance `backend/app/repo_graph/`

**References:**
- [DraCo (arXiv:2405.19782)](https://arxiv.org/abs/2405.19782) — dataflow-guided retrieval, +3.43% exact match over text-similarity
- [RepoHyper (arXiv:2403.06095)](https://arxiv.org/abs/2403.06095) — Repo-level Semantic Graph + Expand-Refine retrieval
- [Code RAG (arXiv:2503.20589)](https://arxiv.org/abs/2503.20589) — in-context code is most effective retrieval type

Current RepoMap has AST → import/call dependency edges. Enhance with:

```
RepoMap v2 Schema
=================

Nodes (existing):
  - FileNode(path, language, loc)
  - SymbolNode(name, kind, file, line_start, line_end)

New node attribute:
  - SymbolNode.role: SymbolRole   # from symbol_roles.py

Edges (existing):
  - imports(file_a, file_b)
  - calls(symbol_a, symbol_b)
  - contains(file, symbol)

NEW — Dataflow Edges:
  - variable_flows_to(symbol_a, symbol_b, variable_name)
    Source: extend existing trace_variable's AST analysis
  - reads_config(symbol, config_key)
    Source: grep for config/settings access patterns
  - writes_to(symbol, sink_type)
    Source: side effect detection from compressed_view

NEW — Usage Pattern Edges:
  - call_frequency(symbol_a, symbol_b, count)
    Source: static analysis of call sites
  - change_coupling(file_a, file_b, score)
    Source: git log co-change analysis (files often modified together)
```

Implementation approach:
1. Extend tree-sitter parser to extract dataflow edges during AST scan
2. Add `git log --follow` analysis for change coupling scores
3. Store new edges in the existing networkx graph structure
4. Update PageRank computation to weight new edge types
5. Persist enhanced graph to `.conductor/knowledge_graph.json`

### 6.2 Evidence Evaluator (Process Supervision)

**File:** `backend/app/agent_loop/evaluator.py`

**Reference:** [RAG-Gym (arXiv:2502.13957)](https://arxiv.org/abs/2502.13957) — process supervision significantly improves agent search efficiency.

**Integration:** Called in `AgentLoopService._maybe_converge()` when the agent signals readiness to answer. Uses a small, fast model (Haiku) to minimize overhead.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EvalVerdict(Enum):
    SUFFICIENT = "sufficient"
    NEEDS_MORE = "needs_more"
    WRONG_DIRECTION = "wrong_direction"


@dataclass
class EvalResult:
    verdict: EvalVerdict
    modules_covered: bool
    code_paths_traced: bool
    has_evidence: bool
    gaps: list[str]


EVAL_PROMPT = """You are evaluating whether a code intelligence agent has
gathered sufficient evidence to answer a question accurately.

Question: {question}

Evidence collected:
- Files accessed: {files}
- Symbols found: {symbols}
- Draft answer (first 2000 chars): {draft_answer}

Evaluate honestly:
1. Are all relevant modules identified? (yes/no)
2. Are key code execution paths traced? (yes/no)
3. Does the draft cite specific file:line evidence? (yes/no)
4. What obvious gaps remain? (list, or "none")

Output JSON:
{{
    "verdict": "sufficient" | "needs_more" | "wrong_direction",
    "modules_covered": true | false,
    "code_paths_traced": true | false,
    "has_evidence": true | false,
    "gaps": ["...", "..."]
}}
"""


async def evaluate_evidence(
    question: str,
    draft_answer: str,
    files_accessed: list[str],
    symbols_found: list[str],
    provider,
) -> EvalResult:
    """Evaluate current evidence before concluding.

    Uses a lightweight model (Haiku) for speed and cost efficiency.
    Adds ~500-1000 tokens overhead per evaluation.
    """
    prompt = EVAL_PROMPT.format(
        question=question,
        files=", ".join(files_accessed[-20:]),
        symbols=", ".join(symbols_found[-20:]),
        draft_answer=draft_answer[:2000],
    )

    response = await provider.chat(
        model="haiku",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
    )

    return _parse_eval(response.content)


def _parse_eval(raw: str) -> EvalResult:
    """Parse evaluator JSON response, with fallback for malformed output."""
    import json
    try:
        data = json.loads(raw)
        return EvalResult(
            verdict=EvalVerdict(data.get("verdict", "needs_more")),
            modules_covered=data.get("modules_covered", False),
            code_paths_traced=data.get("code_paths_traced", False),
            has_evidence=data.get("has_evidence", False),
            gaps=data.get("gaps", []),
        )
    except (json.JSONDecodeError, ValueError):
        return EvalResult(
            verdict=EvalVerdict.NEEDS_MORE,
            modules_covered=False,
            code_paths_traced=False,
            has_evidence=False,
            gaps=["Evaluator response parsing failed"],
        )
```

### 6.3 Cross-Session Memory

**Directory:** `.conductor/`

```
.conductor/
├── symbol_index.json          # Existing
├── summaries/                 # Phase 2: cached module summaries
│   ├── payment.json
│   └── ledger.json
├── knowledge_graph.json       # Phase 3: enhanced RepoMap v2
├── query_patterns.json        # Phase 3: historical query analysis
└── session_traces/            # Phase 1: per-session structured traces
    ├── session_001.json
    └── session_002.json
```

`query_patterns.json` schema — built by analyzing `session_traces/`:

```json
{
    "common_entry_points": ["PaymentService", "AuthController", "OrderAPI"],
    "hot_modules": {"payment": 45, "auth": 32, "order": 28},
    "effective_strategies": {
        "root_cause_analysis": {
            "avg_iterations": 8.2,
            "avg_tokens": 320000,
            "success_rate": 0.85
        },
        "entry_point_discovery": {
            "avg_iterations": 3.5,
            "avg_tokens": 120000,
            "success_rate": 0.95
        }
    },
    "frequently_accessed_files": [
        "backend/app/payment/service.py",
        "backend/app/auth/controller.py"
    ]
}
```

This data feeds back into:
- **Query Classifier**: adjust token budgets based on historical success rates
- **Workspace Priming**: prioritize hot modules in initial context
- **Budget Controller**: use historical averages to set smarter defaults

---

## 7. Phase 4 — 长期能力 (Week 10+)

### 7.1 Impact Analyzer

Enhance existing `get_dependents` tool with:
- Forward traversal of RepoMap v2 dataflow edges
- Change coupling data from git history
- Output: affected modules, affected APIs, risk score (0.0-1.0)
- Reference: extends OpenAI proposal's "Impact Analyzer" concept

### 7.2 Side Effect Analyzer

Enhance existing `trace_variable` tool with:
- Extended sink detection: DB writes, HTTP calls, events, file writes, cache mutations
- User-configurable sink/source patterns via `.conductor/sink_patterns.yaml`
- Confidence levels: "confirmed" (static analysis) vs "probable" (heuristic)
- Cross-file flow continuation

### 7.3 Architecture Analyzer

Based on [LingmaAgent](https://arxiv.org/abs/2406.01422):
- Generate service dependency graph from RepoMap v2
- Detect cyclic dependencies
- Identify layer violations (e.g., controller calling repository directly)
- Dead code detection (PageRank score ≈ 0 + zero references)

### 7.4 Multi-Agent Collaboration

Based on [MANTRA (arXiv:2503.14340)](https://arxiv.org/abs/2503.14340) — 82.8% success rate with multi-agent:

```
Navigator Agent (Haiku — fast, cheap)
    → Decompose question into sub-tasks
    → Assign strategy per sub-task

Explorer Agent (Sonnet — strong reasoning)
    → Execute sub-tasks using tools
    → Collect evidence

Critic Agent (Haiku — fast evaluation)
    → Verify findings completeness
    → Identify gaps

Navigator Agent
    → Synthesize final answer from all findings
```

Uses existing multi-model config (8 models, 3 providers). Navigator/Critic use small models for cost; Explorer uses the strongest available model for quality.

---

## 8. 实施路线图

| Phase | Weeks | Deliverables | Files | Priority |
|-------|-------|-------------|-------|----------|
| 1 | 1-2 | BudgetController | `agent_loop/budget.py` | P0 |
| 1 | 1-2 | Tool Output Policies | `code_tools/output_policy.py` | P0 |
| 1 | 1-2 | Symbol Role Classification | `code_tools/symbol_roles.py` | P0 |
| 1 | 1-2 | Session Trace | `agent_loop/trace.py` | P0 |
| 1 | 1-2 | Config unification + RAG cleanup | `config.py` | P0 |
| 2a | 3-4 | Query Classifier | `agent_loop/query_classifier.py` | P1 |
| 2a | 3-4 | Enhanced Workspace Priming | `agent_loop/workspace_priming.py` | P1 |
| 2b | 4-5 | compressed_view tool | `code_tools/compressed_view.py` | P1 |
| 2b | 4-5 | module_summary tool | `code_tools/module_summary.py` | P1 |
| 2b | 4-5 | expand_symbol tool | `code_tools/expand_symbol.py` | P1 |
| 2c | 5 | System Prompt restructuring | `agent_loop/prompts.py` | P1 |
| 3a | 6-7 | RepoMap v2 (dataflow + usage) | `repo_graph/*.py` | P2 |
| 3b | 7-8 | Evidence Evaluator | `agent_loop/evaluator.py` | P2 |
| 3c | 8-9 | Cross-Session Memory | `.conductor/` schema | P2 |
| 4 | 10+ | Impact/Architecture/Multi-Agent | various | P3 |

---

## 9. 测试策略

### Unit Tests (per module)

```python
# tests/test_budget_controller.py
def test_normal_signal():
    bc = BudgetController(BudgetConfig(max_tokens=100_000))
    bc.track(IterationMetrics(input_tokens=10_000, output_tokens=1_000))
    assert bc.get_signal() == BudgetSignal.NORMAL

def test_warning_signal():
    bc = BudgetController(BudgetConfig(max_tokens=100_000, warning_threshold=0.5))
    bc.track(IterationMetrics(input_tokens=60_000, output_tokens=1_000))
    assert bc.get_signal() == BudgetSignal.WARN_CONVERGE

def test_force_conclude_signal():
    bc = BudgetController(BudgetConfig(max_tokens=100_000, critical_threshold=0.9))
    bc.track(IterationMetrics(input_tokens=95_000, output_tokens=1_000))
    assert bc.get_signal() == BudgetSignal.FORCE_CONCLUDE

def test_diminishing_returns():
    bc = BudgetController(BudgetConfig(max_tokens=1_000_000, diminishing_returns_window=3))
    for _ in range(3):
        bc.track(IterationMetrics(
            input_tokens=10_000, output_tokens=1_000,
            new_files_accessed=0, new_symbols_found=0,
        ))
    assert bc.get_signal() == BudgetSignal.WARN_CONVERGE

# tests/test_symbol_roles.py
def test_route_entry():
    role = classify_symbol_role("create_payment", "payment/views.py", decorators=["app.post"])
    assert role == SymbolRole.ROUTE_ENTRY

def test_domain_model():
    role = classify_symbol_role("Payment", "payment/models.py", base_classes=["BaseModel"])
    assert role == SymbolRole.DOMAIN_MODEL

def test_test_file():
    role = classify_symbol_role("test_payment", "tests/test_payment.py")
    assert role == SymbolRole.TEST_ONLY

def test_infrastructure():
    role = classify_symbol_role("format_date", "utils/helpers.py")
    assert role == SymbolRole.INFRASTRUCTURE

# tests/test_query_classifier.py
def test_entry_point():
    result = classify_query("Where does the /api/payments endpoint start?")
    assert result.query_type == "entry_point_discovery"

def test_root_cause():
    result = classify_query("Why does payment fail for international cards?")
    assert result.query_type == "root_cause_analysis"

def test_architecture():
    result = classify_query("How is the codebase organized?")
    assert result.query_type == "architecture_question"
```

### Integration Tests

- Tool output + BudgetController: verify adaptive truncation activates at low budget
- compressed_view + expand_symbol: verify round-trip (compress → identify → expand)
- Workspace Priming + module_summary: verify cached summaries are used
- Symbol role boosting: verify `find_symbol` results are re-ranked

### Deterministic Agent Tests

Mock LLM responses and verify:
- Tools are dispatched correctly based on query type
- Budget limits terminate the loop
- Convergence checkpoint fires at the right usage level
- Runtime guidance is injected at correct thresholds

### End-to-End Evaluation Questions

Run on a real repository with known answers:

| Query | Type | Expected |
|-------|------|----------|
| "Find all callers of function X" | entry_point_discovery | Correct callers listed |
| "What happens when endpoint Y is called?" | business_flow_tracing | Complete call chain |
| "Why does Z fail with error E?" | root_cause_analysis | Root cause identified |
| "What breaks if I change class W?" | impact_analysis | Affected modules listed |
| "How is the project organized?" | architecture_question | Module summary accurate |
| "Where is config KEY used?" | config_analysis | All consumers found |
| "How does user input reach the database?" | data_lineage | Complete data path |

---

## 10. 参考文献

### Industry Research

| # | Title | URL |
|---|-------|-----|
| 1 | Braintrust — "The canonical agent architecture: A while loop with tools" | https://www.braintrust.dev/blog/agent-while-loop |
| 2 | Anthropic — "Building Effective Agents" | https://www.anthropic.com/research/building-effective-agents |
| 3 | Anthropic — "Effective context engineering for AI agents" | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents |
| 4 | Letta — "Rearchitecting Letta's Agent Loop" | https://www.letta.com/blog/letta-v1-agent |
| 5 | Context Engineering Guide 2026 | https://www.the-ai-corner.com/p/context-engineering-guide-2026 |
| 6 | Redis — "AI Agent Architecture Patterns" | https://redis.io/blog/ai-agent-architecture-patterns/ |

### Academic Papers

| # | Title | arXiv / DOI | URL |
|---|-------|-------------|-----|
| 7 | MutaGReP: Execution-Free Repository-Grounded Plan Search | arXiv:2502.15872 | https://arxiv.org/abs/2502.15872 |
| 8 | DraCo: Dataflow-Guided Retrieval for Repo-Level Code | arXiv:2405.19782 | https://arxiv.org/abs/2405.19782 |
| 9 | RepoHyper: Search-Expand-Refine on Semantic Graphs | arXiv:2403.06095 | https://arxiv.org/abs/2403.06095 |
| 10 | LingmaAgent: Repository Exploration with Knowledge Graphs | arXiv:2406.01422 | https://arxiv.org/abs/2406.01422 |
| 11 | RAG-Gym: Process Supervision for Agents | arXiv:2502.13957 | https://arxiv.org/abs/2502.13957 |
| 12 | MANTRA: Multi-Agent Code Refactoring | arXiv:2503.14340 | https://arxiv.org/abs/2503.14340 |
| 13 | What to Retrieve for Code Generation (Code RAG) | arXiv:2503.20589 | https://arxiv.org/abs/2503.20589 |
| 14 | Agentic RAG Survey | arXiv:2501.09136 | https://arxiv.org/abs/2501.09136 |
| 15 | Agentic Reasoning with Tools | arXiv:2502.04644 | https://arxiv.org/abs/2502.04644 |
| 16 | Token Consumption in Coding Agents (ICLR 2026) | — | https://openreview.net/forum?id=1bUeVB3fov |
| 17 | REPOFUSE: Fused Dual Context for Repo-Level Code | arXiv:2402.14323 | https://arxiv.org/abs/2402.14323 |
| 18 | RepoCoder: Iterative Retrieval and Generation | arXiv:2303.12570 | https://arxiv.org/abs/2303.12570 |
| 19 | On The Importance of Reasoning for Context Retrieval | arXiv:2406.04464 | https://arxiv.org/abs/2406.04464 |
| 20 | Self-Taught Agentic Long Context Understanding | arXiv:2502.15920 | https://arxiv.org/abs/2502.15920 |

---

*End of specification. All code, architecture, and references above are sufficient for an implementation agent to execute the complete plan.*
