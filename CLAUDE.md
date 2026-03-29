# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Conductor is a VS Code collaboration extension with a FastAPI backend. Two main parts:
1. **`extension/`** — TypeScript VS Code extension
2. **`backend/`** — Python FastAPI server

## Commands

### Quick Start
```bash
make setup          # create venv + install all dependencies
make data-up        # start Postgres + Redis (Docker)
make db-update      # apply Liquibase schema migrations
make run-backend    # start backend (dev mode, auto-reload)
make test           # run all tests (backend + extension)
make package        # compile and package extension as .vsix
make test-parity    # validate Python↔TS tool parity
make langfuse-up    # start self-hosted Langfuse (port 3001)
make update-prompt-library   # download latest prompts.chat CSV (agent design reference)
```

### Backend (Python/FastAPI)
```bash
cd backend
uvicorn app.main:app --reload
pytest                             # all tests
pytest -k "test_agent_loop"       # filter by name
pytest tests/test_code_tools.py -v
pytest --cov=. --cov-report=html
```

### Extension (TypeScript/VS Code)
```bash
cd extension
npm run compile    # one-time build
npm run watch      # watch mode
npm test
# F5 in VS Code → "Run VS Code Extension" to debug
```

### Eval
```bash
cd backend

# Code review quality (12 planted-bug cases)
python ../eval/code_review/run.py --provider anthropic --model claude-sonnet-4-20250514
python ../eval/code_review/run.py --filter "requests-001" --no-judge
python ../eval/code_review/run.py --brain --no-judge --verbose   # PR Brain mode
python ../eval/code_review/run.py --gold --gold-model sonnet     # Claude Code CLI baseline

# Agent answer quality (baseline comparison)
python ../eval/agent_quality/run_bedrock.py                  # Bedrock (Sonnet/Haiku)
python ../eval/agent_quality/run_bedrock.py --workflow --haiku  # Haiku explorer + Sonnet judge
python ../eval/agent_quality/run_bedrock.py --brain              # Brain orchestrator
python ../eval/agent_quality/run_qwen.py --workflow            # Qwen (DashScope)

# Tool parity (Python vs TS)
python ../eval/tool_parity/run.py --generate-baseline
```

## Architecture

### Backend Structure

