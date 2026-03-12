# Backend API

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor backend is a FastAPI application providing real-time chat, agentic code intelligence (LLM agent loop + 21 code tools + token budget controller + 3-layer prompts), Git workspace management, file sharing, DuckDB-backed audit logs and TODOs, and multi-provider AI (Bedrock / Anthropic / OpenAI).

### Quick Start

Recommended (from repo root):

```bash
make setup-backend
make run-backend
```

Manual:

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Docs:
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### API Overview

#### System

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe |
| GET | `/public-url` | Current ngrok public URL (if enabled) |
| GET | `/metrics` | Prometheus-compatible metrics |

#### Chat

| Method | Path | Description |
|---|---|---|
| GET | `/invite` | Invite page |
| GET | `/chat` | Guest chat page |
| GET | `/chat/{room_id}/history` | Cursor-based paginated chat history |
| POST | `/chat/{room_id}/ai-message` | Post AI message into room |
| WS | `/ws/chat/{room_id}` | Real-time chat WebSocket |
| GET | `/rooms/{room_id}/settings` | Get room settings |
| PUT | `/rooms/{room_id}/settings` | Update room settings |

#### AI Provider (`/ai/`)

| Method | Path | Description |
|---|---|---|
| GET | `/ai/status` | Active provider + health status |
| POST | `/ai/model` | Set active AI model |
| POST | `/ai/summarize` | Four-stage AI decision summary pipeline |
| POST | `/ai/code-prompt` | Generate coding prompt from decision summary |
| POST | `/ai/code-prompt/selective` | Selective prompt from multi-type summary |
| POST | `/ai/code-prompt/items` | Generate prompt from extracted items |
| GET | `/ai/style-templates` | List available style templates |

#### Agentic Code Intelligence

| Method | Path | Description |
|---|---|---|
| POST | `/api/context/query` | LLM agent loop — iteratively calls 21 code tools (up to 25 iterations, 500K token budget) |
| POST | `/api/context/query/stream` | SSE streaming — real-time tool call progress events |
| POST | `/api/context/explain-rich` | Deep code explanation via agent (replaces XML-prompt pipeline) |
| POST | `/api/context/explain-rich/stream` | SSE streaming for explain-rich |
| GET | `/api/code-tools/available` | List all available code tools |
| POST | `/api/code-tools/execute/{tool_name}` | Directly execute a single code tool |

#### Git Workspace (`/api/git-workspace/`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/git-workspace/health` | Git workspace service health |
| POST | `/api/git-workspace/branches/remote` | List remote branches for a repo URL |
| POST | `/api/git-workspace/workspaces/setup-and-index` | Clone repo + create worktree + index |
| POST | `/api/git-workspace/workspaces/{room_id}/index` | Re-index workspace |
| POST | `/api/git-workspace/workspaces` | Create workspace |
| GET | `/api/git-workspace/workspaces` | List all workspaces |
| GET | `/api/git-workspace/workspaces/{room_id}` | Get workspace info |
| POST | `/api/git-workspace/workspaces/{room_id}/credentials` | Set credentials |
| DELETE | `/api/git-workspace/workspaces/{room_id}/credentials` | Clear credentials |
| POST | `/api/git-workspace/workspaces/{room_id}/sync` | Fetch + merge from remote |
| POST | `/api/git-workspace/workspaces/{room_id}/commit` | Commit staged changes |
| POST | `/api/git-workspace/workspaces/{room_id}/push` | Push to remote |
| DELETE | `/api/git-workspace/workspaces/{room_id}` | Destroy workspace |
| WS | `/api/git-workspace/ws/{room_id}/file-sync` | File sync for `conductor://` FS |
| WS | `/api/git-workspace/ws/{room_id}/delegate-auth` | Delegate auth WebSocket |

#### Workspace Files (`/workspace/`)

