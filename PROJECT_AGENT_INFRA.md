# Project: Agent Infrastructure & Workflow Engine

Last updated: 2026-03-18
**Status: Phases A, B, C, D — COMPLETE**

## Goal

Extract hardcoded multi-agent orchestration into a config-driven workflow engine. Make agent definitions, dispatch strategies, and pipeline stages configurable via YAML + Markdown files. Add observability (Langfuse) and visual workflow editing (React Flow).

## Core Abstraction

Every task in Conductor follows the same pattern:

```
Input → Classifier → Route(s) → Agent(s) → Aggregation → Output
```

Two routing modes cover all current and foreseeable use cases:

| Mode | Behavior | Example |
|------|----------|---------|
| `first_match` | Classifier picks the best route; execute that route's pipeline | Code Explorer: "how does auth work?" → `business_flow_tracing` route |
| `parallel_all_matching` | Classifier picks all matching routes; execute in parallel, then run shared `post_pipeline` | PR Review: security + correctness + test_coverage all match → parallel → arbitrate → synthesize |

## Architecture Overview

```
┌───────────────────────────────────────────────────────────┐
│                    VS Code Extension                       │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ AI Config│  │ Workflow View │  │ Chat + @AI commands  │ │
│  │  Modal   │  │ (React Flow) │  │                      │ │
│  └──────────┘  └──────────────┘  └──────────────────────┘ │
└───────────────────────┬───────────────────────────────────┘
                        │ HTTP / SSE
┌───────────────────────▼───────────────────────────────────┐
│                    FastAPI Backend                          │
│  ┌──────────────────────────────────────────────────────┐  │
│  │                  WorkflowEngine                       │  │
│  │  ┌────────────┐  ┌───────────┐  ┌─────────────────┐  │  │
│  │  │ YAML Loader│  │ Classifier│  │ Route Dispatcher │  │  │
│  │  │ + .md parse│  │  Engine   │  │ (parallel/seq)   │  │  │
│  │  └────────────┘  └───────────┘  └─────────────────┘  │  │
│  └──────────────────────┬───────────────────────────────┘  │
│           ┌─────────────┼─────────────┐                    │
│           ▼             ▼             ▼                    │
│  ┌──────────────┐ ┌──────────┐ ┌───────────┐              │
│  │AgentLoop     │ │code_tools│ │ Langfuse  │              │
│  │Service       │ │(24 tools)│ │ @observe  │              │
│  │(unchanged)   │ │(unchanged│ │           │              │
│  └──────────────┘ └──────────┘ └───────────┘              │
└───────────────────────────────────────────────────────────┘
```

---

## Phase A: Workflow Engine & Config-Driven Agents

**Goal**: Replace hardcoded `agents.py` + `risk_classifier.py` + `query_classifier.py` keyword logic + `_run_multi_perspective_stream` with a unified, config-driven workflow engine.

### A.1 — Config file structure and Pydantic models

Create the config file hierarchy and validation layer.

**File structure:**
```
config/
├── workflows/
│   ├── pr_review.yaml              # PR review — parallel_all_matching
│   └── code_explorer.yaml          # Code intelligence — first_match
│
├── agents/
│   ├── # ── PR Review: explorers ──
│   ├── correctness.md
│   ├── concurrency.md
│   ├── security.md
│   ├── reliability.md
│   ├── test_coverage.md
│   │
│   ├── # ── PR Review: judges ──
│   ├── arbitrator.md
│   ├── review_synthesizer.md
│   │
│   ├── # ── Code Explorer: multi-agent route (business_flow_tracing) ──
│   ├── explore_implementation.md
│   ├── explore_usage.md
│   ├── explore_synthesizer.md
│   │
│   ├── # ── Code Explorer: single-agent routes ──
│   ├── explore_root_cause.md
│   ├── explore_entry_point.md
│   ├── explore_impact.md
│   ├── explore_architecture.md
│   ├── explore_data_lineage.md
│   ├── explore_config.md
│   └── explore_recent_changes.md
│
└── prompts/
    ├── review_base.md              # Shared review agent prompt (provability test, severity, output format)
    └── explorer_base.md            # Shared explorer prompt (CORE_IDENTITY: workspace, budget, core behavior)
```

17 agent files, 2 workflow YAMLs, 2 shared prompt templates.

**Files to create:**
- All config files listed above
- `backend/app/workflow/__init__.py`
- `backend/app/workflow/models.py` — Pydantic models

