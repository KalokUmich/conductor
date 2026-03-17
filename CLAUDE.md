# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Conductor is a VS Code collaboration extension with a FastAPI backend. The project has two main parts:

1. **`extension/`** - TypeScript VS Code extension
2. **`backend/`** - Python FastAPI server

## Commands

### Backend (Python/FastAPI)
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload      # development server
pytest                             # run all tests
pytest -k "test_repo_graph"       # repo graph tests
pytest -k "test_agent_loop"       # agent loop tests
pytest -k "test_code_tools"       # code tools tests
pytest --cov=. --cov-report=html   # coverage report
```

### Extension (TypeScript/VS Code)
```bash
cd extension
npm install
npm run compile                    # one-time build
npm run watch                      # watch mode
npm test                           # run extension tests
npm run lint                       # ESLint
vsce package                       # build .vsix
```

### Eval (Code Review Quality)
```bash
cd eval

# Run all 12 cases against requests repo
python run.py --provider anthropic --model claude-sonnet-4-20250514

# Run a single case (fast check)
python run.py --filter "requests-001"

# Deterministic scoring only (no LLM judge cost)
python run.py --no-judge

# Save results as baseline for regression detection
python run.py --save-baseline

# Use Bedrock provider
python run.py --provider bedrock --model us.anthropic.claude-sonnet-4-5-20250929-v1:0

# Use lighter model for sub-agents + parallel cases
python run.py --provider anthropic --explorer-model claude-haiku-4-5-20251001 --parallelism 3
```

## Architecture

### Backend Structure

```
backend/
├── app/
│   ├── main.py                      # FastAPI app, lifespan, router registration
│   ├── config.py                    # Settings + Secrets from YAML
│   ├── git_workspace/               # Git workspace management (Model A)
│   │   ├── service.py               # GitWorkspaceService
│   │   ├── delegate_broker.py       # DelegateBroker (Model B prep)
│   │   └── router.py                # /api/git-workspace/ endpoints
│   ├── agent_loop/                  # Agentic code intelligence (LLM + tools)
│   │   ├── service.py               # AgentLoopService — LLM loop, tool dispatch
│   │   ├── budget.py                # BudgetController — token-based budget management
│   │   ├── trace.py                 # SessionTrace — per-session JSON trace for offline analysis
│   │   ├── query_classifier.py      # QueryClassifier — keyword + optional LLM classification
│   │   ├── evidence.py              # EvidenceEvaluator — rule-based answer quality check
│   │   ├── prompts.py               # 3-layer system prompt (Core Identity + Strategy + Runtime)
│   │   └── router.py                # POST /api/context/query endpoint
│   ├── code_review/                 # Multi-agent PR review pipeline
│   │   ├── service.py               # CodeReviewService — orchestrates 10-step review pipeline
│   │   ├── agents.py                # Specialized review agents (parallel dispatch)
│   │   ├── models.py                # PRContext, ReviewFinding, ReviewResult, RiskProfile
│   │   ├── diff_parser.py           # Parse git diff into PRContext
│   │   ├── risk_classifier.py       # Risk classification across 5 dimensions
│   │   ├── ranking.py               # Score and rank findings
│   │   ├── dedup.py                 # Merge and deduplicate findings
│   │   └── router.py                # /api/code-review/ endpoints (+ SSE stream)
│   ├── code_tools/                  # 21 code intelligence tools
│   │   ├── schemas.py               # Pydantic models + TOOL_DEFINITIONS for LLM
│   │   ├── tools.py                 # Tool implementations (grep, AST, call graph, git, compressed view)
│   │   ├── output_policy.py         # Per-tool truncation policies (budget-adaptive)
│   │   └── router.py                # /api/code-tools/ direct access endpoints
│   ├── langextract/                 # LangExtract + multi-vendor Bedrock integration
│   │   ├── provider.py              # BedrockLanguageModel — all Bedrock vendors
│   │   ├── claude_provider.py       # Backwards-compat re-exports from provider.py
│   │   ├── catalog.py               # BedrockCatalog — dynamic model discovery
│   │   ├── service.py               # LangExtractService async wrapper
│   │   └── router.py                # GET /api/langextract/models endpoint
│   ├── ai_provider/                 # LLM provider abstraction
│   │   ├── base.py                  # AIProvider ABC + ToolCall/ToolUseResponse/TokenUsage
│   │   ├── claude_bedrock.py        # Bedrock Converse API (+ chat_with_tools)
│   │   ├── claude_direct.py         # Anthropic Messages API (+ chat_with_tools)
│   │   ├── openai_provider.py       # OpenAI Chat Completions (+ chat_with_tools)
│   │   └── resolver.py              # ProviderResolver — health checks, selection
│   └── repo_graph/                  # AST-based symbol extraction + dependency graph
│       ├── parser.py                # tree-sitter AST + regex fallback
│       ├── graph.py                 # networkx dependency graph + PageRank
│       └── service.py               # RepoMapService (map generation, caching)
├── config/
│   └── conductor.settings.yaml      # Non-secret settings template
├── requirements.txt
└── tests/
    ├── conftest.py                  # Centralized stubs (cocoindex, litellm, etc.)
    ├── test_code_tools.py           # 98 tests — all 21 code tools + dispatcher + multi-language
    ├── test_code_review.py          # Code review pipeline tests
    ├── test_agent_loop.py           # 39 tests — agent loop + message format + workspace layout + 3-layer prompt
    ├── test_query_classifier.py     # 26 tests — keyword + LLM classification, dynamic tool sets, filter_tools
    ├── test_compressed_tools.py     # 24 tests — compressed_view, module_summary, expand_symbol
    ├── test_budget_controller.py    # 20 tests — token budget signals, tracking, edge cases
    ├── test_session_trace.py        # 15 tests — SessionTrace, IterationTrace, save/load
    ├── test_evidence.py             # 14 tests — evidence evaluator (file refs, tool calls, budget checks)
    ├── test_symbol_role.py          # 24 tests — symbol role classification + sorting + decorator detection
    ├── test_output_policy.py        # 19 tests — per-tool truncation, budget adaptation
    ├── test_langextract.py          # 57 tests — Bedrock provider, catalog, service, router
    ├── test_repo_graph.py           # 72 tests — parser + graph + service
    ├── test_config_new.py           # 27 tests — config + secrets (RAG remnants removed)
    └── test_git_workspace.py        # Git workspace lifecycle
