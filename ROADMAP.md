# Conductor Project Roadmap

Last updated: 2026-03-09

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
- **CocoIndex Code Search**:
  - AST-aware chunking + embedding + vector storage (sqlite-vec or Postgres)
  - LiteLLM unified embedding (100+ providers via one config field)
  - Default: Cohere Embed v4 via AWS Bedrock
  - Postgres backend with incremental processing
- **RepoMap (Graph-based Context)**:
  - tree-sitter AST parsing for symbol extraction
  - File dependency graph (networkx) with PageRank ranking
  - Hybrid retrieval: vector search + graph-based repo map
  - Personalised PageRank (biased towards vector search results)
- **Reranking (Post-Retrieval Precision)**:
  - 4 configurable reranking backends (none, cohere, bedrock, cross_encoder)
  - Two-stage retrieval: vector search → rerank → top-N
  - Pluggable RerankProvider abstraction
  - Integrated into context router as optional post-retrieval step

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

## Phase 4.5: Semantic Code Search (COMPLETE)

### 4.5.1 CocoIndex Integration
- [x] CocoIndex Code Search Service (AST-aware chunking + sqlite-vec)
- [x] Code search router (`/api/code-search/`)
- [x] Context router with hybrid retrieval (`/api/context/`)
- [x] Per-workspace index management

### 4.5.2 Multi-Provider Embeddings (P0) → LiteLLM Unification
- [x] EmbeddingProvider abstract base class
- [x] LocalEmbeddingProvider (SentenceTransformers, `sbert/` prefix)
- [x] LiteLLMEmbeddingProvider (unified provider for 100+ backends)
- [x] Well-known dimensions map (`_KNOWN_DIMS`, 20+ entries)
- [x] Factory with `sbert/` → Local, everything else → LiteLLM routing
- [x] Legacy backward compatibility via `_legacy_backend_to_model()`
- [x] Unified credential injection (`setdefault()` for all available secrets)
- [x] `COCOINDEX_CODE_EMBEDDING_MODEL` env var passthrough
- [x] Default: `bedrock/cohere.embed-v4:0` ($0.12/1M tokens)
- [x] Comprehensive tests (85+ test cases)

### 4.5.2b Postgres Backend & Incremental Processing
- [x] `storage_backend` setting: `"sqlite"` (default) or `"postgres"`
- [x] `COCOINDEX_DATABASE_URL` env var injection for Postgres
- [x] `incremental: true` → passes `incremental=True` to `cocoindex.build()`
- [x] `is_incremental` property (requires both postgres + incremental=true)
- [x] `storage_backend` and `is_incremental` in health/status endpoints
- [x] Comprehensive tests (integrated into code_search + config tests)

### 4.5.3 RepoMap Graph-Based Context (P1)
- [x] `repo_graph/parser.py` — tree-sitter AST + regex fallback
- [x] `repo_graph/graph.py` — networkx dependency graph + PageRank
- [x] `repo_graph/service.py` — RepoMapService with caching
- [x] Hybrid retrieval in context router (vector + graph)
- [x] Personalised PageRank (biased by vector search results)
- [x] `GET /api/context/context/{room_id}/graph-stats` endpoint
- [x] Comprehensive tests (72 test cases)

### 4.5.4 Reranking for Code Search (P2)
- [x] RerankProvider abstract base class + RerankResult dataclass
- [x] NoopRerankProvider (passthrough, default)
- [x] CohereRerankProvider (Cohere Rerank 3.5 direct API)
- [x] BedrockRerankProvider (Cohere Rerank 3.5 via AWS Bedrock)
- [x] CrossEncoderRerankProvider (local `ms-marco-MiniLM-L-6-v2`)
- [x] Factory function `create_rerank_provider(settings)`
- [x] CohereSecrets in config + `_inject_embedding_env_vars()` for reranking
- [x] Context router: 3-stage pipeline (vector → rerank → graph)
- [x] Per-request `enable_reranking` override + `rerank_score` in response
- [x] `GET /api/context/context/{room_id}/rerank-status` endpoint
- [x] Comprehensive tests (86 reranking + 14 context integration = 100 test cases)

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
| Phase 4.5: Semantic Code Search | ✅ Complete | Sprint 5 |
| Phase 4.5.2: LiteLLM Unified Embeddings | ✅ Complete | Sprint 5 |
| Phase 4.5.2b: Postgres + Incremental | ✅ Complete | Sprint 5 |
| Phase 4.5.3: RepoMap Graph Context | ✅ Complete | Sprint 5 |
| Phase 4.5.4: Reranking for Code Search | ✅ Complete | Sprint 5 |
| Phase 5: Model B + Advanced | 🟡 Planned | Sprint 6 |
| Phase 6: Production Hardening | 🟡 Planned | Sprint 7 |

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