**Pydantic models:**
```python
class AgentConfig(BaseModel):
    name: str
    type: Literal["explorer", "judge"]
    model_role: Literal["explorer", "strong", "classifier"] = "explorer"
    category: Optional[str] = None
    tools: ToolsConfig                    # { core: bool, extra: list[str] }
    budget_weight: float = 1.0
    max_tokens: Optional[int] = None      # judge agents only
    file_scope: list[str] = ["business_logic"]
    input: list[str] = []
    output: Optional[str] = None
    instructions: str = ""                # Markdown body from .md file

class RouteConfig(BaseModel):
    text_patterns: list[str] = []         # for keyword_pattern classifier
    file_patterns: list[str] = []         # for risk_pattern classifier
    boost_rules: list[BoostRule] = []
    pipeline: list[StageConfig]           # this route's pipeline stages
    delegate: Optional[str] = None        # delegate to another workflow YAML

class StageConfig(BaseModel):
    stage: str                            # stage name
    parallel: bool = False
    agents: list[str]                     # paths to agent .md files

class ClassifierConfig(BaseModel):
    type: Literal["risk_pattern", "keyword_pattern"]
    thresholds: Optional[ThresholdConfig] = None

class WorkflowConfig(BaseModel):
    name: str
    description: str = ""
    prompt_template: Optional[str] = None
    budget: BudgetDefaults
    core_tools: list[str] = []
    route_mode: Literal["first_match", "parallel_all_matching"]
    dispatch: DispatchConfig              # classifier config + llm_fallback
    routes: dict[str, RouteConfig]        # dimension/query_type → route
    post_pipeline: list[StageConfig] = [] # shared stages after all routes (parallel_all_matching only)
    post_processing: Optional[PostProcessingConfig] = None
```

**Workflow YAML — `pr_review.yaml`:**
```yaml
name: pr-review
description: Multi-agent PR code review
prompt_template: prompts/review_base.md
route_mode: parallel_all_matching

budget:
  base_tokens: 800_000
  base_iterations: 40
  sub_fraction: 0.7
  min_iterations: 8
  size_multiplier:
    small:  { max_lines: 500,  factor: 0.5 }
    medium: { max_lines: 2000, factor: 1.0 }
    large:  { max_lines: 5000, factor: 1.5 }
    xl:     { max_lines: 99999, factor: 2.0 }
  reject_above: 8000

core_tools:
  - grep
  - read_file
  - find_symbol
  - file_outline
  - compressed_view
  - expand_symbol

dispatch:
  mode: hybrid
  classifier:
    type: risk_pattern
    thresholds:
      high:   { count: 5, ratio: 0.3 }
      medium: { count: 2, ratio: 0.15 }
  llm_fallback:
    enabled: true
    when: "active_agents <= 1"
    model_role: classifier

routes:
  correctness:
    file_patterns:
      - "state.?machine|status|transition|workflow"
      - "persist|save|update|delete|insert|upsert"
      - "transaction|commit|rollback"
      - "validat|assert|check|verify|constrain"
      - "schema|migration|alter.?table"
    boost_rules:
      - when: "business_logic_files >= 10 or changed_lines > 2000"
        min_level: medium
      - when: "schema_files > 0"
        min_level: medium
    pipeline:
      - stage: explore
        agents: [agents/correctness.md]

  concurrency:
    file_patterns:
      - "consumer|listener|handler|callback|webhook"
      - "queue|mq|kafka|sqs|rabbit|amqp|pubsub"
      - "async|await|thread|lock|mutex|semaphore"
      - "retry|backoff|idempoten"
      - "worker|job|task|celery"
    pipeline:
      - stage: explore
        agents: [agents/concurrency.md]

  security:
    file_patterns:
      - "auth|login|logout|session|token|jwt|oauth|sso"
      - "password|secret|credential|api.?key"
      - "permission|rbac|acl|role|access.?control"
      - "encrypt|decrypt|hash|hmac|sign|verify"
      - "sanitiz|escap|xss|csrf|inject|sql"
    pipeline:
      - stage: explore
        agents: [agents/security.md]

  reliability:
    file_patterns:
      - "exception|error|catch|throw|raise"
      - "timeout|deadline|circuit.?breaker"
      - "fallback|recover|graceful|shutdown"
    pipeline:
      - stage: explore
        agents: [agents/reliability.md]

  test_coverage:
    file_patterns: []     # always_run — see trigger.always in agent .md
    pipeline:
      - stage: explore
        agents: [agents/test_coverage.md]

# Shared stages after all parallel routes complete
post_pipeline:
  - stage: arbitrate
    agents: [agents/arbitrator.md]
  - stage: synthesize
    agents: [agents/review_synthesizer.md]

post_processing:
  min_confidence: 0.6
  max_findings_per_agent: 5
  evidence_gate:
    critical_min_evidence: 2
    critical_require_file: true
    critical_require_line: true
    critical_min_tool_calls: 3
```

