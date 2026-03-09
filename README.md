# Conductor

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor is a VS Code collaboration extension plus a FastAPI backend for team chat, Git workspace management, file sharing, and AI-assisted decision/code workflows.

### Current Capabilities

- VS Code WebView collaboration panel with FSM-driven session lifecycle:
  - `Idle`
  - `BackendDisconnected` (join-only mode)
  - `ReadyToHost`
  - `CreatingWorkspace` (provisioning a Git worktree on the backend)
  - `Hosting`
  - `Joining`
  - `Joined`
- Git Workspace management replacing Live Share:
  - Per-room bare repo + worktree isolation (each room gets its own Git branch `session/{room_id}`)
  - Mode A: token authentication via GIT_ASKPASS (user provides a Personal Access Token)
  - Mode B: delegate authentication (VS Code extension performs Git operations on behalf of the backend)
  - File-sync broadcast with debouncing; commit and push from backend
  - **FileSystemProvider** (`conductor://` URI scheme): remote worktree files appear in VS Code explorer as if local; full read/write/delete/rename support backed by the backend REST API
  - **WorkspacePanel**: 5-step native VS Code input wizard for workspace creation (no WebView)
  - **WorkspaceClient**: typed HTTP client for all `/workspace/` endpoints
- Real-time WebSocket chat with:
  - reconnect recovery (`since`)
  - typing indicators
  - read receipts
  - message deduplication
  - paginated history
- File upload/download (20MB limit, extension-host upload proxy, duplicate detection, retry logic)
- Code snippet sharing + editor navigation
- Change review workflow:
  - `POST /generate-changes` (MockAgent)
  - policy check (`POST /policy/evaluate-auto-apply`)
  - per-change diff preview
  - sequential apply/skip
  - audit logging (`POST /audit/log-apply`)
- AI provider workflow:
  - provider health/status (`GET /ai/status`)
  - four-step provider selection + confirmation UI
  - streaming inference (`POST /ai/infer`)
- **Semantic Code Search (CocoIndex)**:
  - AST-aware code chunking + embedding + vector storage (sqlite-vec or Postgres)
  - LiteLLM unified embedding: 100+ providers via one config field (`embedding_model`)
  - Default: Cohere Embed v4 via AWS Bedrock ($0.12/1M tokens, 128K context)
  - Postgres backend with incremental processing (only re-index changed files)
  - Per-workspace index management
- **RepoMap (Graph-Based Context)**:
  - tree-sitter AST parsing for symbol extraction (regex fallback)
  - File dependency graph (networkx) with PageRank ranking
  - Hybrid retrieval: vector search + graph-based repo map
  - Personalised PageRank biased towards query-relevant files
- **Reranking (Post-Retrieval Precision)**:
  - 4 configurable reranking backends: none (default), cohere (Rerank 3.5), bedrock (Cohere on AWS), cross_encoder (local)
  - Two-stage retrieval: vector search → rerank → top-N for improved precision
  - Optional per-request enable/disable with graceful fallback
- Workspace code search:
  - `GET /workspace/{room_id}/search?q=...` — full-text search across all files in a session worktree
  - results include file path, line number, and matched line content
  - VS Code extension `WorkspaceClient.searchCode()` method
  - keyboard shortcut `Ctrl+Shift+F` / `Cmd+Shift+F` opens inline search panel in WebView

### Architecture

```
extension/          VS Code extension (TypeScript)
  src/
    panels/         WebView panels (CollabPanel, WorkspacePanel)
    services/       FSM, WebSocket, FileSystemProvider, WorkspaceClient
    commands/       VS Code command handlers
backend/            FastAPI server (Python)
  app/
    git_workspace/  Git worktree management (Model A/B)
    code_search/    CocoIndex + EmbeddingProvider + RerankProvider abstraction
    repo_graph/     tree-sitter + networkx + PageRank
    context/        Hybrid retrieval (vector + rerank + graph)
    config.py       Settings + Secrets from YAML
    main.py         App factory + lifespan
  config/           YAML config templates
  tests/            pytest test suite (320+ new tests)
```

### Embedding Models (LiteLLM format)

| Model String | Provider | Dimensions | Cost/1M | Context |
|-------------|----------|------------|---------|---------|
| `sbert/sentence-transformers/all-MiniLM-L6-v2` | Local | 384 | Free | — |
| `bedrock/cohere.embed-v4:0` | AWS Bedrock | 1024 | $0.12 | 128K |
| `text-embedding-3-small` | OpenAI | 1536 | $0.02 | 8K |
| `voyage/voyage-code-3` | Voyage AI | 1024 | $0.06 | 16K |
| `mistral/codestral-embed-2505` | Mistral | 1024 | — | — |
| `cohere/embed-english-v3.0` | Cohere | 1024 | $0.10 | — |
| `gemini/text-embedding-004` | Google | 768 | — | — |