```

### Extension Structure

```
extension/src/
├── extension.ts               # Entry point, command registration
├── panels/
│   ├── collabPanel.ts         # Main WebView panel
│   └── workspacePanel.ts      # 5-step workspace creation wizard
├── services/
│   ├── sessionFSM.ts          # Session state machine
│   ├── webSocketService.ts    # WebSocket client
│   ├── fileSystemProvider.ts  # conductor:// URI scheme
│   ├── workspaceClient.ts     # /workspace/ HTTP client
│   └── fileUploadService.ts   # Upload/download proxy
└── commands/
    └── index.ts               # VS Code command handlers
```

### Eval Structure

```
eval/                              # Standalone — excluded from Docker via .dockerignore
├── run.py                         # CLI entrypoint (--filter, --no-judge, --save-baseline, --provider, --model, --parallelism)
├── runner.py                      # Workspace setup (copytree → git init → git apply → git commit) + CodeReviewService execution
├── scorer.py                      # Deterministic scoring: recall, precision, severity, location, recommendation, context
├── judge.py                       # LLM-as-Judge: completeness, reasoning quality, actionability, false positive quality (1-5)
├── report.py                      # Report generation + baseline comparison + regression detection (10% threshold)
├── repos.yaml                     # Repo manifest (name → source_dir, version, language)
├── repos/                         # Plain source trees (no .git) — runner creates temp git repos
│   └── requests/                  # requests v2.31.0 (5.2 MB)
├── cases/
│   └── requests/
│       ├── cases.yaml             # 12 case definitions with ground truth (title_pattern, file_pattern, line_range, severity)
│       └── patches/               # 12 .patch files (4 easy, 5 medium, 3 hard)
│           ├── 001-missing-timeout.patch
│           ├── ...
│           └── 012-response-hook-swallowed.patch
└── baselines/                     # Timestamped JSON baselines for regression detection
```

#### Adding a New Repo

1. Clone at a specific version and remove `.git`:
   ```bash
   git clone --depth 1 --branch v1.0.0 https://github.com/org/repo.git eval/repos/repo
   rm -rf eval/repos/repo/.git
   ```
2. Add entry to `eval/repos.yaml`
3. Create `eval/cases/repo/cases.yaml` and `eval/cases/repo/patches/`

#### Adding a New Case

1. Create a patch against the source tree:
   ```bash
   cp -r eval/repos/requests /tmp/repo-work && cd /tmp/repo-work
   git init && git add -A && git commit -m "base"
   # Make buggy changes...
   git diff > eval/cases/requests/patches/NNN-description.patch
   ```
2. Add case definition to `cases.yaml` with `id`, `patch`, `difficulty`, `title`, `description`, and `expected_findings` (pattern-based ground truth)

#### Scoring Rubric

| Dimension | Weight | What It Measures |
|-----------|--------|-----------------|
| Recall | 35% | Fraction of planted bugs found |
| Precision | 20% | Fraction of findings that are true positives |
| Severity | 15% | Correct severity assignment |
| Location | 10% | Correct file + line range |
| Recommendation | 10% | Fix suggestion matches expected |
| Context | 10% | Cross-file exploration completed |

### Agentic Code Intelligence Architecture

The code context system uses an **LLM agent loop** instead of a traditional RAG pipeline.
The agent iteratively calls code tools to navigate the codebase and answer questions.

```
User query ("How does auth work?")
       ↓