**Workflow YAML — `code_explorer.yaml`:**
```yaml
name: code-explorer
description: Agentic code intelligence — answer questions about the codebase
prompt_template: prompts/explorer_base.md
route_mode: first_match

budget:
  base_tokens: 500_000
  base_iterations: 25
  sub_fraction: 0.6
  min_iterations: 10

core_tools:
  - grep
  - read_file
  - find_symbol
  - file_outline
  - compressed_view
  - expand_symbol

dispatch:
  mode: classifier
  classifier:
    type: keyword_pattern

routes:
  business_flow_tracing:
    text_patterns:
      - "flow|process|trace|how does|what happens"
      - "step by step|lifecycle|pipeline|journey|sequence"
    pipeline:
      - stage: explore
        parallel: true
        agents:
          - agents/explore_implementation.md
          - agents/explore_usage.md
      - stage: synthesize
        agents:
          - agents/explore_synthesizer.md

  root_cause_analysis:
    text_patterns:
      - "bug|error|fail|why|root cause|debug|crash|exception|broken"
    pipeline:
      - stage: investigate
        agents: [agents/explore_root_cause.md]

  entry_point_discovery:
    text_patterns:
      - "entry|endpoint|route|handler|where does|where is|which file"
    pipeline:
      - stage: investigate
        agents: [agents/explore_entry_point.md]

  impact_analysis:
    text_patterns:
      - "impact|affect|break|blast radius|change|modify|refactor|deprecate"
    pipeline:
      - stage: investigate
        agents: [agents/explore_impact.md]

  architecture_question:
    text_patterns:
      - "architecture|structure|overview|design|modules|layers|components"
    pipeline:
      - stage: investigate
        agents: [agents/explore_architecture.md]

  data_lineage:
    text_patterns:
      - "data|lineage|transform|flows to|stored|database|persist|column"
    pipeline:
      - stage: investigate
        agents: [agents/explore_data_lineage.md]

  config_analysis:
    text_patterns:
      - "config|setting|flag|environment|variable|toggle|feature flag"
    pipeline:
      - stage: investigate
        agents: [agents/explore_config.md]

  recent_changes:
    text_patterns:
      - "recent|changed|commit|who|when|blame|history|last week"
    pipeline:
      - stage: investigate
        agents: [agents/explore_recent_changes.md]

  code_review:
    text_patterns:
      - "review|pr review|pull request|do pr|check the pr|review changes"
    delegate: workflows/pr_review.yaml
```

**Agent .md example — single-agent route:**
```markdown
# config/agents/explore_root_cause.md
---
name: explore_root_cause
type: explorer
model_role: explorer

tools:
  core: true
  extra:
    - find_references
    - get_callers
    - get_callees
    - trace_variable
    - git_log
    - git_diff
    - git_blame
    - git_show
    - find_tests
    - detect_patterns

budget_weight: 1.0
input: [query, workspace_layout]
output: answer
---

## Strategy: Root Cause Analysis

1. Find the error location (grep for error messages, exception types)
2. Use expand_symbol to read the error context in detail
3. Trace callers using get_callers — how do we reach this error?
4. Check data flow using trace_variable — what input causes the failure?
5. Use detect_patterns on the affected module to find check-then-act races,
   missing retry logic, or transaction gaps.
6. Check recent changes using git_log/git_diff for regression clues

Target: 8-15 iterations. Answer with root cause, evidence chain, and fix suggestion.
```

**Acceptance criteria:**
- [ ] All Pydantic models validate the YAML/frontmatter schemas
- [ ] `WorkflowConfig` round-trips: load → serialize → load produces identical config
- [ ] PR review YAML + agent .md files contain all data currently hardcoded in `agents.py` + `risk_classifier.py`
- [ ] Code explorer YAML + agent .md files contain all data currently hardcoded in `query_classifier.py` QUERY_TYPES + `prompts.py` STRATEGIES + `_run_multi_perspective_stream`

### A.2 — Workflow loader

Load and validate YAML + Markdown agent files.

