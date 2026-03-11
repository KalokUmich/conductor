# Conductor Project Roadmap

Last updated: 2026-03-11

## Current State

Conductor is a VS Code collaboration extension with a FastAPI backend. The project currently has working implementations of:

- Real-time WebSocket chat (with reconnect, typing indicators, read receipts)
- File upload/download (20MB limit, dedup, retry)
- Code snippet sharing + editor navigation
- Change review workflow (MockAgent, policy check, diff preview, audit log)
- AI provider workflow (health check, provider selection, streaming inference)
- **Git Workspace Management (Model A)**:
  - Per-room bare repo + worktree isolation
  - GIT_ASKPASS token authentication
  - FileSystemProvider (`conductor://` URI scheme)
  - WorkspacePanel 5-step creation wizard
  - WorkspaceClient typed HTTP client
  - Workspace code search (`GET /workspace/{room_id}/search`)
- **Agentic Code Intelligence**:
  - `AgentLoopService` — LLM-driven iterative tool loop (up to 15 iterations)
  - 18 code tools: `grep`, `read_file`, `list_files`, `find_symbol`, `find_references`, `file_outline`, `get_dependencies`, `get_dependents`, `git_log`, `git_diff`, `ast_search`, `get_callees`, `get_callers`, `git_blame`, `git_show`, `find_tests`, `test_outline`, `trace_variable`
  - `trace_variable` — data flow tracing with alias detection, argument→parameter mapping, sink/source patterns
  - Workspace reconnaissance — auto-scan project layout + project marker detection
  - `chat_with_tools()` on all 3 AI providers (Bedrock Converse, Anthropic Messages, OpenAI)
  - `POST /api/context/query` — general code Q&A with agentic loop
  - `POST /api/context/explain-rich` — deep code explanation via agent (replaces XML-prompt pipeline)
  - SSE streaming (`POST /api/context/query/stream`, `/explain-rich/stream`) for real-time progress
- **RepoMap (Graph-based Symbol Index)**:
  - tree-sitter AST parsing for symbol extraction (used by `find_symbol`, `file_outline`, dependency tools)
  - File dependency graph (networkx) with PageRank ranking

## Phase 1: Foundation (COMPLETE)

### 1.1 VS Code Extension Scaffold
- [x] WebView panel with FSM lifecycle
- [x] WebSocket service with reconnect
- [x] Basic chat UI (send/receive messages)
- [x] TypeScript compilation + ESLint
- [x] VS Code command registration

### 1.2 FastAPI Backend Scaffold
- [x] FastAPI app with CORS middleware
- [x] WebSocket endpoint (`/ws/{room_id}`)
- [x] REST chat history endpoint
- [x] Pydantic models for request/response
- [x] pytest test suite

## Phase 2: Collaboration Features (COMPLETE)

### 2.1 Enhanced Chat
- [x] Reconnect with `since` parameter
- [x] Typing indicators (WebSocket broadcast)
- [x] Read receipts
- [x] Message deduplication (client-side UUID)
- [x] Paginated history (`GET /chat/{room_id}/history`)

### 2.2 File Sharing
- [x] File upload endpoint (`POST /files/upload`)
- [x] File download endpoint (`GET /files/{file_id}`)
- [x] 20MB size limit enforcement
- [x] Duplicate detection (SHA-256 hash)
- [x] Extension-host upload proxy
- [x] Retry logic (3 attempts with backoff)

### 2.3 Code Snippet Sharing
- [x] Snippet upload with language metadata
- [x] Editor navigation (open file at line)
- [x] Syntax highlighting in WebView

## Phase 3: AI & Change Workflows (COMPLETE)

### 3.1 Change Review Workflow
- [x] MockAgent for generating changes (`POST /generate-changes`)
- [x] Policy evaluation (`POST /policy/evaluate-auto-apply`)
- [x] Per-change diff preview
- [x] Sequential apply/skip UI
- [x] Audit logging (`POST /audit/log-apply`)

### 3.2 AI Provider Integration
- [x] Provider health/status endpoint (`GET /ai/status`)
- [x] Four-step provider selection UI
- [x] Streaming inference (`POST /ai/infer`)
- [x] Mock provider for testing

## Phase 4: Git Workspace Management (COMPLETE)

