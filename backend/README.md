# Backend API

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor backend is a FastAPI application providing real-time chat, agentic code intelligence (LLM agent loop + 42 tools + token budget controller + 4-layer prompts), a config-driven multi-agent workflow engine (YAML + Markdown agent definitions, Langfuse observability), Git workspace management, file sharing, Jira integration (OAuth 3LO + 5 agent tools), PostgreSQL-backed persistence (schema managed by Liquibase), and multi-provider AI (Bedrock / Anthropic / OpenAI).

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
| POST | `/api/context/query/stream` | LLM Brain orchestrator — SSE stream of tool call + reasoning events (supports optional `code_context` for snippet-based queries) |
| GET | `/api/code-tools/available` | List all available code tools |
| POST | `/api/code-tools/execute/{tool_name}` | Directly execute a single code tool |

**Python CLI (for local-mode extension tool calls):**

```bash
# List all available tools
python -m app.code_tools list

# Execute a tool (JSON output)
python -m app.code_tools grep /path/to/workspace '{"pattern": "authenticate"}'
python -m app.code_tools file_outline /path/to/workspace '{"path": "src/auth.py"}'
```

The `code_tools/__main__.py` module is invoked by the VS Code extension's local tool dispatcher to execute tools via subprocess (all 42 tools are available via this CLI).

#### Workflow Engine (`/api/workflows/`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/workflows` | List available workflows (name, description, route_mode, agent count) |
| GET | `/api/workflows/{name}` | Full workflow config (agents, routes, pipeline) |
| GET | `/api/workflows/{name}/mermaid` | Auto-generated Mermaid flowchart |
| GET | `/api/workflows/{name}/graph` | React Flow-compatible graph JSON (nodes + edges) |
| PUT | `/api/workflows/{name}/models` | Update explorer/judge model assignments |

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

#### Jira Integration (`/api/integrations/jira/`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/integrations/jira/authorize-url` | Generate Atlassian OAuth authorize URL |
| GET | `/api/integrations/jira/callback` | Handle OAuth redirect from Atlassian (browser) |
| POST | `/api/integrations/jira/callback` | Exchange auth code for tokens (from extension) |
| GET | `/api/integrations/jira/status` | Current Jira connection status |
| POST | `/api/integrations/jira/disconnect` | Remove stored tokens |
| GET | `/api/integrations/jira/projects` | List accessible Jira projects |
| GET | `/api/integrations/jira/issue-types` | List issue types for a project |
| GET | `/api/integrations/jira/create-meta` | Field metadata for creating an issue |
| GET | `/api/integrations/jira/search` | Search issues by JQL text query |
| POST | `/api/integrations/jira/issues` | Create a Jira issue |

### Agentic Code Intelligence

`POST /api/context/query/stream` runs the **Brain orchestrator** (strong model) which dispatches specialist sub-agents via `dispatch_explore` / `dispatch_swarm` / `transfer_to_brain`, then synthesizes the answer. Each sub-agent runs an LLM loop (up to **25 iterations**, **800K token budget**). A 4-layer system prompt is injected following Anthropic's prompt design guidelines (goal-oriented, not prescriptive): Identity (agent persona, from `config/agents/*.md`) + Tools (curated per agent) + Skills & Guidelines (workspace context, investigation methodology) + User Message (query + code context). Key principle: tell the model WHAT to achieve, not HOW to do it step by step — Claude's autonomous reasoning outperforms hand-written exploration scripts. A token-based Budget Controller emits NORMAL / WARN_CONVERGE / FORCE_CONCLUDE signals. An Evidence Evaluator rejects weak answers before finalising. Session Traces are saved as JSON for offline analysis.

