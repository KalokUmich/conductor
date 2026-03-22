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

# Agent answer quality (baseline comparison)
python ../eval/agent_quality/run_bedrock.py                  # Bedrock (Sonnet/Haiku)
python ../eval/agent_quality/run_bedrock.py --workflow --haiku  # Haiku explorer + Sonnet judge
python ../eval/agent_quality/run_qwen.py --workflow            # Qwen (DashScope)

# Tool parity (Python vs TS)
python ../eval/tool_parity/run.py --generate-baseline
```

## Architecture

### Backend Structure

```
backend/app/
├── main.py                  # FastAPI app, lifespan, router registration
├── config.py                # Settings + Secrets from YAML
├── agent_loop/              # Agentic code intelligence (LLM + tools)
│   ├── service.py           # AgentLoopService — LLM loop, tool dispatch
│   ├── budget.py            # BudgetController — token-based budget management
│   ├── trace.py             # SessionTrace — per-session JSON trace
│   ├── query_classifier.py  # QueryClassifier — keyword + optional LLM classification
│   ├── evidence.py          # EvidenceEvaluator — rule-based answer quality check
│   ├── completeness.py      # CompletenessCheck — verifies answer covers all query aspects
│   ├── prompts.py           # 3-layer system prompt (Identity + Strategy + Runtime)
│   └── router.py            # POST /api/context/query
├── workflow/                # Config-driven workflow engine
│   ├── models.py            # Pydantic models: WorkflowConfig, AgentConfig, RouteConfig
│   ├── loader.py            # load_workflow() + load_agent() — YAML + Markdown parser
│   ├── classifier_engine.py # ClassifierEngine: risk_pattern + keyword_pattern
│   ├── engine.py            # WorkflowEngine: run_stream(), first_match + parallel_all_matching
│   ├── mermaid.py           # generate_mermaid() — auto-generate Mermaid diagrams
│   ├── router.py            # /api/workflows/ endpoints
│   └── observability.py     # Langfuse @observe decorator (no-op when disabled)
├── code_review/             # Multi-agent PR review pipeline
│   ├── service.py           # CodeReviewService — 10-step review pipeline
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
├── workflows/               # pr_review.yaml (parallel_all_matching), code_explorer.yaml (first_match)
├── agents/                  # 18 agent .md files (YAML frontmatter + Markdown body)
├── prompts/                 # review_base.md, explorer_base.md (shared templates)
└── prompt-library/          # prompts.chat CSV (1500+ role prompts, `make update-prompt-library`)

tests/
├── conftest.py              # Centralized stubs (cocoindex, litellm, etc.)
├── test_code_tools.py       # 98 tests — 24 tools + dispatcher + multi-language
├── test_agent_loop.py       # 47 tests — agent loop + 3-layer prompt + completeness
├── test_query_classifier.py # 26 tests
├── test_compressed_tools.py # 24 tests
├── test_budget_controller.py # 20 tests
├── test_session_trace.py    # 15 tests
├── test_evidence.py         # 14 tests
├── test_symbol_role.py      # 24 tests
├── test_output_policy.py    # 19 tests
├── test_langextract.py      # 57 tests
├── test_repo_graph.py       # 72 tests
└── test_config_new.py       # 27 tests
```

### Extension Structure

```
extension/src/
├── extension.ts             # Entry point, command registration, _handleLocalToolRequest
├── panels/                  # collabPanel.ts, workspacePanel.ts
├── services/
│   ├── conductorStateMachine.ts        # FSM: Idle → ReadyToHost → Hosting → Joined
│   ├── conductorController.ts          # FSM driver
│   ├── workflowPanel.ts                # Workflow visualization WebView (singleton)
│   ├── workspaceClient.ts              # /workspace/ HTTP client
│   ├── workspaceIndexer.ts             # AST symbol extraction
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
│   └── complexToolRunner.ts            # 6 complex tools (compressed_view, trace_variable, etc.)
└── commands/index.ts

extension/media/
├── chat.html      # Main WebView — @AI /ask /pr slash commands, Workflows tab
└── workflow.html  # Workflow visualization — SVG graph + agent detail panel

extension/grammars/          # tree-sitter .wasm grammar files (committed)
├── tree-sitter.wasm         # web-tree-sitter runtime
└── tree-sitter-{lang}.wasm  # Python, JS, TS, Java, Go, Rust, C, C++ (8 files)
```

### Local Mode Tool Dispatch

When the agent runs in local workspace mode, tools are proxied via WebSocket to the extension. The extension runs ALL 24 tools natively — zero Python dependency:

```
RemoteToolExecutor → WebSocket → extension._handleLocalToolRequest
  → localToolDispatcher.ts
    ├── SUBPROCESS (12): grep, read_file, list_files, git_*, find_tests, run_test, module_summary, ast_search
    ├── AST (6):         file_outline, find_symbol, find_references, get_callees, get_callers, expand_symbol
    │                    → web-tree-sitter WASM (treeSitterService + astToolRunner)
    └── COMPLEX (6):     compressed_view, trace_variable, detect_patterns, get_dependencies, get_dependents, test_outline
                         → native TypeScript (complexToolRunner)
