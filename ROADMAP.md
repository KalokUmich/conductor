# Conductor Project Roadmap

Last updated: 2026-03-22

## Current State

Conductor is a VS Code collaboration extension with a FastAPI backend. The project currently has working implementations of:

- Real-time WebSocket chat (with reconnect, typing indicators, read receipts)
- **Chat persistence**: write-through micro-batch Postgres (ChatPersistenceService); Redis hot cache; history survives backend restarts
- File upload/download (20MB limit, dedup, retry)
- Code snippet sharing + editor navigation + **Highlight.js syntax highlighting** in WebView
- Change review workflow (MockAgent, policy check, diff preview, audit log)
- AI provider workflow (health check, provider selection, streaming inference)
- **Browser tools**: Playwright Chromium automation (`browse_url`, `search_web`, `screenshot`)
- **Git Workspace Management (Model A)**:
  - Per-room bare repo + worktree isolation
  - GIT_ASKPASS token authentication
  - FileSystemProvider (`conductor://` URI scheme)
  - WorkspacePanel 5-step creation wizard
  - WorkspaceClient typed HTTP client
  - Workspace code search (`GET /workspace/{room_id}/search`)
- **Agentic Code Intelligence**:
  - `AgentLoopService` — LLM-driven iterative tool loop (up to 25 iterations, 500K token budget)
  - 24 code tools: `grep`, `read_file`, `list_files`, `find_symbol`, `find_references`, `file_outline`, `get_dependencies`, `get_dependents`, `git_log` (+ `search=`), `git_diff`, `ast_search`, `get_callees`, `get_callers`, `git_blame`, `git_show`, `find_tests`, `test_outline`, `trace_variable`, `compressed_view`, `module_summary`, `expand_symbol`, `run_test`
  - 3-layer system prompt: Core Identity + Strategy (by query type) + Runtime Guidance
  - Query classifier: keyword matching (default) or LLM pre-classification (Haiku)
  - Dynamic tool sets: 8-12 tools per query type (reduces LLM confusion)
  - Token-based budget controller with convergence signals
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

## Phase 5.5: Code Understanding Enhancements (IN PROGRESS)

### 5.5.0 Agent Loop Intelligence (IN PROGRESS)
Optimizations guided by OpenAI review and academic research (ICLR 2026, MutaGReP, DraCo, LingmaAgent, RAG-Gym).