| Method | Path | Description |
|---|---|---|
| GET | `/workspace/{room_id}/files` | List root directory |
| GET | `/workspace/{room_id}/files/{path}` | List sub-directory |
| GET | `/workspace/{room_id}/files/{path}/content` | Read file content |
| PUT | `/workspace/{room_id}/files/{path}/content` | Write file content |
| GET | `/workspace/{room_id}/files/stat` | Stat root |
| GET | `/workspace/{room_id}/files/{path}/stat` | Stat file or directory |
| POST | `/workspace/{room_id}/files/{path}/rename` | Rename or move file |
| POST | `/workspace/{room_id}/files/{path}` | Create file or directory |
| DELETE | `/workspace/{room_id}/files/{path}` | Delete file |
| POST | `/workspace/{room_id}/search` | Full-text search in workspace |

#### Files, Todos, Audit, Auth, Policy

| Method | Path | Description |
|---|---|---|
| POST | `/files/upload/{room_id}` | Upload file to room |
| GET | `/files/download/{file_id}` | Download file |
| GET | `/files/check-duplicate/{room_id}` | Check duplicate filename (case-insensitive) |
| DELETE | `/files/room/{room_id}` | Delete all files for a room |
| GET | `/todos/{room_id}` | List TODOs for room |
| POST | `/todos/{room_id}` | Create TODO |
| PUT | `/todos/{room_id}/{todo_id}` | Update TODO |
| DELETE | `/todos/{room_id}/{todo_id}` | Delete TODO |
| POST | `/audit/log-apply` | Record apply operation |
| GET | `/audit/logs` | Query audit logs (optional `room_id` filter) |
| POST | `/auth/sso/start` | Start AWS SSO device authorization flow |
| POST | `/auth/sso/poll` | Poll for AWS SSO token and resolve identity |
| POST | `/auth/google/start` | Start Google OAuth device authorization flow |
| POST | `/auth/google/poll` | Poll for Google OAuth token and resolve identity |
| GET | `/auth/providers` | List enabled auth providers |
| POST | `/policy/evaluate-auto-apply` | Evaluate auto-apply safety |
| POST | `/generate-changes` | Generate ChangeSet |

### Agentic Code Intelligence

`POST /api/context/query` runs an LLM agent loop (up to **25 iterations**, **500K token budget**). The Query Classifier categorises the query into one of 7 types, selects an optimal 8-12 tool subset, and injects a 3-layer system prompt (Core Identity + Strategy + Runtime Guidance). A token-based Budget Controller emits NORMAL / WARN_CONVERGE / FORCE_CONCLUDE signals. An Evidence Evaluator rejects weak answers before finalising. Session Traces are saved as JSON for offline analysis.

**21 code tools:**

| Tool | Description |
|------|-------------|
| `grep` | Regex search (ripgrep) |
| `read_file` | Read file with optional line range |
| `list_files` | Directory tree with depth/glob filters |
| `find_symbol` | AST-based symbol definition search with role classification |
| `find_references` | Symbol usages (grep + AST validation) |
| `file_outline` | All definitions in a file with line numbers |
| `get_dependencies` | Files this file imports |
| `get_dependents` | Files that import this file |
| `git_log` | Recent commits, optionally per-file |
| `git_diff` | Diff between two git refs |
| `ast_search` | Structural AST search via ast-grep (`$VAR`, `$$$MULTI` patterns) |
| `get_callees` | Functions called within a function body |
| `get_callers` | Functions that call a given function (cross-file) |
| `git_blame` | Per-line authorship with commit hash, author, date |
| `git_show` | Full commit details (message + diff) |
| `find_tests` | Test functions covering a given function/class |
| `test_outline` | Test file structure with mocks, assertions, fixtures |
| `trace_variable` | Data flow tracing: alias detection, arg→param mapping, sink/source patterns |
| `compressed_view` | File signatures + call relationships + side effects (~80% token savings) |
| `module_summary` | Module-level summary: services, models, functions, file list (~95% savings) |
| `expand_symbol` | Expand a symbol from compressed view to full source code |

### AI Provider Notes

- Provider keys are configured in `config/conductor.secrets.yaml`; settings in `config/conductor.settings.yaml`.
- `ProviderResolver` health-checks all configured providers at startup and selects the fastest.
- All 3 providers support native tool use (`chat_with_tools`): Bedrock Converse API, Anthropic Messages API, OpenAI Chat Completions.

### WebSocket Protocol (Core)

Connection:
- `ws://<host>/ws/chat/{room_id}`