QueryClassifier (keyword or LLM-based)
  → query_type, strategy hint, dynamic tool_set
       ↓
3-Layer System Prompt:
  L1: Core Identity (always) — hard constraints, exploration pattern
  L2: Strategy (per query type) — e.g. "Business Flow Tracing"
  L3: Runtime Guidance (dynamic) — budget, scatter, convergence
       ↓
AgentLoopService.run_stream(query, workspace_path)
       ↓
  ┌─────────────────────────────────────┐
  │ LLM decides which tools to call     │
  │ (via chat_with_tools)               │
  │ Tools: dynamic subset (8-12 of 21)  │
  │   ↓                                 │
  │ Tool execution (grep, read_file,    │ ← up to 25 iterations
  │   compressed_view, etc.)            │   or 500K input tokens
  │   ↓                                 │
  │ BudgetController.track(usage)       │ ← token tracking
  │   → NORMAL / WARN / FORCE_CONCLUDE  │
  │   ↓                                 │
  │ Results + budget context → LLM      │
  └─────────────────────────────────────┘
       ↓
AgentResult (answer + context_chunks + budget_summary)
```

**21 code tools** in `code_tools/tools.py`:

| Tool | Description |
|------|-------------|
| `grep` | Regex search across files (excludes .git, node_modules, etc.) |
| `read_file` | Read file contents with optional line ranges |
| `list_files` | List directory tree with depth/glob filters |
| `find_symbol` | AST-based symbol definition search (tree-sitter) |
| `find_references` | Find symbol usages (grep + AST validation) |
| `file_outline` | Get all definitions in a file with line numbers |
| `get_dependencies` | Files this file imports (dependency graph) |
| `get_dependents` | Files that import this file (reverse dependencies) |
| `git_log` | Recent commits, optionally per-file |
| `git_diff` | Diff between two git refs |
| `ast_search` | Structural AST search via ast-grep (`$VAR`, `$$$MULTI` patterns) |
| `get_callees` | Functions/methods called within a specific function body |
| `get_callers` | Functions/methods that call a given function (cross-file) |
| `git_blame` | Per-line authorship with commit hash, author, date |
| `git_show` | Full commit details (message + diff) |
| `find_tests` | Find test functions covering a given function/class |
| `test_outline` | Test file structure with mocks, assertions, fixtures |
| `trace_variable` | Data flow tracing: aliases, arg→param mapping, sink/source detection |
| `compressed_view` | File signatures + call relationships + side effects (~80% token savings) |
| `module_summary` | Module-level summary: services, models, functions, file list (~95% savings) |
| `expand_symbol` | Expand a symbol from compressed view to full source code |

**AI Provider `chat_with_tools()`** — implemented in all 3 providers:
- `ClaudeBedrockProvider` — Bedrock Converse API `toolConfig`
- `ClaudeDirectProvider` — Anthropic Messages API `tool_use`
- `OpenAIProvider` — OpenAI Chat Completions `tools` API

### RepoMap / Symbol Extraction

The `repo_graph/` module provides AST-based symbol extraction used by code tools:

1. **Parser** (`parser.py`): Extract symbol definitions and references using tree-sitter AST (with regex fallback)
2. **Graph** (`graph.py`): Directed dependency graph (file A → file B). Uses networkx + PageRank
3. **Service** (`service.py`): RepoMapService for graph building and caching

### Model A Architecture (Current)

```
User provides PAT
       ↓
Extension sends token + repo URL to backend
       ↓
Backend creates bare repo clone with GIT_ASKPASS
       ↓
Backend creates worktree at worktrees/{room_id}/
       ↓