```
backend/app/
├── main.py                  # FastAPI app, lifespan, router registration + service startup init
├── config.py                # Settings + Secrets from YAML
├── agent_loop/              # Agentic code intelligence (LLM + tools)
│   ├── service.py           # AgentLoopService — LLM loop, tool dispatch
│   ├── brain.py             # AgentToolExecutor — dispatch_agent/dispatch_swarm/transfer_to_brain
│   ├── pr_brain.py          # PRBrainOrchestrator — deterministic PR review pipeline via Brain
│   ├── budget.py            # BudgetController — token-based budget management
│   ├── trace.py             # SessionTrace — per-session JSON trace
│   ├── query_classifier.py  # QueryClassifier — keyword + optional LLM classification
│   ├── evidence.py          # EvidenceEvaluator — rule-based answer quality check
│   ├── completeness.py      # CompletenessCheck — verifies answer covers all query aspects
│   ├── interactive.py       # ask_user coordination (register/submit/cleanup)
│   ├── prompts.py           # 4-layer prompt architecture (Identity + Tools + Skills + Task)
│   └── router.py            # POST /api/context/query
├── chat/                    # WebSocket chat + persistence
│   ├── manager.py           # ConnectionManager — WebSocket room management
│   ├── redis_store.py       # Redis-backed hot message cache (6h TTL)
│   ├── persistence.py       # ChatPersistenceService — write-through micro-batch Postgres
│   └── router.py            # /ws/chat/{room_id}, /chat/{room_id}/history, DELETE /chat/{room_id}
├── browser/                 # Playwright-based web browsing tools
│   ├── service.py           # BrowserService — Chromium automation (Playwright)
│   ├── tools.py             # browse_url, search_web, screenshot tool implementations
│   └── router.py            # /api/browser/ endpoints
├── workflow/                # Config-driven workflow engine
│   ├── models.py            # Pydantic models: WorkflowConfig, AgentConfig, RouteConfig
│   ├── loader.py            # load_workflow() + load_agent() — YAML + Markdown parser
│   ├── classifier_engine.py # ClassifierEngine: risk_pattern + keyword_pattern
│   ├── engine.py            # WorkflowEngine: run_stream(), first_match + parallel_all_matching
│   ├── mermaid.py           # generate_mermaid() — auto-generate Mermaid diagrams
│   ├── router.py            # /api/workflows/ endpoints
│   └── observability.py     # Langfuse @observe decorator (no-op when disabled)
├── code_review/             # Multi-agent PR review pipeline
│   ├── service.py           # CodeReviewService — legacy 10-step review pipeline
│   ├── shared.py            # Shared functions (used by both CodeReviewService + PRBrain)
│   ├── agents.py            # Specialized review agents (parallel dispatch)
│   ├── models.py            # PRContext, ReviewFinding, ReviewResult, RiskProfile
│   ├── diff_parser.py       # Parse git diff into PRContext
│   ├── risk_classifier.py   # Risk classification (5 dimensions)
│   ├── ranking.py           # Score and rank findings
│   ├── dedup.py             # Merge and deduplicate findings
│   └── router.py            # /api/code-review/ endpoints (+ SSE stream)
├── code_tools/              # 24 code intelligence tools
│   ├── schemas.py           # Pydantic models + TOOL_DEFINITIONS for LLM
│   ├── tools.py             # Tool implementations
│   ├── output_policy.py     # Per-tool truncation policies (budget-adaptive)
│   ├── __main__.py          # Python CLI: python -m app.code_tools <tool> <ws> '<params>'
│   └── router.py            # /api/code-tools/ endpoints
├── langextract/             # LangExtract + multi-vendor Bedrock integration
├── ai_provider/             # LLM provider abstraction (Bedrock, Direct, OpenAI)
│   ├── base.py              # AIProvider ABC + ToolCall/ToolUseResponse/TokenUsage
│   ├── claude_bedrock.py    # Bedrock Converse API
│   ├── claude_direct.py     # Anthropic Messages API
│   ├── openai_provider.py   # OpenAI Chat Completions
│   └── resolver.py          # ProviderResolver — health checks, selection
├── repo_graph/              # AST-based symbol extraction + dependency graph
├── git_workspace/           # Git workspace management (Model A)
└── integrations/            # External service integrations
    └── jira/                # Jira OAuth 3LO + REST API
        ├── service.py       # JiraOAuthService — OAuth token lifecycle + API calls
        ├── models.py        # JiraTokenPair, JiraProject, JiraIssue, CreateIssueRequest
        └── router.py        # /api/integrations/jira/* endpoints

config/
├── conductor.settings.yaml  # Non-sensitive settings (committed)
├── conductor.secrets.yaml   # Secrets (gitignored)
├── brain.yaml               # Brain orchestrator config (limits, core_tools, model)
├── brains/                  # Specialized Brain configs
│   └── pr_review.yaml       # PR Brain config (agents, budget_weights, post_processing)
├── workflows/               # pr_review.yaml (parallel_all_matching), code_explorer.yaml (first_match)
├── agents/                  # 19 agent .md files (YAML frontmatter + Markdown body)
│   └── pr_arbitrator.md     # Defense attorney for PR review (challenges findings)
├── swarms/                  # Swarm presets (agent group + parallel/sequential)
│   ├── pr_review.yaml       # 5-agent PR review swarm
│   └── business_flow.yaml   # 2-agent business flow tracing
├── prompts/                 # review_base.md, explorer_base.md (shared templates)
└── prompt-library/          # prompts.chat CSV (1500+ role prompts, `make update-prompt-library`)

tests/
├── conftest.py                     # Centralized stubs + fixtures (cocoindex, litellm, etc.)
│
│   # Agent loop & Brain
├── test_agent_loop.py              # 47 tests — AgentLoopService, 4-layer prompt, evidence check
├── test_agent_loop_integration.py  # Integration tests — real Bedrock models (@integration marker)
├── test_brain.py                   # 39 tests — Brain orchestrator, AgentToolExecutor, dispatch modes
├── test_mock_agent.py              # 26 tests — MockProvider scripted responses + agent harness
├── test_interactive.py             # 9 tests  — ask_user coordination (register/submit/cleanup)
├── test_prompt_builder.py          # 64 tests — 4-layer prompt assembly, skill injection, tool hints
│
│   # Code review
├── test_code_review.py             # 67 tests — CodeReviewService legacy pipeline
│
│   # Code tools
├── test_code_tools.py              # 98 tests — 24 tools + dispatcher + multi-language
├── test_compressed_tools.py        # 24 tests — compressed_view, trace_variable, detect_patterns
├── test_detect_patterns.py         # 34 tests — detect_patterns tool (pattern extraction)
│
│   # Tool parity (Python ↔ TypeScript)
├── test_tool_parity.py             # 68 tests — get_dependencies/get_dependents/test_outline parity
├── test_tool_parity_ast.py         # 26 tests — AST tools parity (file_outline, find_symbol, etc.)
├── test_tool_parity_deep.py        # 34 tests — deep parity (trace_variable, compressed_view, etc.)
├── test_local_tools_parity.py      # 23 tests — local mode tool contract validation
│
│   # AI providers
├── test_ai_provider.py             # 131 tests — AIProvider ABC, ClaudeDirectProvider, Bedrock, OpenAI
├── test_bedrock_tool_repair.py     # 64 tests — Bedrock tool call repair + malformed response handling
├── test_litellm_provider.py        # 42 tests — LiteLLM provider adapter
│
│   # Workflow + config
├── test_config_new.py              # 27 tests — Settings + Secrets YAML loading
├── test_config_paths.py            # 3 tests  — Path resolution for audit logs
├── test_style_loader.py            # 22 tests — Agent .md frontmatter + body loader
│
│   # Infrastructure
├── test_budget_controller.py       # 20 tests — BudgetController token accounting
├── test_session_trace.py           # 15 tests — SessionTrace per-session JSON trace
├── test_evidence.py                # 14 tests — EvidenceEvaluator rule-based quality check
├── test_query_classifier.py        # 26 tests — QueryClassifier keyword + LLM classification
├── test_output_policy.py           # 19 tests — Per-tool truncation policies (budget-adaptive)
├── test_symbol_role.py             # 24 tests — Symbol role extraction (AST-based)
├── test_auto_apply_policy.py       # 28 tests — Auto-apply policy enforcement
│
│   # Language processing
├── test_langextract.py             # 57 tests — LangExtract + multi-vendor Bedrock integration
├── test_repo_graph.py              # 72 tests — AST symbol extraction + dependency graph
│
│   # Chat
├── test_chat.py                    # 29 tests — WebSocket chat, identity, lead transfer
├── test_chat_persistence.py        # ChatPersistenceService — micro-batch Postgres writes
│
│   # Integrations
├── test_jira_router.py             # 25 tests — Jira OAuth 3LO + REST API router
├── test_jira_service.py            # 43 tests — JiraOAuthService token lifecycle + API calls
├── test_auth.py                    # 38 tests — SSO ARN parsing, device auth flow
├── test_audit.py                   # 11 tests — AuditLogService + changeset hash
├── test_room_settings.py           # 18 tests — Room settings CRUD
│
│   # Git + workspace
├── test_git_workspace.py           # 75 tests — GitWorkspaceManager, worktree lifecycle
├── test_workspace_files.py         # 39 tests — workspace file browsing + filtering
├── test_db.py                      # 5 tests  — SQLAlchemy engine + table creation (Postgres)
│
│   # Browser + misc
├── test_browser_tools.py           # Browser tools (Playwright) — mocked service
└── test_main.py                    # 1 test   — FastAPI app startup / lifespan smoke test
```