### 4.1 Model A: Token Authentication
- [x] Backend: bare repo clone with GIT_ASKPASS
- [x] Backend: worktree creation per room (`session/{room_id}` branch)
- [x] Backend: file CRUD endpoints (`/workspace/{room_id}/file`)
- [x] Backend: commit + push endpoint
- [x] Extension: WorkspaceClient typed HTTP client
- [x] Extension: WorkspacePanel 5-step creation wizard
- [x] Extension: FSM `CreatingWorkspace` state
- [x] Extension: FileSystemProvider (`conductor://` URI scheme)

### 4.2 Workspace Code Search
- [x] Backend: `GET /workspace/{room_id}/search?q=...` full-text search
- [x] Extension: `WorkspaceClient.searchCode()` method
- [x] Extension: inline search panel in WebView (`Ctrl+Shift+F`)
- [x] Tests: search endpoint + client method coverage

## Phase 4.5: Graph-Based Symbol Index (COMPLETE)

### 4.5.3 RepoMap Graph-Based Context
- [x] `repo_graph/parser.py` — tree-sitter AST + regex fallback
- [x] `repo_graph/graph.py` — networkx dependency graph + PageRank
- [x] `repo_graph/service.py` — RepoMapService with caching
- [x] Powers `find_symbol`, `file_outline`, `get_dependencies`, `get_dependents` tools
- [x] Comprehensive tests (72 test cases)

## Phase 4.6: Agentic Code Intelligence (COMPLETE)

### 4.6.1 Agent Loop + 18 Code Tools
- [x] `AgentLoopService` — iterative LLM tool loop (configurable max iterations)
- [x] `AgentResult` — answer, context_chunks, tool_calls_made, iterations, duration_ms
- [x] SSE streaming via `run_stream()` with typed `AgentEvent` objects
- [x] `grep` — regex search across files (excludes .git, node_modules, etc.)
- [x] `read_file` — file contents with optional line ranges
- [x] `list_files` — directory tree with depth/glob filters
- [x] `find_symbol` — AST-based symbol definition search (tree-sitter)
- [x] `find_references` — symbol usages (grep + AST validation)
- [x] `file_outline` — all definitions in a file with line numbers
- [x] `get_dependencies` — files this file imports (dependency graph)
- [x] `get_dependents` — files that import this file (reverse dependencies)
- [x] `git_log` — recent commits, optionally per-file
- [x] `git_diff` — diff between two git refs
- [x] `ast_search` — structural AST search via ast-grep (`$VAR`, `$$$MULTI` patterns)
- [x] `get_callees` — functions called within a specific function body
- [x] `get_callers` — functions that call a given function (cross-file)
- [x] `git_blame` — per-line authorship with commit hash, author, date
- [x] `git_show` — full commit details (message + diff)
- [x] `find_tests` — find test functions covering a given function/class
- [x] `test_outline` — test file structure with mocks, assertions, fixtures
- [x] `trace_variable` — data flow tracing: aliases, argument→parameter mapping, sink/source detection
- [x] Workspace reconnaissance — auto-scan project layout + project marker detection in system prompt
- [x] Comprehensive tests (67 code tools + 32 agent loop test cases)

### 4.6.2 Tool-Use API for All Providers
- [x] `chat_with_tools()` on `ClaudeBedrockProvider` (Bedrock Converse `toolConfig`)
- [x] `chat_with_tools()` on `ClaudeDirectProvider` (Anthropic Messages `tool_use`)
- [x] `chat_with_tools()` on `OpenAIProvider` (OpenAI Chat Completions `tools`)
- [x] Unified `ToolCall` / `ToolUseResponse` types across all providers

### 4.6.3 Context + Explanation Endpoints
- [x] `POST /api/context/query` — general code Q&A with agentic loop
- [x] `POST /api/context/query/stream` — SSE streaming with real-time progress events
- [x] `POST /api/context/explain-rich` — deep code snippet explanation via agent
- [x] Extension `_callLlm` updated to call `/api/context/explain-rich` (agentic)

## Phase 5: Model B & Advanced Features (PLANNED)

### 5.1 Model B: Delegate Authentication
- [ ] Extension performs Git clone/push via VS Code Git API
- [ ] Backend receives file diffs, not Git credentials
- [ ] No PAT required from user
- [ ] Migration path from Model A sessions