FileSystemProvider mounts conductor://{room_id}/ in VS Code
```

### Code Review Pipeline

The `code_review/` module implements a multi-agent PR review system with a 10-step pipeline:

```
Git diff (main...feature)
       ↓
1. Parse diff → PRContext (files, additions, deletions, categories)
2. Classify risk (5 dimensions)
3. Compute dynamic budget based on PR size
4. Impact graph injection — query callers/dependents of changed files
5. Dispatch specialized review agents (parallel, lightweight model)
6. Merge and dedup findings
7. Adversarial verification — try to disprove each finding
8. Severity arbitration — strong model reviews severity labels
9. Score and rank findings
10. Synthesis pass — strong model produces final polished review
       ↓
ReviewResult (findings + risk profile + summary)
```

Agents reuse `AgentLoopService` from `agent_loop/` with focused prompts and tool budgets. Endpoints: `POST /api/code-review/review` and `POST /api/code-review/review/stream` (SSE).

## Key Patterns

### Agent Loop Pattern
```python
from app.agent_loop.service import AgentLoopService
from app.agent_loop.budget import BudgetConfig

agent = AgentLoopService(
    provider=ai_provider,
    max_iterations=25,
    budget_config=BudgetConfig(max_input_tokens=500_000),
    classifier_provider=haiku_provider,  # optional: LLM pre-classification
    use_llm_classifier=True,            # enable LLM classification (default: keyword)
)
result = await agent.run(query="How does auth work?", workspace_path="/path/to/ws")
# result.answer — LLM's final answer
# result.context_chunks — code read during the loop
# result.tool_calls_made — total tools invoked
# result.budget_summary — token usage breakdown
```

### Code Tools Pattern
```python
from app.code_tools.tools import execute_tool

result = execute_tool("grep", workspace="/path/to/ws", params={"pattern": "authenticate"})
# result.success, result.data, result.error
```

### chat_with_tools Pattern
```python
# All 3 providers (Bedrock, Direct, OpenAI) implement chat_with_tools
response = provider.chat_with_tools(
    messages=[{"role": "user", "content": [{"text": "Find auth code"}]}],
    tools=TOOL_DEFINITIONS,  # from code_tools.schemas
    system="You are a code assistant.",
)
# response.text — model's text output
# response.tool_calls — List[ToolCall] with id, name, input
# response.stop_reason — "end_turn", "tool_use", "max_tokens"
# response.usage — TokenUsage(input_tokens, output_tokens, total_tokens, cache_read/write)
```

### LangExtract Pattern
```python
from app.langextract.service import LangExtractService
from app.langextract.catalog import BedrockCatalog
from langextract.data import ExampleData, Extraction

# Optional: attach a catalog for model discovery + inference profile resolution
catalog = BedrockCatalog(region="eu-west-2")
catalog.refresh()

svc = LangExtractService(
    model_id="claude-sonnet-4-20250514",  # or any Bedrock model ID
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
            Extraction(extraction_class="Action", extraction_text="fix the bug"),
        ],
    )],
)
# result.success, result.documents, result.error

# List available models grouped by vendor
models_by_vendor = svc.list_available_models()  # {"Anthropic": [...], "Amazon": [...]}
```

### Config Pattern
```python
from app.config import load_settings

settings = load_settings()                  # loads YAML files (settings + secrets)
# settings.code_search.repo_map_enabled    # RepoMap config
# settings.secrets.jwt                     # JWT auth secrets
```

## Testing Notes

- Backend tests use `pytest` with mocked external dependencies
- Centralized stubs in `conftest.py` for cocoindex, litellm, sentence_transformers, sqlite_vec
- **Code tools tests** use real filesystem operations (tmp_path fixtures)
- **Agent loop tests** use `MockProvider` subclass of `AIProvider` with scripted responses
- RepoMap tests use real filesystem operations for parser/graph tests
- tree-sitter and networkx are mocked in import stubs
- Config tests verify env var injection via `setdefault()` for all credential types
- **LangExtract tests** mock Bedrock/Anthropic API calls and `lx.extract()`
- ast-grep tests require `ast-grep-cli` installed in the venv
- Run new tests: `pytest tests/test_code_tools.py tests/test_agent_loop.py tests/test_budget_controller.py tests/test_langextract.py -v`

## Environment Variables

```bash
# Backend
BACKEND_HOST=0.0.0.0
BACKEND_PORT=8000
GIT_WORKSPACE_ROOT=/tmp/conductor_workspaces