### Extension Structure

```
extension/src/
├── extension.ts             # Entry point, command registration, _handleLocalToolRequest
│                            # getOnlineRooms, removeQuitRoom, auto-workspace registration
├── panels/                  # collabPanel.ts, workspacePanel.ts
├── services/
│   ├── conductorStateMachine.ts        # FSM: Idle → ReadyToHost → Hosting → Joined
│   ├── conductorController.ts          # FSM driver
│   ├── workflowPanel.ts                # Workflow visualization WebView (singleton)
│   ├── workspaceClient.ts              # /workspace/ HTTP client
│   ├── conductorFileSystemProvider.ts  # conductor:// URI scheme
│   ├── explainWithContextPipeline.ts   # 8-stage code explanation pipeline
│   │                                   # (Selection → LSP → Ranking → Plan → Execute → XML → LLM → Response)
│   ├── lspResolver.ts                  # VS Code LSP definition + references
│   ├── relevanceRanker.ts              # Hybrid structural + semantic relevance scoring
│   ├── contextPlanGenerator.ts         # Deduplicated read-file operation planner
│   ├── xmlPromptAssembler.ts           # Structured XML prompt builder for LLM
│   ├── localToolDispatcher.ts          # Three-tier tool dispatch (all native TS)
│   ├── astToolRunner.ts                # 6 AST tools via web-tree-sitter
│   ├── treeSitterService.ts            # web-tree-sitter WASM wrapper (8 languages)
│   ├── complexToolRunner.ts            # 6 complex tools (compressed_view, trace_variable, etc.)
│   └── chatLocalStore.ts               # Local message cache (IndexedDB via VS Code globalState)
└── commands/index.ts

extension/media/
├── chat.html      # Main WebView — @AI /ask /pr slash commands, Workflows tab
│                  # Online mode room list, renderMessageByType, Highlight.js syntax highlighting
│                  # chatLocalStore integration, mermaid with raw-source fallback
└── workflow.html  # Workflow visualization — SVG graph + agent detail panel

extension/media/highlight.min.js    # Bundled Highlight.js 11.9.0 (no CDN dependency)
extension/media/github-dark.min.css # Highlight.js GitHub Dark theme

extension/grammars/          # tree-sitter .wasm grammar files (committed)
├── tree-sitter.wasm         # web-tree-sitter runtime
└── tree-sitter-{lang}.wasm  # Python, JS, TS, Java, Go, Rust, C, C++ (8 files)
```