Server -> Client:
- `connected`
- `history`
- `message`
- `file`
- `code_snippet`
- `typing`
- `read_receipt`
- `user_joined` / `user_left`
- `session_ended`
- `error`

Client -> Server:
- `join`
- `message`
- `file`
- `code_snippet`
- `typing`
- `read`
- `end_session`

Security model:
- Backend assigns `userId` and role (`host` for first connection, then `guest`).
- Backend ignores forged client identity for sensitive behavior.
- Only host can end session.

### Quick Examples

```bash
# Health check
curl http://localhost:8000/health

# Agentic code query
curl -X POST http://localhost:8000/api/context/query \
  -H "Content-Type: application/json" \
  -d '{"query": "How does authentication work?", "workspace_path": "/path/to/worktree"}'

# Direct tool execution
curl -X POST http://localhost:8000/api/code-tools/execute/grep \
  -H "Content-Type: application/json" \
  -d '{"workspace": "/path/to/worktree", "params": {"pattern": "def authenticate"}}'
```

### Storage

- Audit DB: `audit_logs.duckdb`
- File metadata DB: `file_metadata.duckdb`
- TODOs DB: `todos.duckdb`
- File uploads: `uploads/{room_id}/`
- Git workspaces: `workspaces/{room_id}/` (bare clone + worktree)
- Chat room state: in-memory per process

### Tests

Total: **900+ tests**.

```bash
cd backend
python -m pytest tests/ -v
python -m pytest tests/ -q
pytest --cov=. --cov-report=html   # coverage report
```

Key test files (agentic code intelligence):

| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_code_tools.py` | 98 | All 21 code tools + dispatcher + multi-language |
| `tests/test_agent_loop.py` | 39 | Agent loop + message format + workspace layout + 3-layer prompt |
| `tests/test_budget_controller.py` | 20 | Token budget signals, tracking, edge cases |
| `tests/test_session_trace.py` | 15 | SessionTrace, IterationTrace, save/load |
| `tests/test_evidence.py` | 14 | Evidence evaluator (file refs, tool calls, budget checks) |
| `tests/test_symbol_role.py` | 24 | Symbol role classification + sorting + decorator detection |
| `tests/test_output_policy.py` | 19 | Per-tool truncation policies, budget adaptation |
| `tests/test_query_classifier.py` | 26 | Keyword + LLM classification, dynamic tool sets |
| `tests/test_compressed_tools.py` | 24 | compressed_view, module_summary, expand_symbol |
| `tests/test_langextract.py` | 57 | Bedrock provider, catalog, service, router |
| `tests/test_repo_graph.py` | 72 | Parser + graph + PageRank + RepoMapService |
| `tests/test_config_new.py` | 27 | Config + secrets (RAG remnants removed) |
| `tests/test_git_workspace.py` | — | Git workspace lifecycle |

Additional test files:

- `tests/test_ai_provider.py`: 131 — all 3 AI providers + chat_with_tools + TokenUsage
- `tests/test_prompt_builder.py`: 64 — prompt construction
- `tests/test_workspace_files.py`: 39 — workspace file CRUD
- `tests/test_auth.py`: 38 — AWS SSO + Google OAuth
- `tests/test_chat.py`: 29 — WebSocket + history + typing indicators
- `tests/test_auto_apply_policy.py`: 28 — policy evaluation
- `tests/test_mock_agent.py`: 26 — MockAgent + code generation
- `tests/test_style_loader.py`: 22 — style templates
- `tests/test_room_settings.py`: 18 — room settings
- `tests/test_audit.py`: 14 — audit log

---

<a name="中文"></a>
## 中文

Conductor 后端基于 FastAPI，提供实时聊天、**智能代码分析**（LLM 驱动的 Agent Loop + 13 个代码工具）、Git 工作区管理、文件共享、DuckDB 审计日志与 TODO 管理，以及多 Provider AI 集成（Bedrock / Anthropic / OpenAI）。

### 快速启动

推荐（仓库根目录执行）：

```bash
make setup-backend
make run-backend
```

手动方式：

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

文档地址：
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### API 总览

#### 系统

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | 存活探针 |
| GET | `/public-url` | 当前 ngrok 公网地址（如启用） |
| GET | `/metrics` | Prometheus 指标 |

#### 聊天

| Method | Path | 说明 |
|---|---|---|
| GET | `/invite` | 邀请页 |
| GET | `/chat` | 访客聊天页 |
| GET | `/chat/{room_id}/history` | 历史消息分页（cursor-based） |
| POST | `/chat/{room_id}/ai-message` | 向房间写入 AI 消息 |
| WS | `/ws/chat/{room_id}` | 实时聊天 WebSocket |
| GET | `/rooms/{room_id}/settings` | 获取房间设置 |
| PUT | `/rooms/{room_id}/settings` | 更新房间设置 |

#### AI Provider（`/ai/`）

| Method | Path | 说明 |
|---|---|---|
| GET | `/ai/status` | 活动 Provider 与健康状态 |
| POST | `/ai/model` | 设置活动 AI 模型 |
| POST | `/ai/summarize` | 四阶段决策摘要流水线 |
| POST | `/ai/code-prompt` | 基于决策摘要生成代码提示词 |
| POST | `/ai/code-prompt/selective` | 多类型摘要 selective 提示词 |
| POST | `/ai/code-prompt/items` | 基于条目生成提示词 |
| GET | `/ai/style-templates` | 列出可用的样式模板 |

#### 智能代码分析

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/context/query` | LLM Agent Loop — 迭代调用 13 个代码工具回答代码查询 |
| GET | `/api/code-tools/available` | 列出所有可用代码工具 |
| POST | `/api/code-tools/execute/{tool_name}` | 直接执行单个代码工具 |