```

Grammar WASM files in `extension/grammars/` are committed to the repo. **Do not** re-download
grammars independently — the grammar ABI version must match `web-tree-sitter` (pinned at 0.26.7).
Mismatched versions cause silent fallback to regex extraction with degraded accuracy.

### Agentic Code Intelligence

Active agent loop (not RAG). Agent iteratively calls tools to navigate the codebase.

```
Query → QueryClassifier (keyword or LLM) → 3-layer system prompt
  → AgentLoopService.run_stream()
    → LLM picks tools (8-12 of 24, dynamic per query type)
    → Tool execution (up to 25 iterations / 500K tokens)
    → BudgetController: NORMAL → WARN_CONVERGE (70%) → FORCE_CONCLUDE (90%)
    → EvidenceEvaluator: requires file:line refs + ≥2 tool calls + ≥1 file accessed
    → CompletenessCheck: verifies all query aspects addressed before finalizing
  → AgentResult (answer + context_chunks + budget_summary)
```

**24 code tools** (`code_tools/tools.py`): `grep`, `read_file`, `list_files`, `find_symbol`, `find_references`, `file_outline`, `get_dependencies`, `get_dependents`, `git_log`, `git_diff`, `ast_search`, `get_callees`, `get_callers`, `git_blame`, `git_show`, `find_tests`, `test_outline`, `trace_variable`, `compressed_view`, `module_summary`, `expand_symbol`, `run_test`.

Tools also accessible via `python -m app.code_tools <tool> <workspace> '<json_params>'` (used by extension local mode).

### Config-Driven Workflow Engine

`workflow/` module: two routing modes, config via YAML + Markdown agent files.

- `first_match` — classifier picks best route → run its pipeline (Code Explorer, 9 routes)
- `parallel_all_matching` — all matching routes run in parallel → post_pipeline (PR Review, 6 routes)

```
Input → ClassifierEngine.classify() → WorkflowEngine dispatches routes
  → explorer agents (AgentLoopService, up to 40 iter) or judge (single call)
  → context dict with _stage_results
```

**Workflow API** (`/api/workflows/`): `GET` list/detail/mermaid/graph, `PUT /{name}/models`.

### Code Review Pipeline (10 steps)

```
git diff → parse → classify risk → dynamic budget → impact graph
  → parallel specialized agents → dedup → adversarial verify
  → severity arbitration → rank → synthesis → ReviewResult
```

Endpoints: `POST /api/code-review/review` and `/review/stream` (SSE).

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
3. Runs cross-language parity tests (60+ tests across 13 dual-implementation tools)

```bash
make test-parity          # full validation (contract + shape + output comparison)
make update-contracts     # regenerate contracts after changing Python schemas
```

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

### Anthropic Core Principles

1. **Right Altitude** — Not too vague ("investigate the code"), not too prescriptive ("call get_callers on the gate method"). Target: "Trace the complete lifecycle from trigger to final outcome."
2. **Examples over rule lists** — 3-5 diverse examples teach behavior better than a laundry list of edge-case bullets. Wrap in `<example>` tags.
3. **Explain why, not just what** — Claude generalizes from motivation. "Output will be read by TTS, so avoid ellipses" beats "never use ellipses."
4. **Positive framing** — Say what to do, not what not to do.
5. **Context over instructions** — Provide workspace layout, project docs, detected project roots. Let the model decide the investigation path.
6. **Dial back aggressive language** — "Use this tool when..." not "CRITICAL: You MUST use this tool." Newer models overtrigger on forceful language.
7. **Minimal tool guidance** — If a human can't definitively say which tool to use, don't prescribe it.

### Multi-Agent Workflow Rules

8. **Role specialization** — Each workflow agent has a distinct perspective. NEVER add shared investigation strategies to `explorer_base.md` — this destroys role separation (proven by eval: 60% → 25% regression).
9. **Strategy = output format only** — Layer 2 strategies are for structured output templates (code_review). Don't inject investigation procedures for open-ended queries.

### Agent `.md` File Design (informed by prompts.chat patterns)

10. **One clear role sentence** — Open with what the agent IS and what it traces. prompts.chat's "I want you to act as..." pattern works because it's unambiguous. Our equivalent: "You are investigating from the [perspective] side. Your goal is to trace [scope]."
11. **Goal, not procedure** — Define WHAT to find (domain models, service implementations, completion effects), not HOW to find it (don't say "first grep, then read_file, then get_callers").
12. **Short** — Agent instructions should be 50-150 words. prompts.chat averages 80 words. If you need more, you're probably being too prescriptive.
13. **Consult the prompt library** — Before writing a new agent role, search `config/prompt-library/prompts.csv` for similar roles. Study how they define constraints and scope. Use `for_devs=TRUE` filter for developer-focused prompts.

### Validation

14. **Test with eval** — Any prompt change must be validated with `eval/agent_quality/run_bedrock.py`. Check both direct agent AND workflow mode — changes that help one can break the other.

## What's Next

See [ROADMAP.md](ROADMAP.md). Near-term priorities:
- Model B delegate authentication (no PAT required)
- Cross-session query patterns (learn from session traces)
- Persistent codebase memory (background file-summary indexer)
- Production hardening (Phase 6)
