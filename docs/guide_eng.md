# Conductor Engineering Onboarding Guide

> **For engineers new to the team.** This guide builds your overall mental model first, then dives into the code details of each subsystem. Read sections 1-3 in order; refer to the rest as needed.
>
> **For engineers new to the project.** This guide builds your mental model first, then dives into code. Read sections 1-3 in order; look up the rest as needed.

---

## Table of Contents

1. [System Overview — Read This First](#1-system-overview--read-this-first)
2. [Quick Start](#2-quick-start)
3. [End-to-End Request Tracing — How Code Flows](#3-end-to-end-request-tracing--how-code-flows)
4. [Project Structure](#4-project-structure)
5. [Entry File main.py](#5-entry-file-mainpy)
6. [Brain Orchestrator](#6-brain-orchestrator)
7. [Agentic Code Intelligence — Agent Loop](#7-agentic-code-intelligence--agent-loop)
8. [AI Provider Layer](#8-ai-provider-layer)
9. [Git Workspace Management](#9-git-workspace-management)
10. [Chat System](#10-chat-system)
11. [Extension UI Flows](#11-extension-ui-flows)
12. [File Sharing](#12-file-sharing)
13. [Audit Log and TODO Management](#13-audit-log-and-todo-management)
14. [Authentication](#14-authentication)
15. [Jira Integration](#15-jira-integration)
16. [LangExtract Integration](#16-langextract-integration)
17. [Langfuse Observability](#17-langfuse-observability)
18. [Eval System (eval/)](#18-eval-system-eval)
19. [Testing Conventions](#19-testing-conventions)
20. [Common Development Tasks](#20-common-development-tasks)
21. [Deployment Notes](#21-deployment-notes)

---

## 1. System Overview — Read This First

Before reading any code, understand what this system does and the core design choices it makes.

### 1.1 Product Shape

Conductor is a VS Code extension that lets teams collaborate inside shared rooms and use the `@AI` command to have AI understand and review code.

**Two core user scenarios:**

```
Scenario A: Code Q&A
User types @AI /ask How is this auth flow implemented? in the chat box
      ↓
Backend runs the AI Agent, which autonomously explores the codebase (grep, read files, trace call chains...)
      ↓
Results stream back in real time, ending with detailed analysis with file references

Scenario B: PR Code Review
User types @AI /pr main...feature/auth in the chat box
      ↓
Backend parses the Git diff and dispatches 5 specialized agents in parallel (security, correctness, concurrency, reliability, test coverage)
      ↓
An arbitration agent normalizes severities, then a synthesis agent produces the final report

Scenario C: Jira Smart Operations
User types @AI /jira create Fix login bug in the chat box
      ↓
Brain dispatches the issue_tracking agent (strong model + 500K budget)
The agent first analyzes the code (grep -> read_file), then creates the Jira ticket
      ↓
ask_user confirms ticket details -> create -> returns a clickable Jira link
```

### 1.2 Two Key Architectural Decisions

**Decision 1: Use an Agent Loop instead of a RAG pipeline**

Traditional RAG (vector retrieval + generation) is passive — it chunks code, embeds it into vectors, retrieves similar fragments, and feeds them to the LLM.

Conductor's Agent Loop is active — the LLM decides what to query at each step, tracing code like an engineer would:

```
Find def authenticate() -> see it calls jwt.decode() ->
run get_callers("authenticate") -> find all callers ->
read context from key files -> form a complete answer
```

This solves the "cross-file tracing" and "multi-step reasoning" queries that RAG cannot handle.

**Decision 2: Use YAML/Markdown configuration instead of hard-coded agent logic**

Previously, PR review agent logic, routing strategies, and prompt templates were scattered across Python code. Now they all live in config files under `config/`:

```
config/
├── workflows/pr_review.yaml        # Workflow: which routes, parallel or sequential
├── agents/security.md              # Single agent: tool list, budget, instructions
└── prompts/review_base.md          # Shared prompt templates
```

The workflow engine (`workflow/engine.py`) reads these configs and dynamically orchestrates agents — you can adjust agent behavior without touching Python code.

### 1.3 Major Subsystems at a Glance

```
VS Code Extension
├── webview-ui/src/            — React 18 WebView source (esbuild -> media/webview.js)
│   ├── components/            — MessageBubble, ChatInput, ChatHeader, TaskBoard, Modals
│   ├── contexts/              — ChatContext, SessionContext, VSCodeContext
│   ├── hooks/                 — useWebSocket, useReadReceipts, useHistoryPagination
│   └── types/                 — postMessage command contracts (commands.ts)
├── media/webview.js           — Compiled React WebView bundle (268KB)
└── services/
    ├── localToolDispatcher.ts — Three-tier tool dispatch: subprocess -> AST -> native TS
    ├── astToolRunner.ts       — 6 AST tools (web-tree-sitter based)
    ├── treeSitterService.ts   — web-tree-sitter WASM wrapper (8 languages)
    ├── complexToolRunner.ts   — 6 complex tools (compressed_view, trace_variable, etc.)
    └── chatLocalStore.ts      — Local message cache (VS Code globalState)

FastAPI Backend
├── workflow/          — Config-driven multi-agent workflow engine  <- core new addition
├── agent_loop/        — LLM Agent Loop (43 tools)
├── code_review/       — PR multi-agent review pipeline
├── ai_provider/       — Three-provider abstraction layer (Bedrock / Anthropic / OpenAI)
├── git_workspace/     — Git bare repo + worktree management
├── chat/              — WebSocket chat + Redis hot cache + Postgres persistence
├── browser/           — Playwright Chromium browsing tools (browse_url / search_web / screenshot)
├── code_tools/        — 42 tool implementations (code + file editing + Jira + browser) + Python CLI
└── langextract/       — Multi-vendor Bedrock structured extraction integration
```

---

## 2. Quick Start

### 2.1 Prerequisites

```bash
# System dependencies
git --version     # 2.15+ required (worktree support)
rg --version      # ripgrep, used by the grep code tool
docker --version  # for running Postgres + Redis (data layer)
# ast-grep optional, used for structured AST search (ast_search tool)
```

> The tree-sitter grammar `.wasm` files are committed to `extension/grammars/`, so they work out of the box after cloning — no manual download needed. If the ABI is incompatible, just reinstall the `web-tree-sitter` npm package; the version is pinned in `package.json`.

### 2.2 One-shot Install

```bash
# Create Python venv + install all deps (Python + npm)
make setup
```

Equivalent to:
```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd extension && npm install
```

### 2.3 Configuration File

```bash
# Copy the template, fill in your API keys
cp config/conductor.secrets.yaml.example config/conductor.secrets.yaml
```

`conductor.secrets.yaml` requires at least one of the following:

```yaml
ai_providers:
  anthropic:
    api_key: "sk-ant-..."   # Anthropic Direct
  # or AWS Bedrock:
  aws_bedrock:
    access_key_id: "..."
    secret_access_key: "..."
    region: "us-east-1"
```

### 2.4 Start the Data Layer (must be before backend)

```bash
make data-up      # Start Postgres (5432) + Redis (6379)
make db-update    # Apply Liquibase schema changes
```

> **Why must the data layer start first?** When the backend boots, it initializes singletons like `ChatPersistenceService` and `AuditLogService` that try to connect to Postgres at construction time. If the data layer is not ready, backend startup will fail.

### 2.5 Start the Backend

```bash
make run-backend  # Dev mode (auto-reload, port 8000)
```

The startup log should show:
```
INFO  AI Provider Resolver initialized: active_model=claude-sonnet-4-6, active_provider=anthropic
INFO  Git Workspace module initialized.
INFO  Conductor startup complete.
```

If you see `asyncpg.exceptions.ConnectionDoesNotExistError`, Postgres isn't ready — run `make data-up` first.

### 2.6 Verify

```bash
# Health check
curl http://localhost:8000/health
# -> {"status": "ok"}

# Code Q&A (SSE stream, Brain orchestrator)
curl -N -X POST http://localhost:8000/api/context/query/stream \
  -H "Content-Type: application/json" \
  -d '{"room_id": "demo", "query": "how does authentication work?"}'
```

### 2.7 Run Tests

```bash
make test-backend  # Full backend test suite (1300+)

# Or more granular:
cd backend
pytest tests/test_agent_loop.py -v    # Agent Loop tests
pytest tests/test_code_tools.py -v    # Code tool tests
pytest -k "workflow" -v               # Workflow engine tests
pytest --cov=. --cov-report=html      # Coverage report

# Tool parity (Python <-> TypeScript)
make test-parity
```

---

## 3. End-to-End Request Tracing — How Code Flows

This section traces the full code path of the two most important user actions, from frontend to backend. **This is the fastest way to understand the system.**

### 3.1 Scenario A: User types `@AI /ask Where is the auth logic?`

**Step 1: Extension parses the command** (`extension/webview-ui/src/components/chat/ChatInput.tsx`)

The user types `@AI /ask Where is the auth logic?` in the textarea and presses Enter.

```typescript
// ChatInput.tsx — handleSend() + slashCommands.ts
const { query, isAI } = parseMessageForAI(text);
    // Matches "@AI /ask xxx" or "@AI /pr xxx" or "@AI /jira xxx"
    const slashMatch = text.match(/@AI\s+\/(\w+)\s+(.*)/is);
    if (slashMatch) {
        const cmd = SLASH_COMMANDS.find(c => c.name === '/' + slashMatch[1]);
        query = cmd ? cmd.transform(slashMatch[2]) : text;
    }
    // "/ask" passes through, "/pr" prepends "do PR", "/jira" prepends "[jira]" + intent detection
    vscode.postMessage({ command: 'askAI', query, workspacePath });
}
```

**Step 2: Extension Host forwards the request** (`extension/src/extension.ts`)

```typescript
// extension.ts — message handler
case 'askAI':
    // Issue an SSE request to the backend
    const response = await fetch(`${backendUrl}/api/context/query/stream`, {
        method: 'POST',
        body: JSON.stringify({ query: message.query, workspace_path: message.workspacePath }),
    });
    // Read SSE events line by line, push to WebView for live progress
    for await (const event of readSSE(response)) {
        panel.webview.postMessage({ type: 'agentProgress', event });
    }
```

**Step 3: Backend routes to the Brain orchestrator** (`backend/app/agent_loop/router.py`)

```python
@router.post("/api/context/query/stream")
async def context_query_stream(req: ContextQueryRequest, ...):
    # ... auth, resolve worktree, build ToolExecutor ...
    engine = WorkflowEngine(provider=agent_provider, explorer_provider=explorer_provider, ...)
    brain_context = {"query": req.query, "workspace_path": str(worktree_path)}

    async def event_generator():
        async for event in engine.run_brain_stream(brain_context):
            yield f"event: {event.kind}\ndata: {json.dumps(event.data)}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**Step 4: Brain decides who to dispatch** (`backend/app/agent_loop/brain.py`)

The Brain (strong model) runs its own LLM loop and can call 4 meta-tools:

- `dispatch_agent("agent_name", query)` — single-agent exploration
- `dispatch_swarm("preset_name", queries=[...])` — parallel multi-agent
- `transfer_to_brain("pr_review", params)` — one-shot handoff to a specialized brain (e.g. PR review)
- `ask_user(...)` — confirm direction with the user mid-flight

Example: the user asks about "auth logic"; the Brain sees this is a simple single-agent task and calls `dispatch_agent("entry_point_finder", "auth logic entry point")`.

**Step 5: The dispatched sub-agent runs AgentLoopService** (`backend/app/agent_loop/service.py`)

```python
async def run_stream(self, query, workspace_path):
    messages = [{"role": "user", "content": query}]
    system_prompt = build_sub_agent_system_prompt(
        agent_name=self._agent_identity["name"],
        agent_description=self._agent_identity["description"],
        agent_instructions=self._agent_identity["instructions"],
        strategy_key=self._forced_strategy or None,
        ...
    )

    for i in range(self.max_iterations):
        response = provider.chat_with_tools(messages, tools=active_tools, system=system_prompt)
        if response.stop_reason == "end_turn":
            if evidence_ok(response.text, tool_calls_made):
                return AgentResult(answer=response.text, ...)
            messages.append(feedback_message("need specific file references"))
            continue
        for tool_call in response.tool_calls:
            result = execute_tool(tool_call.name, workspace_path, tool_call.input)
            messages.append(tool_result_block(tool_call.id, result))
```

**The full chain:**
```
User input -> ChatInput.tsx parses -> useWebSocket/extension.ts SSE request ->
agent_loop/router.py -> WorkflowEngine.run_brain_stream() ->
Brain LLM loop -> dispatch_agent / dispatch_swarm / transfer_to_brain ->
sub-agent AgentLoopService.run_stream() -> LLM <-> execute_tool() loop ->
SSE event stream back -> ChatContext -> ThinkingIndicator/MessageBubble live render
```

---

### 3.2 Scenario B: User types `/pr main...feature/auth`

**Step 1: Extension parses and sends**

The `slashCommands.ts` transform rewrites `/pr X` into a marker-tagged query:

```typescript
{ name: "/pr", transform: (args) => `${marker(QUERY_TYPE.CODE_REVIEW)} ${args}` }
// "/pr main...feature/auth" -> "[query_type:code_review] main...feature/auth"
```

The marker is a hint for the Brain LLM; the agreed-upon values live in `backend/app/agent_loop/query_markers.py` (the `QueryType` enum), and the frontend manually mirrors them via the `QUERY_TYPE` constant.

**Step 2: Brain recognizes the marker and transfers to the PR Brain**

The Brain has been taught the `[query_type:code_review]` convention in its prompt; once it sees the marker it calls:

```python
transfer_to_brain("pr_review", params={"workspace_path": ..., "diff_spec": "main...feature/auth"})
```

`brain.py:_transfer_to_brain` validates against an allowlist (currently only `pr_review` is allowed), then launches `PRBrainOrchestrator`.

**Step 3: PRBrainOrchestrator's 6-phase pipeline** (`backend/app/agent_loop/pr_brain.py`)

```
Phase 1: Pre-compute (parse_diff, classify_risk, prefetch_diffs, impact_graph)
Phase 2: Parallel dispatch of review agents (correctness, correctness_b, concurrency, security, reliability, performance, test_coverage)
Phase 3: Post-processing (evidence_gate -> post_filter -> dedup -> score_and_rank)
Phase 4: Adversarial arbitration (pr_arbitrator tries to refute each finding)
Phase 5: Merge recommendation (deterministic)
Phase 6: Synthesis (Brain acts as final judge, seeing both supporting evidence and rebuttals)
```

Each review agent's `.md` frontmatter declares `skill: code_review_pr`, which uses `forced_skill` to inject the `INVESTIGATION_SKILLS["code_review_pr"]` entry from `prompts.py` into Layer 3. This skill is the single source of PR review guidance: it carries the senior engineer persona, provability framework, DO NOT FLAG list, PR-introduced verification rules, and JSON output format.

**The full chain:**
```
"/pr main...feature/auth" ->
slashCommands.transform -> "[query_type:code_review] main...feature/auth" ->
Brain LLM sees the marker -> transfer_to_brain("pr_review") ->
PRBrainOrchestrator 6 phases ->
parallel review agents -> arbitration -> synthesis ->
final ReviewResult
```

---

## 4. Project Structure

```
backend/
├── app/
│   ├── main.py                    # App factory, lifespan startup/shutdown
│   ├── config.py                  # Reads Settings + Secrets from YAML
│   │
│   ├── workflow/                  # Brain orchestrator host + agent/swarm config loading
│   │   ├── models.py              # Pydantic models: AgentConfig, BrainConfig, SwarmConfig
│   │   ├── loader.py              # Loads Markdown agent files + Brain/Swarm YAML
│   │   ├── engine.py              # WorkflowEngine.run_brain_stream() — Brain entry point
│   │   ├── router.py              # /api/brain/swarms — Agent Swarm UI data source
│   │   └── observability.py       # Langfuse @observe decorator (zero-cost when disabled)
│   │
│   ├── agent_loop/                # LLM agent loop engine + Brain orchestrator
│   │   ├── service.py             # AgentLoopService — LLM loop + tool dispatch
│   │   ├── brain.py               # AgentToolExecutor — dispatch_agent / dispatch_swarm / transfer_to_brain
│   │   ├── pr_brain.py            # PRBrainOrchestrator — PR review-specific deterministic pipeline
│   │   ├── query_markers.py       # QueryType enum + marker parsing (frontend/backend shared convention)
│   │   ├── budget.py              # BudgetController — three-level token budget signals
│   │   ├── trace.py               # SessionTrace — JSON trace (for offline analysis)
│   │   ├── evidence.py            # EvidenceEvaluator — answer quality gate
│   │   ├── prompts.py             # Four-layer system prompt builder + 9 Investigation Skills
│   │   └── router.py              # POST /api/context/query/stream (SSE)
│   │
│   ├── code_tools/                # 43 tools (code + file editing + Jira + browser + Fact Vault)
│   │   ├── tools.py               # All tool implementations + execute_tool() dispatcher
│   │   ├── schemas.py             # Pydantic models + LLM tool definitions (TOOL_DEFINITIONS)
│   │   ├── output_policy.py       # Per-tool truncation policy (budget-adaptive)
│   │   ├── __main__.py            # Python CLI entry: python -m app.code_tools <tool> <ws> '<params>'
│   │   └── router.py              # /api/code-tools/ direct invocation interface
│   │
│   ├── ai_provider/               # LLM provider abstraction layer
│   │   ├── base.py                # AIProvider ABC + ToolCall/ToolUseResponse/TokenUsage
│   │   ├── claude_bedrock.py      # AWS Bedrock Converse API
│   │   ├── claude_direct.py       # Anthropic Messages API
│   │   ├── openai_provider.py     # OpenAI Chat Completions
│   │   └── resolver.py            # ProviderResolver — health check + auto-select best
│   │
│   ├── code_review/               # Multi-agent PR review pipeline (10 steps)
│   │   ├── service.py             # CodeReviewService — orchestrates the review flow
│   │   ├── agents.py              # Specialized review agents (parallel dispatch)
│   │   ├── models.py              # PRContext, ReviewFinding, ReviewResult
│   │   ├── diff_parser.py         # git diff -> PRContext
│   │   ├── risk_classifier.py     # 5-dimension risk classification
│   │   ├── ranking.py             # Score and rank findings
│   │   ├── dedup.py               # Dedup and merge findings
│   │   └── router.py              # /api/code-review/ interface (incl. SSE stream)
│   │
│   ├── git_workspace/             # Git workspace management
│   │   ├── service.py             # GitWorkspaceService (bare repo + worktree)
│   │   ├── delegate_broker.py     # DelegateBroker (reserved for Model B)
│   │   └── router.py              # /api/git-workspace/ interface
│   │
│   ├── langextract/               # Multi-vendor Bedrock structured extraction
│   │   ├── provider.py            # BedrockLanguageModel — all Bedrock vendors
│   │   ├── catalog.py             # BedrockCatalog — dynamic model discovery
│   │   ├── service.py             # LangExtractService async wrapper
│   │   └── router.py              # GET /api/langextract/models
│   │
│   ├── repo_graph/                # AST symbol graph (used by code tools)
│   │   ├── parser.py              # tree-sitter AST + regex fallback
│   │   ├── graph.py               # networkx dependency graph + PageRank
│   │   └── service.py             # RepoMapService (graph build + cache)
│   │
│   ├── chat/                      # WebSocket chat + persistence
│   │   ├── manager.py             # ConnectionManager — WebSocket room management
│   │   ├── redis_store.py         # Redis hot cache (6h TTL)
│   │   ├── persistence.py         # ChatPersistenceService — write-through micro-batch Postgres
│   │   └── router.py              # /ws/chat/{room_id}, /chat/{room_id}/history, DELETE /chat/{room_id}
│   │
│   ├── browser/                   # Playwright web browsing tools
│   │   ├── service.py             # BrowserService — Chromium automation
│   │   ├── tools.py               # browse_url, search_web, screenshot implementations
│   │   └── router.py              # /api/browser/ interface
│   │
│   ├── files/                     # File upload/download (PostgreSQL metadata)
│   ├── audit/                     # PostgreSQL audit log
│   ├── todos/                     # PostgreSQL TODO tracking
│   ├── auth/                      # AWS SSO + Google OAuth
│   ├── policy/                    # Auto-apply security evaluation
│   └── workspace_files/           # Worktree file CRUD
│
├── config/
│   ├── conductor.settings.yaml    # Non-sensitive settings (committed)
│   ├── conductor.secrets.yaml     # Sensitive info like API keys (gitignored)
│   ├── brain.yaml                 # Brain limits + core_tools
│   ├── brains/
│   │   └── pr_review.yaml         # PR Brain config (review agents, budget weights, post_processing)
│   ├── agents/                    # Agent definition files (YAML frontmatter + Markdown body)
│   │   ├── security.md            # PR review agent: auth/injection/XSS
│   │   ├── correctness.md         # PR review agent: logic/state/persistence
│   │   └── ... (more)
│   ├── swarms/                    # Swarm presets (agent group + parallel/sequential + synthesis_guide)
│   └── prompts/
│       └── ... shared prompt templates
│
├── requirements.txt
└── tests/                         # 1300+ tests
    ├── conftest.py                # Central stubs (cocoindex, litellm, etc.)
    ├── test_code_tools.py         # 139: 43 tools + dispatcher + multi-language
    ├── test_agent_loop.py         # 55: loop + 4-layer prompt + completeness checks
    ├── test_budget_controller.py  # 20: budget signals
    ├── test_compressed_tools.py   # 24: compressed view tools
    ├── test_evidence.py           # 19: evidence gate
    ├── test_symbol_role.py        # 24: symbol role classification
    ├── test_output_policy.py      # 19: truncation policy
    ├── test_langextract.py        # 57: Bedrock multi-vendor
    ├── test_repo_graph.py         # 72: AST + dependency graph
    ├── test_chat_persistence.py   # ChatPersistenceService — micro-batch Postgres
    ├── test_browser_tools.py      # Browser tools (Playwright, mocked)
    └── ...
```

> **Why this directory layout?** FastAPI encourages separating routes (HTTP layer) from services (business logic layer). Each feature module is a sub-package with its own `router.py` (routes) and `service.py` (business logic), with no coupling between them.

---

## 5. Entry File main.py

The `lifespan` function in `main.py` controls startup and shutdown — it's the entry point for understanding the entire backend initialization flow.

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = load_settings()

    # 1. Git workspace service
    git_service = GitWorkspaceService()
    if settings.git_workspace.enabled:
        await git_service.initialize(settings.git_workspace)
    app.state.git_workspace_service = git_service

    # 2. AI provider resolver (health-check all configured providers, pick the best)
    conductor_config = get_config()
    resolver = ProviderResolver(conductor_config)
    resolver.resolve()
    set_resolver(resolver)
    app.state.agent_provider      = resolver.get_active_provider()      # strong model
    app.state.explorer_provider   = resolver.get_explorer_provider()    # lightweight model (optional)
    app.state.classifier_provider = resolver.get_classifier_provider()  # classifier model (optional)

    # 3. Singleton service init (must be first-called with engine= in lifespan)
    TodoService.get_instance(engine=engine)
    AuditLogService.get_instance(engine=engine)
    FileStorageService.get_instance(engine=engine)
    ChatPersistenceService.get_instance(engine=engine)
    # Note: not initializing in lifespan causes RuntimeError on first request

    # 4. Langfuse observability (self-hosted, optional)
    init_langfuse(settings)

    # 5. Ngrok tunnel (VS Code Remote-WSL scenario, optional)
    if ngrok_cfg.get("enabled"):
        start_ngrok(port=settings.server.port, ...)

    # 6. Bedrock model catalog (dynamically discover available models, optional)
    catalog = BedrockCatalog(region=bedrock_region)
    catalog.refresh()
    app.state.bedrock_catalog = catalog

    yield  # <- the app runs here

    # Shutdown cleanup
    stop_ngrok()
    langfuse_flush()
    await git_service.shutdown()
```

**About the PNA middleware (Private Network Access)**

Chrome 105+ blocks `vscode-webview://` origins from making requests to localhost unless the server returns `Access-Control-Allow-Private-Network: true`.

Note: this uses **pure ASGI middleware** (not `BaseHTTPMiddleware`). The reason is that `BaseHTTPMiddleware` buffers response bodies and silently kills all WebSocket connections (returning close code 1006). Pure ASGI middleware is safe for both HTTP and WebSocket requests.

---

## 6. Brain Orchestrator

The Brain is Conductor's core orchestration layer. It is an LLM loop running on a strong model that **itself** decides which specialized sub-agents to dispatch — no keyword-based classifier, no YAML routing table.

### 6.1 Core Abstraction

```
Query -> Brain (LLM loop) -> pick a meta-tool -> sub-agent / swarm / specialized brain -> result -> Brain synthesis
```

The Brain is told in its prompt about all available sub-agents + swarms (loaded from `config/agents/*.md` and `config/swarms/*.yaml`). It dispatches via the 4 meta-tools below:

| Meta-tool | Purpose |
|---|---|
| `dispatch_agent("name", query)` | A single specialized agent explores one specific target |
| `dispatch_swarm("preset", queries=[...])` | Run 3-6 agents in parallel using a preset |
| `transfer_to_brain("pr_review", params)` | One-shot switch to a specialized brain (currently only PR Brain) |
| `ask_user(question, options)` | Confirm direction with the user mid-flight |

### 6.2 Agent Definition File Format

Each agent is a Markdown file: the YAML frontmatter is metadata, the body is the agent's persona + instructions:

```markdown
---
name: security
description: Check authentication, injection, secret leak and other security risks
model: explorer          # explorer (lightweight model) or strong (strong model)

tools:                   # tools available to this agent
  - grep
  - read_file
  - find_references
  - get_callers
  - trace_variable       # data flow tracing, essential for security agent

skill: code_review_pr    # injects the code_review_pr skill from prompts.py (senior engineer review style)

limits:
  max_iterations: 20
  budget_tokens: 200000
---

## Security Review Strategy

1. Check auth and authorization changes
   - Use grep to search for jwt, token, session related code
   - Use get_callers to find all callers of authentication functions
   ...

2. Check injection risks
   - Use trace_variable to track how user input flows into SQL queries
   ...
```

### 6.3 Swarm Presets

`config/swarms/*.yaml` defines "a group of agents plus parallel/sequential mode + synthesis guide", letting the Brain dispatch multiple agents at once:

```yaml
# config/swarms/business_flow.yaml
name: business_flow
description: Trace one business flow in parallel from multiple angles
mode: parallel
agents:
  - explore_implementation
  - explore_usage
  - explore_data_flow
synthesis_guide: |
  Synthesize in the order entry -> main flow -> data/state changes -> side effects.
  Must provide file:line references.
```

### 6.4 PR Brain — The Only Specialized Brain

`pr_review` is the only specialized brain activated via `transfer_to_brain`. It runs `PRBrainOrchestrator`, a 6-phase deterministic pipeline (pre-compute -> dispatch review agents -> post-process -> arbitration -> merge recommendation -> synthesis); see `backend/app/agent_loop/pr_brain.py` for implementation details.

**PR-review-scoped infrastructure** (Phase 9.15 + 9.18 hardening):

* **Fact Vault (short-term memory)** — `backend/app/scratchpad/`. On each PR review start, `PRBrainOrchestrator` opens a per-session SQLite file (`~/.conductor/scratchpad/{task_id}-{uuid}.sqlite`, e.g. `ado-MyProject-pr-12345-b37b7979.sqlite`). Every sub-agent `grep` / `read_file` / `find_symbol` call is routed through `CachedToolExecutor`, which transparently dedupes via exact-key lookup or range-intersection (a cached `read_file` for lines 100-150 satisfies a later request for 101-130). Typical 7-agent parallel review hits 25-40% cache rate; `cleanup()` deletes the file + WAL sidecars when the review ends.
* **Tree-sitter scan hardening** — `backend/app/repo_graph/parse_pool.py`. Every file parse runs in an isolated subprocess (`forkserver` start method on POSIX) with a 60s wall-clock cap enforced by the main process via `SIGKILL`. This is the only reliable timeout primitive — tree-sitter's Python binding holds the GIL through the C-level parse, so any thread-level timeout blocks forever (py-spy on sentry-007 confirmed). A paired JSX-depth heuristic pre-filters `.tsx` files >20 KB with estimated nesting >15 levels directly to regex, avoiding the first-encounter 60s SIGKILL budget.
* **Degradation signal surfacing** — when tree-sitter times out or fails, `FileSymbols.extracted_via = "regex"`; `find_symbol` tags each match with `extracted_via: "regex"`, and `file_outline` shape becomes `{"definitions": [...], "extracted_via": "regex", "note": "..."}`. Tool descriptions tell the agent: if you see that marker, prefer `grep` / `read_file` for authoritative structural info.

This infrastructure landed with Phase 9.15 Fact Vault + 9.18 Scan Hardening; see `ROADMAP.md` for the full history. The upcoming PR Brain v2 refactor (`docs/PR_BRAIN_V2_PLAN.md`) will layer on top — switching to a coordinator pattern where Brain plans investigations and Haiku workers answer narrow checks, with severity classification unified in Brain's synthesis phase.

### 6.5 Query Markers — Shared Frontend/Backend Convention

Frontend slash commands (`/pr`, `/jira`, `/summary`, `/diff`) prepend a `[query_type:X]` marker to the query as a routing hint for the Brain LLM. The marker strings are centrally defined in the `QueryType` enum in `backend/app/agent_loop/query_markers.py`; the frontend's `extension/webview-ui/src/utils/slashCommands.ts` mirrors them via the same-named `QUERY_TYPE` constant (both sides must be updated together).

The marker is **not parsed by any Python code** — it's just a hint in prompt context that lets the Brain reliably pick the right dispatch path even when intent is ambiguous.

### 6.6 Brain Swarms API

```bash
# Returns all specialized brains + swarms the Brain can dispatch (incl. agent composition)
GET /api/brain/swarms
# -> {
#     "brain_model": "claude-sonnet-4-6",
#     "core_tools": [...],
#     "specialized_brains": [{ "name": "pr_review", "type": "brain", "mode": "pipeline", "agents": [...] }],
#     "swarms": [{ "name": "business_flow", "type": "swarm", "mode": "parallel", "agents": [...] }]
#   }
```

Used by the extension's Agent Swarm UI tab to visualize the Brain's handoff targets (`transfer_to_brain` and `dispatch_swarm`).

---

## 7. Agentic Code Intelligence — Agent Loop

### 7.1 Why Not RAG?

Traditional RAG:

```
chunk code -> vector embed -> similarity retrieval -> feed to LLM -> answer
```

Problem: retrieval is static, result quality depends on vector matching, and it cannot handle chained reasoning like "first find function A, then trace what A calls."

Agent Loop:

```
LLM sees the question -> decides to grep keywords first
                       -> sees results, decides to read a particular file
                       -> sees function calls, decides to use get_callers to find callers
                       -> traces the full call chain, forms an answer
```

The LLM can decide each next step based on existing information, enabling true multi-step reasoning.

### 7.2 43 Tools (Code + File Editing + Jira + Browser + Fact Vault)

Tools are spread across three registries, dispatched uniformly via `execute_tool(name, workspace, params)`:
- **Code tools** (32): `code_tools/tools.py` (includes `search_facts` added in Phase 9.15)
- **Jira tools** (5): `integrations/jira/tools.py`
- **Browser tools** (6): `browser/tools.py`

**Search tools:**

| Tool | Parameters | Description |
|------|------|------|
| `grep` | `pattern`, `path?`, `file_glob?` | Ripgrep regex search, auto-excludes .git/node_modules |
| `ast_search` | `pattern`, `lang?`, `path?` | Structured AST search (ast-grep), `$VAR` matches any node |
| `find_symbol` | `name`, `kind?` | AST symbol definition search, results include role classification |
| `find_references` | `name`, `file?` | Symbol reference search (grep + AST validation) |

**File reading tools:**

| Tool | Parameters | Description |
|------|------|------|
| `read_file` | `path`, `start_line?`, `end_line?` | Read file content, supports line ranges |
| `list_files` | `path?`, `depth?`, `glob?` | Directory tree, supports glob filter |
| `file_outline` | `path` | All definitions in a file with line numbers |
| `compressed_view` | `path`, `focus?` | Signatures + call relations + side effects, saves ~80% tokens |
| `module_summary` | `path` | Module-level summary: services/models/function list, saves ~95% tokens |
| `expand_symbol` | `name`, `file?` | Restore full source from compressed view |

**Call graph tools:**

| Tool | Parameters | Description |
|------|------|------|
| `get_callers` | `name`, `file?` | Who calls this function (cross-file) |
| `get_callees` | `name`, `file` | What this function calls |
| `get_dependencies` | `file` | Which files this file imports |
| `get_dependents` | `file` | Which files import this file |

**Git tools:**

| Tool | Parameters | Description |
|------|------|------|
| `git_log` | `file?`, `search?`, `n?` | Recent commits, supports search by file and commit message |
| `git_diff` | `base`, `head?`, `file?` | Diff between two refs |
| `git_blame` | `file`, `start_line`, `end_line` | Author info per line |
| `git_show` | `ref` | Full commit details; use `HEAD~1:file.py` to view a file before the change |

**Test tools:**

| Tool | Parameters | Description |
|------|------|------|
| `find_tests` | `name`, `file?` | Find tests covering a function/class |
| `test_outline` | `file` | Test file structure (mocks, assertions, fixtures) |
| `run_test` | `file`, `function?` | Actually run tests, return pass/fail + output |

**Data flow tools:**

| Tool | Parameters | Description |
|------|------|------|
| `trace_variable` | `name`, `file`, `line` | Trace variable flow: alias detection, parameter passing, sink/source |
| `detect_patterns` | `path?`, `patterns?` | Architectural pattern detection (singleton, factory, observer, etc.) |

**Other code tools:**

| Tool | Parameters | Description |
|------|------|------|
| `glob` | `pattern`, `path?` | Fast file pattern matching (e.g. `**/*.ts`) |
| `git_diff_files` | `base`, `head?` | List of files changed between two refs |
| `git_hotspots` | `n?`, `since?` | Recently churned hotspot files (change count x author count) |
| `list_endpoints` | `path?`, `framework?` | Extract API route definitions (Flask/FastAPI/Express, etc.) |
| `extract_docstrings` | `path` | Extract docstrings of functions/classes in a module |
| `db_schema` | `path?` | Database schema introspection (SQLAlchemy models) |

**File editing tools:**

| Tool | Parameters | Description |
|------|------|------|
| `file_edit` | `path`, `old_text`, `new_text` | Search-and-replace edit (must `read_file` first to prevent overwriting unread content) |
| `file_write` | `path`, `content` | Full file write/create (existing files require `read_file` first) |

> ⚠️ **file_write / file_edit preserve content whitespace verbatim.** `_repair_tool_params` Pattern 3 normally `.strip()`s every string param, but `file_write.content` / `file_edit.old_string` / `file_edit.new_string` are on a whitelist — otherwise trailing newlines on every written file would be silently killed, breaking POSIX text-file convention (Phase 9.18 step 3 fix).

**Fact Vault tools** (Phase 9.15, only available inside a PR review session):

| Tool | Parameters | Description |
|------|------|------|
| `search_facts` | `tool?`, `path?`, `pattern?`, `limit?` | Query previously-cached tool-call results from the current PR review session (`grep` / `read_file` / `find_symbol` / …). Returns metadata only — seeing that a fact exists lets the agent decide whether to re-run (served transparently from cache by `CachedToolExecutor`) or skip the lookup entirely |

**Jira integration tools** (see [§15 Jira Integration](#15-jira-integration) for details):

| Tool | Parameters | Description |
|------|------|------|
| `jira_search` | `query`, `max_results?` | JQL or free-text search; shortcuts: `my tickets` / `my sprint` / `blockers` |
| `jira_get_issue` | `issue_key` | Fetch full issue details (description, comments, subtasks) |
| `jira_create_issue` | `project_key`, `summary`, `description`, ... | Create a ticket (ADF description, code blocks, parent_key subtasks) |
| `jira_update_issue` | `issue_key`, `transition_to?`, `comment?`, ... | Status transition, comment, field update (Done/Closed/Resolved blocked) |
| `jira_list_projects` | — | List accessible Jira projects |

**Browser tools:**

| Tool | Parameters | Description |
|------|------|------|
| `web_search` | `query` | Web search |
| `web_navigate` | `url` | Headless browser navigates to a URL |
| `web_click` | `selector` | Click a page element |
| `web_fill` | `selector`, `value` | Fill a form field |
| `web_screenshot` | — | Take a page screenshot |
| `web_extract` | `selector?` | Extract page content |

### 7.3 Tool Output Policy (output_policy.py)

Different tools have different truncation strategies, to prevent any single tool result from blowing up the context window:

```python
# Search tools truncate by result count
"grep":          Policy(max_results=50)
# File reading truncates by lines (never mid-line)
"read_file":     Policy(max_lines=300, truncate_unit="lines")
# Git tools get a more generous character limit
"git_show":      Policy(max_chars=8000)
# Compressed view tools don't need truncation (they're already compact)
"compressed_view": Policy(max_chars=20000)

# Budget-adaptive: when remaining tokens < 100K, all limits shrink by 50%
if budget_controller.remaining_tokens < 100_000:
    policy.scale(0.5)
```

### 7.4 Token Budget Controller (BudgetController)

```python
# budget.py
class BudgetController:
    def check_and_signal(self, usage: TokenUsage) -> BudgetSignal:
        ratio = self.total_input_tokens / self.max_input_tokens

        if ratio < 0.70:
            return BudgetSignal.NORMAL         # normal exploration
        elif ratio < 0.90:
            return BudgetSignal.WARN_CONVERGE  # converge: ban broad searches, only allow verification calls
        else:
            return BudgetSignal.FORCE_CONCLUDE # force end: LLM must give an answer immediately
```

When the signal becomes `WARN_CONVERGE`, the agent loop injects constraints into the system prompt to prevent the LLM from continuing broad searches like grep or find_symbol. On `FORCE_CONCLUDE`, it directly injects a "give the final answer immediately" instruction.

### 7.5 Four-layer System Prompt (prompts.py)

The system prompt for each LLM call is composed of four layers (following Anthropic's official prompt engineering guidance):

```
L1: Identity (system prompt, unique to each agent)
    ├── Agent identity: name, responsibility, investigation perspective
    ├── Goal-oriented: understand code behavior, locate features, trace data flow
    └── Answer format: must include file:line references, code blocks must quote real code

L2: Tools (curated tool set per query type)
    ├── brain.yaml core_tools ∪ tools configured in the agent .md
    └── Enriched tool descriptions: 3-4 sentences, when to use / when not to / what it doesn't return

L3: Skills & Guidelines (context shared across agents)
    ├── Workspace layout, project docs (README/CLAUDE.md)
    ├── Investigation methodology (9 Investigation Skills: entry_point, root_cause,
    │   architecture, impact, data_lineage, recent_changes, code_explanation,
    │   config_analysis, issue_tracking)
    ├── Budget signals (NORMAL / WARN_CONVERGE / FORCE_CONCLUDE)
    └── Convergence guidance (stop after iteration 6-7 once you have strong evidence)

L4: User Message (query + optional code context)
    └── Brain query + optional code_context (selected code snippet)
        **Never inject agent identity info into the user message**
```

**Design principle:** Follows Anthropic's official prompt engineering guidance (see §7.5.1 below).

#### 7.5.1 Anthropic Prompt Design Guidelines

The principles below come from Anthropic's official documentation ([Prompt Engineering Best Practices](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices), [Context Engineering for Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)) and are validated by the Conductor project's eval suite.

**1. Right Altitude — Find the right level of abstraction**

Anthropic's core principle: don't be too broad (the model lacks direction) or too specific (the model becomes brittle).

> "The optimal altitude strikes a balance: specific enough to guide behavior effectively, yet flexible enough to provide the model with strong heuristics to guide behavior."

For reasoning tasks, lean toward higher levels of abstraction:
> "Prefer general instructions over prescriptive steps. A prompt like 'think thoroughly' often produces better reasoning than a hand-written step-by-step plan. Claude's reasoning frequently exceeds what a human would prescribe."

```
# ❌ Too specific (brittle)
"if you find isFinished or isComplete, use get_callers to trace downstream"

# ❌ Too broad
"investigate the code"

# ✅ Right altitude
"Trace the complete lifecycle from trigger to final outcome — not just the middle steps."
```

**2. Examples Over Rule Lists**

> "Teams will often stuff a laundry list of edge cases into a prompt... We do not recommend this. We recommend working to curate a set of diverse, canonical examples that effectively portray the expected behavior. For an LLM, examples are the 'pictures' worth a thousand words."

3-5 diverse examples are more effective than long rule lists. Wrap examples in `<example>` tags to distinguish them from instructions.

**3. Explain Why, Don't Just Issue Commands**

> "Claude is smart enough to generalize from the explanation."

Don't say "don't use ellipses"; say "the output will be read aloud by a TTS engine, and ellipses can't be pronounced correctly." The model can generalize from motivation to more correct behavior.

**4. Positive Framing**

> "Tell Claude what to do instead of what not to do."

Don't say "Don't stop at the middle steps"; say "Trace the complete lifecycle from trigger to final outcome."

**5. Context Over Instructions**

Provide workspace layout, project docs, dependency information and other context, and let the model decide its own path. The `{workspace_layout_section}` and `{project_docs_section}` in CORE_IDENTITY embody this principle.

**6. Multi-Agent Role Specialization**

Each agent has a unique investigation perspective. Shared step-by-step instructions destroy the value of parallelism.

> **Practical lesson:** Adding a "start broad, follow domain model" strategy to the shared `explorer_base.md` dropped the workflow eval score from 60% to 25%, because both agents ended up taking the same implementation path.

**7. Minimal, Unambiguous Tool Sets**

> "If a human engineer can't definitively say which tool should be used in a given situation, an AI agent can't be expected to do better."

Tool output should be token-efficient, with non-overlapping functionality.

**8. Dial Back for Newer Models**

> "If your prompts were designed to reduce undertriggering on tools, these models may now overtrigger. Where you might have said 'CRITICAL: You MUST use this tool when...', you can use more normal prompting like 'Use this tool when...'"

### 7.6 Evidence Evaluator (EvidenceEvaluator)

When the LLM is about to finish (`stop_reason == "end_turn"`), the EvidenceEvaluator checks the answer's quality:

```python
def evaluate(self, answer: str, state: AgentState) -> EvidenceResult:
    checks = [
        # Does the answer include "file.py:42" or a code block?
        has_file_references(answer) or has_code_blocks(answer),
        # Have at least 2 tool calls been made?
        state.tool_calls_made >= 2,
        # Has at least 1 file been accessed?
        len(state.files_accessed) >= 1,
    ]

    if all(checks) or state.budget_signal == FORCE_CONCLUDE:
        return EvidenceResult.PASS
    else:
        # Inject feedback so the LLM keeps investigating
        return EvidenceResult.RETRY(
            feedback="Answer must include specific file paths and line number references. Please continue investigating."
        )
```

### 7.7 HTTP Interface

```bash
# Synchronous interface (waits for full result)
POST /api/context/query
{ "query": "how does auth work?", "room_id": "room-123" }

# SSE streaming interface (live progress)
POST /api/context/query/stream
# Returns event stream:
# data: {"type": "tool_start", "tool": "grep", "params": {...}}
# data: {"type": "tool_result", "result": "..."}
# data: {"type": "answer", "text": "The auth flow..."}

# Directly execute a single tool (debugging)
POST /api/code-tools/execute/grep
{ "workspace": "/path/to/repo", "params": {"pattern": "authenticate"} }
```

### 7.8 Local Mode Tool Dispatch (Option E)

When the user opens a repo locally (non git-worktree mode), tool calls are forwarded by the backend over WebSocket to the extension for execution. The extension uses a three-tier dispatch architecture (`localToolDispatcher.ts`):

```
backend AgentLoopService
  -> RemoteToolExecutor
  -> WebSocket tool_request -> Extension _handleLocalToolRequest
      ↓
  localToolDispatcher.ts (all native TypeScript, zero Python deps)
      ├── Tier 1: SUBPROCESS (12) -> child_process (rg/git)
      │   grep, read_file, list_files, git_log, git_diff, git_diff_files,
      │   git_blame, git_show, find_tests, run_test, ast_search, get_repo_graph
      │
      ├── Tier 2: AST (6) -> web-tree-sitter WASM
      │   file_outline, find_symbol, find_references,
      │   get_callees, get_callers, expand_symbol
      │   (treeSitterService.ts + astToolRunner.ts)
      │
      └── Tier 3: COMPLEX (6) -> native TypeScript
          compressed_view, trace_variable, detect_patterns,
          get_dependencies, get_dependents, test_outline
          (complexToolRunner.ts)
```

**Fallback chain:** Each tier automatically falls back to the legacy subprocess implementation if it fails.

**All native TypeScript:** Ships in the .vsix; users do not need to install Python.

**Grammar WASM management:**

Grammar files are committed to `extension/grammars/` and work after cloning the repo, with no manual download. To swap versions, replace the `.wasm` files manually and ensure the `web-tree-sitter` npm package version matches the grammar ABI (currently pinned in `extension/package.json`).

---

## 8. AI Provider Layer

### 8.1 Unified Abstraction (AIProvider ABC)

```python
# ai_provider/base.py
class AIProvider(ABC):
    @abstractmethod
    def chat_with_tools(
        self,
        messages: list[dict],   # Bedrock Converse format
        tools: list[dict],
        system: str = "",
    ) -> ToolUseResponse: ...

@dataclass
class ToolUseResponse:
    text: str                      # model text output
    tool_calls: list[ToolCall]     # tools the model wants to call
    stop_reason: str               # "end_turn" | "tool_use" | "max_tokens"
    usage: TokenUsage              # input/output token counts

@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0    # Anthropic prompt cache
    cache_write_tokens: int = 0
```

The **internal message format** uses Bedrock Converse format uniformly (content block array); the OpenAI provider handles format conversion internally:

```python
# Message format accepted by all providers
messages = [
    {"role": "user",      "content": [{"text": "Find auth code"}]},
    {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "t1", "name": "grep", "input": {...}}}
    ]},
    {"role": "user",      "content": [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": "..."}]}}
    ]},
]
```

### 8.2 Three Provider Implementations

| Provider | File | Underlying API | Notes |
|--------|------|---------|------|
| `ClaudeBedrockProvider` | `claude_bedrock.py` | Bedrock Converse API | Supports cross-region inference profiles |
| `ClaudeDirectProvider` | `claude_direct.py` | Anthropic Messages API | Supports prompt cache |
| `OpenAIProvider` | `openai_provider.py` | OpenAI Chat Completions | Converts message format internally |

### 8.3 ProviderResolver — Auto-select Best

```python
# ai_provider/resolver.py
class ProviderResolver:
    def resolve(self):
        for name, provider in self._configured_providers():
            try:
                latency = provider.health_check()
                self._healthy.append((name, provider, latency))
            except Exception:
                pass
        # Sort by latency, pick the fastest
        self._active = min(self._healthy, key=lambda x: x[2])
```

ProviderResolver also supports three roles:
- `get_active_provider()` -> strong model (Sonnet/GPT-4), used for review synthesis and important decisions
- `get_explorer_provider()` -> lightweight (explorer) model (Haiku/Qwen), used for explorer agents, low cost
- `get_classifier_provider()` -> classifier-only model, can be used for LLM-assisted routing classification

**View current provider status:**
```bash
GET /ai/status
# -> {"active_model": "claude-sonnet-4-6", "active_provider": "anthropic", "models": [...]}
```

---

## 9. Git Workspace Management

### 9.1 Architecture

```
User provides GitHub PAT + Repo URL
             ↓
Backend authenticates via GIT_ASKPASS, clones as a bare repo
             ↓
For each collaboration room, creates a git worktree (independent working directory)
             ↓
VS Code FileSystemProvider mounts the worktree as a conductor:// virtual file system
```

**Why a bare repo?**
A bare repo only contains the `.git` content (no working directory), suitable for server-side storage and supports creating multiple worktrees.

**Why worktrees?**
Multiple rooms can share the same bare repo (saving disk + network), and each worktree is an independent working directory operating on its own branch, so they don't interfere with each other.

### 9.2 Workspace Creation Flow

```python
# git_workspace/service.py
class GitWorkspaceService:
    async def create_workspace(self, room_id, repo_url, token, branch) -> WorkspaceInfo:
        bare_path = self.workspaces_dir / "repos" / f"{room_id}.git"
        worktree_path = self.workspaces_dir / "worktrees" / room_id

        # 1. Create a temporary GIT_ASKPASS script (echo PAT)
        askpass = self._create_askpass_script(token)
        env = {**os.environ, "GIT_ASKPASS": askpass}

        # 2. Clone as a bare repo
        await run(["git", "clone", "--bare", repo_url, str(bare_path)], env=env)
        os.unlink(askpass)  # delete immediately to avoid token leakage

        # 3. Create the worktree on a dedicated branch
        branch_name = f"session/{room_id}"
        await run(["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
                  cwd=str(bare_path))

        return WorkspaceInfo(room_id=room_id, worktree_path=worktree_path, ...)
```

### 9.3 Path Sandbox

All code tools go through `_resolve()` to ensure paths can't escape the worktree:

```python
def _resolve(workspace: str, rel_path: str) -> Path:
    ws = Path(workspace).resolve()
    target = (ws / rel_path).resolve()
    if not str(target).startswith(str(ws)):
        raise ValueError(f"path out of bounds: {rel_path}")
    return target
```

All tool inputs and outputs use **relative paths** and never expose the server's absolute paths.

### 9.4 Workspace Interface

```bash
# Create workspace (clone + worktree)
POST /api/git-workspace/workspaces
{ "room_id": "room-123", "repo_url": "https://github.com/...", "token": "ghp_..." }

# List workspaces
GET /api/git-workspace/workspaces

# Sync (fetch + merge from remote)
POST /api/git-workspace/workspaces/{room_id}/sync

# Commit changes
POST /api/git-workspace/workspaces/{room_id}/commit
{ "message": "fix: auth token expiry" }

# Push
POST /api/git-workspace/workspaces/{room_id}/push

# Delete workspace
DELETE /api/git-workspace/workspaces/{room_id}
```

---

## 10. Chat System

### 10.1 Room Model and Persistence

Each collaboration session is a **room**, identified by `room_id`. Messages use a **write-through** model:

```
send message -> Redis hot cache (6h TTL, write and read immediately)
             -> ChatPersistenceService (micro-batch, writes to Postgres every 3 messages or 5 seconds)
```

**Postgres is the source of truth.** On reconnect, history is loaded from Postgres; Redis is a read cache.

```python
# chat/manager.py
class ConnectionManager:
    def __init__(self):
        # room_id -> [WebSocket, ...]
        self.active_connections: dict[str, list[WebSocket]] = {}
        # room_id -> [message, ...] (in-memory cache, rebuilt from Postgres after restart)
        self.room_messages: dict[str, list[dict]] = {}

    async def broadcast(self, room_id: str, msg: dict) -> None:
        dead = []
        for ws in self.active_connections.get(room_id, []):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active_connections[room_id].remove(ws)
```

### 10.2 WebSocket Protocol

Connection address: `ws://<host>/ws/chat/{room_id}`

**Server -> client:**

| type | When triggered |
|------|---------|
| `connected` | Connection established, returns user_id and role |
| `history` | Historical messages on connection |
| `message` | New message (chat / code_snippet / stack_trace / ai_message) |
| `typing` | Someone is typing |
| `read_receipt` | Message has been seen by some user |
| `user_joined` / `user_left` | Member joins/leaves room |
| `session_ended` | Host ended the session |

**Client -> server:**

| type | Action |
|------|------|
| `join` | Join room |
| `message` | Send message |
| `typing` | Typing |
| `read` | Mark message as read |
| `end_session` | End session (host only) |

### 10.3 AI Message Injection

AI replies are not sent through the WebSocket; instead they are injected via a separate HTTP endpoint, so the backend can take time to generate without blocking:

```python
@router.post("/chat/{room_id}/ai-message")
async def post_ai_message(room_id: str, req: AiMessageRequest):
    provider = get_resolver().get_active_provider()
    response = await provider.chat(messages=req.context_messages)
    msg = {"type": "ai_message", "content": response.text}
    await manager.broadcast(room_id, msg)
    return {"status": "sent"}
```

### 10.4 Reconnection

When the extension reconnects, it uses the `since` parameter to fetch missed messages and avoid reloading duplicates:

```python
@router.get("/chat/{room_id}/history")
async def get_history(room_id: str, since: str | None = None, limit: int = 50):
    messages = manager.get_history(room_id)
    if since:
        messages = [m for m in messages if m["timestamp"] > since]
    return messages[-limit:]
```

History records include a `codeSnippet` field (for `code_snippet` type messages), ensuring code snippets render correctly after reconnection.

### 10.5 Room Deletion

```python
# DELETE /chat/{room_id}
# Clears: in-memory history, Redis cache, Postgres records, associated files, audit logs
@router.delete("/chat/{room_id}")
async def delete_room(room_id: str):
    manager.clear_room(room_id)          # in-memory
    await redis_store.delete_room(...)   # Redis
    await persistence.delete_room(...)   # Postgres
    ...
```

The extension calls this endpoint when the user leaves a room, fully clearing the history.

---

## 11. Extension UI Flows

This section explains the two usage modes on the extension side, and how they interact with the backend. Required reading for new engineers debugging UI issues.

### 11.1 Two Session Modes

```
┌─────────────────────────────────────────┐
│  Extension launches -> select mode      │
│                                         │
│  [Online mode]                          │
│   └── Load room list (GET /chat/rooms)  │
│   └── Join existing room or create new  │
│   └── Git Workspace managed server-side │
│                                         │
│  [Local mode]                           │
│   └── Auto-register local workspace     │
│       (POST /api/git-workspace/         │
│        workspaces/local)                │
│   └── Tools forwarded over WebSocket    │
│       to extension for local execution  │
└─────────────────────────────────────────┘
```

**Key differences:**

| | Online mode | Local mode |
|---|---|---|
| Git workspace | Backend bare clone + worktree | User's local directory |
| Tool execution | Backend Python | Extension TypeScript (localToolDispatcher) |
| Chat history | Postgres (persistent) | chatLocalStore (VS Code globalState) |
| AI calls | Via backend `/api/context/query` | Same (both go through backend) |

### 11.2 Online Mode: Loading the Room List

```typescript
// StatePanels.tsx — ReadyToHostPanel mode switching
// User toggles between Local/Online tabs
const [mode, setMode] = useState<"local" | "online">("local");
// When online mode is selected:
        loadOnlineRooms();  // post a message to the Extension Host
    }
}

// extension.ts — getOnlineRooms handler
case 'getOnlineRooms':
    const resp = await fetch(`${backendUrl}/chat/rooms?email=${userEmail}`);
    const rooms = await resp.json();
    panel.webview.postMessage({ command: 'onlineRooms', rooms });
```

### 11.3 Local Mode: Auto-registering the Workspace

```typescript
// extension.ts — _handleStartSession()
// Called automatically after the user clicks "New Session" (no manual "Use Local" button)
async _handleStartSession() {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders?.length) {
        // Warn if no workspace is open
        const action = await vscode.window.showWarningMessage(
            'No workspace folder open.',
            'Open Folder'
        );
        return;
    }
    // Register the local workspace with the backend
    await fetch(`${backendUrl}/api/git-workspace/workspaces/local`, {
        method: 'POST',
        body: JSON.stringify({ room_id, path: folders[0].uri.fsPath }),
    });
}
```

### 11.4 Local Tool Dispatch (localToolDispatcher)

In local mode, the agent's tool call flow is:

```
backend AgentLoopService
  -> RemoteToolExecutor (detects this is a local session)
  -> WebSocket tool_request message
  -> Extension._handleLocalToolRequest()
  -> localToolDispatcher.ts (pure TypeScript, zero Python deps)
      ├── 13 subprocess tools: grep/git/read/glob etc.
      ├── 6 AST tools: file_outline/find_symbol etc. (web-tree-sitter)
      ├── 6 complex tools: compressed_view/trace_variable etc.
      └── 2 file editing tools: file_edit/file_write (fileEditRunner.ts)
      Note: Jira tools (5) and browser tools (6) always run on the backend, not via local dispatch
  -> tool_response message returns to backend
  -> backend continues the agent loop
```

---

## 12. File Sharing

### 12.1 Upload Flow

```
VS Code WebView (browser sandbox)
  ↓  cannot send HTTP directly, uses vscode.postMessage
Extension Host (Node.js)
  ↓  multipart POST /api/files/upload
Backend -> PostgreSQL records metadata (file_id, room_id, sha256, original filename)
  ↓  returns file_id
Extension broadcasts file_id over WebSocket
  ↓  other members receive it and request GET /api/files/{file_id}
```

**Why does the Extension Host proxy this?** The VS Code WebView runs in a browser sandbox and cannot send arbitrary HTTP requests (CORS restrictions). The Extension Host is a Node.js process and has no such restriction.

### 12.2 Deduplication

On upload, SHA-256 is computed; if the hash already exists, the existing `file_id` is returned and the file is not stored again:

```python
def upload_file(self, room_id: str, filename: str, content: bytes) -> FileRecord:
    sha256 = hashlib.sha256(content).hexdigest()
    existing = self.db.query("SELECT * FROM files WHERE sha256 = ?", [sha256]).fetchone()
    if existing:
        return FileRecord.from_row(existing)  # dedup
    file_id = str(uuid4())
    path = self.upload_dir / room_id / file_id
    path.write_bytes(content)
    self.db.execute("INSERT INTO files VALUES ...", [file_id, room_id, sha256, ...])
    return FileRecord(file_id=file_id, ...)
```

---

## 13. Audit Log and TODO Management

### 13.1 Storage Layer

Audit logs and TODO data are persisted in PostgreSQL, sharing the same database instance as other business data.

### 13.2 Audit Log

Records every accept/reject of an AI-suggested change by a user:

```python
# audit/service.py
service = AuditLogService.get_instance()  # singleton

service.log_apply(AuditLogCreate(
    room_id="room-123",
    changeset_hash=sha256(changeset),   # changeset fingerprint, useful for traceability
    applied_by="user-456",
    mode="manual",                       # "manual" | "auto"
))

# Query the audit history of a room
logs = service.get_logs(room_id="room-123")
```

**Schema:**
```sql
CREATE TABLE audit_logs (
    id             INTEGER PRIMARY KEY,
    room_id        VARCHAR,
    changeset_hash VARCHAR,
    applied_by     VARCHAR,
    mode           VARCHAR,
    timestamp      TIMESTAMP
)
```

### 13.3 TODO Tracking

Each room has its own TODO list with full CRUD support:

```bash
GET    /todos/{room_id}           # list TODOs of a room
POST   /todos/{room_id}           # create TODO
PATCH  /todos/{room_id}/{todo_id} # update status/text
DELETE /todos/{room_id}/{todo_id} # delete
```

TODOs are persisted in PostgreSQL and survive service restarts.

---

## 14. Authentication

### 14.1 AWS SSO (Device Authorization Flow)

```yaml
# conductor.settings.yaml
sso:
  enabled: true
  start_url: "https://d-xxxx.awsapps.com/start"
  region: "eu-west-2"
```

Flow:
1. User calls `POST /auth/sso/start` -> gets a device code and verification URL
2. User opens the URL in a browser and signs in on the AWS-hosted page
3. Extension polls `POST /auth/sso/poll` until login completes
4. Receives a session token, cached in `globalState` (with TTL)

### 14.2 Git Credentials (PAT)

The Personal Access Token is passed to Git via the `GIT_ASKPASS` mechanism (see Section 9). The backend never persists the PAT; it only holds it in memory during git operations.

---

## 15. Jira Integration

Conductor's Jira integration has three layers: OAuth connection -> REST API -> Agent tools. Users interact with Jira via the `@AI /jira` command; the Brain dispatches an agent with the issue_tracking skill, which uses the 5 Jira tools to autonomously complete the task.

### 15.1 Architecture Overview

```
User: @AI /jira create Fix login bug
      ↓
Extension: /jira transform -> "[jira] Create a Jira ticket for: Fix login bug..."
      ↓
POST /api/context/query/stream -> Brain (Sonnet)
      ↓
Brain: create_plan -> dispatch_agent(
    tools=["grep", "read_file", "jira_search", "jira_create_issue", ...],
    skill="issue_tracking", model="strong", budget_tokens=500000)
      ↓
Sub-agent:
  1. grep/read_file analyzes the code -> collects affected files
  2. jira_search checks for duplicate tickets
  3. ask_user confirms ticket details (summary/project/priority/component)
  4. jira_create_issue creates the ticket -> returns browse_url
      ↓
Brain: synthesizes the result, returns the final answer with the Jira link
      ↓
Chat UI: automatically renders ticket keys like DEV-123 as clickable Jira links
```

### 15.2 Four User Intents

The `/jira` slash command detects intent based on user input and generates the corresponding query:

| Command | Intent | Agent behavior |
|------|------|-----------|
| `/jira create Fix login bug` | CREATE | Analyze code -> check for duplicates -> assess complexity -> ask_user to confirm -> create ticket -> return link |
| `/jira DEV-123` | CONSULT | Fetch ticket details -> read related code -> explain what to do + suggest implementation |
| `/jira my tickets` | SEARCH | JQL search -> group by priority -> suggest what to tackle first |
| `/jira my sprint` | SEARCH | openSprints() JQL -> group by status -> highlight blockers |
| `/jira blockers` | SEARCH | High-priority + blocked label -> suggest what to unblock first |
| `/jira workload` | SEARCH | All assigned tickets -> stats by status and priority -> suggest a focus plan |
| `/jira update DEV-123 ...` | UPDATE | ask_user to confirm -> status transition/comment/field update |
| `/jira` (empty) | SEARCH | List all open tickets, grouped by priority |

### 15.3 OAuth 3LO Flow

```
User clicks "Connect Jira"
      ↓
GET /api/integrations/jira/authorize-url
      -> Generates Atlassian authorize URL + state (CSRF protection)
      ↓
User signs in to Atlassian in the browser, authorizes Conductor
      ↓
Atlassian redirects to redirect_uri -> backend exchanges code -> tokens + cloud_id
      ↓
Browser opens vscode://publisher.conductor/jira/callback?connected=true
      ↓
Extension JiraUriHandler:
      -> POST /api/integrations/jira/callback to exchange code
      -> GET /api/integrations/jira/tokens to retrieve token
      -> JiraTokenStore stores them in SecretStorage + .conductor/jira.json
      -> Auto-restores connection on startup (no need to re-authorize)
```

**Configuration (conductor.secrets.yaml):**

```yaml
jira:
  client_id: "your-atlassian-client-id"
  client_secret: "your-atlassian-client-secret"
  redirect_uri: "https://your-backend-url/api/integrations/jira/callback"
  teams:
    - id: "uuid-1234"
      name: "Platform"
```

**Configuration (conductor.settings.yaml):**

```yaml
jira:
  enabled: true
  allowed_projects: ["DEV", "FN", "FO", "HELP", "PT", "REN"]
  branch_formats:
    feature: "feature/{ticket}-{content}"
    bugfix: "bugfix/{ticket}-{content}"
```

### 15.4 5 Agent Tools

These tools are registered in `JIRA_TOOL_REGISTRY` and called by sub-agents dispatched by the Brain:

```python
# jira_search — supports JQL, free text, and shortcuts
result = jira_search(workspace, query="my sprint")
# Shortcuts auto-expand:
#   "my tickets"  -> assignee = currentUser() AND status NOT IN (Done, Closed, Resolved)
#   "my sprint"   -> assignee = currentUser() AND sprint IN openSprints()
#   "blockers"    -> priority IN (Highest, Blocker) OR labels = blocked

# jira_get_issue — full details (ADF -> plain text conversion)
result = jira_get_issue(workspace, issue_key="DEV-123")
# -> summary, description, status, priority, assignee, components, comments, subtasks

# jira_create_issue — ADF description + code blocks + subtasks
result = jira_create_issue(workspace,
    project_key="DEV", summary="Fix auth bug",
    description="Token expiry...\n```python\ndef refresh()...\n```",
    parent_key="DEV-100")  # create as a subtask under an Epic

# jira_update_issue — safety: Done/Closed/Resolved are blocked
result = jira_update_issue(workspace,
    issue_key="DEV-123", transition_to="In Progress", comment="Started work")
# ⚠️ transition_to="Done" → returns error, requires the user to close it manually

# jira_list_projects — list accessible projects
result = jira_list_projects(workspace)
```

### 15.5 issue_tracking Investigation Skill

When the Brain dispatches a sub-agent it injects the `issue_tracking` skill (L3 prompt), which guides the agent to act per intent:

- **CREATE**: investigate code first -> check for duplicates -> assess complexity -> use `jira_project_guide.yaml` to map file paths to project + component -> `ask_user` to confirm -> create
- **CONSULT**: fetch ticket -> read related code -> output a structured report (ticket header + code mapping + suggested approach)
- **SEARCH**: build JQL -> group by priority -> suggest focus
- **UPDATE**: `ask_user` to confirm -> apply update

**Project mapping (`config/jira_project_guide.yaml`):**

The agent uses git diff file paths to automatically match a Jira project and component:

```yaml
projects:
  DEV:
    description: "Core engineering"
    repos:
      abound-server:
        rules:
          - paths: ["abound-server/"]
            component: "JBE"
          - paths: ["CDE/"]
            candidates: ["Mortgage", "Decision Engine"]  # agent picks based on context
        default_component: "JBE"
```

### 15.6 TODO <-> Ticket Two-Way Sync

The extension provides two-way linking between TODOs and Jira tickets:

```
┌─ Backlog (3 sections) ────────────────────┐
│                                            │
│  📎 Linked (TODO + Jira linked)            │  ← can be dragged into AI Working Space
│    {jira:DEV-123} Fix auth timeout         │
│    Status: In Progress │ Priority: High    │
│                                            │
│  📝 Code TODOs (no Jira link)              │
│    // TODO: refactor this function         │
│    src/auth.ts:42                          │
│                                            │
│  🔵 Jira Tickets (current user's open)     │
│    DEV-456: Add retry logic                │
│    Status: To Do │ Priority: Medium        │
│                                            │
├─ AI Working Space ─────────────────────────┤
│  Drag Linked cards into this area to       │
│  have AI analyze them                      │
└────────────────────────────────────────────┘
```

**Key components:**

| File | Responsibility |
|------|------|
| `ticketProvider.ts` | `ITicketProvider` interface + `JiraTicketProvider` implementation (batch status query, my tickets) |
| `todoScanner.ts` | Workspace TODO scanner (supports `{jira:KEY}` tags + bare KEY pattern, 43+ file types) |
| `webview-ui/src/components/tasks/TasksTab.tsx` | 3-section Backlog UI + AI Working Space + drag-and-drop |

**`{jira:KEY}` tag:**

Add a `{jira:DEV-123}` tag in TODO comments in code to link to a Jira ticket:

```python
# TODO: Fix token expiry check
# TODO_DESC: {jira:DEV-123} Token refresh not triggered before expiry
```

### 15.7 Ticket Creation UI

The extension provides a Jira ticket creation form (`showJiraModal(prefill)`):

- **Component multi-select**: chip/tag UI + dropdown filter
- **Agent prefill**: agent analyzes the code and prefills all fields
- **User editable**: all fields can be modified before submission
- **Confirmation modal**: blurred background overlay, Create / Cancel buttons

### 15.8 Jira Rendering in Chat

- **Auto-link ticket keys**: `DEV-123` in AI replies is automatically rendered as a clickable Jira link (Jira must be connected)
- **Structured output**: the agent outputs a formatted ticket header + code mapping + priority grouping based on the skill prompt

### 15.9 REST API Endpoints

```bash
# OAuth
GET  /api/integrations/jira/authorize-url  # get authorize URL
GET  /api/integrations/jira/callback        # OAuth browser redirect handler
POST /api/integrations/jira/callback        # extension directly exchanges code
GET  /api/integrations/jira/status          # current connection status
POST /api/integrations/jira/disconnect      # disconnect
GET  /api/integrations/jira/tokens          # get token (for extension local persistence)
POST /api/integrations/jira/refresh         # refresh token

# CRUD
GET  /api/integrations/jira/projects        # list accessible projects
GET  /api/integrations/jira/issue-types     # query a project's issue types
GET  /api/integrations/jira/create-meta     # field metadata required to create an issue
GET  /api/integrations/jira/search          # JQL/text search (?q=...&maxResults=10)
GET  /api/integrations/jira/undone          # current user's open tickets (shortcut)
GET  /api/integrations/jira/issue/{key}     # get a single issue's full details
POST /api/integrations/jira/issues          # create issue
POST /api/integrations/jira/issue/{key}/transition  # status transition
```

### 15.10 Extension Token Persistence

Tokens are no longer in-memory only. The extension uses `JiraTokenStore` for local persistence:

| Data | Storage | Security |
|------|---------|---------|
| access_token, refresh_token | VS Code SecretStorage (OS keychain) | Encrypted |
| expires_at, cloud_id, site_url | `.conductor/jira.json` | Plaintext (non-sensitive) |

The connection is auto-restored from local storage on startup, and tokens are auto-refreshed on expiry, so users don't need to re-authorize.

### 15.11 Test Coverage

| File | Tests | Coverage |
|------|-------|------|
| `test_jira_router.py` | 45 | OAuth 3LO + REST API endpoints |
| `test_jira_service.py` | 48 | Token lifecycle + API calls |
| `test_jira_tools.py` | 21 | Agent tools (search/create/update/get_issue) |
| `ticketProvider.test.ts` | 93 | ITicketProvider + tag parsing + status query |

### 15.12 Common Issues

| Symptom | Cause | Fix |
|------|------|------|
| `Jira integration is not enabled` | `jira.client_id` not configured in secrets | Fill in `conductor.secrets.yaml` -> `make app-restart` |
| Ticket keys don't auto-link after connecting | `jiraSiteUrl` not set | Confirm Jira is connected (the JiraModal component sets siteUrl after receiving `jiraConnected`) |
| `jira_update_issue` cannot close a ticket | Safety blocks Done/Closed/Resolved | By design — agents shouldn't auto-close tickets; users must do it in Jira manually |
| Team field not found | `customfield_10001` varies between Jira instances | Call `GET /create-meta` first to confirm team_field_key |
| "my sprint" returns empty | Project has no Sprint board configured | Confirm the Jira project has Scrum board enabled and an active Sprint |

---

## 16. LangExtract Integration

`langextract/` provides a Bedrock multi-vendor plugin for Google's [langextract](https://github.com/google/langextract) library.

### 16.1 BedrockCatalog — Dynamic Model Discovery

```python
from app.langextract.catalog import BedrockCatalog

catalog = BedrockCatalog(region="eu-west-2")
catalog.refresh()  # calls list_foundation_models() + list_inference_profiles()

# Group by vendor (for UI dropdowns)
models = catalog.models_by_vendor()
# -> {"Anthropic": ["claude-sonnet-4-6", ...], "Amazon": ["nova-pro", ...], ...}
```

`BedrockCatalog` handles the `eu.` prefix for cross-region inference profiles automatically — no manual ID construction needed.

### 16.2 LangExtractService

```python
from app.langextract.service import LangExtractService
from langextract.data import ExampleData, Extraction

svc = LangExtractService(
    model_id="claude-sonnet-4-20250514",
    region="eu-west-2",
    catalog=catalog,
)
result = await svc.extract_from_text(
    text="Meeting notes: Alice will review the PR by March 15...",
    prompt="Extract people, dates, and action items.",
    examples=[ExampleData(
        text="Bob will fix the bug by Friday.",
        extractions=[
            Extraction(extraction_class="Person", extraction_text="Bob"),
            Extraction(extraction_class="Date", extraction_text="Friday"),
        ],
    )],
)
```

`BedrockLanguageModel` is registered via `@router.register()`, so `lx.extract(model_id="...")` automatically uses Bedrock. `ClaudeLanguageModel` is kept as a backwards-compatible alias.

---

## 17. Langfuse Observability

Langfuse provides nested execution trees, cost tracking, and latency analysis — a complement to SessionTrace:

| | SessionTrace | Langfuse |
|---|---|---|
| Data | Tool params, thinking text, budget signals | Cost, latency, nested tree |
| Storage | Local JSON file | Postgres (self-hosted) |
| UI | None (offline analysis) | Web UI (team-visible) |
| Overhead | ~0 (local file write) | ~0.1ms (async SDK) |

### 17.1 Start Langfuse Locally

```bash
# Start Langfuse + PostgreSQL (port 3001)
make langfuse-up

# View logs
make langfuse-logs

# Stop
make langfuse-down
```

Visit `http://localhost:3001`, create a project, and grab the API keys.

### 17.2 Configuration

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

### 17.3 Trace Structure

Example Langfuse trace tree for a PR review:

```
brain: pr_review                         45.2s  $0.38
├── transfer_to_brain("pr_review")       0.1ms
├── PRBrainOrchestrator phase 1: pre-compute (parse_diff, classify_risk, prefetch_diffs)
├── dispatch agent: correctness          35.2s  $0.12
│   └── agent: correctness (explorer)
│       ├── llm_call (generation)         1.2s   -> tool: grep
│       ├── llm_call (generation)         0.9s   -> tool: read_file
│       └── ... (18 tool calls total)
├── dispatch agent: security             32.8s  $0.09
│   └── ... (parallel with correctness)
├── stage: arbitrate                      3.8s  $0.08
│   └── agent: arbitrator (judge)
└── stage: synthesize                     2.3s  $0.03
    └── agent: review_synthesizer (judge)
```

### 17.4 @observe Decorator

Use the zero-intrusion decorator in workflow code:

```python
from app.workflow.observability import observe

# When Langfuse is disabled, these decorators are zero-cost no-ops
@observe(name="agent")
async def _run_agent(self, agent: AgentConfig, ...):
    ...
```

When Langfuse is disabled (`langfuse.enabled: false` or the package isn't installed), `@observe` returns the original function with no overhead at all.

---

## 18. Eval System (eval/)

`eval/` is a standalone set of three eval suites (excluded from the Docker image via `.dockerignore`):

```
eval/
├── code_review/          Code review quality (12 requests legacy + 50 Greptile benchmark)
├── agent_quality/        Agent Loop answer quality (baseline comparison)
└── tool_parity/          Python vs TypeScript tool output comparison
```

See `eval/README.md` for detailed docs.

### 18.1 Code Review Eval (code_review/)

Plants known bugs (git patches) into real open-source codebases, runs the full PR Brain / `CodeReviewService` pipeline, and checks whether the findings match expectations.

Two case sets coexist:

- **12 requests-v2.31.0 legacy cases** — the original in-house cases with a controlled difficulty gradient, good for fast unit-style regression.
- **50 Greptile benchmark cases** — aligned with Greptile's public AI Code Review Benchmark (10 real bug-fix PRs each from sentry / cal.com / grafana / keycloak / discourse). Used to compare `catch_rate` against commercial reviewers like Cursor / Copilot / CodeRabbit. Full details of the data pipeline in `eval/code_review/GREPTILE_BENCHMARK.md`.

```bash
cd backend

# One-time setup for the Greptile dataset (clone 5 forks + materialize 50 base
# snapshots + regenerate patches locally)
python ../eval/code_review/setup_greptile_dataset.py

# Run everything (legacy + Greptile) through PR Brain (production code path)
python ../eval/code_review/run.py --brain \
    --provider bedrock \
    --model "eu.anthropic.claude-sonnet-4-6" \
    --explorer-model "eu.anthropic.claude-haiku-4-5-20251001-v1:0"

# Run only the 50 Greptile cases with per-finding verbose output
python ../eval/code_review/run.py --brain --filter greptile- --verbose

# Run a subset (quick sanity check)
python ../eval/code_review/run.py --filter "requests-001" --no-judge
python ../eval/code_review/run.py --filter "greptile-sentry" --no-judge

# Save current results as a baseline (for future comparison)
python ../eval/code_review/run.py --save-baseline

# Gold standard: run Claude Code CLI directly (quality ceiling)
python ../eval/code_review/run.py --gold --gold-model opus --save-baseline
```

**12 legacy cases:** 4 easy, 5 medium, 3 hard (based on requests v2.31.0).

**50 Greptile cases:** 44 auto-imported from `greptile-apps[bot]` inline review comments, 6 hand-annotated (the bot left no usable anchors). Ground truth is `(file, line, severity, category)`; the scorer uses **catch_rate** as the headline metric (aligned with Greptile's published 82 %).

**Scoring dimensions:** Recall (35 %), Precision (20 %), Severity accuracy (15 %), Location accuracy (10 %), Fix suggestion (10 %), Context depth (10 %), plus **catch_rate** — whether at least one expected finding was produced on the right `(file, line)` for each case, matching Greptile's headline metric.

#### 18.1.1 Greptile data pipeline: two key techniques

The 50-case pipeline depends on two very specific technical decisions, each worth remembering on its own. Full derivation in `eval/code_review/GREPTILE_BENCHMARK.md` §7 / §8 and Appendix A (git object model primer).

**(1) Merge-base patch alignment (dataset bootstrap, `materialize_greptile_bases.py`)**

The naive approach — apply the GitHub-API-returned `.diff` against `base_sha` — **fails** on Greptile's forks because GitHub's `.diff` endpoint computes the diff against `merge-base(base_sha, head_sha)`, not against `base_sha` literally, and the Greptile forks periodically sync upstream, so `base_sha` contains commits the diff does not know about. `git apply` reports "patch does not apply".

Fix: derive both the snapshot and the patch from **the same local fork clone**, anchored at the merge-base.

```bash
# 1. blobless clone (--filter=blob:none: commits + trees only, blobs fetched on demand)
git clone --filter=blob:none https://github.com/ai-code-review-evaluation/sentry-greptile.git

# 2. compute the merge-base (pure commit-graph walk, no blobs needed)
merge_base=$(git merge-base $base_sha $head_sha)

# 3. materialize the source snapshot at merge_base via git archive
#    (blobs transparently fetched on first use)
git archive --format=tar $merge_base | tar -x -C repos/greptile_bases/sentry/001/

# 4. regenerate the patch locally from the same merge_base
#    (trees already local, only changed-file blobs are fetched)
git diff $merge_base $head_sha > cases/greptile_sentry/patches/001.patch
```

Snapshot and patch are guaranteed consistent — `git apply` always succeeds. **Generalisation**: any time you want to "replay a PR", do not mix API diffs with API `base_sha`. Anchor snapshot and patch to the same commit pair in the same local git state.

**(2) Hardlink workspace (per-case preparation, `runner.py::setup_workspace`)**

Running 50 cases requires an independent writable workspace per case (must `git init` + `git apply` + `git commit`) without polluting the shared base snapshots (6 GB × 50 = 300 GB is not an option, and snapshots need to be reusable across runs).

The naive `shutil.copytree` approach costs ~90 seconds per case on sentry's ~17K files — 75 minutes total across 50 cases, hostile to an interactive debug loop.

`setup_workspace` uses **hardlinks plus atomic-write-triggered copy-on-write**:

```python
def _link_or_copy(s, d):
    try:
        os.link(str(s), str(d))       # same inode, instantaneous
    except OSError:
        shutil.copy2(str(s), str(d))  # cross-filesystem fallback
```

A hardlink is just a new directory entry pointing at an existing inode — zero bytes copied. 17K files drop from ~90 s to ~1 s.

The key insight: `git apply` (and every well-behaved Unix writer) modifies files using **write-new-file + rename** — it writes a brand-new inode to a temp file, then renames it over the target. Rename only touches the **workspace's** directory entry; the snapshot's directory entry still points at the **old** inode, which is never touched. The hardlink breaks at exactly the one file that got modified; all the other ~17 000 files stay shared with the snapshot.

For a PR that changes 3 files, each workspace owns 3 new inodes (plus `.git/`); the other 17K files are shared with the snapshot for free. `rmtree` on cleanup frees only the workspace-only inodes, leaving the snapshot pristine.

**Generalisation**: any time you need to hand many consumers an "independent writable view" of a large read-only dataset — test runners, CI build caches, container rootfs prep, eval harnesses — hardlinks + the atomic-write convention give you almost-free per-file copy-on-write without needing btrfs / overlayfs / zfs or any special kernel support. pnpm's `node_modules` store, Nix's `/nix/store`, `cp --link`, `rsync --link-dest`, and ccache are all variants of this pattern.

The shared philosophy behind both techniques: **trust git's object model, trust the POSIX filesystem conventions, and introduce as little new abstraction as possible**.

### 18.2 Agent Quality Eval (agent_quality/)

End-to-end tests of Agent Loop answer quality against baseline answers:

```bash
cd backend

# Run all baseline cases
python ../eval/agent_quality/run.py

# Run a specific case
python ../eval/agent_quality/run.py --case abound_render_approval

# Compare direct Agent vs Workflow (multi-Agent)
python ../eval/agent_quality/run.py --compare
```

Baseline files live in `eval/agent_quality/baselines/*.json`; each JSON defines `workspace`, `question`, and `required_findings` (with weights and match patterns).

### 18.3 Tool Parity Eval (tool_parity/)

Compares the outputs of the Python (tree-sitter) and TypeScript (extension) tool implementations to confirm they agree:

```bash
cd backend

# Generate Python baseline
python ../eval/tool_parity/run.py --generate-baseline

# Compare TS output (requires the extension to be running)
python ../eval/tool_parity/run.py --compare
```

---

## 19. Testing Conventions

### 19.1 Running Tests

```bash
cd backend
pytest                                            # full 1300+ tests
pytest tests/test_agent_loop.py -v               # Agent Loop
pytest tests/test_code_tools.py -v               # code tools
pytest tests/test_budget_controller.py -v        # budget controller
pytest tests/test_compressed_tools.py -v         # compressed view tools
pytest tests/test_langextract.py -v              # LangExtract
pytest tests/test_repo_graph.py -v               # dependency graph
pytest tests/test_chat_persistence.py -v         # chat persistence
pytest tests/test_browser_tools.py -v            # browser tools (Playwright, mocked)
pytest --cov=. --cov-report=html                 # coverage report

# Tool parity verification (Python <-> TypeScript)
make test-parity                                  # contract check + shape verification + subprocess verification
```

**Main test files:**

| File | Count | Coverage |
|------|------|---------|
| `test_code_tools.py` | 139 | All 43 tools + dispatcher + multi-language |
| `test_agent_loop.py` | 55 | Agent Loop + 4-layer prompt + completeness checks |
| `test_brain.py` | 64 | Brain orchestrator + dispatch patterns |
| `test_jira_tools.py` | 21 | Jira agent tools |
| `test_jira_service.py` | 48 | Jira OAuth + API service |
| `test_jira_router.py` | 45 | Jira REST endpoints |
| `test_budget_controller.py` | 20 | Budget signal transitions, tracking, edge cases |
| `test_session_trace.py` | 23 | SessionTrace JSON save/load |
| `test_evidence.py` | 19 | Evidence evaluator quality gate |
| `test_symbol_role.py` | 24 | Symbol role classification + decorator detection |
| `test_output_policy.py` | 19 | Per-tool truncation policy, budget-adaptive |
| `test_compressed_tools.py` | 24 | compressed_view, module_summary, expand_symbol |
| `test_langextract.py` | 57 | Bedrock provider, catalog, service |
| `test_repo_graph.py` | 72 | Parser + dependency graph + PageRank |
| `test_config_new.py` | 27 | Config + Secrets |
| `test_chat_persistence.py` | — | ChatPersistenceService micro-batch writes, flush timer |
| `test_browser_tools.py` | — | Browser tools (Playwright service mocked) |

### 19.2 Test Infrastructure

**Central stubs in `conftest.py`:** libraries like cocoindex, litellm, sentence_transformers, sqlite_vec are stubbed so tests can run without installing every external dependency.

**Code tool tests use the real filesystem:**

```python
def test_grep(tmp_path):
    # Create a real file
    (tmp_path / "app.py").write_text("def authenticate(user): ...")
    result = execute_tool("grep", str(tmp_path), {"pattern": "authenticate"})
    assert result.success
    assert "app.py" in result.data
    assert result.data["app.py"][0]["line"] == 1
```

**Agent Loop tests use a MockProvider:**

```python
class MockProvider(AIProvider):
    def __init__(self, responses: list[ToolUseResponse]):
        self._it = iter(responses)

    def chat_with_tools(self, messages, tools, system="") -> ToolUseResponse:
        return next(self._it)

async def test_agent_loop_basic():
    provider = MockProvider([
        # Round 1: LLM decides to call grep
        ToolUseResponse(tool_calls=[ToolCall(id="t1", name="grep",
                                             input={"pattern": "authenticate"})]),
        # Round 2: LLM sees the result and gives the answer
        ToolUseResponse(text="Auth is in auth/router.py:42", stop_reason="end_turn"),
    ])

    agent = AgentLoopService(provider=provider, max_iterations=25,
                             budget_config=BudgetConfig(max_input_tokens=500_000))
    result = await agent.run("How does auth work?", "/tmp/workspace")

    assert "auth/router.py" in result.answer
    assert result.tool_calls_made == 1
    assert result.budget_summary["total_input_tokens"] > 0
```

The MockProvider lets you test the full agent loop logic — tool calls, result injection, iteration control, budget signals, evidence validation — without calling a real API.

### 19.3 Workflow Engine Tests

Workflow tests use the **real config files** (`config/workflows/*.yaml`, `config/agents/*.md`) and only mock the AI provider:

```python
def test_workflow_pr_review():
    workflow = load_workflow("workflows/pr_review.yaml")
    engine = WorkflowEngine(provider=MockProvider([...]))

    context = {
        "file_paths": ["src/auth/router.py", "src/auth/jwt.py"],
        "changed_lines": 150,
        "workspace_path": str(tmp_path),
    }
    result = await engine.run(workflow, context)

    # Auth files should trigger the security route
    assert "security" in result["_active_routes"]
    assert "_stage_results" in result
```

---

## 20. Common Development Tasks

### 20.1 Add a New Agent

1. Create a `.md` file under `config/agents/`:

```markdown
---
name: performance
type: explorer
model_role: explorer

tools:
  core: true
  extra:
    - get_callees
    - trace_variable
    - git_log

budget_weight: 1.0

trigger:
  always: false
---

## Performance Analysis Strategy

1. Use grep to search for common performance issue patterns (N+1 queries, missing indexes, full table scans)
2. Use get_callees to analyze the call depth of hot paths
3. Use trace_variable to track the flow of variables that carry large data volumes
...
```

2. Reference it in the workflow YAML:

```yaml
# config/workflows/pr_review.yaml
routes:
  performance:           # new route
    file_patterns:
      - "query|select|fetch|load|bulk"
    pipeline:
      - stage: explore
        agents: [agents/performance.md]
```

3. Run tests to confirm the workflow loads correctly:

```bash
pytest -k "test_workflow" -v
```

### 20.2 Add a New Code Tool

1. Implement the tool function in `code_tools/tools.py`:

```python
def _run_find_todo(workspace: str, params: dict) -> ToolResult:
    """Search for TODO/FIXME comments"""
    pattern = params.get("pattern", "TODO|FIXME|HACK|XXX")
    ws = Path(workspace)
    results = []
    for f in ws.rglob("*.py"):
        if ".git" in str(f) or "node_modules" in str(f):
            continue
        for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if re.search(pattern, line, re.IGNORECASE):
                results.append({"file": str(f.relative_to(ws)), "line": i, "content": line.strip()})
    return ToolResult(success=True, data=results)
```

2. Register it in the `execute_tool()` dispatcher:

```python
TOOL_REGISTRY = {
    ...
    "find_todo": _run_find_todo,
}
```

3. Add the JSON Schema to `TOOL_DEFINITIONS` in `schemas.py` (the tool description the LLM sees):

```python
{
    "name": "find_todo",
    "description": "Search for TODO/FIXME comments in the codebase",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern (default: TODO|FIXME|HACK|XXX)"}
        }
    }
}
```

4. Add a truncation policy in `output_policy.py`:

```python
"find_todo": Policy(max_results=30),
```

5. Add a test in `tests/test_code_tools.py`.

### 20.3 Add a New AI Provider

1. Subclass `AIProvider`:

```python
# ai_provider/my_provider.py
class MyProvider(AIProvider):
    def chat_with_tools(self, messages, tools, system="") -> ToolUseResponse:
        # Convert Bedrock-format messages to My API format
        api_messages = _convert_messages(messages)
        api_tools = _convert_tools(tools)

        resp = my_api.chat(messages=api_messages, tools=api_tools, system=system)

        # Convert back to the unified format
        return ToolUseResponse(
            text=resp.text,
            tool_calls=[ToolCall(id=tc.id, name=tc.name, input=tc.args) for tc in resp.tool_calls],
            stop_reason=resp.finish_reason,
            usage=TokenUsage(input_tokens=resp.usage.prompt, output_tokens=resp.usage.completion),
        )
```

2. Register it in `ProviderResolver`:

```python
# ai_provider/resolver.py
def _configured_providers(self) -> list[tuple[str, AIProvider]]:
    ...
    if self._config.ai_providers.my_provider.api_key:
        yield "my_provider", MyProvider(self._config.ai_providers.my_provider)
```

### 20.4 Modify an Agent's Tool Set or Persona

Edit `config/agents/*.md` directly — no Python code changes needed:

```markdown
---
name: security
tools: [grep, read_file, find_references, get_callers, trace_variable, db_schema]
limits:
  max_iterations: 25
---

## Security Review Strategy
- New: check raw SQL string concatenation in SQLAlchemy
- ...
```

Restart the backend after editing — agents are reloaded from file each time the Brain starts.

### 20.5 Debug the Agent Loop

The fastest way to debug is to use the synchronous interface and watch the full logs:

```python
import asyncio
import logging
logging.basicConfig(level=logging.DEBUG)

from app.agent_loop.service import AgentLoopService
from app.agent_loop.budget import BudgetConfig
from app.ai_provider.claude_direct import ClaudeDirectProvider

provider = ClaudeDirectProvider(api_key="sk-ant-...")
agent = AgentLoopService(
    provider=provider,
    max_iterations=5,
    budget_config=BudgetConfig(max_input_tokens=50_000),
)

result = asyncio.run(agent.run(
    query="how does authentication work?",
    workspace_path="/path/to/your/repo"
))

print(result.answer)
print(f"Tool calls: {result.tool_calls_made}")
print(f"Token usage: {result.budget_summary}")
```

---

## 21. Deployment Notes

### 21.1 System Dependencies

```bash
# Required
git >= 2.15    # worktree support
ripgrep (rg)   # underlying search engine used by the grep tool

# Optional
ast-grep       # ast_search tool (structured AST queries)
docker         # for running Langfuse (self-hosted observability)
```

### 21.2 Directory Layout

The following directories must be writable at runtime:

```
/var/conductor/workspaces/
├── repos/       # bare git clones (one per room)
└── worktrees/   # working directories (one per room)
```

Each active room takes about 2-3x the repo size in disk space.

### 21.3 Config File Search Order

```python
# config.py — config file search order
_SEARCH_DIRS = [
    Path("config/"),           # config/ in the current working directory
    Path("../config/"),        # config/ in the parent directory
    Path.home() / ".conductor/",  # user home directory
]
```

### 21.4 Dockerfile Example

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ .
COPY config/ ./config/

RUN mkdir -p /var/conductor/workspaces/repos /var/conductor/workspaces/worktrees

ENV GIT_WORKSPACE_ROOT=/var/conductor/workspaces

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

> All persistent data (audit logs, TODOs, file metadata) lives in PostgreSQL.

### 21.5 Docker Component Network (Local Development)

Local Docker Compose uses three compose files that share the same `conductor-net` Docker network:

```
docker/docker-compose.data.yaml   -> Postgres (conductor-postgres:5432)
                                    Redis    (conductor-redis:6379)
docker/docker-compose.app.yaml    -> Backend  (uses container names to reach the data layer)
docker/docker-compose.langfuse.yaml -> Langfuse (uses container name to reach Postgres)
```

**Startup commands:**

```bash
make data-up       # start Postgres + Redis first
make langfuse-up   # start Langfuse (shares the same Postgres)
make app-up        # start the backend (joins the same network)

# Or start the full stack in one shot
make docker-up
```

**Note (WSL2 scenario):** `host.docker.internal` cannot be resolved from inside containers under some WSL2 configurations. All compose files now use container names (`conductor-postgres`, `conductor-redis`) instead of `host.docker.internal`, communicating over the shared `conductor-net` network.

### 21.6 Health Check and Monitoring

```bash
# Basic liveness check
GET /health
# -> {"status": "ok"}

# Prometheus metrics (minimal)
GET /metrics
# -> conducator_up 1

# AI provider status
GET /ai/status
# -> {"active_model": "...", "available_providers": [...]}
```

### 21.7 Cloud Deployment Environment Variables (ECS / K8s)

The Docker image ships with `config/conductor.secrets.yaml` (dev defaults baked in).
For cloud deployment, set environment variables to override any secret —
**env vars take priority over YAML values**.

**How to configure:** Set the variables in your ECS Task Definition or K8s Secret.
Variables not set will fall back to the dev defaults in the YAML file.

#### Full Variable Reference

| Environment variable | Corresponding secret | Required |
|---------|-----------|------|
| **AI Providers** | | |
| `CONDUCTOR_AWS_ACCESS_KEY_ID` | ai_providers.aws_bedrock.access_key_id | Required for Bedrock |
| `CONDUCTOR_AWS_SECRET_ACCESS_KEY` | ai_providers.aws_bedrock.secret_access_key | Required for Bedrock |
| `CONDUCTOR_AWS_SESSION_TOKEN` | ai_providers.aws_bedrock.session_token | Set when using temporary credentials |
| `CONDUCTOR_AWS_REGION` | ai_providers.aws_bedrock.region | Defaults to us-east-1 |
| `CONDUCTOR_ANTHROPIC_API_KEY` | ai_providers.anthropic.api_key | Required for Anthropic Direct |
| `CONDUCTOR_OPENAI_API_KEY` | ai_providers.openai.api_key | Required for OpenAI |
| `CONDUCTOR_ALIBABA_API_KEY` | ai_providers.alibaba.api_key | Required for DashScope |
| `CONDUCTOR_ALIBABA_BASE_URL` | ai_providers.alibaba.base_url | Defaults to Singapore region |
| `CONDUCTOR_MOONSHOT_API_KEY` | ai_providers.moonshot.api_key | Required for Moonshot |
| `CONDUCTOR_MOONSHOT_BASE_URL` | ai_providers.moonshot.base_url | Defaults to api.moonshot.ai |
| **Database** | | |
| `CONDUCTOR_POSTGRES_USER` | postgres.user | Defaults to conductor |
| `CONDUCTOR_POSTGRES_PASSWORD` | postgres.password | Defaults to conductor |
| `DATABASE_URL` | Full connection URL (overrides host/port/db config) | Either/or |
| **Integrations** | | |
| `CONDUCTOR_JIRA_CLIENT_ID` | jira.client_id | Required for Jira integration |
| `CONDUCTOR_JIRA_CLIENT_SECRET` | jira.client_secret | Required for Jira integration |
| `CONDUCTOR_GOOGLE_CLIENT_ID` | google_sso.client_id | Required for Google SSO |
| `CONDUCTOR_GOOGLE_CLIENT_SECRET` | google_sso.client_secret | Required for Google SSO |
| `CONDUCTOR_NGROK_AUTHTOKEN` | ngrok.authtoken | Required for Ngrok |
| **Observability** | | |
| `LANGFUSE_PUBLIC_KEY` | langfuse.public_key | Required for Langfuse |
| `LANGFUSE_SECRET_KEY` | langfuse.secret_key | Required for Langfuse |
| `LANGFUSE_HOST` | langfuse.host (in settings.yaml) | Defaults to localhost:3001 |

#### Minimal ECS Task Definition Example

```json
{
  "containerDefinitions": [{
    "name": "conductor-backend",
    "image": "your-ecr/conductor-backend:latest",
    "environment": [
      { "name": "CONDUCTOR_AWS_REGION", "value": "eu-west-2" },
      { "name": "CONDUCTOR_POSTGRES_PASSWORD", "value": "prod-password" },
      { "name": "CONDUCTOR_JIRA_CLIENT_ID", "value": "..." },
      { "name": "CONDUCTOR_JIRA_CLIENT_SECRET", "value": "..." },
      { "name": "LANGFUSE_PUBLIC_KEY", "value": "pk-lf-prod-..." },
      { "name": "LANGFUSE_SECRET_KEY", "value": "sk-lf-prod-..." },
      { "name": "LANGFUSE_HOST", "value": "https://langfuse.your-domain.com" }
    ],
    "secrets": [
      {
        "name": "CONDUCTOR_AWS_ACCESS_KEY_ID",
        "valueFrom": "arn:aws:secretsmanager:eu-west-2:...:conductor/aws-key-id"
      },
      {
        "name": "CONDUCTOR_AWS_SECRET_ACCESS_KEY",
        "valueFrom": "arn:aws:secretsmanager:eu-west-2:...:conductor/aws-secret"
      }
    ]
  }]
}
```

> **Tip:** For sensitive values (API keys), prefer the ECS `secrets` field to inject from
> AWS Secrets Manager or SSM Parameter Store rather than plaintext `environment`.

---

## PR Checklist

Before submitting a PR:

- [ ] `pytest` passes (0 failures)
- [ ] `npm test` passes (extension tests)
- [ ] New code has test coverage
- [ ] If you modified workflow config: confirm `pytest -k "workflow"` passes
- [ ] If you modified code tool schemas: run `make update-contracts` and commit the generated files
- [ ] `make test-parity` passes (Python <-> TypeScript tool parity verification)
- [ ] `CLAUDE.md` updated (if you introduced new patterns or modules)
- [ ] `ROADMAP.md` updated (if you completed a roadmap item)
- [ ] No hard-coded API keys or passwords

---

*Reach out if you have questions, or just look at the architecture diagrams in `CLAUDE.md`.* 🚀