### Local Mode Tool Dispatch

When the agent runs in local workspace mode, tools are proxied via WebSocket to the extension. The extension runs ALL tools natively — zero Python dependency:

```
RemoteToolExecutor → WebSocket → extension._handleLocalToolRequest
  → localToolDispatcher.ts
    ├── SUBPROCESS (12): grep, read_file, list_files, git_log, git_diff, git_diff_files,
    │                    git_blame, git_show, find_tests, run_test, ast_search, get_repo_graph
    ├── AST (6):         file_outline, find_symbol, find_references, get_callees, get_callers, expand_symbol
    │                    → web-tree-sitter WASM (treeSitterService + astToolRunner)
    └── COMPLEX (6):     compressed_view, trace_variable, detect_patterns, get_dependencies, get_dependents, test_outline
                         → native TypeScript (complexToolRunner)
```

Grammar WASM files in `extension/grammars/` are committed to the repo. **Do not** re-download
grammars independently — the grammar ABI version must match `web-tree-sitter` (pinned at 0.26.7).
Mismatched versions cause silent fallback to regex extraction with degraded accuracy.

### Brain Orchestrator (Agentic Code Intelligence)

The **Brain** is an LLM orchestrator (strong model) that replaces the keyword classifier.
It understands queries, dispatches specialist agents, evaluates findings, and synthesizes answers.

```
Query → Brain (Sonnet, meta-tools: dispatch_agent, dispatch_swarm, transfer_to_brain, ask_user)
  → Brain decides: SIMPLE (1 agent) | COMPLEX (handoff) | SWARM (parallel) | TRANSFER (specialized brain)
  → dispatch_agent → AgentLoopService (Haiku, code tools, isolated context)
    → 4-layer prompt: L1 system (agent identity) + L2 tools + L3 skills (workspace) + L4 query
    → Tool execution (up to 20 iter / 420K tokens)
    → Evidence check (internal retry)
    → Returns condensed AgentFindings to Brain
  → Brain synthesizes final answer with file:line evidence
```

**Sub-agent prompt assembly (4-layer):**
- **Layer 1 (system prompt)**: Built per-agent from `.md` description + instructions — defines who this agent is
- **Layer 2 (tools)**: `brain.yaml` core_tools ∪ agent `.md` tools ∪ signal_blocker
- **Layer 3 (skills)**: Workspace layout, project docs, investigation patterns, risk signals, budget — shared across agents. PR review agents get `code_review_pr` skill (severity framework, DO NOT FLAG list, JSON output format).
- **Layer 4 (user message)**: The query from Brain + optional code_context — no role injection