**Files to create:**
- `backend/app/workflow/loader.py`

**Responsibilities:**
- `load_workflow(path) → WorkflowConfig` — parse YAML, resolve agent file references, resolve `delegate` references
- `load_agent(path) → AgentConfig` — parse Markdown frontmatter (YAML) + body
- Validate `input`/`output` declarations against pipeline stage ordering
- Resolve `prompt_template` path → load shared prompt Markdown
- Resolve `core: true` in agent tools → expand to core_tools list from workflow
- Config file search order: `./config/`, `../config/`, `~/.conductor/`

**Acceptance criteria:**
- [ ] `load_workflow("workflows/pr_review.yaml")` returns fully populated `WorkflowConfig`
- [ ] `load_workflow("workflows/code_explorer.yaml")` returns fully populated `WorkflowConfig` with 9 routes
- [ ] `delegate: workflows/pr_review.yaml` in code_review route → resolved to loaded WorkflowConfig
- [ ] Missing agent file → clear error message with path
- [ ] Invalid frontmatter → validation error with field name and line
- [ ] Agent with `input: [findings]` in stage before findings are produced → load-time error
- [ ] Unit tests: 20+ covering happy path, missing files, invalid YAML, input/output validation, delegate resolution

### A.3 — Classifier engine

Replace `risk_classifier.py` and `query_classifier.py` keyword logic with a generic, config-driven classifier.

**Files to create:**
- `backend/app/workflow/classifier_engine.py`

**Built-in classifier types:**
- `risk_pattern` — regex match against file paths → dimension risk levels (replaces `risk_classifier.py`)
- `keyword_pattern` — regex match against query text → best matching route (replaces keyword part of `query_classifier.py`)

**Interface:**
```python
class ClassifierEngine:
    def __init__(self, config: ClassifierConfig, routes: dict[str, RouteConfig]): ...

    def classify(self, signals: dict) -> ClassifierResult:
        """
        signals for risk_pattern:   {"file_paths": [...], "file_categories": [...], "changed_lines": int}
        signals for keyword_pattern: {"query_text": str}

        Returns ClassifierResult:
          .matched_routes: dict[str, MatchLevel]   (for parallel_all_matching)
          .best_route: str                          (for first_match)
        """
```

**Acceptance criteria:**
- [ ] `risk_pattern` produces identical results to current `risk_classifier.py` for all test cases
- [ ] `keyword_pattern` produces identical results to current `query_classifier.py` keyword matching for all query types
- [ ] `boost_rules` (e.g. "schema_files > 0 → correctness ≥ medium") work correctly
- [ ] `thresholds` config (count + ratio → level) matches current `_level_from_count` logic
- [ ] Unit tests: 30+ (migrate existing `test_query_classifier.py` keyword tests + risk_classifier tests)

### A.4 — Workflow engine

The core orchestrator that executes a workflow.

**Files to create:**
- `backend/app/workflow/engine.py`

**Interface:**
```python
class WorkflowEngine:
    def __init__(self, provider, explorer_provider=None, trace_writer=None): ...

    async def run(self, workflow: WorkflowConfig, context: dict) -> dict:
        """Execute the workflow. Returns aggregated results."""

    async def run_stream(self, workflow: WorkflowConfig, context: dict) -> AsyncGenerator:
        """Same as run() but yields SSE events for progress."""
```

**Execution logic:**
```python
# Classify input
result = classifier.classify(signals)

if workflow.route_mode == "first_match":
    # Code Explorer: pick best route, run its pipeline
    route = workflow.routes[result.best_route]
    if route.delegate:
        delegate_wf = load_workflow(route.delegate)
        return await self.run(delegate_wf, context)
    for stage in route.pipeline:
        await self._run_stage(stage, context)

elif workflow.route_mode == "parallel_all_matching":
    # PR Review: run all matching routes in parallel, then post_pipeline
    matching = [r for name, r in workflow.routes.items()
                if result.matched_routes.get(name, "low") >= "medium"
                or r has always_run agent]
    await gather(*[self._run_pipeline(r.pipeline, context) for r in matching])
    for stage in workflow.post_pipeline:
        await self._run_stage(stage, context)
```

**Stage execution:**
- If agent `type == explorer`: create `AgentLoopService` with agent's tools, budget, prompt (composed from shared template + agent instructions)
- If agent `type == judge`: single `provider.call_model()` with agent's prompt
- If `stage.parallel == True`: run all agents in stage concurrently
- Collect results into `context` dict (keyed by stage name)
- LLM semaphore for concurrent API call limiting (existing pattern)

