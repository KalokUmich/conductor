# Conducator Architecture Upgrade Proposal

## I. Overview

This upgrade contains two major changes:

1. **Git Workspace Module** — Replaces Live Share with backend-managed Git branches as the core of code collaboration, supporting two authentication modes (Token Direct / Client Delegate), switchable via settings configuration.
2. **CocoIndex Code Integration** — Removes the existing home-built RAG (FAISS + Bedrock Embeddings) and integrates cocoindex-code as the code semantic search engine, configurable via settings.

---

## II. Git Workspace Module

### 2.1 Architecture Overview

```
Backend workspaces directory structure:
workspaces/
├── {repo_hash}/                     # One top-level directory per repo (first 12 chars of SHA256(repo_url))
│   ├── bare.git/                    # bare clone (shared, cloned only once)
│   └── worktrees/
│       ├── {room_id_1}/            # Room 1's git worktree → branch session/{room_id_1}
│       └── {room_id_2}/            # Room 2's git worktree → branch session/{room_id_2}
```

**Key Design Decision: Using git worktree**
- A single bare repo can have multiple independent working directories, each corresponding to a branch
- Full isolation: worktrees do not pollute each other
- Space-efficient: shared .git objects
- Native Git operations: add/commit/push work normally inside a worktree

### 2.2 Authentication Modes (switchable via settings)

#### Mode A: Token Direct (`git_auth_mode: "token"`) — Recommended default

**Flow:**
1. When Host creates a room, the extension prompts the user for Git credentials
2. User provides a GitHub/GitLab Personal Access Token (or obtains one via OAuth Device Flow)
3. Token is transmitted encrypted to the backend over WebSocket
4. Backend stores the Token in memory only (never persisted), associated with the room
5. Backend uses `GIT_ASKPASS` helper (a small subprocess that reads env vars) to perform all git operations
6. When the room is destroyed, the Token is immediately removed from memory

**Security Properties:**
- Token stays only in memory, never written to disk
- Separate GIT_ASKPASS subprocess — Token not exposed to main process's environment directly
- Auto-expire after room lifecycle ends

#### Mode B: Client Delegate (`git_auth_mode: "delegate"`) — Edge Case

**Flow:**
1. Backend does not store any credentials
2. When a git operation requires authentication, the backend sends a `git.auth_required` message to the client over WebSocket
3. The client's extension intercepts this, prompts the user, and returns a one-time token response
4. Backend uses the token only for this single operation, immediately discards

**Security Properties:**
- Backend is stateless regarding credentials
- Suitable for environments where tokens should not be stored server-side (e.g., shared cloud backends)
- Higher latency for authenticated operations

---

## III. CocoIndex Code Integration

### 3.1 Architecture Overview

CocoIndex-Code replaces the current FAISS + Amazon Bedrock home-built RAG:

```
Old: Files → Bedrock Embeddings → FAISS Index → Semantic Search
New: Files → CocoIndex (AST chunking) → sqlite-vec Index → Semantic Search
```

### 3.2 Embedding Backend Options

Configured in `conductor.settings.yaml`:

| Backend | Config Value | Description |
|---------|-------------|-------------|
| Local SentenceTransformers | `local` | CPU/GPU local model, no external API calls |
| Amazon Bedrock | `bedrock` | AWS managed, uses credentials from secrets.yaml |
| OpenAI Embeddings | `openai` | OpenAI API, uses credentials from secrets.yaml |

### 3.3 Credential Reuse Mechanism

CocoIndex cannot directly read `conductor.secrets.yaml`, but it can read from environment variables. The backend handles this transparently:

```python
def _inject_embedding_env_vars(settings: AppSettings) -> None:
    """Inject credentials from secrets into environment variables for CocoIndex."""
    backend = settings.code_search.embedding_backend
    secrets = settings.secrets  # from conductor.secrets.yaml
    
    if backend == "bedrock":
        os.environ["AWS_ACCESS_KEY_ID"] = secrets.aws.access_key_id
        os.environ["AWS_SECRET_ACCESS_KEY"] = secrets.aws.secret_access_key
        os.environ["AWS_DEFAULT_REGION"] = secrets.aws.region
    elif backend == "openai":
        os.environ["OPENAI_API_KEY"] = secrets.openai.api_key
```

This allows CocoIndex to access credentials without requiring any changes to its configuration format.

---

## IV. Configuration Changes

### 4.1 New `conductor.settings.yaml` Sections