**Four dispatch modes:**
- **SIMPLE** (~80%): one agent, trust result, done
- **COMPLEX** (~15%): agent → evaluate → handoff to different specialist with previous findings
- **SWARM** (~5%): `dispatch_swarm("business_flow")` — predefined parallel presets
- **TRANSFER**: `transfer_to_brain("pr_review")` — one-way handoff to specialized Brain (PR reviews)

**PR Brain** (`agent_loop/pr_brain.py`): Specialized deterministic pipeline for PR reviews. Activated via `transfer_to_brain("pr_review")`. Combines Brain's 4-layer prompts with CodeReviewService's deterministic post-processing:

```
transfer_to_brain("pr_review") → PRBrainOrchestrator
  Phase 1: Pre-compute (parse_diff, classify_risk, prefetch_diffs, impact_graph)
  Phase 2: Dispatch review agents (correctness[strong], security, reliability, concurrency, test_coverage)
  Phase 3: Post-process (evidence_gate → post_filter → dedup → score_and_rank)
  Phase 4: Adversarial arbitration (pr_arbitrator tries to rebut each finding)
  Phase 5: Merge recommendation (deterministic)
  Phase 6: Synthesis — Brain as final judge (sees sub-agent evidence + arbitrator counter-evidence)
```

Key design: The arbitrator is a **defense attorney** — it tries to rebut findings with counter-evidence and a rebuttal confidence score. It does NOT adjust severity. The synthesis LLM sees both sides (prosecution + defense) and makes the final call.

**Configuration:**
- `config/brain.yaml` — Brain limits (iterations, budget, concurrency, timeout) + core_tools
- `config/brains/pr_review.yaml` — PR Brain config (agents, budget_weights, post_processing)
- `config/agents/*.md` — Agent definitions (name, description, model, tools, limits + identity instructions)
- `config/swarms/*.yaml` — Swarm presets (agent group + parallel/sequential mode + synthesis_guide)

**Interactive AI:** Brain can `ask_user` for clarification when queries have multiple valid directions. Q&A answers are cached in session and injected into Brain's prompt for reuse across sub-agents.

**24 code tools** (`code_tools/tools.py`): `grep`, `read_file`, `list_files`, `find_symbol`, `find_references`, `file_outline`, `get_dependencies`, `get_dependents`, `git_log`, `git_diff`, `ast_search`, `get_callees`, `get_callers`, `git_blame`, `git_show`, `find_tests`, `test_outline`, `trace_variable`, `compressed_view`, `module_summary`, `expand_symbol`, `run_test`.

Tools also accessible via `python -m app.code_tools <tool> <workspace> '<json_params>'` (used by extension local mode).

**Workflow API** (`/api/workflows/`): `GET` list/detail/mermaid/graph, `PUT /{name}/models`.

### Code Review Pipeline

**Two paths available:**

1. **Legacy** (`CodeReviewService`): Hardcoded 10-step pipeline with Python `AgentSpec` definitions. Direct `POST /api/code-review/review` and `/review/stream` (SSE).

2. **PR Brain** (`PRBrainOrchestrator`): Brain-based pipeline with 4-layer prompts, per-agent `.md` identity, adversarial arbitration. Activated via `transfer_to_brain("pr_review")` from the Brain chat flow.

Both share the same post-processing code via `code_review/shared.py` (parse_findings, evidence_gate, dedup, ranking).

```
Legacy:  git diff → parse → risk → budget → impact → parallel agents → dedup → arbitration → synthesis
PR Brain: transfer_to_brain → pre-compute → dispatch agents (4-layer) → post-process → adversarial arbitration → synthesis (judge)
```

### Model A Git Workspace

```
User PAT → backend bare clone (GIT_ASKPASS) → worktree per room
  → FileSystemProvider mounts conductor://{room_id}/ in VS Code
```

## Key Patterns

### Agent Loop
```python
from app.agent_loop.service import AgentLoopService
from app.agent_loop.budget import BudgetConfig

agent = AgentLoopService(
    provider=ai_provider,
    max_iterations=25,
    budget_config=BudgetConfig(max_input_tokens=500_000),
)
result = await agent.run(query="How does auth work?", workspace_path="/path/to/ws")
# result.answer, result.context_chunks, result.tool_calls_made, result.budget_summary
```