**Acceptance criteria:**
- [ ] PR review workflow (`parallel_all_matching`) produces identical results to current `CodeReviewService.review()`
- [ ] Code explorer multi-perspective route (`first_match` → `business_flow_tracing`) produces identical results to current `_run_multi_perspective_stream()`
- [ ] Code explorer single-agent routes produce identical results to current single-agent `AgentLoopService` with corresponding strategy/tools
- [ ] `delegate` route (code_review → pr_review.yaml) works correctly
- [ ] Parallel stage dispatches agents concurrently (verify with timing)
- [ ] Sequential stages wait for previous stage to complete
- [ ] SSE events match current event format (backward compatible with WebView)
- [ ] Unit tests: 25+ (mock provider, both route_modes, delegate, stage ordering, parallel dispatch, budget scaling)

### A.5 — Integration: rewire existing services

Replace hardcoded orchestration with WorkflowEngine calls.

**Files to modify:**
- `backend/app/code_review/service.py` — delegate to `WorkflowEngine.run()` with `pr_review.yaml`
- `backend/app/code_review/agents.py` — remove `AGENT_SPECS`, `_AGENT_PROMPT_TEMPLATE`, `_FOCUS_DESCRIPTIONS`; keep `_parse_findings`, `_repair_output`, `_evidence_gate`, `run_review_agent` (now reads from `AgentConfig`)
- `backend/app/agent_loop/service.py` — `_run_multi_perspective_stream` and single-agent dispatch both delegate to `WorkflowEngine` with `code_explorer.yaml`
- `backend/app/agent_loop/prompts.py` — `STRATEGIES` dict moves to agent .md files; `CORE_IDENTITY` moves to `prompts/explorer_base.md`
- `backend/app/agent_loop/query_classifier.py` — `QUERY_TYPES` keyword patterns move to `code_explorer.yaml` routes; LLM classification stays in Python (used as `llm_fallback`)

**Files to delete:**
- `backend/app/code_review/risk_classifier.py` — logic moved to `classifier_engine.py` `risk_pattern` type

**Acceptance criteria:**
- [ ] `pytest tests/test_code_review.py` — all existing tests pass
- [ ] `pytest tests/test_agent_loop.py` — all existing tests pass
- [ ] `pytest tests/test_query_classifier.py` — all existing tests pass (keyword matching behavior identical)
- [ ] Eval: `python eval/run.py --provider anthropic --filter "requests-001"` — scores within 5% of baseline
- [ ] API contract unchanged: `POST /api/code-review/review` and `POST /api/context/query/stream` produce same response format

### A.6 — Mermaid diagram generation

Auto-generate workflow visualization from config.

**Files to create:**
- `backend/app/workflow/mermaid.py`

**Interface:**
```python
def generate_mermaid(workflow: WorkflowConfig) -> str:
    """Generate a Mermaid flowchart from a workflow config."""
```

**Generates different diagrams per route_mode:**

For `parallel_all_matching` (PR Review):
```mermaid
graph TD
    DIFF([PR Diff]) --> CLASSIFY{Risk Classifier}
    CLASSIFY -->|correctness| CORR[correctness]
    CLASSIFY -->|security| SEC[security]
    CLASSIFY -->|always| TEST[test_coverage]
    CORR & SEC & TEST --> DEDUP[Dedup + Filter]
    DEDUP --> ARB[Arbitrator]
    ARB --> SYNTH[Synthesizer]
    SYNTH --> RESULT([Review])
```

For `first_match` (Code Explorer):
```mermaid
graph TD
    QUERY([User Query]) --> CLASSIFY{Keyword Classifier}
    CLASSIFY -->|flow tracing| FLOW_A[Implementation] & FLOW_B[Usage]
    FLOW_A & FLOW_B --> FLOW_SYNTH[Synthesizer]
    CLASSIFY -->|root cause| ROOT[Root Cause Agent]
    CLASSIFY -->|entry point| ENTRY[Entry Point Agent]
    CLASSIFY -->|impact| IMPACT[Impact Agent]
    CLASSIFY -->|architecture| ARCH[Architecture Agent]
    CLASSIFY -->|...| OTHER[...]
    FLOW_SYNTH & ROOT & ENTRY & IMPACT & ARCH & OTHER --> ANSWER([Answer])
```

**Files to modify:**
- `backend/app/workflow/router.py` — add `GET /api/workflows/{name}/mermaid` endpoint

