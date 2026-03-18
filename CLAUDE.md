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
make run-backend    # start backend (dev mode, auto-reload)
make test           # run all tests (backend + extension)
make package        # compile and package extension as .vsix
make langfuse-up    # start self-hosted Langfuse (port 3001)
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

### Eval (Code Review Quality)
```bash
cd eval
python run.py --provider anthropic --model claude-sonnet-4-20250514
python run.py --filter "requests-001" --no-judge   # fast single case
python run.py --save-baseline                       # save regression baseline
python run.py --gold --gold-model opus --save-baseline  # gold-standard ceiling
python run.py --provider anthropic --compare-gold   # compare vs gold
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
│   └── router.py            # /api/code-tools/ endpoints
├── langextract/             # LangExtract + multi-vendor Bedrock integration
├── ai_provider/             # LLM provider abstraction (Bedrock, Direct, OpenAI)
│   ├── base.py              # AIProvider ABC + ToolCall/ToolUseResponse/TokenUsage
│   ├── claude_bedrock.py    # Bedrock Converse API
│   ├── claude_direct.py     # Anthropic Messages API
│   ├── openai_provider.py   # OpenAI Chat Completions
│   └── resolver.py          # ProviderResolver — health checks, selection
├── repo_graph/              # AST-based symbol extraction + dependency graph
└── git_workspace/           # Git workspace management (Model A)

config/
├── conductor.settings.yaml  # Non-sensitive settings (committed)
├── conductor.secrets.yaml   # Secrets (gitignored)
├── workflows/               # pr_review.yaml (parallel_all_matching), code_explorer.yaml (first_match)
├── agents/                  # 17 agent .md files (YAML frontmatter + Markdown body)
└── prompts/                 # review_base.md, explorer_base.md (shared templates)

tests/
├── conftest.py              # Centralized stubs (cocoindex, litellm, etc.)
├── test_code_tools.py       # 98 tests — 24 tools + dispatcher + multi-language
├── test_agent_loop.py       # 39 tests — agent loop + 3-layer prompt
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
├── extension.ts             # Entry point, command registration
├── panels/                  # collabPanel.ts, workspacePanel.ts
├── services/
│   ├── conductorStateMachine.ts   # FSM: Idle → ReadyToHost → Hosting → Joined
│   ├── conductorController.ts     # FSM driver
│   ├── workflowPanel.ts           # Workflow visualization WebView (singleton)
│   ├── workspaceClient.ts         # /workspace/ HTTP client
│   ├── workspaceIndexer.ts        # AST symbol extraction
│   └── conductorFileSystemProvider.ts  # conductor:// URI scheme
└── commands/index.ts

extension/media/
├── chat.html      # Main WebView — @AI /ask /pr slash commands, Workflows tab
└── workflow.html  # Workflow visualization — SVG graph + agent detail panel
```

### Agentic Code Intelligence

Active agent loop (not RAG). Agent iteratively calls tools to navigate the codebase.

```
Query → QueryClassifier (keyword or LLM) → 3-layer system prompt
  → AgentLoopService.run_stream()
    → LLM picks tools (8-12 of 24, dynamic per query type)
    → Tool execution (up to 25 iterations / 500K tokens)
    → BudgetController: NORMAL → WARN_CONVERGE (70%) → FORCE_CONCLUDE (90%)
  → AgentResult (answer + context_chunks + budget_summary)
```

**24 code tools** (`code_tools/tools.py`): `grep`, `read_file`, `list_files`, `find_symbol`, `find_references`, `file_outline`, `get_dependencies`, `get_dependents`, `git_log`, `git_diff`, `ast_search`, `get_callees`, `get_callers`, `git_blame`, `git_show`, `find_tests`, `test_outline`, `trace_variable`, `compressed_view`, `module_summary`, `expand_symbol`, `run_test`.

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

## Eval System

`eval/` — standalone quality measurement for `CodeReviewService`. 12 planted-bug cases against requests v2.31.0. Scoring: recall (35%), precision (20%), severity (15%), location (10%), recommendation (10%), context (10%).

To add a new case: create a patch in `eval/cases/{repo}/patches/`, add entry to `cases.yaml` with `expected_findings` (pattern-based ground truth). See `eval/` for details.

## What's Next

See [ROADMAP.md](ROADMAP.md). Near-term priorities:
- Model B delegate authentication (no PAT required)
- Cross-session query patterns (learn from session traces)
- Persistent codebase memory (background file-summary indexer)
- Production hardening (Phase 6)