```yaml
# Git Workspace Settings
git_workspace:
  enabled: true
  workspaces_dir: "./workspaces"          # Where worktree directories live
  git_auth_mode: "token"                   # "token" (Mode A) or "delegate" (Mode B)
  credential_ttl_seconds: 3600             # Auto-expire credentials after 1 hour
  max_worktrees_per_repo: 20               # Safety limit
  cleanup_on_room_close: true              # Auto-cleanup worktree when room ends

# Code Search Settings  
code_search:
  enabled: true
  index_dir: "./cocoindex_data"            # Where sqlite-vec index lives
  embedding_backend: "local"               # "local", "bedrock", or "openai"
  local_model_name: "all-MiniLM-L6-v2"   # SentenceTransformers model name
  bedrock_model_id: "amazon.titan-embed-text-v2:0"  # If using bedrock
  openai_model_name: "text-embedding-3-small"        # If using openai
  chunk_size: 512                          # Max tokens per chunk
  top_k_results: 5                         # Number of results to return
```

### 4.2 Changes to `config.py`

New Pydantic models added:

```python
class GitWorkspaceSettings(BaseModel):
    enabled: bool = True
    workspaces_dir: str = "./workspaces"
    git_auth_mode: Literal["token", "delegate"] = "token"
    credential_ttl_seconds: int = 3600
    max_worktrees_per_repo: int = 20
    cleanup_on_room_close: bool = True

class CodeSearchSettings(BaseModel):
    enabled: bool = True
    index_dir: str = "./cocoindex_data"
    embedding_backend: Literal["local", "bedrock", "openai"] = "local"
    local_model_name: str = "all-MiniLM-L6-v2"
    bedrock_model_id: str = "amazon.titan-embed-text-v2:0"
    openai_model_name: str = "text-embedding-3-small"
    chunk_size: int = 512
    top_k_results: int = 5
```

### 4.3 Changes to `main.py`

```python
# In lifespan startup:
if settings.git_workspace.enabled:
    await git_workspace_service.initialize(settings.git_workspace)
    logger.info("Git Workspace module initialized")

if settings.code_search.enabled:
    _inject_embedding_env_vars(settings)  # Inject credentials into env
    await code_search_service.initialize(settings.code_search)
    logger.info("CocoIndex Code Search initialized")

# Removed: old RAG/embeddings initialization
# Removed: faiss_index, bedrock_embeddings module imports
```

### 4.4 Changes to `context/router.py`

```python
# Old:
async def get_context(room_id: str, query: str):
    results = await rag_service.search(query, room_id=room_id)
    return format_rag_results(results)

# New:
async def get_context(room_id: str, query: str):
    results = await code_search_service.search(
        query=query,
        workspace_path=git_workspace_service.get_worktree_path(room_id)
    )
    return format_cocoindex_results(results)
```

---

## V. New Module File Structure

```
backend/app/
├── git_workspace/
│   ├── __init__.py
│   ├── schemas.py           # Pydantic models for requests/responses
│   ├── service.py           # Core git worktree operations
│   ├── router.py            # FastAPI routes for git workspace management
│   └── delegate_broker.py   # Mode B: WebSocket credential delegation broker
└── code_search/
    ├── __init__.py
    ├── schemas.py           # Pydantic models for search requests/results
    ├── service.py           # CocoIndex integration & search logic
    └── router.py            # FastAPI routes for code search
```

---

## VI. Dependencies

New dependencies to add to `requirements.txt` / `pyproject.toml`:

```
cocoindex-code>=0.3.0
sqlite-vec>=0.1.0
sentence-transformers>=2.6.0   # For local embedding backend
gitpython>=3.1.40              # For git operations in service.py
```

Removed dependencies:
```
faiss-cpu                      # Replaced by sqlite-vec
langchain-community            # If only used for RAG
```

---

## VII. Migration Steps

1. **Install new dependencies**: `pip install cocoindex-code sqlite-vec sentence-transformers gitpython`
2. **Update `conductor.settings.yaml`**: Add the new `git_workspace` and `code_search` sections
3. **Update `conductor.secrets.yaml`**: Verify existing AWS/OpenAI credentials are present if needed
4. **Run database migration** (if applicable): CocoIndex will auto-create its sqlite-vec index on first run
5. **Restart backend**: The lifespan handler will auto-initialize both modules
6. **Verify**: Check `/api/git-workspace/health` and `/api/code-search/health` endpoints

---

## VIII. Rollback Plan

If issues are found post-deployment:

1. Set `git_workspace.enabled: false` and `code_search.enabled: false` in settings
2. Re-enable `live_share` and old `rag` modules (previously they would be preserved but disabled)
3. Restart backend

No database migrations are required — the new sqlite-vec index is additive and separate from existing data stores.
