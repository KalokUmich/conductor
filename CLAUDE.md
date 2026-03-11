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
uvicorn main:app --reload          # development server
pytest                             # run all tests
pytest -k "test_embedding"        # embedding provider tests
pytest -k "test_repo_graph"       # repo graph tests
pytest -k "test_rerank"           # reranking provider tests
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

## Architecture

### Backend Structure

```
backend/
├── app/
│   ├── main.py                      # FastAPI app, lifespan, router registration
│   ├── config.py                    # Settings + Secrets from YAML, env injection
│   ├── git_workspace/               # Git workspace management (Model A)
│   │   ├── service.py               # GitWorkspaceService
│   │   ├── delegate_broker.py       # DelegateBroker (Model B prep)
│   │   └── router.py                # /api/git-workspace/ endpoints
│   ├── agent_loop/                  # Agentic code intelligence (LLM + tools)
│   │   ├── service.py               # AgentLoopService — LLM loop, tool dispatch
│   │   ├── prompts.py               # System prompt for code navigation agent
│   │   └── router.py                # POST /api/context/query endpoint
│   ├── code_tools/                  # 13 code intelligence tools
│   │   ├── schemas.py               # Pydantic models + TOOL_DEFINITIONS for LLM
│   │   ├── tools.py                 # Tool implementations (grep, AST, call graph, git)
│   │   └── router.py                # /api/code-tools/ direct access endpoints
│   ├── langextract/                 # LangExtract + multi-vendor Bedrock integration
│   │   ├── provider.py              # BedrockLanguageModel — all Bedrock vendors
│   │   ├── claude_provider.py       # Backwards-compat re-exports from provider.py
│   │   ├── catalog.py               # BedrockCatalog — dynamic model discovery
│   │   ├── service.py               # LangExtractService async wrapper
│   │   └── router.py                # GET /api/langextract/models endpoint
│   ├── ai_provider/                 # LLM provider abstraction
│   │   ├── base.py                  # AIProvider ABC + ToolCall/ToolUseResponse
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
    ├── test_code_tools.py           # 67 tests — all 18 code tools + dispatcher
    ├── test_agent_loop.py           # 32 tests — agent loop + message format + workspace layout
    ├── test_langextract.py          # 57 tests — Bedrock provider, catalog, service, router
    ├── test_repo_graph.py           # 72 tests — parser + graph + service
    ├── test_config_new.py           # 60+ tests — config + secrets + env vars
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

### Agentic Code Intelligence Architecture

The code context system uses an **LLM agent loop** instead of a traditional RAG pipeline.
The agent iteratively calls code tools to navigate the codebase and answer questions.

```
User query ("How does auth work?")
       ↓
AgentLoopService.run(query, workspace_path)
       ↓
  ┌─────────────────────────────────────┐
  │ LLM decides which tools to call     │
  │ (via chat_with_tools)               │
  │   ↓                                 │
  │ Tool execution (grep, read_file,    │ ← up to 15 iterations
  │   find_symbol, etc.)                │
  │   ↓                                 │
  │ Results fed back to LLM             │
  └─────────────────────────────────────┘
       ↓
AgentResult (answer + context_chunks)
```

**18 code tools** in `code_tools/tools.py`:

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

## Key Patterns

### Agent Loop Pattern
```python
from app.agent_loop.service import AgentLoopService

agent = AgentLoopService(provider=ai_provider, max_iterations=15)
result = await agent.run(query="How does auth work?", workspace_path="/path/to/ws")
# result.answer — LLM's final answer
# result.context_chunks — code read during the loop
# result.tool_calls_made — total tools invoked
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
from app.config import load_settings, _inject_embedding_env_vars

settings = load_settings()                  # loads YAML files
_inject_embedding_env_vars(settings)        # pushes ALL available secrets → env vars
# Uses os.environ.setdefault() — never overwrites existing env vars
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
- Run new tests: `pytest tests/test_code_tools.py tests/test_agent_loop.py tests/test_langextract.py -v`

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

- **Agentic Code Intelligence** — replaced RAG pipeline (CocoIndex + embeddings + reranking) with LLM agent loop + 18 code tools. The agent iteratively navigates code to answer questions.
- **Code Tools** (`code_tools/`) — 18 tool implementations including data flow tracing, git semantic analysis, test association, ast-grep structural search, and function-level call graph
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
- 200+ test cases across code tools, agent loop, repo graph, and langextract

## What's Next

See [ROADMAP.md](ROADMAP.md) for planned features. Current focus:
- Precise static taint analysis (Phase C — long-term R&D, see ROADMAP 5.5.4)
- Model B delegate authentication
- Conflict resolution for concurrent edits
- Enterprise features (room access control, audit export)