### chat_with_tools (all 3 providers)
```python
response = provider.chat_with_tools(
    messages=[{"role": "user", "content": [{"text": "Find auth code"}]}],
    tools=TOOL_DEFINITIONS,
    system="You are a code assistant.",
)
# response.text, response.tool_calls (List[ToolCall]), response.stop_reason, response.usage
```

### Code Tools
```python
from app.code_tools.tools import execute_tool
result = execute_tool("grep", workspace="/path/to/ws", params={"pattern": "authenticate"})
# result.success, result.data, result.error
```

### Chat WebView (chat.html) Key Patterns

- **Message rendering**: use `renderMessageByType(msg)` — dispatches to the correct renderer based on `msg.type` (`text`, `code_snippet`, `ai_response`, `file_share`, etc.). Do NOT use `renderMessage()` directly for history/cached messages as it only handles text.
- **Syntax highlighting**: `highlightCodeBlocks(container)` — called after inserting any message DOM. Requires Highlight.js (`highlight.min.js` + `github-dark.min.css`) loaded from bundled files (not CDN).
- **Mermaid diagrams**: wrap `mermaid.render()` calls in `.catch()` to show raw source as fallback on parse error (Qwen/other LLMs sometimes emit invalid syntax).
- **Online mode**: auto-loads room list from `GET /chat/rooms?email=...` via `getOnlineRooms` extension command on mode selection. Rooms have status dots. Deleting a room calls `DELETE /chat/{roomId}` to purge history from Postgres.
- **Local workspace**: `_handleStartSession` auto-registers the workspace via `POST /api/git-workspace/workspaces/local` — no manual "Use Local" button needed. If no workspace folder is open, shows a warning with "Open Folder" action.
- **AI status retries**: silent retry up to 3 times before showing error banner — avoids false alarms on transient connectivity hiccups.

## Testing Notes

- Backend: `pytest` with mocked external dependencies
- `conftest.py`: stubs for cocoindex, litellm, sentence_transformers, sqlite_vec
- Code tools tests: real filesystem operations (`tmp_path` fixtures)
- Agent loop tests: `MockProvider` subclass with scripted responses
- Workflow tests: real config files from `config/`; `MockProvider` for agent execution
- ast-grep tests require `ast-grep-cli` in the venv
- tree-sitter and networkx are mocked in import stubs

### Tool Parity Testing

Python and TypeScript tools must produce equivalent output. `make test-parity` validates this:

1. Checks `contracts/tool_contracts.json` matches Python Pydantic schemas
2. Validates TS tool output shapes against the contract
3. **Validates 11 subprocess tools** by calling the Python CLI (`python -m app.code_tools`) and checking `{success, data}` shape — done inside `extension/tests/validate_contract.js`
4. Runs cross-language parity tests (60+ tests across 13 dual-implementation tools)

```bash
make test-parity          # full validation (contract + shape + output comparison)
make update-contracts     # regenerate contracts after changing Python schemas
```

Contract output: `contracts/tool_contracts.json` (JSON Schema) + `extension/src/services/toolContracts.d.ts` (TypeScript interfaces). Regenerate after any schema change with `make update-contracts`.

### Tool Change Process

When modifying or adding a code tool:

1. **Python first**: implement/modify in `backend/app/code_tools/tools.py`
2. **Update schema**: if params/result shape changed, update `schemas.py`
3. **Regenerate contracts**: `make update-contracts`
4. **Port to TS**: update the appropriate module:
   - Complex: `extension/src/services/complexToolRunner.ts`
   - AST: `extension/src/services/astToolRunner.ts`
5. **Add parity tests**: `test_tool_parity_ast.py` or `test_tool_parity_deep.py`
6. **Validate**: `make test-parity`

## Configuration

```bash
cp config/conductor.secrets.yaml.example config/conductor.secrets.yaml
# Fill in API keys
```

Key settings in `conductor.settings.yaml`:
- `classifier.use_llm` / `classifier.model_id` — enable LLM pre-classification
- `langfuse.enabled` + secrets in `conductor.secrets.yaml`
- `workflow_models.{name}.explorer` / `.judge` — model per workflow role