**42 tools** (code + file-edit + Jira + browser):

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
| `git_log` | Recent commits, optionally per-file; `search=` param filters by commit message |
| `git_diff` | Diff between two git refs |
| `ast_search` | Structural AST search via ast-grep (`$VAR`, `$$$MULTI` patterns) |
| `get_callees` | Functions called within a function body |
| `get_callers` | Functions that call a given function (cross-file) |
| `git_blame` | Per-line authorship with commit hash, author, date |
| `git_show` | Full commit details (message + diff) — also reads pre-change file at `HEAD~1:path` |
| `find_tests` | Test functions covering a given function/class |
| `test_outline` | Test file structure with mocks, assertions, fixtures |
| `trace_variable` | Data flow tracing: alias detection, arg→param mapping, sink/source patterns |
| `compressed_view` | File signatures + call relationships + side effects (~80% token savings) |
| `module_summary` | Module-level summary: services, models, functions, file list (~95% savings) |
| `expand_symbol` | Expand a symbol from compressed view to full source code |
| `run_test` | Execute a test file or specific test function; returns pass/fail + output (optional verification) |
| `glob` | Fast file pattern matching (e.g. `**/*.ts`) |
| `detect_patterns` | Architectural pattern detection (singleton, factory, observer, etc.) |
| `git_diff_files` | List changed files between two git refs |
| `git_hotspots` | Files with most recent churn (changes × authors) |
| `list_endpoints` | Extract API route definitions (Flask, FastAPI, Express, etc.) |
| `extract_docstrings` | Extract docstrings from a module's functions/classes |
| `db_schema` | Database schema introspection (SQLAlchemy models) |
| `file_edit` | Partial file edit via search-and-replace (read_file required first) |
| `file_write` | Full file write/create (read_file required for existing files) |
| `jira_search` | Search Jira issues (JQL or free text; shortcuts: "my tickets", "my sprint", "blockers") |
| `jira_get_issue` | Fetch full Jira issue details (description, comments, subtasks) |
| `jira_create_issue` | Create Jira ticket with ADF description, code block support, parent_key for sub-tasks |
| `jira_update_issue` | Update Jira issue (transitions, comments, fields, labels; Done/Closed/Resolved blocked) |
| `jira_list_projects` | List accessible Jira projects |
| `web_search` | Web search via Playwright |
| `web_navigate` | Navigate to URL in headless browser |
| `web_click` | Click element on page |
| `web_fill` | Fill form field |
| `web_screenshot` | Capture page screenshot |
| `web_extract` | Extract page content |

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

# Agentic code query (SSE stream of Brain orchestrator events)
curl -N -X POST http://localhost:8000/api/context/query/stream \
  -H "Content-Type: application/json" \
  -d '{"room_id": "demo", "query": "How does authentication work?"}'

# Direct tool execution
curl -X POST http://localhost:8000/api/code-tools/execute/grep \
  -H "Content-Type: application/json" \
  -d '{"workspace": "/path/to/worktree", "params": {"pattern": "def authenticate"}}'
```

### Storage

All OLTP data is stored in **PostgreSQL** (shared instance with Langfuse):

| Table | Description |
|---|---|
| `repo_tokens` | PAT cache for git workspace authentication |
| `session_traces` | Agent loop session metrics (JSON trace) |
| `audit_logs` | Changeset apply audit trail |
| `file_metadata` | Uploaded file metadata |
| `todos` | Room-scoped task tracking |
| `integration_tokens` | OAuth tokens for external integrations (Jira, etc.) |

Other storage:
- File uploads: `uploads/{room_id}/`
- Git workspaces: `workspaces/{room_id}/` (bare clone + worktree)
- Chat room state: in-memory per process

> Schema is managed by **Liquibase** (`database/changelog/`). Run `make db-update` after `make data-up`.
> Langfuse manages its own tables internally (Prisma migrations).

### Docker Networking

All Docker Compose files share the `conductor-net` network. Services communicate via container names:

- `conductor-postgres:5432` — Postgres (backend + Langfuse)
- `conductor-redis:6379` — Redis (backend)

This avoids `host.docker.internal` resolution issues in WSL2 / Linux Docker environments.

### Tests

Total: **1200+ tests**.

```bash
cd backend
python -m pytest tests/ -v
python -m pytest tests/ -q
pytest --cov=. --cov-report=html   # coverage report
```

Key test files (agentic code intelligence):

| File | Tests | Coverage |
|------|-------|----------|
| `tests/test_code_tools.py` | 139 | All 42 tools + dispatcher + multi-language |
| `tests/test_agent_loop.py` | 55 | Agent loop + message format + workspace layout + 4-layer prompt + completeness |
| `tests/test_budget_controller.py` | 20 | Token budget signals, tracking, edge cases |
| `tests/test_session_trace.py` | 15 | SessionTrace, IterationTrace, save/load |
| `tests/test_evidence.py` | 14 | Evidence evaluator (file refs, tool calls, budget checks) |
| `tests/test_symbol_role.py` | 24 | Symbol role classification + sorting + decorator detection |
| `tests/test_output_policy.py` | 19 | Per-tool truncation policies, budget adaptation |
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

### Eval System

Three independent evaluation suites in `eval/` (excluded from Docker). See `eval/README.md` for full documentation.

**Code Review Eval** (`eval/code_review/`) — measures `PRBrainOrchestrator` (v2 coordinator-worker design) quality against 42 planted-bug cases across 4 suites (requests + greptile-sentry + greptile-grafana + greptile-keycloak).

```bash
cd backend