**Acceptance criteria:**
- [ ] Generates valid Mermaid syntax for both `pr_review.yaml` and `code_explorer.yaml`
- [ ] Shows agent names, types, budget weights
- [ ] Shows classifier dimensions/keywords as edge labels
- [ ] Renders correctly in GitHub Markdown preview

---

## Phase B: Frontend — Workflow Model Configuration

**Goal**: Let users select models for each workflow role (explorer, judge) from the existing AI Config modal.

### B.1 — Backend: workflow model configuration endpoint

**Files to create/modify:**
- `backend/app/workflow/router.py` — new router

**Endpoints:**
- `GET /api/workflows` — list available workflows (name, description, route_mode, agent count, current model assignments)
- `GET /api/workflows/{name}` — full workflow config (agents, routes, pipeline, classifier, mermaid)
- `PUT /api/workflows/{name}/models` — update model assignments: `{ "explorer": "model-id", "judge": "model-id" }`

**Model assignment storage:**
- Add `workflow_models` section to `conductor.settings.yaml`:
  ```yaml
  workflow_models:
    pr-review:
      explorer: "claude-haiku-4-5-bedrock"
      judge: "claude-sonnet-4-6-bedrock"
    code-explorer:
      explorer: "qwen3.5-flash"
      judge: "claude-sonnet-4-6-bedrock"
  ```
- WorkflowEngine reads `model_role` from agent config, resolves to actual provider via settings

**Acceptance criteria:**
- [ ] `GET /api/workflows` returns both `pr-review` and `code-explorer`
- [ ] `PUT /api/workflows/pr-review/models` persists to settings YAML
- [ ] WorkflowEngine uses the configured model for each role
- [ ] Fallback: if no override configured, use default provider

### B.2 — Frontend: workflow model selection in AI Config modal

**Files to modify:**
- `extension/media/chat.html` — extend existing AI Config modal

**UI design:**
- Add a third tab to existing AI Config modal: **"Workflows"**
- Tab shows a card per workflow (PR Review, Code Explorer)
- Each card has two dropdowns:
  - **Explorer Model** — filtered to models with `explorer: true` or `classifier: true` in settings
  - **Judge Model** — filtered to enabled strong models
- Save button calls `PUT /api/workflows/{name}/models`
- Show current Mermaid diagram below model selection (rendered as SVG via mermaid-js or as static image)

**Acceptance criteria:**
- [ ] Tab appears in AI Config modal
- [ ] Dropdowns populated from `GET /api/workflows/{name}` + available models
- [ ] Selection persists across VS Code reloads
- [ ] Mermaid preview renders correctly in WebView

---

## Phase C: Langfuse Integration

**Goal**: Add production observability with nested execution trees, cost tracking, and latency analysis. Must run locally (self-hosted Docker). Coexists with existing SessionTrace.

### C.1 — Langfuse local setup

**Files to create:**
- `docker/docker-compose.langfuse.yaml` — Langfuse self-hosted stack (Langfuse server + PostgreSQL)
- `docs/langfuse-setup.md` — Setup instructions

**Docker compose:**
```yaml
services:
  langfuse-db:
    image: postgres:16
    environment:
      POSTGRES_DB: langfuse
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse
    volumes:
      - langfuse-db:/var/lib/postgresql/data
    ports:
      - "5433:5432"

  langfuse:
    image: langfuse/langfuse:latest
    environment:
      DATABASE_URL: postgresql://langfuse:langfuse@langfuse-db:5432/langfuse
      NEXTAUTH_SECRET: conductor-langfuse-secret
      NEXTAUTH_URL: http://localhost:3001
      SALT: conductor-salt
    ports:
      - "3001:3000"
    depends_on:
      - langfuse-db
```

**Acceptance criteria:**
- [ ] `docker compose -f docker/docker-compose.langfuse.yaml up` starts Langfuse on localhost:3001
- [ ] Langfuse web UI accessible, can create project and get API keys
- [ ] Documented in setup guide

### C.2 — Backend Langfuse instrumentation

**Files to modify:**
- `backend/requirements.txt` — add `langfuse>=2.0`
- `backend/app/workflow/engine.py` — add `@observe` decorators
- `backend/app/agent_loop/service.py` — add `@observe` on `run_stream`, LLM calls, tool executions
- `backend/app/config.py` — add Langfuse config section

