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
│   ├── code_search/                 # CocoIndex semantic code search
│   │   ├── service.py               # CodeSearchService (sqlite + postgres backends)
│   │   ├── embedding_provider.py    # LiteLLM unified + Local SentenceTransformers
│   │   ├── rerank_provider.py       # 4 reranking backends (abstract + concrete)
│   │   ├── schemas.py               # Request/response Pydantic models
│   │   └── router.py                # /api/code-search/ endpoints
│   ├── repo_graph/                  # RepoMap graph-based context
│   │   ├── parser.py                # tree-sitter AST + regex fallback
│   │   ├── graph.py                 # networkx dependency graph + PageRank
│   │   └── service.py               # RepoMapService (map generation, caching)
│   └── context/
│       └── router.py                # /api/context/ hybrid retrieval + reranking
├── config/
│   └── conductor.settings.yaml      # Non-secret settings template
├── requirements.txt
└── tests/
    ├── conftest.py                  # Centralized stubs (cocoindex, litellm, etc.)
    ├── test_embedding_provider.py   # 85+ tests — LiteLLM + Local providers
    ├── test_rerank_provider.py      # 86 tests — all 4 reranking backends
    ├── test_repo_graph.py           # 72 tests — parser + graph + service
    ├── test_config_new.py           # 60+ tests — config + secrets + env vars
    ├── test_context.py              # 42+ tests — context router + hybrid + reranking
    ├── test_code_search.py          # 72+ tests — code search service + router
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

### Embedding Provider Architecture

The `embedding_provider.py` module defines an `EmbeddingProvider` ABC with 2 implementations:

- **`LocalEmbeddingProvider`** — SentenceTransformers, triggered by `sbert/` model prefix
- **`LiteLLMEmbeddingProvider`** — unified provider for 100+ backends via LiteLLM

Common model strings:

| Model String | Provider | Dimensions | Cost/1M |
|-------------|----------|------------|------|
| `sbert/sentence-transformers/all-MiniLM-L6-v2` | Local | 384 | Free |
| `bedrock/cohere.embed-v4:0` | AWS Bedrock | 1024 | $0.12 |
| `text-embedding-3-small` | OpenAI | 1536 | $0.02 |
| `voyage/voyage-code-3` | Voyage AI | 1024 | $0.06 |
| `mistral/codestral-embed-2505` | Mistral | 1024 | — |

Default: **bedrock/cohere.embed-v4:0**.

### Reranking Provider Architecture

The `rerank_provider.py` module defines a `RerankProvider` ABC with 4 implementations:

| Provider | Default Model | Cost | Notes |
|----------|--------------|------|-------|
| `none` | — | Free | Passthrough (no reranking) |
| `cohere` | `rerank-v3.5` | $2/1K queries | Direct Cohere API |
| `bedrock` | `cohere.rerank-v3-5:0` | $2/1K queries | Reuses AWS creds |
| `cross_encoder` | `ms-marco-MiniLM-L-6-v2` | Free | Local, ~80 MB |

Default: **none** (disabled). Enable for better search precision.

Configuration in `conductor.settings.yaml`:
```yaml
code_search:
  embedding_model: "bedrock/cohere.embed-v4:0"  # Any LiteLLM model string
  storage_backend: "sqlite"                      # sqlite | postgres
  incremental: true                              # Only with postgres
  rerank_backend: "none"                         # none | cohere | bedrock | cross_encoder
  rerank_top_n: 5
  rerank_candidates: 20
```

Credentials in `conductor.secrets.yaml`:
```yaml
# All available credentials injected at startup via setdefault()
aws:
  access_key_id: "AKIA..."
  secret_access_key: "..."
  region: "us-east-1"
openai:
  api_key: "sk-..."
voyage:
  api_key: "pa-..."
mistral:
  api_key: "..."
cohere:
  api_key: "..."                   # For Cohere Rerank and/or Embed
```

### RepoMap Architecture

The `repo_graph/` module implements Aider-style repository mapping:

1. **Parser** (`parser.py`): Extract symbol definitions and references from source files using tree-sitter AST parsing (with regex fallback)
2. **Graph** (`graph.py`): Build a directed dependency graph (file A → file B means A references symbols defined in B). Uses networkx for storage and PageRank computation
3. **Service** (`service.py`): `RepoMapService` generates text-based repo maps showing top-ranked files and their symbols

