# Backend API

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor backend is a FastAPI application providing real-time chat, agentic code intelligence (LLM agent loop + 24 code tools + token budget controller + 3-layer prompts), a config-driven multi-agent workflow engine (YAML + Markdown agent definitions, Langfuse observability), Git workspace management, file sharing, DuckDB-backed audit logs and TODOs, and multi-provider AI (Bedrock / Anthropic / OpenAI).

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
| POST | `/api/context/query` | LLM agent loop — iteratively calls 24 code tools (up to 25 iterations, 500K token budget) |
| POST | `/api/context/query/stream` | SSE streaming — real-time tool call progress events |
| POST | `/api/context/explain-rich` | Deep code explanation via agent (replaces XML-prompt pipeline) |
| POST | `/api/context/explain-rich/stream` | SSE streaming for explain-rich |
| GET | `/api/code-tools/available` | List all available code tools |
| POST | `/api/code-tools/execute/{tool_name}` | Directly execute a single code tool |

#### Workflow Engine (`/api/workflows/`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/workflows` | List available workflows (name, description, route_mode, agent count) |
| GET | `/api/workflows/{name}` | Full workflow config (agents, routes, pipeline, classifier) |
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

### Agentic Code Intelligence

`POST /api/context/query` runs an LLM agent loop (up to **25 iterations**, **500K token budget**). The Query Classifier categorises the query into one of 7 types, selects an optimal 8-12 tool subset, and injects a 3-layer system prompt (Core Identity + Strategy + Runtime Guidance). A token-based Budget Controller emits NORMAL / WARN_CONVERGE / FORCE_CONCLUDE signals. An Evidence Evaluator rejects weak answers before finalising. Session Traces are saved as JSON for offline analysis.

**24 code tools:**

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
| `tests/test_code_tools.py` | 98 | All 24 code tools + dispatcher + multi-language |
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

### Code Review Eval

A standalone eval system in `eval/` (excluded from Docker) measures `CodeReviewService` quality against known bugs planted in real open-source repos. 12 cases against requests v2.31.0 (4 easy, 5 medium, 3 hard).

```bash
# Pipeline mode: run CodeReviewService against all 12 cases
python eval/run.py --provider anthropic --model claude-sonnet-4-20250514

# Single case
python eval/run.py --filter "requests-001"

# Deterministic only (no LLM judge)
python eval/run.py --no-judge

# Save baseline for regression detection
python eval/run.py --save-baseline

# Gold-standard mode: invoke Claude Code CLI directly (quality ceiling)
python eval/run.py --gold --save-baseline
python eval/run.py --gold --gold-model opus --gold-max-budget 5.0

# Compare pipeline run against gold baseline
python eval/run.py --compare-gold
```

**Two baseline types:**
- **Pipeline baseline** (`eval/baselines/`) — your own previous runs; detect regressions
- **Gold baseline** (`eval/gold_baselines/`) — Claude Code CLI (Opus) direct run; quality ceiling

Gold runner invokes `claude -p --output-format stream-json --dangerously-skip-permissions`, strips `ANTHROPIC_API_KEY` (uses subscription), and saves full `GoldTrace` (tool calls, files read, cost) to `eval/gold_traces/`.

Scoring: recall (35%), precision (20%), severity (15%), location (10%), recommendation (10%), context (10%). Optional LLM-as-Judge evaluates completeness, reasoning, actionability, and false positive quality (1-5 scale). Regressions flagged at >10% composite drop; CLI exits with code 1 for CI integration.

**Adding a new repo:** clone at a specific version → remove `.git` → add to `eval/repos.yaml` → create `eval/cases/<repo>/cases.yaml` and patches.

See `docs/GUIDE.md` section 11 for full documentation.

---

<a name="中文"></a>
## 中文

Conductor 后端基于 FastAPI，提供实时聊天、**智能代码分析**（LLM 驱动的 Agent Loop + 24 个代码工具 + Token 预算控制器 + 三层 Prompt）、**配置驱动的多 Agent 工作流引擎**（YAML + Markdown Agent 定义，Langfuse 可观测性）、Git 工作区管理、文件共享、DuckDB 审计日志与 TODO 管理，以及多 Provider AI 集成（Bedrock / Anthropic / OpenAI）。

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
| POST | `/api/context/query` | LLM Agent Loop — 迭代调用 24 个代码工具回答代码查询（最多 25 轮 / 500K token） |
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