### 5.2 Conflict Resolution
- [ ] Detect concurrent edit conflicts in worktree
- [ ] Show conflict diff in VS Code merge editor
- [ ] Three-way merge with base branch
- [ ] Conflict notification via WebSocket broadcast

### 5.3 Workspace Search Enhancements
- [ ] Search result navigation in VS Code (jump to file:line)
- [ ] Regex search support
- [ ] Search across all active rooms (admin view)
- [ ] Search history and saved queries

### 5.4 Enterprise Features
- [ ] Room access control (invite-only rooms)
- [ ] Audit log export (CSV/JSON)
- [ ] Session recording and replay
- [ ] Admin dashboard (active rooms, user count, file stats)

## Phase 5.5: Code Understanding Enhancements (PLANNED)

### 5.5.1 Cross-Layer / Cross-Service Tracing
Today the agent traces dependencies within a single service. The next frontier is answering:

> "A user clicks 'Apply for More Credit' in the TypeScript frontend — which Python services are invoked, what SQL is ultimately run, and which database tables are written?"

This requires the agent to follow HTTP client calls across language boundaries (TypeScript → Python → SQL), map REST endpoints to their handler functions, and connect repository/ORM calls to concrete table operations.

- [ ] Cross-language call graph: TypeScript `fetch`/axios → FastAPI endpoint → service → DB
- [ ] HTTP endpoint registry (map URL patterns to handler functions across repos)
- [ ] ORM/query layer tracing (SQLAlchemy, Prisma, raw SQL detection)
- [ ] Multi-repo workspace support (agent can span more than one git worktree)
- [ ] "Request lifecycle" tool that assembles the full chain in one answer

### 5.5.2 Persistent Codebase Memory
Currently every `explain-rich` request starts from zero — the agent re-explores the same modules every time. Pre-building a module-level summary index would give the agent "working memory":

> "This file's responsibility is X, it depends on Y, it is called by Z."

The agent then skips basic exploration and immediately targets the relevant code.

- [ ] Background indexer: generate per-file summaries after workspace creation
- [ ] Store summaries in a lightweight key-value store (e.g. SQLite, Redis)
- [ ] Inject relevant summaries into the agent's initial context
- [ ] Incremental refresh on file change (watch worktree for edits)
- [ ] Cache invalidation on git pull / branch switch

### 5.5.3 Heuristic Data Flow Tracing (COMPLETE)
The `trace_variable` tool enables tracking how a value flows through function call boundaries:

> "How does `loan_id` flow from the HTTP request body, through service and repository layers, and into the final SQL WHERE clause?"

The agent chains `trace_variable` calls — each hop's `flows_to` output becomes the next hop's input.

- [x] `trace_variable` tool — single-hop analysis with alias detection, argument→parameter mapping
- [x] Forward direction: detect where a value flows to (call sites + sinks)
- [x] Backward direction: detect where a value comes from (callers + sources)
- [x] Alias detection: transitive `x = var; y = x` tracking within function bodies
- [x] Argument-to-parameter mapping: resolve callee definitions, map positional/keyword args to formal params
- [x] Sink pattern library: ORM `.filter()`/`.where()`, JPA `findBy*()`, SQL `execute()`, HTTP body, return, log
- [x] Source pattern library: HTTP `request.json`/`req.body`, annotations (`@RequestParam`, `@PathVariable`), config, DB result
- [x] Multi-language: Python, Java, TypeScript/JavaScript
- [x] Agent prompt strategy for chaining hops and verifying low-confidence connections
- [x] 15 tests covering forward/backward tracing, alias detection, param mapping, sink/source detection

#### Current limitations (heuristic approach)
| Limitation | Description |
|---|---|
| **Complex control flow aliases** | `if cond: x = loan_id` / `else: x = other` — can't tell which branch |
| **Higher-order functions** | `map(process, loans)` — can't trace into lambdas/closures |
| **Dynamic dispatch** | Interface → implementation resolution requires type inference |
| **Container shape tracking** | `data = {"id": loan_id}` → `data["id"]` across function boundaries |
| **Cross-language boundaries** | TS `fetch("/api/loans")` → Python `@app.post("/api/loans")` — URL pattern match only |
| **Framework magic** | DI containers, middleware chains, decorators that transform arguments |