Environment variables:
```bash
BACKEND_HOST=0.0.0.0
BACKEND_PORT=8000
GIT_WORKSPACE_ROOT=/tmp/conductor_workspaces
ANTHROPIC_API_KEY=sk-ant-...     # or AWS_* for Bedrock, OPENAI_API_KEY for OpenAI
```

### Database Schema

Schema is managed by **Liquibase** (`database/changelog/`). The backend does NOT auto-create tables.

```bash
make data-up        # start Postgres + Redis
make db-update      # apply pending changesets
make db-status      # show pending changesets (dry run)
make db-rollback-one  # rollback last changeset
```

- `docker/init-db.sql` creates the `langfuse` database on first Docker start
- Langfuse manages its own tables internally (Prisma migrations)
- New changelog files go in `database/changelog/changes/` (formatted SQL)
- **Liquibase connection**: URL, username, and password are passed as `--url`, `--username`, `--password` CLI args in the Makefile (not in `liquibase.properties`). This is required because Java cannot parse bash `${VAR:-default}` syntax in JDBC URLs — use plain `${VAR}` or CLI args only.

### Chat Persistence

Chat messages use a **write-through** model:
1. Every message is written to Redis immediately (6h TTL hot cache)
2. `ChatPersistenceService` batches messages and writes to Postgres in groups of 3 (flush timer: 5s)
3. Postgres is the source of truth — history is loaded from Postgres on reconnect

Singleton services (`TODOService`, `AuditLogService`, `FileStorageService`, `ChatPersistenceService`) are initialized in `main.py` lifespan with the async SQLAlchemy engine. Do NOT call `get_instance()` without providing `engine=` on the first call.

## Eval System

Three eval suites under `eval/`. See `eval/README.md` for full docs.

```
eval/
├── code_review/        12 planted-bug cases against requests v2.31.0
├── agent_quality/      Agentic loop answer quality vs baselines
└── tool_parity/        Python vs TS tool output comparison
```

- **Code review**: `eval/code_review/run.py` — scoring: recall (35%), precision (20%), severity (15%), location (10%), recommendation (10%), context (10%)
- **Agent quality**: `eval/agent_quality/run_bedrock.py` / `run_qwen.py` — pattern-match answers against `required_findings` in baseline JSON
- **Tool parity**: `eval/tool_parity/run.py` — diff Python vs TS tool outputs for the same inputs

## Agent & Prompt Design Principles