**Hybrid retrieval** in `context/router.py`:
- Vector search (CocoIndex) finds semantically similar code
- Reranking (optional) re-scores candidates for better precision
- Graph search (PageRank) finds structurally important files
- PageRank is personalised: biased towards files from vector search

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

### EmbeddingProvider Pattern
```python
from backend.app.code_search.embedding_provider import create_embedding_provider

# Factory routes: sbert/ → Local, everything else → LiteLLM
provider = create_embedding_provider(settings)
vectors = await provider.embed_texts(["def main(): pass"])  # batch embed
query_vec = await provider.embed_query("search for main")   # query embed
```

### RerankProvider Pattern
```python
from backend.app.code_search.rerank_provider import create_rerank_provider

reranker = create_rerank_provider(settings)  # factory
results = await reranker.rerank(
    query="how does authentication work",
    documents=["chunk1...", "chunk2...", ...],
    top_n=5,
)
# results: List[RerankResult] sorted by relevance score (descending)
```

### RepoMapService Pattern
```python
from backend.app.repo_graph.service import RepoMapService

svc = RepoMapService(top_n=10)
graph = svc.build_graph("/path/to/workspace")        # build or cache
ranked = svc.get_ranked_files("/path/to/workspace")   # PageRank ranking
repo_map = svc.generate_repo_map("/path/to/workspace")  # text map
files = svc.get_context_files(ws, vector_files)       # hybrid merge
```

### Config Pattern
```python
from backend.app.config import load_settings, _inject_embedding_env_vars

settings = load_settings()                  # loads YAML files
_inject_embedding_env_vars(settings)        # pushes ALL available secrets → env vars
# Uses os.environ.setdefault() — never overwrites existing env vars
```

## Testing Notes

- Backend tests use `pytest` with mocked external dependencies
- Centralized stubs in `conftest.py` for cocoindex, litellm, sentence_transformers, sqlite_vec
- LiteLLM embedding provider tested with mocked `litellm.embedding()` calls
- Local embedding provider tested with mocked SentenceTransformer
- All reranking providers are tested with mocked API clients
- RepoMap tests use real filesystem operations for parser/graph tests
- tree-sitter and networkx are mocked in import stubs
- Config tests verify env var injection via `setdefault()` for all credential types
- Code search tests cover sqlite and postgres backends, incremental processing

## Environment Variables

```bash
# Backend
BACKEND_HOST=0.0.0.0
BACKEND_PORT=8000
GIT_WORKSPACE_ROOT=/tmp/conductor_workspaces

# Credentials (all injected by _inject_embedding_env_vars via setdefault)
AWS_ACCESS_KEY_ID=...            # Bedrock models
AWS_SECRET_ACCESS_KEY=...        # Bedrock models
AWS_DEFAULT_REGION=us-east-1     # Bedrock models
OPENAI_API_KEY=sk-...            # OpenAI models
VOYAGE_API_KEY=pa-...            # Voyage models
MISTRAL_API_KEY=...              # Mistral models
CO_API_KEY=...                   # Cohere (rerank + embed)

# CocoIndex (set by service.py)
COCOINDEX_CODE_EMBEDDING_MODEL=bedrock/cohere.embed-v4:0
COCOINDEX_DATABASE_URL=postgresql://...  # Only when storage_backend=postgres

# Extension (VS Code settings)
conductor.backendUrl=http://localhost:8000
conductor.enableWorkspace=true
```

## Recent Changes

- **LiteLLM Unified Embeddings** — replaced 5 hand-written provider classes with `LiteLLMEmbeddingProvider` + `LocalEmbeddingProvider`, supporting 100+ providers via one config field (`embedding_model`)
- **Postgres Backend + Incremental Processing** — `storage_backend: "postgres"` for production-ready vector storage with incremental re-indexing
- **Unified Credential Injection** — `_inject_embedding_env_vars()` now injects ALL available secrets via `setdefault()` (never overwrites existing env vars)
- **Legacy Backward Compatibility** — `_legacy_backend_to_model()` maps old `embedding_backend` values to LiteLLM model strings
- **Reranking** — 4 configurable reranking backends (`RerankProvider` abstraction) integrated into the context router
- **RepoMap** — tree-sitter + networkx graph + PageRank for graph-based context
- **Hybrid retrieval** — vector search + reranking + graph search combined in context router
- 400+ backend test cases

## What's Next

See [ROADMAP.md](ROADMAP.md) for planned features. Current focus:
- Model B delegate authentication
- Conflict resolution for concurrent edits
- Enterprise features (room access control, audit export)