### 5.5.4 Precise Static Taint Analysis (PLANNED — long-term)
Moving from heuristic regex+AST to CodeQL-level precision. This is a **research direction**, not a near-term deliverable.

> Goal: fully automated, sound taint tracking from source (HTTP input) to sink (SQL/ORM/external API) with zero false negatives and minimal false positives.

#### Required infrastructure
- [ ] SSA-form intermediate representation — transform each function into Static Single Assignment form where every variable is assigned exactly once. This is the foundation for precise alias analysis. Requires a proper AST → IR lowering pass per language (tree-sitter AST is not sufficient; need control flow graph construction).
- [ ] Inter-procedural type inference — resolve dynamic dispatch (`interface.method()` → concrete class), generics (`List<Loan>.get()` → `Loan`), and overloaded methods. Requires type constraint propagation (Hindley-Milner style for TypeScript, flow-sensitive for Java/Python).
- [ ] Taint propagation engine — forward/backward dataflow analysis over the call graph. Each statement is a transfer function: assignments propagate taint, sanitizers kill taint, transformers modify taint labels. Needs fixed-point iteration over the call graph (worklist algorithm).
- [ ] Framework-specific models — pre-built summaries for common frameworks:
  - FastAPI/Flask/Django: `request.json["key"]` is a taint source; `Response(data)` is a sink
  - Spring Boot: `@RequestBody` → taint source; `JpaRepository.save()` → sink
  - Express/NestJS: `req.body.field` → source; `res.json()` → sink
  - SQLAlchemy/Prisma/Hibernate: `.filter()`, `.execute()`, `.query()` → sinks
- [ ] Cross-language bridge — for `TypeScript → HTTP → Python` hops:
  - Parse OpenAPI/Swagger/GraphQL schema definitions as bridge contracts
  - Match `fetch("/api/loans", {body: {loan_id}})` to `@app.post("/api/loans") def handler(body: LoanRequest)`
  - Propagate taint labels across the HTTP boundary using field name matching
- [ ] Incremental analysis — re-analyze only changed files + their transitive dependents (not the whole codebase). Requires a dependency-aware invalidation cache.

#### Reference implementations to study
- **CodeQL** (GitHub/Semmle): the gold standard. Full SSA, inter-procedural, 20+ language support. But requires compilation database + offline analysis.
- **Semgrep** (r2c): pattern-based with limited inter-procedural support. Good for single-file taint rules but weak on cross-function flows.
- **Joern** (ShiftLeft): Code Property Graph — combines AST + CFG + PDG. Works on C/C++/Java/Python. Open-source.
- **WALA** (IBM): Java/JavaScript static analysis framework with SSA and points-to analysis.

#### Estimated effort
This is a **multi-quarter R&D effort** requiring compiler engineering expertise. The SSA construction alone is ~2-3 person-months per language. The pragmatic path is to incrementally improve the heuristic tool while researching whether integrating with an existing engine (CodeQL or Joern) is viable for our architecture.

## Phase 6: Production Hardening (PLANNED)

### 6.1 Performance
- [ ] Worker pool for Git operations (avoid blocking event loop)
- [ ] Worktree cleanup scheduler (remove stale sessions)
- [ ] File diff streaming (chunked transfer for large files)
- [ ] Backend horizontal scaling (shared Redis for WebSocket state)

### 6.2 Security
- [ ] Token rotation (short-lived PATs via OAuth device flow)
- [ ] Rate limiting on all endpoints
- [ ] Path traversal hardening audit
- [ ] Secrets scanning in uploaded files

### 6.3 Observability
- [ ] Structured logging (JSON, correlation IDs)
- [ ] OpenTelemetry tracing
- [ ] Prometheus metrics endpoint
- [ ] Health check improvements (deep checks for Git, AI provider)

## Milestone Summary

