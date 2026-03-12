# Conductor 后端代码指南 / Backend Code Walkthrough

**面向工程师的 Conductor 后端深度解读**
**A Deep Dive Into the Conductor Backend for Engineers**

---

*本指南同时提供中英文说明。每个小节先给出英文，再附中文注解。*
*This guide provides both English and Chinese explanations. Each section shows English first, then a Chinese annotation block.*

---

## Table of Contents / 目录

1. [Project Layout / 项目结构](#1-project-layout)
2. [Entry Point: main.py / 入口](#2-entry-point-mainpy)
3. [Chat System / 聊天系统](#3-chat-system)
4. [Agentic Code Intelligence / 智能代码分析](#4-agentic-code-intelligence)
5. [AI Provider Integration / AI 提供商集成](#5-ai-provider-integration)
6. [Git Workspace Management / Git 工作区管理](#6-git-workspace-management)
7. [File Sharing / 文件共享](#7-file-sharing)
8. [Audit & Todos / 审计与任务追踪](#8-audit--todos)
9. [Authentication / 身份认证](#9-authentication)
10. [LangExtract Integration / LangExtract 集成](#10-langextract-integration)
11. [Testing Patterns / 测试模式](#11-testing-patterns)
12. [Deployment Notes / 部署说明](#12-deployment-notes)
13. [Contributing / 贡献指南](#13-contributing)


---

## 1. Project Layout

```
backend/
├── app/
│   ├── main.py                    # App factory, lifespan, router registration
│   ├── config.py                  # Settings + Secrets from YAML, env injection
│   ├── agent_loop/                # Agentic code intelligence engine
│   │   ├── service.py             # AgentLoopService — LLM loop + tool dispatch
│   │   ├── budget.py              # BudgetController — token-based budget management
│   │   ├── trace.py               # SessionTrace — per-session JSON trace
│   │   ├── query_classifier.py    # QueryClassifier — keyword + LLM classification
│   │   ├── evidence.py            # EvidenceEvaluator — answer quality gate
│   │   ├── prompts.py             # 3-layer system prompt (Core Identity + Strategy + Runtime)
│   │   └── router.py              # POST /api/context/query (+ /stream)
│   ├── code_tools/                # 21 code intelligence tools
│   │   ├── tools.py               # Tool implementations (grep, AST, call graph, git, compressed view)
│   │   ├── schemas.py             # Pydantic models + TOOL_DEFINITIONS for LLM
│   │   ├── output_policy.py       # Per-tool truncation policies (budget-adaptive)
│   │   └── router.py              # /api/code-tools/ direct endpoints
│   ├── ai_provider/               # LLM provider abstraction layer
│   │   ├── base.py                # AIProvider ABC + ToolCall/ToolUseResponse/TokenUsage
│   │   ├── claude_bedrock.py      # AWS Bedrock Converse API (+ chat_with_tools)
│   │   ├── claude_direct.py       # Anthropic Messages API (+ chat_with_tools)
│   │   ├── openai_provider.py     # OpenAI Chat Completions (+ chat_with_tools)
│   │   └── resolver.py            # ProviderResolver — health checks, selection
│   ├── chat/                      # WebSocket + HTTP chat endpoints
│   │   ├── router.py              # WebSocket handler, HTTP history/AI message
│   │   ├── manager.py             # Room state, user list, broadcast
│   │   └── stack_trace_parser.py  # Parse exception stack traces
│   ├── git_workspace/             # Git worktree management
│   │   ├── service.py             # GitWorkspaceService
│   │   ├── delegate_broker.py     # DelegateBroker (Model B prep)
│   │   └── router.py              # /api/git-workspace/ endpoints
│   ├── langextract/               # LangExtract + multi-vendor Bedrock integration
│   │   ├── provider.py            # BedrockLanguageModel — all Bedrock vendors
│   │   ├── claude_provider.py     # Backwards-compat re-exports
│   │   ├── catalog.py             # BedrockCatalog — dynamic model discovery
│   │   ├── service.py             # LangExtractService async wrapper
│   │   └── router.py              # GET /api/langextract/models
│   ├── repo_graph/                # AST symbol graph (used by code tools)
│   │   ├── parser.py              # tree-sitter AST + regex fallback
│   │   ├── graph.py               # networkx dependency graph + PageRank
│   │   └── service.py             # RepoMapService (map generation, caching)
│   ├── files/                     # File upload/download (DuckDB metadata)
│   ├── audit/                     # DuckDB audit log (apply/skip events)
│   ├── todos/                     # DuckDB-backed TODO tracker per room
│   ├── auth/                      # AWS SSO + Google SSO
│   ├── agent/                     # MockAgent + style-driven code generation
│   ├── policy/                    # Auto-apply policy evaluation
│   └── workspace_files/           # Per-workspace file CRUD endpoints
├── config/
│   ├── conductor.settings.yaml    # Non-secret settings template
│   └── conductor.secrets.yaml     # API keys (gitignored)
├── requirements.txt
└── tests/
    ├── conftest.py                # Centralized stubs (cocoindex, litellm, etc.)
    ├── test_code_tools.py         # 98 tests — all 21 tools + dispatcher + multi-language
    ├── test_agent_loop.py         # 39 tests — agent loop + 3-layer prompt + workspace layout
    ├── test_budget_controller.py  # 20 tests — token budget signals, tracking, edge cases
    ├── test_session_trace.py      # 15 tests — SessionTrace, IterationTrace, save/load
    ├── test_evidence.py           # 14 tests — evidence evaluator
    ├── test_symbol_role.py        # 24 tests — symbol role classification + sorting
    ├── test_output_policy.py      # 19 tests — per-tool truncation, budget adaptation
    ├── test_query_classifier.py   # 26 tests — keyword + LLM classification, dynamic tool sets
    ├── test_compressed_tools.py   # 24 tests — compressed_view, module_summary, expand_symbol
    ├── test_langextract.py        # 57 tests — Bedrock provider, catalog, service, router
    ├── test_repo_graph.py         # 72 tests — parser + graph + service
    ├── test_config_new.py         # 27 tests — config + secrets
    └── test_git_workspace.py      # Git workspace lifecycle
```

**Why this layout? / 为什么这样组织代码？**

FastAPI encourages separating route handlers (routers) from business logic (services). Routers handle HTTP concerns; services handle domain logic.

FastAPI 鼓励将路由处理器（routers）与业务逻辑（services）分离。路由只负责 HTTP 层面的事务，服务层负责领域逻辑（调用 Git、调用 AI API、管理状态）。

---

## 2. Entry Point: main.py

The lifespan function runs on startup and shutdown:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = load_settings()

    # Git Workspace
    git_service = GitWorkspaceService()
    if settings.git_workspace.enabled:
        await git_service.initialize(settings.git_workspace)
    app.state.git_workspace_service = git_service

    # AI Provider Resolver (powers agent loop, summaries, etc.)
    resolver = ProviderResolver(get_config())
    resolver.resolve()
    set_resolver(resolver)
    app.state.agent_provider = resolver.get_active_provider()

    # Ngrok tunnel (optional — for VS Code Remote-WSL)
    if ngrok_cfg.get("enabled"):
        start_ngrok(port=settings.server.port, ...)

    yield   # ← app is running here
    stop_ngrok()
    await git_service.shutdown()
```

**What's happening here? / 发生了什么？**

1. **Git Workspace** — clones repos and creates worktrees for collaborative sessions.
2. **ProviderResolver** — health-checks all configured LLM providers (Bedrock, Anthropic, OpenAI) and selects the best one. The selected provider powers the agent loop and chat summaries.
3. **Ngrok** — optional tunnel so that VS Code WebView running in Windows (Remote-WSL) can reach the backend on WSL localhost.

**Private Network Access (PNA) middleware** — Chrome 105+ blocks `vscode-webview://` origins from fetching `localhost`. A pure ASGI middleware (not `BaseHTTPMiddleware`, which would break WebSocket upgrades) injects `Access-Control-Allow-Private-Network: true` into every response.

**中文说明:** `ProviderResolver` 在启动时对所有配置的 LLM 提供商做健康检查（延迟测量），选出最优的一个。后续的 agent loop 和 AI 摘要功能都使用这个 provider，无需手动切换。

---

## 3. Chat System / 聊天系统

### 3.1 Room Model

Each collaboration session is a **room** identified by a `room_id` string. The `ConnectionManager` in `chat/manager.py` tracks all active connections and in-memory message history.

```python
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}
        self.room_messages: dict[str, list[dict]] = {}

    async def connect(self, room_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self.active_connections.setdefault(room_id, []).append(ws)

    async def broadcast(self, room_id: str, message: dict) -> None:
        for ws in self.active_connections.get(room_id, []):
            await ws.send_json(message)
```

**中文说明:** 每个房间用 `room_id` 标识。`ConnectionManager` 是全局单例，维护所有 WebSocket 连接和房间内的消息历史。

### 3.2 WebSocket Endpoint

```python
@router.websocket("/api/chat/ws/{room_id}")
async def websocket_endpoint(ws: WebSocket, room_id: str, user_id: str):
    await manager.connect(room_id, ws)
    try:
        while True:
            data = await ws.receive_json()
            await route_message(room_id, user_id, data, ws)
    except WebSocketDisconnect:
        manager.disconnect(room_id, ws)
        await manager.broadcast(room_id, {"type": "user_left", "user_id": user_id})
```

### 3.3 Message Types / 消息类型

The system supports a rich set of message types beyond plain text:

| Type | Description |
|------|-------------|
| `chat` | Plain text chat message |
| `code_snippet` | Code block with language, filename |
| `stack_trace` | Exception stack trace (parsed by `stack_trace_parser.py`) |
| `test_failure` | Test runner failure output |
| `ai_message` | AI-generated response (injected via HTTP, not WS) |
| `lead_transfer` | Transfer lead role to another participant |
| `user_joined` / `user_left` | Presence events |
| `typing` | Typing indicator |
| `read_receipt` | Message seen acknowledgement |

**中文说明:** 系统支持丰富的消息类型。`code_snippet` 用于共享代码片段；`stack_trace` 可解析异常堆栈并高亮相关帧；`ai_message` 是后端通过 HTTP POST 注入的 AI 回复，不经过 WebSocket 通道。

### 3.4 AI Messages

AI responses are injected into the room via a dedicated HTTP endpoint (not WebSocket), so the backend can take time to generate them:

```python
@router.post("/api/chat/{room_id}/ai-message")
async def post_ai_message(room_id: str, req: AiMessageRequest):
    """Generate and broadcast an AI response to the room."""
    provider = get_resolver().get_active_provider()
    response = await provider.chat(messages=req.context_messages)
    msg = {"type": "ai_message", "content": response.text, "model": response.model_id}
    await manager.broadcast(room_id, msg)
    manager.append_history(room_id, msg)
    return {"status": "sent"}
```

### 3.5 Chat History / 聊天历史

On reconnect, the extension fetches missed messages:

```python
@router.get("/api/chat/{room_id}/history")
async def get_history(room_id: str, since: Optional[str] = None, limit: int = 50):
    messages = manager.get_history(room_id)
    if since:
        since_dt = datetime.fromisoformat(since)
        messages = [m for m in messages if m["timestamp"] > since_dt.isoformat()]
    return messages[-limit:]
```

---

## 4. Agentic Code Intelligence / 智能代码分析

This is the core innovation of the current architecture. Instead of a traditional RAG (retrieval-augmented generation) pipeline with vector embeddings, Conductor uses an **LLM agent loop** that iteratively navigates the codebase using tools.

### 4.1 Overview / 总体架构

```
User query ("How does auth work?")
       ↓
QueryClassifier (keyword or LLM-based)
  → query_type, strategy hint, dynamic tool_set (8-12 of 21)
       ↓
3-Layer System Prompt:
  L1: Core Identity (always) — hard constraints, exploration pattern
  L2: Strategy (per query type) — e.g. "Business Flow Tracing"
  L3: Runtime Guidance (dynamic) — budget, scatter, convergence
       ↓
AgentLoopService.run(query, workspace_path)  [up to 25 iterations / 500K tokens]
       ↓
  ┌───────────────────────────────────────────────┐
  │ LLM decides which tools to call               │
  │   ↓                                           │
  │ Tool execution (grep, read_file, etc.)        │
  │   ↓                                           │
  │ BudgetController.track(usage)                 │
  │   → NORMAL / WARN_CONVERGE / FORCE_CONCLUDE   │
  │   ↓                                           │
  │ Results + budget context → LLM                │
  └───────────────────────────────────────────────┘
       ↓
EvidenceEvaluator — rejects weak answers, forces re-investigation
       ↓
AgentResult(answer, context_chunks, tool_calls_made, budget_summary)
```

**中文说明:** 传统 RAG 流程是一次性检索 + 生成，无法处理"先找到函数定义，再跟踪其调用链"这类需要多步推理的问题。Agentic 方式让 LLM 自主决定每一步要调用哪个工具，像人类工程师一样逐步探索代码库。QueryClassifier 将查询分为 7 种类型并选出最优工具子集；BudgetController 追踪 token 用量并发出三级信号；EvidenceEvaluator 确保答案有具体文件引用才允许输出。

### 4.2 The 21 Code Tools / 21 个代码工具

All tools live in `code_tools/tools.py` and share a common `execute_tool(name, workspace, params)` dispatcher. The agent sees only the **dynamically selected subset** (8-12 tools) for its query type:

| Tool | Purpose |
|------|---------|
| `grep` | Regex search across files (ripgrep, excludes `.git`/`node_modules`) |
| `read_file` | Read file content with optional line range |
| `list_files` | Directory tree with depth/glob filter |
| `find_symbol` | AST-based symbol definition search with role classification |
| `find_references` | Find symbol usages (grep + AST validation) |
| `file_outline` | All definitions in a file with line numbers |
| `get_dependencies` | Files this file imports |
| `get_dependents` | Files that import this file |
| `git_log` | Recent commits, optionally per-file |
| `git_diff` | Diff between two git refs |
| `ast_search` | Structural AST search via ast-grep (`$VAR`, `$$$MULTI` patterns) |
| `get_callees` | Functions/methods called within a specific function body |
| `get_callers` | Functions/methods that call a given function (cross-file) |
| `git_blame` | Per-line authorship with commit hash, author, date |
| `git_show` | Full commit details (message + diff) |
| `find_tests` | Test functions covering a given function/class |
| `test_outline` | Test file structure with mocks, assertions, fixtures |
| `trace_variable` | Data flow tracing: aliases, arg→param mapping, sink/source patterns |
| `compressed_view` | File signatures + call relationships + side effects (~80% token savings) |
| `module_summary` | Module-level summary: services, models, functions (~95% savings) |
| `expand_symbol` | Expand a compressed symbol to full source code |

**中文说明:** `ast_search` 使用 ast-grep CLI 进行结构化 AST 查询，支持模式变量（`$VAR` 匹配任意节点）。`get_callers`/`get_callees` 实现了函数级调用图，可跨文件追踪函数调用关系。`compressed_view` 和 `module_summary` 用签名代替函数体，可节省 80-95% token；`trace_variable` 支持多跳数据流追踪（HTTP 入参 → SQL WHERE 子句）。

### 4.3 QueryClassifier / 查询分类器

`query_classifier.py` classifies each query into one of **7 types** using keyword matching (fast) or optional LLM pre-classification (Haiku):

| Type | Example Phrases | Strategy |
|------|----------------|----------|
| `architecture` | "how is X structured", "overview" | module_summary first |
| `bug_root_cause` | "why does X fail", "root cause" | git + call graph |
| `feature_implementation` | "how to add", "implement" | file outline + grep |
| `code_review` | "review", "code smell" | find_references + test |
| `explanation` | "explain", "what does X do" | compressed_view + read |
| `test_coverage` | "test", "coverage" | find_tests + test_outline |
| `general` | (default) | balanced mix |

Each type selects 8-12 tools from the full 21. This reduces hallucinated tool calls and token waste.

### 4.4 BudgetController / Token 预算控制器

`budget.py` tracks cumulative token usage and emits signals to the agent loop:

- **NORMAL** — below 70% of the 500K token budget
- **WARN_CONVERGE** — at 70% or diminishing returns detected. Broad searches (grep, find_symbol) are blocked; only verification calls allowed.
- **FORCE_CONCLUDE** — at 90% or max 25 iterations reached. Agent must produce a final answer immediately.

The LLM sees a compact budget context string each turn so it can self-regulate.

### 4.5 EvidenceEvaluator / 证据评估器

`evidence.py` acts as a quality gate before the agent finalises its answer. It rejects answers that:

- Have no `file:line` references (e.g., `src/auth.py:42`)
- Were produced with fewer than 2 tool calls
- Accessed no files during the loop

If budget remains, the evaluator forces the LLM to investigate further. At FORCE_CONCLUDE, the check is bypassed.

### 4.6 Path Sandboxing / 路径沙箱

Every tool enforces that file paths stay within the workspace root:

```python
def _resolve(workspace: str, rel_path: str) -> Path:
    ws = Path(workspace).resolve()
    target = (ws / rel_path).resolve()
    if not str(target).startswith(str(ws)):
        raise ValueError(f"Path escapes workspace: {rel_path}")
    return target
```

This prevents directory traversal attacks (`../../etc/passwd`). All paths returned by tools are **relative** to the workspace root.

**中文说明:** 所有工具接收和返回的路径都是相对于 workspace 根目录的相对路径，`_resolve()` 函数确保解析后的绝对路径不会超出沙箱范围。

### 4.7 Using the Agent / 如何调用 Agent

```python
from app.agent_loop.service import AgentLoopService
from app.agent_loop.budget import BudgetConfig

agent = AgentLoopService(
    provider=ai_provider,
    max_iterations=25,
    budget_config=BudgetConfig(max_input_tokens=500_000),
    classifier_provider=haiku_provider,  # optional LLM pre-classification
    use_llm_classifier=True,
)
result = await agent.run(
    query="How does the authentication flow work?",
    workspace_path="/path/to/worktrees/room-123"
)
# result.answer          — LLM's final answer
# result.context_chunks  — code snippets read during the loop
# result.tool_calls_made — total number of tool calls
# result.budget_summary  — token usage breakdown
```

The HTTP endpoint (supports SSE streaming):

```bash
POST /api/context/query
{ "query": "How does auth work?", "room_id": "room-123" }

POST /api/context/query/stream        # SSE: real-time tool call progress
```

---

## 5. AI Provider Integration / AI 提供商集成

### 5.1 Provider Abstraction / 提供商抽象层

All LLM providers implement the `AIProvider` ABC in `ai_provider/base.py`:

```python
class AIProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[dict], system: str = "") -> ToolUseResponse: ...

    @abstractmethod
    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
    ) -> ToolUseResponse: ...
```

Three concrete implementations:

| Provider | File | API |
|----------|------|-----|
| `ClaudeBedrockProvider` | `claude_bedrock.py` | AWS Bedrock Converse API |
| `ClaudeDirectProvider` | `claude_direct.py` | Anthropic Messages API |
| `OpenAIProvider` | `openai_provider.py` | OpenAI Chat Completions |

**中文说明:** 所有提供商实现同一个抽象基类，agent loop 不需要知道底层用的是哪个提供商。`chat_with_tools()` 是 agent loop 使用的核心方法，支持工具调用（function calling）。

### 5.2 chat_with_tools() Pattern / 工具调用模式

```python
# All 3 providers implement this
response = provider.chat_with_tools(
    messages=[{"role": "user", "content": [{"text": "Find auth code"}]}],
    tools=TOOL_DEFINITIONS,   # from code_tools/schemas.py
    system="You are a code assistant.",
)
# response.text        — model's text output
# response.tool_calls  — List[ToolCall] with id, name, input
# response.stop_reason — "end_turn" | "tool_use" | "max_tokens"
```

The internal message format follows the **Bedrock Converse format** (content blocks with `text`, `toolUse`, `toolResult` types). The OpenAI provider translates this to/from OpenAI format internally.

### 5.3 ProviderResolver / 提供商选择器

On startup, `ProviderResolver` probes all configured providers:

```python
resolver = ProviderResolver(config)
resolver.resolve()              # tests all providers, picks fastest
provider = resolver.get_active_provider()
```

If Bedrock is unavailable (e.g., no AWS credentials), it automatically falls back to Anthropic Direct or OpenAI. Health status is available via:

```bash
GET /api/ai-providers/status
# → [{"name": "aws_bedrock", "available": true, "latency_ms": 312}, ...]
```

**中文说明:** `ProviderResolver` 在启动时测试所有配置的提供商（延迟测量），自动选出最优的一个。即使主提供商不可用，系统也能自动降级到备用提供商，无需手动干预。



---

## 6. Git Workspace Management / Git 工作区管理

### 6.1 The Core Idea / 核心思路

Conductor implements workspace sharing via Git worktrees (no VS Code Live Share dependency):

1. Host provides a Git repo URL + Personal Access Token (PAT)
2. Backend clones as a **bare repository** (no working tree)
3. Backend creates a **Git worktree** for the session (`worktrees/{room_id}/`)
4. Each room gets its own branch (`session/{room_id}`)
5. The worktree is the sandbox for the agent loop and all code tools

**Why bare repo?** A bare repo contains only the `.git` contents — it's designed for server-side storage and worktree creation, not direct editing.

**Why worktrees?** Multiple rooms can share the same upstream repo with isolated working directories — changes in room A don't affect room B.

**中文说明:** 每个协作房间对应一个 git worktree，隔离在自己的分支上。bare repo 是服务端共享仓库的标准做法；worktree 让同一仓库可以有多个独立的工作目录。

### 6.2 GitWorkspaceService

```python
class GitWorkspaceService:
    def repo_path(self, room_id: str) -> Path:
        return self.workspaces_dir / "repos" / f"{room_id}.git"

    def worktree_path(self, room_id: str) -> Path:
        return self.workspaces_dir / "worktrees" / room_id

    async def create_workspace(self, room_id: str, repo_url: str, token: str) -> WorkspaceInfo:
        # 1. Clone bare repo with GIT_ASKPASS
        # 2. Create worktree on branch session/{room_id}
        # 3. Return WorkspaceInfo with both paths
        ...
```

### 6.3 GIT_ASKPASS Mechanism / 凭证传递机制

Git calls the `GIT_ASKPASS` script when it needs credentials. A temporary shell script echoes the PAT:

```python
def _create_askpass_script(self, token: str) -> str:
    script = Path(tempfile.mktemp(suffix=".sh"))
    script.write_text(f'#!/bin/sh\necho "{token}"\n')
    script.chmod(0o700)
    return str(script)
```

The script is removed after the clone. The token is never logged or persisted to disk beyond this temp script.

**中文说明:** `GIT_ASKPASS` 是 Git 的标准非交互式凭证机制。脚本文件权限设置为 `0700`（只有所有者可执行），clone 完成后立即删除，确保 token 不会泄露。

---

## 7. File Sharing / 文件共享

Files uploaded to a room are stored with DuckDB-backed metadata. The `files/` module handles multipart uploads and per-room file listing.

### 7.1 Upload Flow / 上传流程

```
Extension host (Node.js)
  ↓  multipart POST /api/files/upload
Backend → DuckDB metadata (file_id, room_id, sha256, filename)
  ↓  returns file_id
Extension broadcasts file_id to room via WebSocket
  ↓  other members request GET /api/files/{file_id}
```

**中文说明:** VS Code WebView 无法直接发起任意 HTTP 请求（沙盒限制），文件上传由 extension host（Node.js 层）代理发送到后端。后端用 DuckDB 记录文件元数据（room 隔离），不依赖外部数据库。

### 7.2 Deduplication / 去重

Files are SHA-256 hashed on upload. If two members upload the same file, only one copy is stored — the second upload returns the same `file_id`.

### 7.3 Workspace File Operations / 工作区文件操作

`workspace_files/` provides per-worktree CRUD:

```
GET  /api/workspace-files/{room_id}/list?path=src/
GET  /api/workspace-files/{room_id}/read?path=src/main.py
POST /api/workspace-files/{room_id}/write  { path, content }
```

All paths are sandboxed within the room's worktree (same `_resolve()` guard used by code tools).

---

## 8. Audit & Todos / 审计与任务追踪

### 8.1 Audit Logs (DuckDB) / 审计日志

`audit/service.py` uses **DuckDB** — a zero-dependency embedded analytical database — to persist apply/skip events for every AI-suggested change.

**Schema:**
```sql
CREATE TABLE audit_logs (
    id             INTEGER PRIMARY KEY,
    room_id        VARCHAR,
    summary_id     VARCHAR,
    changeset_hash VARCHAR,   -- SHA-256 of the applied changeset
    applied_by     VARCHAR,   -- user_id
    mode           VARCHAR,   -- 'manual' | 'auto'
    timestamp      TIMESTAMP
)
```

**Usage:**
```python
service = AuditLogService.get_instance()   # singleton
entry = service.log_apply(AuditLogCreate(
    room_id="room-123",
    changeset_hash=sha256(changeset),
    applied_by="user-456",
    mode="manual",
))
logs = service.get_logs(room_id="room-123")
```

**中文说明:** 审计日志使用 DuckDB 持久化，无需 Postgres。每次用户接受或拒绝 AI 建议的变更都会记录一条审计记录。`changeset_hash` 是变更集的 SHA-256，可用于重建完整的审计追踪。

### 8.2 TODO Tracker (DuckDB) / 任务追踪

`todos/` provides room-scoped TODO items backed by DuckDB:

```
GET    /todos/{room_id}           — list all TODOs for a room
POST   /todos/{room_id}           — create a new TODO
PATCH  /todos/{room_id}/{todo_id} — update status/text
DELETE /todos/{room_id}/{todo_id} — remove
```

TODOs are scoped per room and survive server restarts (DuckDB persists to `audit_logs.duckdb` configured in `conductor.settings.yaml`).

---

## 9. Authentication / 身份认证

### 9.1 AWS SSO

Configured via `conductor.settings.yaml`:

```yaml
sso:
  enabled: true
  start_url: "https://d-xxxx.awsapps.com/start"
  region: "eu-west-2"
```

The `auth/` module provides:
- `POST /api/auth/aws-sso/start` — redirect to AWS SSO login
- `GET  /api/auth/aws-sso/callback` — exchange code for session token
- `GET  /api/auth/me` — current user info

**中文说明:** AWS SSO 使用 PKCE 流程，用户在 AWS 托管页面登录，不需要后端存储密码。获取的 session token 用于后续 API 鉴权。

### 9.2 Google SSO

```yaml
google_sso:
  enabled: false
```

Google SSO follows the same OAuth 2.0 flow. Enable by setting `enabled: true` and providing `client_id` / `client_secret` in `conductor.secrets.yaml`.

### 9.3 Git Credentials (Model A PAT)

Personal Access Tokens are passed via `GIT_ASKPASS` (see Section 6.3) and never persisted. They live in memory only for the duration of git operations.

---

## 10. LangExtract Integration / LangExtract 集成

`langextract/` provides a **multi-vendor Bedrock language model plugin** for Google's [langextract](https://github.com/google/langextract) library. It supports all Bedrock vendors (Claude, Amazon Nova, Llama, Mistral, DeepSeek, Qwen) via the unified Converse API.

### 10.1 BedrockCatalog — Dynamic Model Discovery

```python
from app.langextract.catalog import BedrockCatalog

catalog = BedrockCatalog(region="eu-west-2")
catalog.refresh()   # calls list_foundation_models() + list_inference_profiles()

# Group by vendor for UI dropdowns
models = catalog.models_by_vendor()
# → {"Anthropic": [...], "Amazon": [...], "Meta": [...], ...}

# Flat list for selection
ids = catalog.get_model_ids()
```

`BedrockCatalog` handles `eu.` cross-region inference profile prefixes automatically, making cross-region models available without manual ID construction.

### 10.2 LangExtractService

```python
from app.langextract.service import LangExtractService
from langextract.data import ExampleData, Extraction

svc = LangExtractService(
    model_id="claude-sonnet-4-20250514",   # or any Bedrock model ID
    region="eu-west-2",
    catalog=catalog,   # optional: enables model discovery
)
result = await svc.extract_from_text(
    text="Meeting notes: Alice will review the PR by March 15...",
    prompt="Extract people, dates, and action items.",
    examples=[ExampleData(
        text="Bob will fix the bug by Friday.",
        extractions=[
            Extraction(extraction_class="Person", extraction_text="Bob"),
            Extraction(extraction_class="Date", extraction_text="Friday"),
        ],
    )],
)
# result.success, result.documents, result.error

# List available models grouped by vendor
models_by_vendor = svc.list_available_models()
```

The `BedrockLanguageModel` class (and backwards-compatible `ClaudeLanguageModel` alias) is registered via `@router.register()` so `lx.extract(model_id="claude-...")` works automatically with langextract's standard API.

The `GET /api/langextract/models` endpoint returns the vendor-grouped model list for UI consumption.

**中文说明:** `BedrockLanguageModel` 是 langextract 的 Bedrock 提供商插件，通过统一 Converse API 支持所有 Bedrock 厂商模型。`BedrockCatalog` 在启动时动态发现可用模型，并自动处理跨区域推理配置（`eu.` 前缀）。`ClaudeLanguageModel` 作为向后兼容别名保留。

---

## 11. Testing Patterns / 测试规范

### 11.1 Backend Tests

All backend tests use `pytest`. Total: **900+ tests**. Run them with:

```bash
cd backend
pytest                                        # all tests (900+)
pytest -k "test_agent_loop"                  # agent loop tests only
pytest -k "test_code_tools"                  # code tools tests only
pytest -k "test_budget_controller"           # budget controller tests
pytest -k "test_langextract"                 # langextract tests
pytest --cov=. --cov-report=html             # coverage report
```

**Key test files:**

| File | Tests | What it covers |
|------|-------|----------------|
| `test_code_tools.py` | 98 | All 21 tools + dispatcher + multi-language |
| `test_agent_loop.py` | 39 | Agent loop + 3-layer prompt + workspace layout |
| `test_budget_controller.py` | 20 | Token budget signals, tracking, WARN/FORCE |
| `test_session_trace.py` | 15 | SessionTrace JSON save/load |
| `test_evidence.py` | 14 | Evidence evaluator quality gate |
| `test_symbol_role.py` | 24 | Symbol role classification + decorator detection |
| `test_output_policy.py` | 19 | Per-tool truncation, budget adaptation |
| `test_query_classifier.py` | 26 | Keyword + LLM classification, dynamic tool sets |
| `test_compressed_tools.py` | 24 | compressed_view, module_summary, expand_symbol |
| `test_langextract.py` | 57 | Bedrock provider, catalog, service, router |
| `test_repo_graph.py` | 72 | Parser + graph + PageRank + service |
| `test_config_new.py` | 27 | Config + secrets (RAG remnants removed) |

**Test infrastructure:**
- `tests/conftest.py` — centralized stubs for `cocoindex`, `litellm`, `sentence_transformers`, `sqlite_vec`
- Code tools tests use **real filesystem** (`tmp_path` fixtures), not mocks
- Agent loop tests use `MockProvider` with scripted `ToolUseResponse` sequences
- LangExtract tests mock `lx.extract()` and boto3 API calls

### 11.2 Agent Loop Testing / Agent Loop 测试

Agent loop tests use a `MockProvider` subclass with scripted tool-use responses:

```python
class MockProvider(AIProvider):
    def __init__(self, responses: list[ToolUseResponse]):
        self._responses = iter(responses)

    def chat_with_tools(self, messages, tools, system=""):
        return next(self._responses)

agent = AgentLoopService(
    provider=MockProvider([
        ToolUseResponse(tool_calls=[ToolCall(id="1", name="grep",
            input={"pattern": "authenticate"})]),
        ToolUseResponse(text="Auth is handled in auth/router.py", stop_reason="end_turn"),
    ]),
    max_iterations=25,
    budget_config=BudgetConfig(max_input_tokens=500_000),
)

result = await agent.run("How does auth work?", "/tmp/ws")
assert "auth" in result.answer.lower()
assert result.budget_summary["total_input_tokens"] > 0
```

**中文说明:** `MockProvider` 允许在不调用真实 LLM API 的情况下测试 agent loop 的完整流程，包括工具调用、结果注入、迭代逻辑、预算信号和证据验证。

### 11.3 Code Tools Testing / 代码工具测试

Code tool tests create real temporary workspaces with actual source files:

```python
def test_grep(tmp_path):
    (tmp_path / "app.py").write_text("def authenticate(user): ...")
    result = execute_tool("grep", str(tmp_path), {"pattern": "authenticate"})
    assert result.success
    assert "app.py" in result.data
```

This ensures tools work against real file I/O, not mocked filesystems.

---

## 12. Deployment / 部署

### 12.1 Environment Variables / 环境变量

All secrets go in `backend/config/conductor.secrets.yaml` (never committed). Non-secret settings live in `backend/config/conductor.settings.yaml`.

```bash
# Runtime (set via env or conductor.settings.yaml)
BACKEND_HOST=0.0.0.0
BACKEND_PORT=8000

# Git workspace
GIT_WORKSPACE_ROOT=/var/conductor/workspaces

# AI providers (set via conductor.secrets.yaml)
AWS_ACCESS_KEY_ID=...          # Bedrock
AWS_SECRET_ACCESS_KEY=...      # Bedrock
AWS_DEFAULT_REGION=us-east-1
OPENAI_API_KEY=sk-...          # OpenAI
ANTHROPIC_API_KEY=sk-ant-...   # Direct Anthropic
```

**中文说明:** 凭证通过 `conductor.secrets.yaml` 配置，由 `config.py` 读取后注入为环境变量（使用 `os.environ.setdefault`，不覆盖已有值）。

### 12.2 Running Locally / 本地运行

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Extension (watch mode)
cd extension
npm install
npm run watch
# Then press F5 in VS Code to launch Extension Development Host
```

### 12.3 File System Layout / 文件系统布局

```
/var/conductor/workspaces/
├── repos/        # bare git clones (one per room)
└── worktrees/    # working directories (one per room)
```

Both directories must be writable by the process user. Disk usage is roughly 2-3× the repo size per active room.

### 12.4 Git Requirements

- Git 2.15+ (worktree support)
- `ripgrep` (`rg`) in PATH — used by the `grep` code tool
- `ast-grep` CLI in PATH (optional) — used by the `ast_search` code tool

### 12.5 Docker

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y git ripgrep && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ .

ENV GIT_WORKSPACE_ROOT=/var/conductor/workspaces

RUN mkdir -p /var/conductor/workspaces/repos /var/conductor/workspaces/worktrees

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**中文说明:** Docker 镜像不再需要 Postgres。只需 `git` + `ripgrep` + Python 依赖即可运行完整后端。

---

## 13. Contributing / 贡献指南

### Code Style / 代码风格

**Python:**
- Black formatting (line length 100)
- Type hints on all public functions
- Docstrings for all public classes and non-trivial functions
- `ruff` for linting

**TypeScript:**
- ESLint with VS Code recommended ruleset
- Strict mode enabled (`"strict": true` in tsconfig)
- No `any` types

### Adding a New Code Tool / 新增代码工具

1. Add the tool implementation in `backend/app/code_tools/tools.py`
2. Add its JSON schema to `TOOL_DEFINITIONS` in `code_tools/schemas.py`
3. Register it in the `execute_tool()` dispatcher
4. Add tests to `tests/test_code_tools.py`
5. Update the tools table in `CLAUDE.md`

### Adding a New AI Provider / 新增 AI 提供商

1. Subclass `AIProvider` in `ai_provider/base.py`
2. Implement `chat()` and `chat_with_tools()`
3. Register in `ProviderResolver.resolve()`
4. Add health-check tests

### Pull Request Checklist / PR 检查清单

- [ ] `pytest` passes with 0 failures
- [ ] `npm test` passes for extension
- [ ] New code has test coverage
- [ ] `CLAUDE.md` updated if new patterns introduced
- [ ] `ROADMAP.md` updated if completing a roadmap item
- [ ] No hardcoded secrets or API keys

---

Happy coding! 🚀 / 编程愉快！