# Run all 12 cases
python ../eval/code_review/run.py --provider anthropic --model claude-sonnet-4-20250514

# Single case, no LLM judge
python ../eval/code_review/run.py --filter "requests-001" --no-judge

# Save baseline for regression detection
python ../eval/code_review/run.py --save-baseline

# Gold-standard ceiling (Claude Code CLI)
python ../eval/code_review/run.py --gold --gold-model opus --save-baseline
```

Scoring: recall (35%), precision (20%), severity (15%), location (10%), recommendation (10%), context (10%).

**Agent Quality Eval** (`eval/agent_quality/`) — measures agentic loop answer quality against baseline cases.

```bash
python ../eval/agent_quality/run.py                          # all baselines
python ../eval/agent_quality/run.py --case abound_render_approval  # specific case
python ../eval/agent_quality/run.py --compare                # direct vs workflow
```

**Tool Parity Eval** (`eval/tool_parity/`) — compares Python vs TypeScript tool output.

```bash
python ../eval/tool_parity/run.py --generate-baseline
python ../eval/tool_parity/run.py --compare
```

---

<a name="中文"></a>
## 中文

Conductor 后端基于 FastAPI，提供实时聊天、**智能代码分析**（LLM 驱动的 Agent Loop + 42 个工具 + Token 预算控制器 + 四层 Prompt）、**配置驱动的多 Agent 工作流引擎**（YAML + Markdown Agent 定义，Langfuse 可观测性）、Git 工作区管理、文件共享、Jira 集成（OAuth 3LO + 5 个 Agent 工具）、PostgreSQL 持久化（Liquibase 管理表结构），以及多 Provider AI 集成（Bedrock / Anthropic / OpenAI）。

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
| POST | `/api/context/query/stream` | LLM Brain Orchestrator — SSE 流，实时推送工具调用 + 推理事件（支持 `code_context` 代码段查询） |
| GET | `/api/code-tools/available` | 列出所有可用代码工具 |
| POST | `/api/code-tools/execute/{tool_name}` | 直接执行单个代码工具 |

**Python CLI（供 Extension 本地模式调用）：**

```bash
# 列出所有工具
python -m app.code_tools list