`POST /api/context/query` 运行 LLM Agent Loop（最多 25 轮迭代，500K token 预算）。QueryClassifier 将查询分类为 7 种类型，选出最优 8-12 工具子集，注入三层 System Prompt（核心身份 + 策略 + 运行时指导）。BudgetController 发出 NORMAL / WARN_CONVERGE / FORCE_CONCLUDE 信号。EvidenceEvaluator 在最终输出前拒绝证据不足的答案。Session Trace 以 JSON 保存供离线分析。

**24 个代码工具：**

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

共 **900+ 个测试**。

```bash
cd backend
python -m pytest tests/ -v
python -m pytest tests/ -q
pytest --cov=. --cov-report=html   # 覆盖率报告
```

智能代码分析模块主要测试文件：

| 文件 | 测试数 | 覆盖内容 |
|------|--------|----------|
| `test_code_tools.py` | 98 | 全部 24 个工具 + 调度器 + 多语言 |
| `test_agent_loop.py` | 39 | Agent Loop + 三层 Prompt + 工作区布局 |
| `test_budget_controller.py` | 20 | Token 预算信号、追踪、边界情况 |
| `test_session_trace.py` | 15 | SessionTrace JSON 保存/加载 |
| `test_evidence.py` | 14 | 证据评估器质量门控 |
| `test_symbol_role.py` | 24 | 符号角色分类 + 装饰器检测 |
| `test_output_policy.py` | 19 | 每工具截断策略、预算自适应 |
| `test_query_classifier.py` | 26 | 关键词 + LLM 分类、动态工具集 |
| `test_compressed_tools.py` | 24 | compressed_view、module_summary、expand_symbol |
| `test_langextract.py` | 57 | Bedrock Provider、Catalog、Service、Router |
| `test_repo_graph.py` | 72 | Parser + 依赖图 + PageRank + Service |
| `test_config_new.py` | 27 | Config + Secrets |

### 代码评审评估

独立的 `eval/` 目录（通过 `.dockerignore` 排除在 Docker 镜像之外）提供 `CodeReviewService` 质量评估系统。在真实开源仓库中植入已知 bug，衡量评审质量。目前有 12 个用例（基于 requests v2.31.0）。

```bash
# Pipeline 模式：运行我们的 CodeReviewService
python eval/run.py --provider anthropic --model claude-sonnet-4-20250514

# 单个用例
python eval/run.py --filter "requests-001"

# 仅确定性评分（不使用 LLM Judge）
python eval/run.py --no-judge

# 保存 Pipeline 基线用于回归检测
python eval/run.py --save-baseline

# Gold 模式：直接调用 Claude Code CLI（质量天花板基线）
python eval/run.py --gold --save-baseline
python eval/run.py --gold --gold-model opus --gold-max-budget 5.0

# 将 Pipeline 结果与 Gold 基线对比
python eval/run.py --compare-gold
```

**两种基线：**
- **Pipeline 基线**（`eval/baselines/`）— 自身上次的运行结果，用于检测回归
- **Gold 基线**（`eval/gold_baselines/`）— Claude Code CLI（Opus）直接运行的结果，代表质量天花板

Gold Runner 直接调用 `claude -p --output-format stream-json --dangerously-skip-permissions`，自动去除 `ANTHROPIC_API_KEY`（使用月费订阅而非 API 额度），并将完整调查轨迹（工具调用、读取文件、费用等）保存至 `eval/gold_traces/`。

评分维度：召回率 (35%)、精确率 (20%)、严重程度准确性 (15%)、定位准确性 (10%)、修复建议 (10%)、上下文深度 (10%)。可选 LLM Judge 评估完整性、推理质量、可操作性和误报质量（1-5 分）。综合分数下降 >10% 触发回归警告，CLI 返回退出码 1 便于 CI 集成。

**添加新仓库：** 克隆指定版本 → 删除 `.git` → 添加到 `eval/repos.yaml` → 创建 `eval/cases/<repo>/cases.yaml` 和补丁文件。

详见 `docs/GUIDE.md` 第 11 节。