# AI Provider Credentials (configured in conductor.secrets.yaml)
AWS_ACCESS_KEY_ID=...            # Bedrock provider
AWS_SECRET_ACCESS_KEY=...        # Bedrock provider
AWS_DEFAULT_REGION=us-east-1     # Bedrock provider
OPENAI_API_KEY=sk-...            # OpenAI provider

# Extension (VS Code settings)
conductor.backendUrl=http://localhost:8000
conductor.enableWorkspace=true
```

## Recent Changes

- **Code Review Eval System** — `eval/` standalone eval suite for measuring `CodeReviewService` quality against planted bugs in real repos. 12 cases against requests v2.31.0 (4 easy, 5 medium, 3 hard) covering timeout, auth leak, SSL bypass, redirect loop, etc. Deterministic scorer (6 dimensions, weighted composite) + LLM-as-Judge (4 criteria, 1-5). Baseline save/load with 10% regression detection. CLI: `python eval/run.py --provider anthropic --filter "requests-001" --no-judge --save-baseline`.
- **Multi-Agent Code Review** — `code_review/` module implements a 10-step PR review pipeline: diff parsing → risk classification → dynamic budget → impact graph → parallel specialized agents → dedup → adversarial verification → severity arbitration → ranking → synthesis. Reuses `AgentLoopService` with per-agent prompts and budgets. Endpoints at `/api/code-review/review` (+ SSE stream).
- **Evidence Evaluator** — `evidence.py` in `agent_loop/` checks answer quality before finalizing: requires file:line refs or code blocks, ≥2 tool calls, ≥1 file accessed. If evidence insufficient and budget remains, rejects the answer and forces the LLM to investigate further. 14 tests.
- **Symbol Role Classification** — `find_symbol` results now include a `role` field (route_entry, business_logic, domain_model, infrastructure, utility, test, unknown) and are sorted by role priority. Classification uses 3 tiers: decorator/annotation context (reads lines above symbol), file path patterns, name patterns. 24 tests.
- **Session Trace** — `SessionTrace` in `agent_loop/trace.py` records per-iteration metrics (LLM latency, tool latencies, token breakdown, budget signals) as structured JSON. Saved to `{trace_dir}/{session_id}.json` for offline analysis. Opt-in via `trace_dir` on `AgentLoopService`. 15 tests.
- **Tool Output Policy** — `output_policy.py` in `code_tools/` replaces uniform 30KB hard cutoff with per-tool truncation policies. Search tools truncate by result count, read_file by line boundaries, git tools with generous char limits. Budget-adaptive: limits shrink 50% when remaining tokens < 100K. 19 tests.
- **Config Cleanup** — removed RAG remnants: `EmbeddingSecrets`, `VoyageSecrets`, `MistralSecrets`, `CohereSecrets`, `AwsSecrets`, `OpenAISecrets`, `_inject_embedding_env_vars()`. Cleaned `CodeSearchSettings` to only `repo_map_enabled`/`repo_map_top_n`. Removed RAG router from `main.py`.
- **Token-Based Budget Controller** — `BudgetController` in `agent_loop/budget.py` replaces iteration-only budget management with token tracking. Tracks cumulative input/output tokens per session via `TokenUsage` extracted from all 3 providers (Bedrock Converse, Anthropic Messages, OpenAI). Three signals: NORMAL → WARN_CONVERGE (70% threshold or diminishing returns) → FORCE_CONCLUDE (90% or max iterations). LLM sees token budget context each turn. `budget_summary` dict in `AgentResult` for downstream analysis. 20 tests.
- **TokenUsage on ToolUseResponse** — `TokenUsage` dataclass added to `ai_provider/base.py`. All 3 providers now extract `input_tokens`, `output_tokens`, `total_tokens`, and cache token counts from API responses and attach to `ToolUseResponse.usage`.
- **Agentic Code Intelligence** — replaced RAG pipeline (CocoIndex + embeddings + reranking) with LLM agent loop + 21 code tools. The agent iteratively navigates code to answer questions.
- **3-Layer System Prompt** — `prompts.py` restructured from monolithic ~7500-token prompt into 3 layers: Core Identity (~100 lines, always included), Strategy (~30 lines, selected by query type), Runtime Guidance (dynamic budget/scatter). ~4000 tokens per call.
- **Query Classifier** — `query_classifier.py` classifies into 7 types with keyword matching (default) or optional LLM pre-classification (Haiku). Configurable via `classifier.use_llm` / `classifier.model_id` in settings YAML. Each type defines a dynamic tool_set (8-12 of 21 tools). 26 tests.
- **Dynamic Tool Set** — LLM only sees tools relevant to query type (e.g. root_cause gets git tools, architecture gets module_summary). Reduces hallucinated tool calls and token waste. `filter_tools()` helper in schemas.py.
- **Accumulated Text Trimming** — agent loop keeps only last 3 thinking turns to prevent context window bloat from intermediate reasoning.
- **Budget Hard Constraints** — WARN_CONVERGE now refuses new broad searches (grep, find_symbol) and only allows verification calls (expand_symbol, read_file with line ranges).
- **Compressed View Tools** — 3 new tools for token-efficient code navigation:
  - `compressed_view` — file signatures + call relationships + side effects + raises (~80% savings vs read_file). Rich symbol extraction including class methods. Multi-language.
  - `module_summary` — module-level summary: services, models, controllers, functions, imports, file list (~95% savings). Classifies symbols by role.
  - `expand_symbol` — expand a symbol from compressed view to full source. Workspace-wide search with substring matching.
  - 24 tests in `test_compressed_tools.py`.
- **Language Support Hardening** — `find_tests` and `test_outline` now support Java (JUnit/Mockito), Go (testing.T/testify), Rust (#[test]), and C/C++. 10 new language-specific tests.
- **Code Tools** (`code_tools/`) — 21 tool implementations including data flow tracing, git semantic analysis, test association, ast-grep structural search, function-level call graph, and compressed view tools
- **Data Flow Tracing** (`trace_variable`) — tracks a variable across function boundaries: alias detection (transitive), argument→parameter mapping via callee resolution, sink patterns (ORM `.filter()`, SQL `execute()`, JPA `findBy*()`, HTTP body, return), source patterns (HTTP request, annotations, config, DB result). Agent chains hops to trace e.g. `loan_id` from HTTP request to SQL WHERE clause.
- **Git Semantic Tools** — `git_blame` (per-line authorship) + `git_show` (full commit details with diff) for understanding why code was written
- **Test Association Tools** — `find_tests` (find tests for a function/class) + `test_outline` (test structure with mocks, assertions, fixtures)
- **Workspace Reconnaissance** — auto-scan workspace directory layout + detect project markers (pom.xml, package.json, go.mod, etc.) before first LLM call, so agent knows project structure from iteration 1
- **ast-grep Integration** — structural AST search via ast-grep CLI, supports pattern variables (`$VAR`, `$$$MULTI`), auto-detects language from file extension, meta-variable extraction
- **Function-Level Call Graph** — `get_callees` finds what a function calls; `get_callers` finds who calls a function. Works with tree-sitter AST and regex fallback.
- **Multi-Language Parser Fallback** — regex-based symbol extraction for Java, Go, Rust, C, C++ when tree-sitter is unavailable
- **LangExtract Multi-Vendor Bedrock** (`langextract/`) — `BedrockLanguageModel` provider supports ALL Bedrock models (Claude, Amazon Nova, Llama, Mistral, DeepSeek, Qwen, etc.) via the unified Converse API. `BedrockCatalog` dynamically discovers available models at startup via `list_foundation_models()` + `list_inference_profiles()`, handles `eu.` inference profiles for cross-region models, groups by vendor. `GET /api/langextract/models` endpoint for UI model selection. Backwards-compatible `ClaudeLanguageModel` alias preserved.
- **Agent Loop** (`agent_loop/`) — `AgentLoopService` drives the LLM loop, dispatches tool calls, collects context chunks; accumulated-text fallback for empty answers
- **SSE Streaming** — real-time progress for both `/query/stream` and `/explain-rich/stream` with live tool call progress in the WebView
- **Collapsible AI Explanations** — explanation cards in chat can be collapsed/expanded
- **`chat_with_tools()`** — added to all 3 AI providers (Bedrock Converse, Anthropic Messages, OpenAI Chat Completions) for native tool use
- **`POST /api/context/query`** — new endpoint replacing the old hybrid retrieval context endpoint
- **RepoMap** — tree-sitter + networkx graph + PageRank (still used by find_symbol, file_outline, dependency tools)
- 900+ test cases across code tools, agent loop, budget controller, session trace, output policy, query classifier, compressed tools, repo graph, config, and langextract

## What's Next

See [ROADMAP.md](ROADMAP.md) for planned features. Current focus:
- Precise static taint analysis (Phase C — long-term R&D, see ROADMAP 5.5.4)
- Model B delegate authentication
- Conflict resolution for concurrent edits
- Enterprise features (room access control, audit export)