#### Token-Based Budget Controller (COMPLETE)
- [x] `TokenUsage` dataclass in `ai_provider/base.py` — extracted from all 3 providers (Bedrock, Anthropic Direct, OpenAI)
- [x] `BudgetController` in `agent_loop/budget.py` — tracks cumulative input/output tokens per session
- [x] Three budget signals: `NORMAL`, `WARN_CONVERGE` (70% threshold or diminishing returns), `FORCE_CONCLUDE` (90% threshold or max iterations)
- [x] Diminishing returns detection: if last N iterations found no new files or symbols, signal convergence
- [x] File/symbol tracking: `track_file()` / `track_symbol()` for dedup-aware progress monitoring
- [x] Budget context injection: LLM sees token usage, iteration count, files/symbols accessed in each turn
- [x] Integrated into `AgentLoopService.run_stream()` — replaces iteration-only budget note with token-aware context
- [x] `budget_summary` dict in `AgentResult` for downstream logging/analysis
- [x] 20 unit tests covering all signal transitions, tracking, edge cases
- References: [ICLR 2026 — Token Consumption in Coding Agents](https://openreview.net/forum?id=1bUeVB3fov), [Anthropic — Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

#### Structured Session Trace (COMPLETE)
- [x] `SessionTrace` dataclass in `agent_loop/trace.py` — per-session JSON trace for offline analysis
- [x] `IterationTrace` + `ToolCallTrace` — per-iteration metrics with per-tool latencies
- [x] Record LLM latency, tool latencies, token breakdown, budget signals emitted per iteration
- [x] `trace.save(trace_dir)` persists as `{session_id}.json` for offline analysis
- [x] Integrated into `AgentLoopService.run_stream()` — traces saved on every exit path
- [x] `trace_dir` parameter on `AgentLoopService` constructor (opt-in)
- [x] 15 unit tests covering all trace dataclasses, save/load, aggregation
- Reference: [RAG-Gym — Process Supervision for Agents](https://arxiv.org/abs/2502.13957)

#### Tool Output Policy (COMPLETE)
- [x] `output_policy.py` in `code_tools/` — per-tool truncation policies (max_results, max_chars, truncate_unit)
- [x] Differentiated policies for 18 tools: search tools limit by result count, read_file by lines, git tools generous chars
- [x] Budget-adaptive: shrink limits by 50% when remaining input tokens < 100K
- [x] Replaced uniform 30KB hard cutoff with `apply_policy()` in `_tool_result_block()`
- [x] Line-boundary truncation for `read_file` (truncate_unit="lines")
- [x] 19 unit tests covering per-tool policies, budget adaptation, edge cases

#### Config Cleanup (COMPLETE)
- [x] Removed RAG remnants: `EmbeddingSecrets`, `VoyageSecrets`, `MistralSecrets`, `CohereSecrets`, `AwsSecrets`, `OpenAISecrets` (embedding-specific)
- [x] Removed `_inject_embedding_env_vars()` — no longer needed without embedding pipeline
- [x] Cleaned `CodeSearchSettings` — kept only `repo_map_enabled` and `repo_map_top_n`
- [x] Removed RAG router registration from `main.py` (endpoints were already returning 503)
- [x] Updated `load_settings()` log message to remove embedding/rerank references
- [x] Updated test suite: `test_config_new.py` rewritten to match cleaned config

#### Query Classifier + LLM Pre-Classification (COMPLETE)
- [x] Keyword-based classification into 7 query types (entry_point, flow_tracing, root_cause, impact, architecture, config, data_lineage)
- [x] Per-type strategy suggestion, initial tool hints, and token budget recommendation
- [x] Classification result injected into initial user message for LLM guidance
- [x] Integrated into `AgentLoopService.run_stream()` alongside existing `_is_high_level_query`
- [x] Optional LLM pre-classification via `classify_query_with_llm()` using lightweight model (Haiku) — ~100ms, ~$0.001 per call
- [x] Configurable in `conductor.settings.yaml` via `classifier.use_llm` and `classifier.model_id`
- [x] Falls back to keyword matching on LLM failure
- [x] Dynamic tool set per query type — only 8-12 of 24 tools exposed to LLM (reduces confusion and token waste)
- [x] `filter_tools()` helper in `schemas.py` for tool set filtering
- [x] 26 tests in `test_query_classifier.py` (keyword, LLM mock, tool_set, filter_tools)

#### Compressed View Tools (COMPLETE)
- [x] `compressed_view` tool — file signatures + call relationships + side effects + raises (~80% token savings vs read_file)
  - Rich symbol extraction: classes + methods within classes (richer than repo_graph parser)
  - Multi-language: Python, JS/TS, Java, Go, Rust
  - Focus filter: narrow output to a specific symbol (substring match)
  - Side effect detection: db_write, http_call, event_publish, file_write, cache_write
- [x] `module_summary` tool — module-level summary: services, models, controllers, functions, imports, file list (~95% token savings)
  - Multi-language support (all 10 supported file extensions)
  - Classifies symbols by role (Service, Model, Controller, etc.)
- [x] `expand_symbol` tool — lazy expansion from compressed to full source
  - With or without file_path (workspace search fallback)
  - Substring match when exact name not found
  - Shows alternatives when multiple candidates exist
- [x] All 3 tools registered in TOOL_REGISTRY, TOOL_DEFINITIONS, output policies
- [x] System prompt updated with tool usage priority and efficient exploration patterns
- [x] 24 tests in `test_compressed_tools.py`
- References: [MutaGReP](https://arxiv.org/abs/2502.15872), [LingmaAgent](https://arxiv.org/abs/2406.01422)

#### Language Support Hardening (COMPLETE)
- [x] `find_tests` — added test file glob patterns for Java, Rust, C/C++ (`*Test.java`, `*_test.rs`, `*_test.c`, etc.)
- [x] `find_tests` — added Java `@Test`/`@ParameterizedTest` and Rust `#[test]`/`#[tokio::test]` detection
- [x] `test_outline` — added Java (JUnit/Mockito), Go (testing.T/testify), Rust (#[test]) parsers
- [x] 10 new language-specific tests in `test_code_tools.py`

#### Symbol Role Classification (COMPLETE)
- [x] Classify symbols into 7 roles: route_entry, business_logic, domain_model, infrastructure, utility, test, unknown
- [x] 3-tier classification: decorator/annotation context (reads 5 lines above symbol) → file path patterns → name patterns
- [x] `find_symbol` results sorted by role priority: route_entry > business_logic > domain_model > infrastructure > utility > test > unknown
- [x] Within same role, exact name matches before substring matches
- [x] Multi-language: Python decorators (@app.route, @Service), Java annotations (@Entity, @RestController, @Repository), path conventions
- [x] Each result includes `role` field for downstream filtering
- [x] 24 tests in `test_symbol_role.py` (path, name, decorator, annotation, priority, sorting)

#### 3-Layer System Prompt (COMPLETE)
- [x] Layer 1: Core Identity (~100 lines) — always included: hard constraints, exploration pattern, answer format
- [x] Layer 2: Strategy (~30 lines) — selected by query classifier: 7 strategies (entry_point, flow_tracing, root_cause, impact, architecture, config, data_lineage)
- [x] Layer 3: Runtime Guidance — injected dynamically by service.py: budget context, scatter warnings, convergence checkpoints
- [x] Prompt compressed from ~7500 to ~4000 tokens per LLM call
- [x] Removed redundant tool descriptions (already in TOOL_DEFINITIONS)
- [x] Removed contradictory rules (e.g. "NEVER 3 greps" vs "maximize parallelism")
- [x] `build_system_prompt()` accepts `query_type` to select Layer 2 strategy
- [x] Accumulated text trimming — keeps only last 3 thinking turns to limit context growth
- [x] Budget hard constraints at WARN_CONVERGE — refuses new broad searches, only allows verification calls
- [x] 47 tests in `test_agent_loop.py` (including strategy selection by query type + completeness check)

#### RepoMap v2: Dataflow-Enhanced Graph (PLANNED)
- [ ] Dataflow edges: variable_flows_to, reads_config, writes_to
- [ ] Change coupling from git log co-change analysis
- [ ] Enhanced PageRank with new edge types
- References: [DraCo](https://arxiv.org/abs/2405.19782), [RepoHyper](https://arxiv.org/abs/2403.06095)

#### Evidence Evaluator (COMPLETE)
- [x] Rule-based evidence completeness check before finalizing answers
  - Check 1: answer must contain file:line references or code blocks (unless very short)
  - Check 2: agent must have made ≥ 2 tool calls
  - Check 3: agent must have accessed ≥ 1 file
- [x] If evidence insufficient AND budget remains (≥ 2 iterations), reject the answer and inject guidance forcing the LLM to investigate further
- [x] Graceful degradation: if no budget remains, let the weak answer through
- [x] Integrated into `AgentLoopService.run_stream()` at the "Final answer" checkpoint
- [x] 14 tests in `test_evidence.py`
- Reference: [RAG-Gym](https://arxiv.org/abs/2502.13957)
- [ ] Future: Optional Haiku-based evaluation when rules are insufficient

#### Completeness Verifier (COMPLETE)
Adds a second quality gate after EvidenceEvaluator to ensure the answer is complete and not truncated mid-thought.
- [x] `completeness.py` in `agent_loop/` — `CompletenessCheck` detects incomplete answers (truncated sentences, unresolved placeholders, trailing "...")
- [x] `check_completeness()` called after evidence gate; re-prompts LLM if answer appears cut off
- [x] Improves stability of long agent answers at high token budgets

#### Code Review Eval System (COMPLETE)
Standalone eval system in `eval/` for measuring `CodeReviewService` quality against planted bugs.
- [x] `runner.py` — workspace setup (copytree → git init → git apply → git commit) + CodeReviewService execution
- [x] `scorer.py` — deterministic scoring: recall (35%), precision (20%), severity (15%), location (10%), recommendation (10%), context (10%)
- [x] `judge.py` — LLM-as-Judge: completeness, reasoning quality, actionability, false positive quality (1-5 scale)
- [x] `report.py` — report generation + baseline comparison + regression detection (10% threshold)
- [x] `run.py` — CLI entrypoint: `--filter`, `--no-judge`, `--save-baseline`, `--provider`, `--model`, `--parallelism`
- [x] 12 cases against requests v2.31.0 (4 easy, 5 medium, 3 hard): timeout, connection error, encoding, content-length, auth leak, URL scheme, cookie threading, chunked encoding, proxy auth, redirect loop, SSL bypass, hook suppression
- [x] Repos stored as plain source (no `.git`); runner creates temp git repo per case
- [x] Pattern-based ground truth matching (title_pattern regex, file_pattern, line_range, severity, category)
- [x] `requires_context` field validates cross-file exploration
- [x] Timestamped JSON baselines for regression detection
- [x] Excluded from Docker via `.dockerignore`

#### Config-Driven Workflow Engine (COMPLETE)
Extract hardcoded multi-agent orchestration into a config-driven engine with YAML workflow definitions and Markdown agent files.

- [x] `workflow/models.py` — Pydantic models: `WorkflowConfig`, `AgentConfig`, `RouteConfig`, `ClassifierResult`, `StageConfig`, `BudgetDefaults`
- [x] `workflow/loader.py` — `load_workflow()` + `load_agent()` — parse YAML + Markdown frontmatter, resolve delegate references, validate input/output ordering
- [x] `workflow/classifier_engine.py` — generic `ClassifierEngine` with `risk_pattern` (file path regex → PR Review dimensions) and `keyword_pattern` (query text → Code Explorer routes)
- [x] `workflow/engine.py` — `WorkflowEngine.run_stream()` — `first_match` mode (Code Explorer: best route) and `parallel_all_matching` mode (PR Review: all matching routes in parallel, then post_pipeline)
- [x] `workflow/mermaid.py` — `generate_mermaid()` auto-generates Mermaid flowchart from any `WorkflowConfig` (different layout per route_mode)
- [x] `workflow/router.py` — 5 REST endpoints: `GET /api/workflows`, `GET /api/workflows/{name}`, `GET /api/workflows/{name}/mermaid`, `GET /api/workflows/{name}/graph`, `PUT /api/workflows/{name}/models`
- [x] Config files: `config/workflows/pr_review.yaml` (6 routes, parallel_all_matching), `config/workflows/code_explorer.yaml` (9 routes, first_match, includes `delegate` to pr_review)
- [x] 18 agent `.md` files in `config/agents/` — 5 PR explorer agents, 2 PR judge agents, 3 code explorer multi-agent, 8 code explorer single-agent routes (incl. `explore_code_explanation`)
- [x] 2 shared prompt templates in `config/prompts/` — `review_base.md`, `explorer_base.md`
- [x] `workflow/observability.py` — Langfuse `@observe` decorator; zero overhead when disabled (no-op function wrapper)
- [x] `main.py` — `init_langfuse()` at startup, `flush()` at shutdown, `workflow_router` registered

#### Langfuse Observability (COMPLETE)
Self-hosted LLM tracing with nested execution trees, cost tracking, and latency analysis.
- [x] `docker/docker-compose.langfuse.yaml` — Langfuse server + PostgreSQL self-hosted stack (port 3001)
- [x] `langfuse>=2.0` in `requirements.txt`
- [x] `LangfuseSettings` + `LangfuseSecrets` in `config.py`
- [x] `make langfuse-up`, `make langfuse-down`, `make langfuse-logs` Makefile targets
- [x] Traces nested as: workflow → route → agent → llm_call → tool
- [x] Coexists with SessionTrace — Langfuse adds Web UI + team sharing; SessionTrace keeps tool params + thinking text

#### Workflow Visualization Panel (COMPLETE)
Interactive workflow graph in the VS Code extension WebView.
- [x] `GET /api/workflows/{name}/graph` — React Flow-compatible JSON (nodes + edges with labels)
- [x] `extension/media/workflow.html` — SVG graph rendered with dark glass theme; node types: explorer (violet), judge (indigo), classifier (diamond), group (dashed border)
- [x] `extension/src/services/workflowPanel.ts` — singleton WebView panel class
- [x] `conductor.showWorkflow` command registered in `extension.ts` and `package.json`
- [x] Graph icon button in chat header opens the panel
- [x] Node click shows agent detail sidebar (tools, budget, trigger conditions, prompt excerpt)
- [x] Two tabs: PR Review and Code Explorer

#### Slash Command System (COMPLETE)
Cleaner `@AI` command format with floating menu and ghost text hints.
- [x] `@AI /ask xxx` (passthrough) and `@AI /pr branch...base` (transforms to `do PR main...feature/x`)
- [x] Floating menu above chat textarea — appears on `@AI /`, filters by prefix, keyboard navigation (↑↓ Enter Tab Escape)
- [x] Ghost text hint overlay — color-transparent textarea + positioned div shows e.g. "main...feature/branch-name"
- [x] Commands in `SLASH_COMMANDS` JS array — extensible registry
- [x] Backward compatible: bare `@AI xxx` and old `@AI do PR ...` still work unchanged
- [x] "Workflows" tab in AI Config modal for explorer/judge model selection per workflow

#### Cross-Session Query Patterns (PLANNED)
Analyze session traces to learn from past queries and improve future performance.
- [ ] Build `query_patterns.json` from offline analysis of session traces
- [ ] Track common entry points, hot modules, and effective tool strategies per query type
- [ ] Feed historical data back into Query Classifier — bias initial tool selection toward patterns that worked
- [ ] Warm-start the budget controller based on observed token costs for similar queries
- Reference: [RAG-Gym — Process Supervision](https://arxiv.org/abs/2502.13957)

#### Multi-Agent Collaboration (PLANNED — long-term)
Split the single-agent loop into specialized sub-agents for complex queries.
- [ ] Navigator Agent (Haiku) — decompose complex questions into sub-tasks + assign strategies
- [ ] Explorer Agent (Sonnet) — execute sub-tasks, collect evidence, call tools
- [ ] Critic Agent (Haiku) — verify completeness, identify gaps, suggest follow-ups
- [ ] Final synthesis by Navigator aggregating Explorer outputs
- [ ] Shared evidence store across sub-agents (avoid duplicate tool calls)
- Reference: [MANTRA — 82.8% success rate with multi-agent](https://arxiv.org/abs/2502.15872)

#### Architecture Analyzer (PLANNED)
Higher-level architectural analysis beyond individual file dependencies.
- [ ] Generate service dependency graph from RepoMap v2 edges
- [ ] Detect cyclic dependencies (strongly connected components in import graph)
- [ ] Identify layer violations (e.g., controller importing repository directly, skipping service layer)
- [ ] Dead code detection (PageRank ≈ 0 + zero references)
- [ ] Output as structured JSON for visualization in WebView

#### Side Effect Analyzer Enhancement (PLANNED)
Extend `trace_variable` with richer sink/source detection.
- [ ] User-configurable sink/source patterns via `.conductor/sink_patterns.yaml`
- [ ] Confidence levels: "confirmed" (pattern match + AST verification) vs "probable" (pattern only)
- [ ] Cross-file flow continuation: auto-chain `trace_variable` across function boundaries
- [ ] Extended sink patterns: message queues (Kafka, RabbitMQ), cache writes (Redis), event emit

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
- [ ] **Evaluate PowerMem integration** ([oceanbase/powermem](https://github.com/oceanbase/powermem)) — AI memory system with vector retrieval + Ebbinghaus forgetting curve + multi-agent isolation. Potential benefits:
  - Cross-session query pattern learning (fact extraction from session traces)
  - PR review memory (recall previous review findings for the same module)
  - 96.5% token reduction via selective context injection vs full history
  - Per-agent memory spaces map to Brain → sub-agent architecture

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

## Phase 7: External Service Integrations (IN PROGRESS)

**Database**: Shared Postgres (Langfuse instance, port 5433), new `conductor` database. SQLAlchemy async with `DatabaseSettings` pool config.

**Backend module structure:**
```
backend/app/integrations/
├── __init__.py
├── db.py                      # SQLAlchemy async engine + session factory
├── token_store.py             # IntegrationTokenStore — Postgres-backed, keyed by (user_email, provider)
├── jira/
│   ├── service.py             # JiraOAuthService — OAuth 3LO + token refresh
│   ├── api_client.py          # JiraApiClient — REST API wrapper with auto-refresh
│   ├── models.py              # Pydantic: JiraTokenPair, JiraProject, JiraIssue, CreateIssueRequest
│   └── router.py              # /api/integrations/jira/* endpoints
├── teams/
│   ├── service.py             # TeamsService — Bot Framework + Graph API
│   ├── models.py              # Pydantic: TeamsMessage, TeamsChannel
│   └── router.py              # /api/integrations/teams/* endpoints
└── slack/
    ├── service.py             # SlackService — slash commands + webhooks
    ├── models.py              # Pydantic: SlackCommand, SlackWebhookPayload
    └── router.py              # /api/integrations/slack/* endpoints
```

### 7.0 Database Foundation (COMPLETE)
- [x] `sqlalchemy[asyncio]` + `asyncpg` in `requirements.txt`
- [x] `backend/app/db/engine.py` — async engine + session factory
- [x] `backend/app/db/models.py` — 6 SQLAlchemy ORM tables: `repo_tokens`, `session_traces`, `audit_logs`, `file_metadata`, `todos`, `integration_tokens`
- [x] `docker/docker-compose.data.yaml` — shared Postgres + Redis; `init-db.sql` creates `langfuse` database
- [x] Schema managed by **Liquibase** (`database/changelog/`) — replaced Alembic
- [x] `make db-update` / `make db-status` / `make db-rollback-one` Makefile targets
- Acceptance criteria:
  - [x] `make data-up` creates both `conductor` and `langfuse` databases
  - [x] SQLAlchemy async engine connects to Postgres
  - [x] `IntegrationToken` model with `(user_email, provider)` unique constraint
  - [x] Langfuse continues to work unchanged
  - [x] Unit tests with async SQLite fallback for CI

### 7.1 Jira OAuth Backend (COMPLETE)
- Atlassian OAuth 2.0 (3LO) flow on the backend
- Access token: 1h lifetime; Refresh token: 90 days, rotating (each refresh returns new refresh token)
- Scopes: `read:jira-work`, `write:jira-work`, `read:jira-user`, `offline_access`
- `cloudId` fetched from `accessible-resources` after token exchange
- Config: `JiraSettings` + `JiraSecrets` in `config.py`
- Files created: `integrations/jira/service.py` (JiraOAuthService), `models.py`, `router.py`
- Acceptance criteria:
  - [x] `POST /callback` exchanges auth code for tokens
  - [x] `get_valid_token()` auto-refreshes expired access tokens using rotating refresh token
  - [x] `cloudId` fetched and stored
  - [x] `GET /status` returns connection state
  - [x] `POST /disconnect` removes tokens
  - [x] All endpoints return 400 when `jira.enabled: false`
  - [x] Unit tests with mocked httpx calls (39 tests in `test_jira_service.py`)

### 7.2 Jira API Service (COMPLETE)
- `JiraOAuthService` handles both OAuth lifecycle and API calls via `httpx.AsyncClient`
- API base: `https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3/...`
- Endpoints: `GET /projects`, `GET /issue-types?projectKey=X`, `GET /create-meta`, `POST /issues` (create)
- Acceptance criteria:
  - [x] Auto-refreshes token on 401 response (single retry)
  - [x] `POST /issues` creates a Jira ticket and returns issue key + URL
  - [x] `GET /projects` returns project list
  - [x] `GET /issue-types` returns issue types per project
  - [x] `GET /create-meta` returns field metadata (priorities, components, teams)
  - [x] All endpoints return 401 when user has no valid connection
  - [x] Unit tests with mocked Jira API responses (29 tests in `test_jira_router.py`)
  - [x] `GET /search?q=...` — JQL text search with formatted results

### 7.3 Extension Jira Auth UI (COMPLETE)
- "Connect Jira" button in chat panel integrations section
- OAuth flow: browser callback → backend exchanges code → HTML auto-redirects to `vscode://ai-collab/jira/callback?connected=true` → `JiraUriHandler` refreshes status
- Connection status cached in `globalState` via `jiraAuthService.ts` (follows `ssoIdentityCache.ts` pattern with 48h TTL)
- `publisher` field added to `package.json`; `onUri` activation event registered
- Files created: `extension/src/services/jiraAuthService.ts` (JiraUriHandler + cache functions)
- Files modified: `extension/package.json`, `extension/src/extension.ts`, `backend/app/integrations/jira/router.py` (VS Code redirect in callback HTML)
- Acceptance criteria:
  - [x] "Connect Jira" button visible in chat panel
  - [x] Browser opens to Atlassian authorize page
  - [x] `vscode://` callback captured and forwarded to backend
  - [x] Connection status cached in globalState with TTL
  - [x] "Disconnect Jira" command works (clears globalState + backend tokens)
  - [x] Stale connection auto-cleared on extension reload

### 7.4 Extension Jira Ticket Creation UI (COMPLETE)
- Ticket creation modal: project dropdown, issue type selector, priority, team, components, summary, description
- Slash commands: `@AI /jira create [summary]` opens modal with pre-filled summary, `@AI /jira search [query]` searches Jira issues
- Search results rendered as compact cards with clickable issue keys
- Files modified: `extension/media/chat.html` (slash commands + search rendering), `extension/src/extension.ts` (jiraSearch handler)
- Backend: `GET /api/integrations/jira/search?q=...` — JQL text search endpoint added
- Acceptance criteria:
  - [x] Project/issue-type dropdowns load from API
  - [x] Submit creates ticket, shows issue key + clickable URL
  - [x] `/jira create` and `/jira search` slash commands work
  - [x] Search results displayed with status, priority, assignee
  - [x] Form validates required fields

### 7.5 Microsoft Teams Integration
- Bot Framework webhook: `POST /api/integrations/teams/bot/messages`
- Graph API for reading channels/messages (app-level client credentials auth)
- Bot commands: `@Conductor review PR#123`, `@Conductor ask "..."`
- Results posted as Adaptive Cards
- Files to create: `integrations/teams/service.py`, `models.py`, `router.py`
- Files to modify: `config.py` (add `TeamsSettings`/`TeamsSecrets`), `main.py`, settings YAML
- Acceptance criteria:
  - [ ] Bot validates HMAC signatures on incoming Activities
  - [ ] `@Conductor review` and `@Conductor ask` commands work
  - [ ] Channel messages can be read via Graph API
  - [ ] Review results posted as Adaptive Cards

### 7.6 Slack Integration
- Slash command endpoint: `POST /api/integrations/slack/commands` with HMAC-SHA256 signature validation
- Incoming webhooks for posting results
- Commands: `/conductor review PR#123`, `/conductor ask "..."`
- Results formatted as Slack Block Kit messages
- Files to create: `integrations/slack/service.py`, `models.py`, `router.py`
- Files to modify: `config.py` (add `SlackSettings`/`SlackSecrets`), `main.py`, settings YAML
- Acceptance criteria:
  - [ ] Validates Slack request signatures
  - [ ] `/conductor review` triggers CodeReviewService, posts formatted results
  - [ ] `/conductor ask` triggers AgentLoopService, posts answer
  - [ ] Webhook URL configurable per channel

### Dependency Graph
```
7.0 (DB Foundation) ──> 7.1 (Jira OAuth) ──┬──> 7.2 (Jira API) ──┬──> 7.4 (Ticket UI)
                                            │                      │
                                            ├──> 7.3 (Auth UI) ────┘
                                            ├──> 7.5 (Teams) [parallel]
                                            └──> 7.6 (Slack) [parallel]
```

### Config Additions
- `conductor.settings.yaml`: `integrations.jira.enabled`, `integrations.teams.enabled`, `integrations.slack.enabled`
- `conductor.secrets.yaml`: `integrations.jira.client_id/client_secret`, `integrations.teams.*`, `integrations.slack.*`
- `DatabaseSecrets.url`: `postgresql+asyncpg://langfuse:langfuse@localhost:5433/conductor`

## Phase 8: Infrastructure & UI Hardening (COMPLETE)

Completed 2026-03-22. Quality-of-life improvements and infrastructure fixes.

### 8.1 Chat Persistence (COMPLETE)
- [x] `chat/persistence.py` — `ChatPersistenceService` write-through micro-batch Postgres (batch=3, flush=5s)
- [x] Postgres as source of truth; Redis as hot cache (6h TTL)
- [x] `DELETE /chat/{room_id}` endpoint — purges history from Postgres, Redis, files, and audit logs
- [x] History endpoint returns `codeSnippet` field for `code_snippet` messages (fixes blank code on rejoin)
- [x] `test_chat_persistence.py` covering batch writes, flush timer, delete

### 8.2 Browser Tools (COMPLETE)
- [x] `browser/` — Playwright Chromium-based web browsing tools (`browse_url`, `search_web`, `screenshot`)
- [x] `make browser-install` target for Playwright Chromium
- [x] `test_browser_tools.py` with mocked Playwright service

### 8.3 DuckDB Removal (COMPLETE)
- [x] Removed `duckdb` from `requirements.txt` — all storage is now PostgreSQL via SQLAlchemy async
- [x] Removed stale `.duckdb` / `.duckdb.wal` runtime files
- [x] `make clean` now deletes `*.duckdb` and `*.duckdb.wal`

### 8.4 Singleton Service Startup Init (COMPLETE)
- [x] `TODOService`, `AuditLogService`, `FileStorageService` initialized in `main.py` lifespan with async engine
- [x] Prevents `RuntimeError: requires an AsyncEngine on first call` on first request

### 8.5 Tool Parity: Subprocess Validation (COMPLETE)
- [x] `extension/tests/validate_contract.js` now validates 11 subprocess tools via Python CLI
- [x] `runPythonTool()` uses `execFileSync` calling `python -m app.code_tools`
- [x] `ast_search` and `run_test` warn instead of fail when CLI tool not installed
- [x] `get_repo_graph` added to `SUBPROCESS_TOOLS` in `localToolDispatcher.ts` (was silently unreachable)

### 8.6 Liquibase Connection Fix (COMPLETE)
- [x] Connection args (`--url`, `--username`, `--password`) moved to Makefile CLI params — Java cannot parse bash `${VAR:-default}` syntax in JDBC URLs
- [x] `database/liquibase.properties` contains only `changeLogFile` and `search-path`

### 8.7 Chat UI Overhaul (COMPLETE)
- [x] **Syntax highlighting**: Bundled Highlight.js 11.9.0 (`highlight.min.js` + `github-dark.min.css`) — no CDN
- [x] **Message rendering**: `renderMessageByType()` dispatcher — routes history/cached messages to correct renderer per type
- [x] **Online mode room list**: loads rooms from `/chat/rooms?email=...`, shows status dots, supports rejoin
- [x] **chatLocalStore.ts**: local message cache for offline/reconnect history
- [x] **Auto-workspace registration**: `_handleStartSession` registers local workspace automatically — removed "Use Local" button
- [x] **Leave/Quit merged**: single Leave button (quit behavior — ends session for all)
- [x] **Mermaid fallback**: shows raw source when diagram fails to parse (Qwen compatibility)
- [x] **AI status silent retry**: up to 3 retries before showing error banner
- [x] **Error banners**: unified `.error-banner` class with dismiss button across all 5 error containers
- [x] **Markdown enhancements**: tables, links, italic in `inlineFormat()`, larger code blocks (`max-h-80`)

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
| Phase 5.5: Code Understanding Enhancements | 🟢 In Progress | Sprint 7–9 |
| Phase 5.6: Config-Driven Workflow Engine (A-D) | ✅ Complete | Sprint 9 |
| Phase 6: Production Hardening | 🟡 Planned | Sprint 9 |
| Phase 7.0–7.2: DB Foundation + Jira Backend | ✅ Complete | Sprint 10 |
| Phase 7.3–7.4: Jira Extension UI | ✅ Complete | Sprint 11 |
| Phase 7.5–7.6: Teams + Slack | 🟡 Planned | Sprint 12 |
| Phase 8: Infrastructure & UI Hardening | ✅ Complete | Sprint 12 |

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

### ADR-015: Integration Token Store — Postgres with SQLAlchemy async
**Decision**: Store OAuth tokens for external integrations (Jira, Teams, Slack) in a shared Postgres instance (the Langfuse database server, port 5433) using SQLAlchemy async, with an `integration_tokens` table keyed by `(user_email, provider)`.
**Rationale**: Postgres over SQLite for consistency with the existing Langfuse infrastructure, better concurrency under multiple users, and native JSONB support for provider-specific metadata (Jira `cloudId`, Teams `tenantId`). SQLAlchemy async chosen over raw asyncpg for declarative models and migration support. The `DatabaseSettings` pool config (`pool_size`, `max_overflow`) already exists in `config.py`.
**Status**: Planned for Phase 7.0.