# 执行工具（JSON 格式输出）
python -m app.code_tools grep /path/to/workspace '{"pattern": "authenticate"}'
python -m app.code_tools compressed_view /path/to/workspace '{"path": "src/auth.py"}'
```

Extension 的本地工具调度器通过此 CLI 以子进程方式执行工具（全部 42 个工具均可通过此 CLI 访问）。

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

`POST /api/context/query/stream` 运行 **Brain Orchestrator**（强模型），通过 `dispatch_explore` / `dispatch_swarm` / `transfer_to_brain` 调度专精子 Agent 并最终合成答案。每个子 Agent 跑独立的 LLM 循环（最多 25 轮迭代，800K token 预算）。注入四层 System Prompt（遵循 Anthropic 提示词设计规范：目标导向，非逐步脚本）：身份（来自 `config/agents/*.md` 的 Agent 人格）+ 工具（按 Agent 精选）+ 技能与指南（工作区上下文、调查方法论）+ 用户消息（查询 + 代码上下文）。核心原则：告诉模型要达成什么目标，而非如何逐步执行——Claude 的自主推理优于手写的探索策略。BudgetController 发出 NORMAL / WARN_CONVERGE / FORCE_CONCLUDE 信号。EvidenceEvaluator 在最终输出前拒绝证据不足的答案。Session Trace 以 JSON 保存供离线分析。

**42 个工具**（代码 + 文件编辑 + Jira + 浏览器）：

| 工具 | 说明 |
|------|------|
| `grep` | 正则搜索（ripgrep） |
| `read_file` | 读取文件（可指定行范围） |
| `list_files` | 目录树（支持深度/glob 过滤） |
| `find_symbol` | AST 符号定义搜索（tree-sitter），含角色分类 |
| `find_references` | 符号用法搜索（grep + AST 验证） |
| `file_outline` | 列出文件中所有定义及行号 |
| `get_dependencies` | 该文件导入的文件列表 |
| `get_dependents` | 导入该文件的文件列表 |
| `git_log` | 最近 commit（可按文件过滤）；`search=` 参数按 commit 消息搜索 |
| `git_diff` | 两个 git ref 之间的 diff |
| `ast_search` | 结构化 AST 搜索，via ast-grep（`$VAR`、`$$$MULTI` 模式） |
| `get_callees` | 函数体内调用的函数列表 |
| `get_callers` | 跨文件调用该函数的调用方列表 |
| `git_blame` | 每行代码的作者信息（commit hash、作者、日期） |
| `git_show` | 完整 commit 详情（消息 + diff）；可查看变更前的文件 |
| `find_tests` | 查找覆盖指定函数/类的测试函数 |
| `test_outline` | 测试文件结构（mock、断言、fixture） |
| `trace_variable` | 数据流追踪：别名检测、参数传递映射、source/sink 识别 |
| `compressed_view` | 文件签名 + 调用关系 + 副作用（节省约 80% token） |
| `module_summary` | 模块级摘要：服务、模型、函数、文件列表（节省约 95% token） |
| `expand_symbol` | 将压缩视图中的符号展开为完整源码 |
| `run_test` | 执行测试文件或指定测试函数；返回通过/失败 + 输出（可选验证步骤） |
| `glob` | 快速文件模式匹配（如 `**/*.ts`） |
| `detect_patterns` | 架构模式检测（单例、工厂、观察者等） |
| `git_diff_files` | 列出两个 git ref 之间变更的文件 |
| `git_hotspots` | 最近变更热点文件（变更次数 × 作者数） |
| `list_endpoints` | 提取 API 路由定义（Flask、FastAPI、Express 等） |
| `extract_docstrings` | 提取模块中函数/类的文档字符串 |
| `db_schema` | 数据库 Schema 内省（SQLAlchemy 模型） |
| `file_edit` | 部分文件编辑（搜索替换，需先 read_file） |
| `file_write` | 完整文件写入/创建（已存在文件需先 read_file） |
| `jira_search` | 搜索 Jira Issue（JQL 或自由文本；快捷方式：my tickets / my sprint / blockers） |
| `jira_get_issue` | 获取 Jira Issue 完整详情（描述、评论、子任务） |
| `jira_create_issue` | 创建 Jira Ticket（ADF 描述、代码块、parent_key 创建子任务） |
| `jira_update_issue` | 更新 Jira Issue（状态转换、评论、字段、标签；Done/Closed/Resolved 被阻止） |
| `jira_list_projects` | 列出可访问的 Jira 项目 |
| `web_search` | 网页搜索（Playwright） |
| `web_navigate` | 无头浏览器导航到 URL |
| `web_click` | 点击页面元素 |
| `web_fill` | 填写表单字段 |
| `web_screenshot` | 截取页面截图 |
| `web_extract` | 提取页面内容 |

### AI Provider 说明

- Provider 密钥配置在 `config/conductor.secrets.yaml`，设置在 `config/conductor.settings.yaml`。
- `ProviderResolver` 在启动时对所有已配置的 Provider 做健康检查，选取响应最快的一个。
- 三个 Provider 均支持原生 Tool Use（`chat_with_tools`）：Bedrock Converse API、Anthropic Messages API、OpenAI Chat Completions。

### 存储

所有 OLTP 数据存储在 **PostgreSQL**（与 Langfuse 共享实例）：

| 表名 | 描述 |
|---|---|
| `repo_tokens` | Git 工作区 PAT 缓存 |
| `session_traces` | Agent Loop 会话追踪（JSON） |
| `audit_logs` | 变更审计日志 |
| `file_metadata` | 上传文件元数据 |
| `todos` | 房间级任务跟踪 |
| `integration_tokens` | 外部集成 OAuth 令牌（Jira 等） |

其他存储：
- 文件上传：`uploads/{room_id}/`
- Git 工作区：`workspaces/{room_id}/`（裸克隆 + worktree）
- 聊天房间状态：进程内存

> 表结构由 **Liquibase** 管理（`database/changelog/`）。启动后运行 `make db-update`。
> Langfuse 内部自动管理自己的表（Prisma migrations）。

### Docker 网络

所有 Docker Compose 文件共享 `conductor-net` 网络，服务间通过容器名通信：

- `conductor-postgres:5432` — Postgres（后端 + Langfuse）
- `conductor-redis:6379` — Redis（后端）

避免 WSL2 / Linux Docker 环境下 `host.docker.internal` 解析失败的问题。

### 测试

共 **1200+ 个测试**。

```bash
cd backend
python -m pytest tests/ -v
python -m pytest tests/ -q
pytest --cov=. --cov-report=html   # 覆盖率报告
```

智能代码分析模块主要测试文件：

| 文件 | 测试数 | 覆盖内容 |
|------|--------|----------|
| `test_code_tools.py` | 139 | 全部 42 个工具 + 调度器 + 多语言 |
| `test_agent_loop.py` | 55 | Agent Loop + 四层 Prompt + 工作区布局 + 完整性检查 |
| `test_budget_controller.py` | 20 | Token 预算信号、追踪、边界情况 |
| `test_session_trace.py` | 15 | SessionTrace JSON 保存/加载 |
| `test_evidence.py` | 14 | 证据评估器质量门控 |
| `test_symbol_role.py` | 24 | 符号角色分类 + 装饰器检测 |
| `test_output_policy.py` | 19 | 每工具截断策略、预算自适应 |
| `test_compressed_tools.py` | 24 | compressed_view、module_summary、expand_symbol |
| `test_langextract.py` | 57 | Bedrock Provider、Catalog、Service、Router |
| `test_repo_graph.py` | 72 | Parser + 依赖图 + PageRank + Service |
| `test_config_new.py` | 27 | Config + Secrets |

### 评估系统

`eval/` 目录（通过 `.dockerignore` 排除在 Docker 镜像之外）包含三套独立评估套件，详见 `eval/README.md`。

**代码评审评估**（`eval/code_review/`）— 衡量 `PRBrainOrchestrator`（v2 coordinator-worker 设计）质量，基于 4 个 suite 共 42 个植入 bug 用例（requests + greptile-sentry + greptile-grafana + greptile-keycloak）。

```bash
cd backend
python ../eval/code_review/run.py --provider anthropic --model claude-sonnet-4-20250514
python ../eval/code_review/run.py --filter "requests-001" --no-judge
python ../eval/code_review/run.py --save-baseline
python ../eval/code_review/run.py --gold --gold-model opus --save-baseline
```

**Agent 质量评估**（`eval/agent_quality/`）— 衡量 Agent Loop 答案质量，与基线对比。

```bash
python ../eval/agent_quality/run.py
python ../eval/agent_quality/run.py --case abound_render_approval
python ../eval/agent_quality/run.py --compare
```

**工具一致性评估**（`eval/tool_parity/`）— 对比 Python 和 TypeScript 工具输出。

```bash
python ../eval/tool_parity/run.py --generate-baseline
python ../eval/tool_parity/run.py --compare
```