| Milestone | Status | Completed |
|-----------|--------|----------|
| Phase 1: Foundation | ✅ Complete | Sprint 1 |
| Phase 2: Collaboration | ✅ Complete | Sprint 2 |
| Phase 3: AI Workflows | ✅ Complete | Sprint 3 |
| Phase 4: Git Workspace (Model A) | ✅ Complete | Sprint 4 |
| Phase 4.2: Workspace Code Search | ✅ Complete | Sprint 4 |
| Phase 4.5: Graph-Based Symbol Index (RepoMap) | ✅ Complete | Sprint 5 |
| Phase 4.6: Agentic Code Intelligence | ✅ Complete | Sprint 6 |
| Phase 5: Model B + Advanced | 🟡 Planned | Sprint 7 |
| Phase 5.5: Code Understanding Enhancements | 🟡 Planned | Sprint 7–8 |
| Phase 6: Production Hardening | 🟡 Planned | Sprint 9 |

## Architecture Decision Log

### ADR-001: Model A over Model B for initial workspace
**Decision**: Implement Model A (PAT token via GIT_ASKPASS) first.
**Rationale**: Simpler to implement, test, and debug. Model B requires the extension to proxy Git operations, adding significant complexity. Model A validates the core workspace isolation design.
**Status**: Implemented in Phase 4.

### ADR-002: FileSystemProvider over SFTP/SCP
**Decision**: Use VS Code `FileSystemProvider` API with `conductor://` URI scheme.
**Rationale**: Native VS Code integration without SSH infrastructure. Files appear in the file explorer, search, and editor just like local files.
**Status**: Implemented in Phase 4.

### ADR-003: WorkspacePanel over WebView wizard
**Decision**: Use native VS Code `InputBox` / `QuickPick` for workspace creation.
**Rationale**: No CSP configuration needed. Integrates with VS Code themes. Feels native compared to a WebView form.
**Status**: Implemented in Phase 4.

### ADR-004: Per-room worktrees over shared workspace
**Decision**: Each session room gets its own Git branch (`session/{room_id}`) and worktree.
**Rationale**: Isolates concurrent sessions. Allows independent commit history per room. Simplifies conflict detection.
**Status**: Implemented in Phase 4.

### ADR-005: Inline search panel over separate view
**Decision**: Workspace code search opens in an inline WebView panel with `Ctrl+Shift+F`.
**Rationale**: Familiar keyboard shortcut. Keeps search results visible alongside code. No need for a separate VS Code sidebar view.
**Status**: Implemented in Phase 4.2.

### ADR-009: Aider-style RepoMap for graph-based context
**Decision**: Use tree-sitter AST parsing + networkx dependency graph + PageRank for file importance ranking.
**Rationale**: Vector search finds semantically similar code but misses structural context. The dependency graph identifies files that are structurally important (heavily imported, central to architecture) even if they don't contain text matching the query. Personalised PageRank bridges the two: bias towards files from vector search, then expand via graph.
**Status**: Implemented in Phase 4.5.3.

### ADR-013: Unified credential injection with setdefault()
**Decision**: `_inject_embedding_env_vars()` now injects ALL available credentials at startup using `os.environ.setdefault()` instead of only injecting the active backend's credentials with direct assignment.
**Rationale**: With LiteLLM, the active backend is determined by the model string prefix, not a separate `embedding_backend` field. Injecting all available credentials means any model string works without config changes. Using `setdefault()` ensures pre-existing environment variables (from IAM roles, instance profiles, or CI) are never silently overwritten.
**Status**: Implemented in Phase 4.5.2.

### ADR-014: Agentic tool loop over RAG pipeline for code context
**Decision**: Replace the vector search → rerank → graph RAG pipeline with an LLM agent loop (`AgentLoopService`) that iteratively calls code-intelligence tools to gather context.
**Rationale**: RAG pipelines are passive — they retrieve pre-indexed chunks and hope the relevant code was indexed at the right granularity. An agent is active: it decides what to read next based on what it has already seen, follows import chains, reads function implementations, and checks who calls what. This enables cross-file reasoning (e.g. tracing a Protocol to its concrete implementation, or finding where a DI container resolves a dependency) that vector similarity cannot replicate. The trade-off is higher latency (up to 10–15 LLM calls per query) vs. a single retrieval + generation pass — acceptable for an on-demand "Explain" action where depth matters more than speed. The RepoMap (ADR-009) is retained as a structural index powering the `find_symbol`, `file_outline`, and dependency tools used by the agent.
**Status**: Implemented in Phase 4.6.