**Config:**
```yaml
# conductor.settings.yaml
langfuse:
  enabled: true
  host: "http://localhost:3001"
# conductor.secrets.yaml
langfuse:
  public_key: "pk-..."
  secret_key: "sk-..."
```

**Instrumentation points (~15-20 lines of decorators):**

```python
# workflow/engine.py
@observe(name="workflow:{workflow.name}")
async def run(self, workflow, context): ...

@observe(name="stage:{stage.name}")
async def _run_stage(self, stage, context): ...

@observe(name="route:{route_name}")
async def _run_route(self, route, context): ...

# agent_loop/service.py
@observe(name="agent:{agent_name}")
async def run_stream(self, query, workspace_path): ...

@observe(as_type="generation", name="llm_call")
def _call_llm(self, messages, tools, system): ...

@observe(name="tool:{tool_name}")
def _execute_tool(self, name, params): ...
```

**Langfuse trace example for PR review:**
```
workflow: pr-review                                  45.2s  $0.38
├── classify: risk_pattern                           0.1ms
│   → correctness=HIGH, security=MEDIUM
├── route: correctness                               35.2s  $0.12
│   └── agent: correctness (explorer)
│       ├── llm_call (generation)                    1.2s
│       │   └── tool: grep                           0.3s
│       ├── llm_call (generation)                    0.9s
│       │   └── tool: read_file                      0.1s
│       └── ... (18 tool calls)
├── route: security                                  32.8s  $0.09
│   └── agent: security (explorer)
│       └── ...
├── route: test_coverage                             21.3s  $0.06
│   └── ...
├── stage: arbitrate                                  3.8s  $0.08
│   └── agent: arbitrator (judge)
└── stage: synthesize                                 2.3s  $0.03
    └── agent: review_synthesizer (judge)
```