### ADR-006: CocoIndex over FAISS for code search
**Decision**: Use CocoIndex for AST-aware code chunking and sqlite-vec for vector storage.
**Rationale**: AST-aware chunking produces more meaningful code segments than naive text splitting. sqlite-vec is embedded (no external vector DB infra needed). CocoIndex handles language detection and symbol boundary detection.
**Status**: Implemented in Phase 4.5.

### ADR-007: Cohere Embed v4 as default embedding model
**Decision**: Default to Cohere Embed v4 via AWS Bedrock ($0.12/1M tokens, 128K context).
**Rationale**: Best price-performance ratio. 128K context window handles large code files. Reuses existing AWS Bedrock credentials from AI Summary feature. Titan V2 was considered but has only 8K context at $0.20/1M.
**Status**: Implemented in Phase 4.5.2.

### ADR-008: Pluggable embedding backends
**Decision**: Abstract embedding behind `EmbeddingProvider` with 5 concrete implementations.
**Rationale**: Teams have different cloud provider preferences and API access. Local mode (SentenceTransformers) enables offline/free development. Voyage AI's `voyage-code-3` is code-specialised. Having all options lets teams choose based on cost, latency, and quality.
**Status**: Implemented in Phase 4.5.2.

### ADR-009: Aider-style RepoMap for graph-based context
**Decision**: Use tree-sitter AST parsing + networkx dependency graph + PageRank for file importance ranking.
**Rationale**: Vector search finds semantically similar code but misses structural context. The dependency graph identifies files that are structurally important (heavily imported, central to architecture) even if they don't contain text matching the query. Personalised PageRank bridges the two: bias towards files from vector search, then expand via graph.
**Status**: Implemented in Phase 4.5.3.

### ADR-010: Two-stage reranking with pluggable backends
**Decision**: Add optional reranking as a post-retrieval step with 4 backends (none, cohere, bedrock, cross_encoder).
**Rationale**: Vector search returns approximate results — the embedding compresses semantics into a single vector, losing nuance. A reranker (cross-encoder) sees the full query and document text together, making more precise relevance judgments. Cohere Rerank 3.5 provides excellent quality at $2/1K queries. Bedrock variant reuses existing AWS credentials. Cross-encoder provides a free local option for development. The noop backend allows disabling reranking with zero overhead.
**Status**: Implemented in Phase 4.5.4.

### ADR-011: LiteLLM over hand-written provider classes
**Decision**: Replace 5 hand-written embedding provider classes (Bedrock, OpenAI, Voyage, Mistral, Local) with LiteLLMEmbeddingProvider + LocalEmbeddingProvider.
**Rationale**: Maintaining 5 separate classes with vendor-specific API calls was a maintenance burden. LiteLLM provides a unified `litellm.embedding()` interface for 100+ providers. One config field (`embedding_model`) replaces five. Adding a new provider requires zero code changes — just update the model string. LocalEmbeddingProvider is kept as a zero-cost fallback for development/CI since SentenceTransformers doesn't go through any API. Legacy backward compatibility is maintained via `_legacy_backend_to_model()`.
**Status**: Implemented in Phase 4.5.2.

### ADR-012: Postgres backend with incremental processing
**Decision**: Add Postgres as an alternative storage backend to sqlite-vec, with incremental re-indexing.
**Rationale**: sqlite-vec has single-writer limitation and requires full re-index on every change. Postgres supports concurrent access (needed for multi-instance deployments) and CocoIndex can track file checksums for incremental processing (only re-index changed files). The `is_incremental` property only returns true when both `incremental: true` and `storage_backend: "postgres"` are set, preventing misconfiguration.
**Status**: Implemented in Phase 4.5.2b.

### ADR-013: Unified credential injection with setdefault()
**Decision**: `_inject_embedding_env_vars()` now injects ALL available credentials at startup using `os.environ.setdefault()` instead of only injecting the active backend's credentials with direct assignment.
**Rationale**: With LiteLLM, the active backend is determined by the model string prefix, not a separate `embedding_backend` field. Injecting all available credentials means any model string works without config changes. Using `setdefault()` ensures pre-existing environment variables (from IAM roles, instance profiles, or CI) are never silently overwritten.
**Status**: Implemented in Phase 4.5.2.