### Reranking Backend Options

| Backend | Model | Cost/1K | Notes |
|---------|-------|---------|-------|
| `none` | — | Free | Default, passthrough |
| `cohere` | rerank-v3.5 | $2.00 | Direct Cohere API |
| `bedrock` | cohere.rerank-v3-5:0 | $2.00 | Reuses AWS creds |
| `cross_encoder` | ms-marco-MiniLM-L-6-v2 | Free | Local, ~80 MB |

Switch backends in `conductor.settings.yaml`:
```yaml
code_search:
  embedding_model: "bedrock/cohere.embed-v4:0"  # Any LiteLLM model string
  storage_backend: "sqlite"                      # sqlite | postgres
  incremental: true                              # Only with postgres
  rerank_backend: "none"                         # none | cohere | bedrock | cross_encoder
```

### Quick Start

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload

# Extension
cd extension
npm install
npm run compile
# Press F5 in VS Code to launch Extension Development Host
```

### Running Tests

```bash
cd backend
pytest                                           # all tests
pytest tests/test_embedding_provider.py -v       # embedding tests (85+)
pytest tests/test_rerank_provider.py -v          # reranking tests (86)
pytest tests/test_repo_graph.py -v               # repo graph tests (72)
pytest tests/test_config_new.py -v               # config tests (60+)
pytest tests/test_code_search.py -v              # code search tests (72+)
pytest tests/test_context.py -v                  # context router tests (42+)
```

### Documentation

- [Architecture](docs/ARCHITECTURE.md) — system components and data flow
- [Backend Guide](docs/GUIDE.md) — code walkthrough for junior engineers
- [Guide Addendum](docs/GUIDE_ADDENDUM.md) — embedding providers, RepoMap, and reranking
- [Testing](TESTING.md) — comprehensive test guide (EN + 中文)
- [Roadmap](ROADMAP.md) — project phases and ADRs
- [Claude](CLAUDE.md) — guide for AI coding assistants

---

<a name="中文"></a>
## 中文

Conductor 是一个 VS Code 协作扩展 + FastAPI 后端，用于团队聊天、Git 工作区管理、文件共享和 AI 辅助代码工作流。

### 当前功能

- **语义代码搜索**: CocoIndex AST 感知分块 + LiteLLM 统一 embedding (100+ 提供商)
  - 默认: Cohere Embed v4 (AWS Bedrock, $0.12/百万 token, 128K 上下文)
  - 本地: SentenceTransformers (免费, 无需 API 密钥)
  - 存储: sqlite-vec (默认) 或 Postgres (增量处理，生产环境)
- **RepoMap 图上下文**: tree-sitter AST 解析 + networkx 依赖图 + PageRank 排名
- **重排序 (Reranking)**: 4 种可配置后端 (none / cohere / bedrock / cross_encoder)
  - 两阶段检索: 向量搜索 → 重排序 → top-N，提高搜索精度
  - Cohere Rerank 3.5 (API 或 Bedrock) + 本地 cross-encoder
- **混合检索**: 向量搜索 + 重排序 + 图搜索组合, 个性化 PageRank
- Git 工作区管理 (替代 Live Share)
- 实时 WebSocket 聊天
- 文件上传/下载
- AI 提供者集成
- 变更审查工作流

### Embedding 配置

在 `conductor.settings.yaml` 中切换:
```yaml
code_search:
  embedding_model: "bedrock/cohere.embed-v4:0"  # LiteLLM 格式模型字符串
  storage_backend: "sqlite"                      # sqlite | postgres
  incremental: true                              # 仅 postgres 生效
  rerank_backend: "none"                         # none | cohere | bedrock | cross_encoder
```

密钥在 `conductor.secrets.yaml` 中配置:
```yaml
aws:
  access_key_id: "AKIA..."
  secret_access_key: "..."
openai:
  api_key: "sk-..."
voyage:
  api_key: "pa-..."
mistral:
  api_key: "..."
cohere:
  api_key: "..."
```

### 测试

```bash
cd backend
pytest                                           # 所有测试
pytest tests/test_embedding_provider.py -v       # embedding 测试 (85+ 项)
pytest tests/test_rerank_provider.py -v          # reranking 测试 (86 项)
pytest tests/test_repo_graph.py -v               # 图测试 (72 项)
pytest tests/test_code_search.py -v              # 代码搜索测试 (72+ 项)
pytest tests/test_context.py -v                  # 上下文路由测试 (42+ 项)
```