#### Git 工作区（`/api/git-workspace/`）

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/git-workspace/health` | Git 工作区服务健康检查 |
| POST | `/api/git-workspace/branches/remote` | 列出仓库远端分支 |
| POST | `/api/git-workspace/workspaces/setup-and-index` | 克隆仓库 + 创建 worktree + 建索引 |
| POST | `/api/git-workspace/workspaces/{room_id}/index` | 重新建索引 |
| POST | `/api/git-workspace/workspaces` | 创建工作区 |
| GET | `/api/git-workspace/workspaces` | 列出所有工作区 |
| GET | `/api/git-workspace/workspaces/{room_id}` | 获取工作区信息 |
| POST | `/api/git-workspace/workspaces/{room_id}/credentials` | 设置凭据 |
| DELETE | `/api/git-workspace/workspaces/{room_id}/credentials` | 清除凭据 |
| POST | `/api/git-workspace/workspaces/{room_id}/sync` | 拉取并合并远端变更 |
| POST | `/api/git-workspace/workspaces/{room_id}/commit` | 提交暂存变更 |
| POST | `/api/git-workspace/workspaces/{room_id}/push` | 推送到远端 |
| DELETE | `/api/git-workspace/workspaces/{room_id}` | 销毁工作区 |
| WS | `/api/git-workspace/ws/{room_id}/file-sync` | `conductor://` 文件同步 WebSocket |
| WS | `/api/git-workspace/ws/{room_id}/delegate-auth` | 委托认证 WebSocket |

#### 工作区文件（`/workspace/`）

| Method | Path | 说明 |
|---|---|---|
| GET | `/workspace/{room_id}/files` | 列出根目录 |
| GET | `/workspace/{room_id}/files/{path}` | 列出子目录 |
| GET | `/workspace/{room_id}/files/{path}/content` | 读取文件内容 |
| PUT | `/workspace/{room_id}/files/{path}/content` | 写入文件内容 |
| GET | `/workspace/{room_id}/files/stat` | 根目录 stat |
| GET | `/workspace/{room_id}/files/{path}/stat` | 文件/目录 stat |
| POST | `/workspace/{room_id}/files/{path}/rename` | 重命名或移动文件 |
| POST | `/workspace/{room_id}/files/{path}` | 创建文件或目录 |
| DELETE | `/workspace/{room_id}/files/{path}` | 删除文件 |
| POST | `/workspace/{room_id}/search` | 工作区全文搜索 |

#### 文件、TODO、审计、Auth、策略