When creating or editing agent definitions (`config/agents/*.md`), system prompts (`prompts.py`), or workflow configs, follow these principles. Sources: [Anthropic Prompt Engineering](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices), [Context Engineering for Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). We also maintain a local copy of [prompts.chat](https://github.com/f/prompts.chat) (1500+ prompts) at `config/prompt-library/` — primarily used as **example references** when designing new agent roles, not as direct templates.

### 4-Layer Prompt Architecture (mandatory)

Every agent prompt — Brain or sub-agent — MUST follow this 4-layer structure. Each layer has a distinct purpose and MUST NOT bleed into another.

| Layer | Purpose | Where it lives | What goes here |
|-------|---------|----------------|----------------|
| **1. System Prompt** | Who the agent is, what it cares about, how it behaves | `system` parameter in LLM call | Agent identity, perspective, behavioral rules, answer format. **Each sub-agent gets its own system prompt** — no shared "generic agent" identity. Built from agent `.md` description + instructions. |
| **2. Tools** | What the agent can do and when to use each tool | `tools` parameter in LLM call | Tool definitions with clear descriptions. Treat tool descriptions as prompts — they guide behavior. `brain.yaml` core_tools + agent-specific tools from `.md` frontmatter. |
| **3. Skills & Guidelines** | Project-specific knowledge and reusable patterns | Appended to system prompt, clearly separated | Workspace layout, project docs (README/CLAUDE.md), investigation patterns (domain models first, scope searches, etc.), risk signals, budget. Shared across agents — same project context for all. |
| **4. User Messages** | The actual task plus focused context | `messages` parameter in LLM call | The query from Brain, plus any code_context snippet. Keep it specific and scoped to what the agent needs right now. **Never inject agent identity or role into user messages.** |

**Key rules:**
- Agent identity (Layer 1) MUST be in the system prompt, never in the user message. The old pattern of appending `## Your Role` to the query violates this — the agent's role defines how it processes ALL messages, not just one.
- Layer 3 (Skills & Guidelines) is shared context, not identity. Two agents in the same workspace see the same project docs and investigation patterns, but have different system prompts.
- Layer 2 (Tools) is curated per agent. An implementation tracer gets `get_callers` + `trace_variable`; a usage tracer gets `find_tests` + `test_outline`. The tool set IS part of the agent's capabilities.

### Anthropic Core Principles

1. **Right Altitude** — Not too vague ("investigate the code"), not too prescriptive ("call get_callers on the gate method"). Target: "Trace the complete lifecycle from trigger to final outcome."
2. **Examples over rule lists** — 3-5 diverse examples teach behavior better than a laundry list of edge-case bullets. Wrap in `<example>` tags.
3. **Explain why, not just what** — Claude generalizes from motivation. "Output will be read by TTS, so avoid ellipses" beats "never use ellipses."
4. **Positive framing** — Say what to do, not what not to do.
5. **Context over instructions** — Provide workspace layout, project docs, detected project roots (Layer 3). Let the model decide the investigation path.
6. **Dial back aggressive language** — "Use this tool when..." not "CRITICAL: You MUST use this tool." Newer models overtrigger on forceful language.
7. **Minimal tool guidance** — If a human can't definitively say which tool to use, don't prescribe it. Let tool descriptions (Layer 2) guide the model.

### Multi-Agent Workflow Rules

8. **Role specialization** — Each agent has a distinct identity (Layer 1 system prompt). Shared investigation patterns belong in Layer 3, not Layer 1. Never add shared strategies to individual agent identities — this destroys role separation (proven by eval: 60% → 25% regression).
9. **Structured output via strategy** — Output format templates (e.g. code_review) are injected as a Layer 3 skill when the agent's frontmatter sets `strategy: code_review`. Don't inject investigation procedures for open-ended queries.
10. **Adversarial arbitration for PR reviews** — Sub-agents provide evidence FOR findings (prosecution). The arbitrator provides evidence AGAINST (defense). The synthesis LLM acts as judge, seeing both sides. The arbitrator does NOT adjust severity — it provides counter-evidence and a rebuttal confidence score.
11. **DO NOT FLAG list** — PR review agents have an explicit exclusion list: style/formatting, pre-existing issues, speculative concerns, secondary effects of the same root cause, design disagreements, generated/vendored code.
12. **Per-agent model selection** — Critical review dimensions (correctness) use the strong model; others use the explorer model. Set `model: strong` in the agent `.md` frontmatter.

### Agent `.md` File Design (informed by prompts.chat patterns)

10. **One clear role sentence** — Open with what the agent IS and what it traces. prompts.chat's "I want you to act as..." pattern works because it's unambiguous. Our equivalent: "You are investigating from the [perspective] side. Your goal is to trace [scope]." This becomes the core of the agent's Layer 1 system prompt.
11. **Goal, not procedure** — Define WHAT to find (domain models, service implementations, completion effects), not HOW to find it (don't say "first grep, then read_file, then get_callers").
12. **Short** — Agent instructions should be 50-150 words. prompts.chat averages 80 words. If you need more, you're probably being too prescriptive.
13. **Consult the prompt library** — Before writing a new agent role, search `config/prompt-library/prompts.csv` for similar roles. Study how they define constraints and scope. Use `for_devs=TRUE` filter for developer-focused prompts.

### Validation

15. **Test with eval** — Any prompt change must be validated with eval. For PR review: `eval/code_review/run.py --brain --verbose`. For exploration: `eval/agent_quality/run_bedrock.py --brain`. Check multiple modes — changes that help one can break another.

## What's Next

See [ROADMAP.md](ROADMAP.md). Near-term priorities:
- Microsoft Teams integration (Phase 7.5)
- Slack integration (Phase 7.6)
- Model B delegate authentication (no PAT required — Phase 5.1)
- Cross-session query patterns (learn from session traces — Phase 5.5)
- Persistent codebase memory (background file-summary indexer — Phase 5.5.2)
- Production hardening (Phase 6)