**SessionTrace coexistence:**
- SessionTrace continues saving to local JSON/SQLite (unchanged)
- Langfuse runs in parallel via decorators (no code conflict)
- If Langfuse is disabled (`langfuse.enabled: false`), decorators are no-ops
- SessionTrace provides: tool params, thinking_text, budget_signals (Langfuse doesn't capture these)
- Langfuse provides: Web UI, nested execution trees, cost dashboard, team sharing

**Acceptance criteria:**
- [ ] PR review shows nested trace in Langfuse: workflow → routes → agents → llm_calls → tools
- [ ] Code explorer shows nested trace: workflow → route → agent → llm_calls → tools
- [ ] Token counts and costs displayed correctly per trace
- [ ] Latency breakdown visible per span
- [ ] SessionTrace still saves to local storage (existing tests pass)
- [ ] `langfuse.enabled: false` → zero Langfuse overhead, no errors
- [ ] Existing tests pass without Langfuse server running (graceful degradation)

### C.3 — Langfuse integration in Makefile

**Files to modify:**
- `Makefile`

**New targets:**
```makefile
langfuse-up:        ## Start Langfuse (local Docker)
langfuse-down:      ## Stop Langfuse
langfuse-logs:      ## View Langfuse logs
```

**Acceptance criteria:**
- [ ] `make langfuse-up` starts the stack
- [ ] `make run-backend` works with or without Langfuse running

---

## Phase D: React Flow Workflow Visualization

**Goal**: Add an interactive workflow visualization panel in the VS Code extension using React Flow. First iteration is read-only (no drag-and-drop editing).

### D.1 — Backend: workflow graph endpoint

**Files to modify:**
- `backend/app/workflow/router.py`

**New endpoint:**
- `GET /api/workflows/{name}/graph` — returns React Flow-compatible JSON:
  ```json
  {
    "nodes": [
      {"id": "classify", "type": "classifier", "data": {"label": "Keyword Classifier", "route_count": 9}},
      {"id": "route:root_cause", "type": "explorer", "data": {"label": "Root Cause", "tools": 16, "weight": 1.0}},
      {"id": "route:flow_tracing", "type": "group", "data": {"label": "Flow Tracing (multi-agent)"}},
      {"id": "arbitrator", "type": "judge", "data": {"label": "Arbitrator", "model": "strong"}},
      ...
    ],
    "edges": [
      {"source": "classify", "target": "route:root_cause", "label": "bug|error|fail"},
      {"source": "classify", "target": "route:flow_tracing", "label": "flow|process|how does"},
      ...
    ]
  }
  ```

**Acceptance criteria:**
- [ ] Returns valid React Flow graph for both `pr-review` and `code-explorer`
- [ ] Node types distinguish explorer/judge/classifier/group
- [ ] Edge labels show trigger conditions (patterns or risk dimensions)

### D.2 — Extension: Workflow visualization panel

**Files to create:**
- `extension/media/workflow.html` — standalone WebView panel for workflow visualization

**Files to modify:**
- `extension/src/extension.ts` — register `conductor.showWorkflow` command
- `extension/src/panels/workflowPanel.ts` — new WebView panel class
- `extension/media/chat.html` — add workflow visualization button to chat header

**UI design:**
- Button in chat header bar (next to existing AI Config button): graph icon, tooltip "View Agent Workflows"
- Click opens a new WebView panel (side-by-side with chat)
- Panel uses React Flow (loaded via CDN or bundled) to render the workflow graph
- Two tabs: **"PR Review"** and **"Code Explorer"**
- Nodes styled to match the chat.html dark glass theme:
  - Explorer nodes: violet border, show agent name + tool count + budget weight
  - Judge nodes: indigo border, show agent name + model role
  - Classifier node: diamond shape, show route count
  - Start/end nodes: rounded, gradient background
  - Multi-agent group: dashed border grouping parallel agents
- Edges animated (dotted line flow animation)
- Click a node → sidebar shows agent details: full tool list, budget, trigger conditions, first 5 lines of prompt

**React Flow integration:**
- Use `@xyflow/react` (React Flow v12+) bundled via esbuild or loaded from CDN
- Since WebView is a sandboxed iframe, use standalone HTML with inline React Flow
- Alternative: render server-side as SVG using `@xyflow/react` SSR (simpler for v1)

**Acceptance criteria:**
- [ ] Button visible in chat header when connected to backend
- [ ] Panel opens and shows PR Review workflow as interactive graph
- [ ] Can switch between PR Review and Code Explorer tabs
- [ ] Nodes show correct data (agent names, tool counts, budgets)
- [ ] Click node shows detail sidebar
- [ ] Graph layout is clean (no overlapping nodes)
- [ ] Matches existing dark glass visual theme

---

## Implementation Order

```
Phase A (Workflow Engine) ──────────────────────────── ~1 week
  A.1 Config files + Pydantic models          Day 1
  A.2 Workflow loader                          Day 2
  A.3 Classifier engine                        Day 2-3
  A.4 Workflow engine                          Day 3-4
  A.5 Integration + migration                  Day 4-5
  A.6 Mermaid generation                       Day 5

Phase B (Model Config UI) ─────────────────────────── ~2 days
  B.1 Backend endpoints                        Day 1
  B.2 Frontend modal tab                       Day 2

Phase C (Langfuse) ────────────────────────────────── ~2 days
  C.1 Docker setup                             Day 1
  C.2 Backend instrumentation                  Day 1-2
  C.3 Makefile integration                     Day 2

Phase D (React Flow) ──────────────────────────────── ~3 days
  D.1 Graph endpoint                           Day 1
  D.2 WebView panel                            Day 2-3
```

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Workflow engine produces different results than current code | A.5 runs existing 900+ tests + eval pipeline as regression gate |
| `first_match` classifier gives wrong route | Keyword patterns migrated verbatim from tested `query_classifier.py`; LLM fallback available |
| Langfuse adds latency to agent loop | Langfuse SDK is async, decorators are ~0.1ms overhead. Disable flag available |
| React Flow bundle too large for WebView | Fallback: render Mermaid SVG server-side, no JS framework needed |
| YAML config becomes complex | Workflow YAML stays under 100 lines; agent details live in .md files; routes are the only variable-length section |
| `delegate` creates circular references | Loader detects cycles at load time and raises error |

## Files Not Modified

These modules are stable and require no changes:
- `backend/app/code_tools/` — all 24 tools, schemas, output_policy
- `backend/app/ai_provider/` — all 3 providers, resolver
- `backend/app/repo_graph/` — parser, graph, service
- `backend/app/code_review/models.py` — data models (PRContext, ReviewFinding, etc.)
- `backend/app/code_review/dedup.py` — finding deduplication
- `backend/app/code_review/ranking.py` — finding scoring
- `backend/app/code_review/diff_parser.py` — git diff parsing
- `backend/app/agent_loop/budget.py` — BudgetController
- `backend/app/agent_loop/evidence.py` — EvidenceEvaluator
- `backend/app/agent_loop/trace.py` — SessionTrace (kept alongside Langfuse)
- `extension/src/services/` — all existing services
- `eval/` — eval suite (used as regression gate, not modified)