| Method | Path | 说明 |
|---|---|---|
| POST | `/files/upload/{room_id}` | 向房间上传文件 |
| GET | `/files/download/{file_id}` | 下载文件 |
| GET | `/files/check-duplicate/{room_id}` | 检查文件名重复（大小写不敏感） |
| DELETE | `/files/room/{room_id}` | 删除房间全部文件 |
| GET | `/todos/{room_id}` | 列出房间 TODO |
| POST | `/todos/{room_id}` | 创建 TODO |
| PUT | `/todos/{room_id}/{todo_id}` | 更新 TODO |
| DELETE | `/todos/{room_id}/{todo_id}` | 删除 TODO |
| POST | `/audit/log-apply` | 记录应用操作 |
| GET | `/audit/logs` | 查询审计日志（可按 `room_id` 过滤） |
| POST | `/auth/sso/start` | 启动 AWS SSO 设备授权流程 |
| POST | `/auth/sso/poll` | 轮询 AWS SSO token 并解析身份 |
| POST | `/auth/google/start` | 启动 Google OAuth 设备授权流程 |
| POST | `/auth/google/poll` | 轮询 Google OAuth token 并解析身份 |
| GET | `/auth/providers` | 列出启用的认证提供商 |
| POST | `/policy/evaluate-auto-apply` | 评估自动应用安全性 |
| POST | `/generate-changes` | 生成 ChangeSet |

### 智能代码分析

`POST /api/context/query` 运行 LLM Agent Loop（最多 15 轮迭代）。Agent 迭代调用代码工具探索代码库，回答代码导航问题，**无需预先构建向量索引**。

**13 个代码工具：**

| 工具 | 说明 |
|------|------|
| `grep` | 正则搜索（ripgrep） |
| `read_file` | 读取文件（可指定行范围） |
| `list_files` | 目录树（支持深度/glob 过滤） |
| `find_symbol` | AST 符号定义搜索（tree-sitter） |
| `find_references` | 符号用法搜索（grep + AST 验证） |
| `file_outline` | 列出文件中所有定义及行号 |
| `get_dependencies` | 该文件导入的文件列表 |
| `get_dependents` | 导入该文件的文件列表 |
| `git_log` | 最近 commit（可按文件过滤） |
| `git_diff` | 两个 git ref 之间的 diff |
| `ast_search` | 结构化 AST 搜索，via ast-grep（`$VAR`、`$$$MULTI` 模式） |
| `get_callees` | 函数体内调用的函数列表 |
| `get_callers` | 跨文件调用该函数的调用方列表 |

### AI Provider 说明

- Provider 密钥配置在 `config/conductor.secrets.yaml`，设置在 `config/conductor.settings.yaml`。
- `ProviderResolver` 在启动时对所有已配置的 Provider 做健康检查，选取响应最快的一个。
- 三个 Provider 均支持原生 Tool Use（`chat_with_tools`）：Bedrock Converse API、Anthropic Messages API、OpenAI Chat Completions。

### 存储

- 审计日志：`audit_logs.duckdb`
- 文件元数据：`file_metadata.duckdb`
- TODO：`todos.duckdb`
- 文件上传：`uploads/{room_id}/`
- Git 工作区：`workspaces/{room_id}/`（裸克隆 + worktree）
- 聊天房间状态：进程内存

### 测试

共 **670 个测试**。

```bash
cd backend
python -m pytest tests/ -v
python -m pytest tests/ -q
```

分布：
- `tests/test_ai_provider.py`: 131
- `tests/test_repo_graph.py`: 67
- `tests/test_prompt_builder.py`: 64
- `tests/test_code_tools.py`: 52
- `tests/test_git_workspace.py`: 48
- `tests/test_config_new.py`: 48
- `tests/test_workspace_files.py`: 39
- `tests/test_auth.py`: 38
- `tests/test_chat.py`: 29
- `tests/test_auto_apply_policy.py`: 28
- `tests/test_mock_agent.py`: 26
- `tests/test_style_loader.py`: 22
- `tests/test_langextract.py`: 21
- `tests/test_agent_loop.py`: 21
- `tests/test_room_settings.py`: 18
- `tests/test_audit.py`: 14
- `tests/test_config_paths.py`: 3
- `tests/test_main.py`: 1
