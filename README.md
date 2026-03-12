# Conductor

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor is a VS Code collaboration extension + FastAPI backend for team chat, Git workspace sharing, file sharing, and **agentic AI code intelligence**.

### Features

- **Agentic Code Intelligence** — LLM agent loop (up to 25 iterations, 500K token budget) that iteratively navigates the codebase using 21 code tools (grep, AST search, call graph, git log, data flow tracing, compressed views, ...) to answer questions. No pre-built vector index needed.
- **3-Layer System Prompt** — Core Identity + per-query-type Strategy + dynamic Runtime Guidance. Query Classifier categorises queries into 7 types and selects the optimal 8-12 tool subset, reducing token waste.
- **Token-Based Budget Controller** — tracks cumulative input/output tokens; emits NORMAL → WARN_CONVERGE → FORCE_CONCLUDE signals. LLM sees budget context each turn.
- **Evidence Evaluator** — rule-based quality gate before finalising answers: requires file:line references, ≥2 tool calls, ≥1 file accessed. Rejects weak answers if budget remains.
- **Session Trace** — per-session JSON trace (LLM latency, tool latencies, token breakdown, budget signals) saved for offline analysis.
- **Git Workspace Management** — per-room bare repo + worktree isolation. Files appear in VS Code explorer via a `conductor://` URI scheme (FileSystemProvider).
- **Real-time Chat** — WebSocket rooms with typing indicators, read receipts, reconnect recovery, and AI message injection.
- **File Sharing** — multipart upload with SHA-256 deduplication, DuckDB-backed metadata.
- **Audit & Todos** — DuckDB-persisted audit log (AI change apply/skip events) and room-scoped TODO tracker.
- **Multi-Provider AI** — Bedrock Converse, Anthropic Direct, OpenAI. ProviderResolver health-checks all configured providers at startup and picks the fastest. All 3 providers implement `chat_with_tools()`.
- **LangExtract Integration** — multi-vendor Bedrock language model plugin for Google's langextract library. `BedrockCatalog` dynamically discovers all available Bedrock models (Claude, Amazon Nova, Llama, Mistral, DeepSeek, Qwen) at startup.

### Architecture

```
┌──────────────────────────┐     ┌──────────────────────────────────────────┐
│   VS Code Extension      │     │   FastAPI Backend                        │
│                          │     │                                          │
│  ┌──────────────────┐    │ WS  │  ┌───────────────────────────────────┐  │
│  │ SessionFSM       │    │◄────┼──│ WebSocket Manager (rooms/broadcast)│  │
│  │ WebSocketService  │    │     │  └───────────────────────────────────┘  │
│  │ CollabPanel       │    │     │                                          │
│  └──────────────────┘    │     │  ┌───────────────────────────────────┐  │
│                          │     │  │ Agent Loop Service                 │  │
│  ┌──────────────────┐    │HTTP │  │  QueryClassifier → 3-layer prompt │  │
│  │ WorkspaceClient   │◄──┼─────┼──│  LLM ←→ 21 Code Tools (dynamic  │  │
│  │ WorkspacePanel    │    │     │  │  subset) → BudgetController       │  │
│  │ FileSystemProvider│    │     │  │  → EvidenceEvaluator → SSE stream │  │
│  └──────────────────┘    │     │  └───────────────────────────────────┘  │
│                          │     │                                          │
│                          │     │  ┌───────────────────────────────────┐  │
│                          │     │  │ AI Provider Layer                  │  │
│                          │     │  │  ProviderResolver → health check  │  │
│                          │     │  │  ├─ ClaudeBedrockProvider         │  │
│                          │     │  │  ├─ ClaudeDirectProvider          │  │
│                          │     │  │  └─ OpenAIProvider                │  │
│                          │     │  └───────────────────────────────────┘  │
│                          │     │                                          │
│                          │     │  ┌───────────────────────────────────┐  │
│                          │     │  │ Git Workspace Service              │  │
│                          │     │  │  bare clone → worktree per room   │  │
│                          │     │  └───────────────────────────────────┘  │
│                          │     │                                          │
│                          │     │  ┌───────────────────────────────────┐  │
│                          │     │  │ DuckDB Storage                    │  │
│                          │     │  │  audit_logs / todos / file meta   │  │
│                          │     │  └───────────────────────────────────┘  │
└──────────────────────────┘     └──────────────────────────────────────────┘
```

### 21 Code Tools

The agent selects an optimal 8-12 tool subset per query type (reducing hallucinated calls and token waste):

| Tool | Description |
|------|-------------|
| `grep` | Regex search (ripgrep) |
| `read_file` | Read file content with line range |
| `list_files` | Directory tree |
| `find_symbol` | AST-based symbol definition (with role classification) |
| `find_references` | All usages of a symbol |
| `file_outline` | All definitions in a file |
| `get_dependencies` | Files this file imports |
| `get_dependents` | Files that import this file |
| `git_log` | Recent commits |
| `git_diff` | Diff between refs |
| `ast_search` | Structural AST search (ast-grep, `$VAR`/`$$$MULTI` patterns) |
| `get_callees` | Functions called within a function |
| `get_callers` | Functions that call a given function (cross-file) |
| `git_blame` | Per-line authorship with commit hash, author, date |
| `git_show` | Full commit details (message + diff) |
| `find_tests` | Test functions covering a given function/class |
| `test_outline` | Test file structure with mocks, assertions, fixtures |
| `trace_variable` | Data flow tracing: alias detection, arg→param mapping, sink/source patterns |
| `compressed_view` | File signatures + call relationships + side effects (~80% token savings) |
| `module_summary` | Module-level summary: services, models, functions, file list (~95% savings) |
| `expand_symbol` | Expand a symbol from compressed view to full source code |

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
pytest                                        # all tests (900+)
pytest tests/test_code_tools.py -v            # 21 code tools (98 tests)
pytest tests/test_agent_loop.py -v            # agent loop + 3-layer prompt (39 tests)
pytest tests/test_budget_controller.py -v     # token budget controller (20 tests)
pytest tests/test_session_trace.py -v         # session trace (15 tests)
pytest tests/test_evidence.py -v              # evidence evaluator (14 tests)
pytest tests/test_symbol_role.py -v           # symbol role classification (24 tests)
pytest tests/test_output_policy.py -v         # per-tool output policies (19 tests)
pytest tests/test_query_classifier.py -v      # query classifier (26 tests)
pytest tests/test_compressed_tools.py -v      # compressed view tools (24 tests)
pytest tests/test_langextract.py -v           # langextract multi-vendor (57 tests)
pytest tests/test_repo_graph.py -v            # repo graph (72 tests)
pytest tests/test_config_new.py -v            # config (27 tests)
pytest tests/test_git_workspace.py -v         # git workspace
pytest --cov=. --cov-report=html              # coverage report
```

### Documentation

- [Backend Guide](docs/GUIDE.md) — code walkthrough (EN + 中文)
- [Roadmap](ROADMAP.md) — project phases and ADRs
- [Claude](CLAUDE.md) — guide for AI coding assistants

---

<a name="中文"></a>
## 中文

Conductor 是一个 VS Code 协作扩展 + FastAPI 后端，用于团队聊天、Git 工作区共享、文件共享和 **Agentic AI 代码智能分析**。

### 功能特性

- **Agentic 代码智能** — LLM agent loop（最多 25 轮迭代，50 万 token 预算），通过迭代调用 21 个代码工具（grep、AST 搜索、调用图、git log、数据流追踪、压缩视图等）主动探索代码库，无需预建向量索引。
- **三层系统提示** — 核心身份 + 按查询类型选择的策略层 + 动态运行时引导。QueryClassifier 将查询分为 7 种类型，并为每种类型选择最优的 8-12 个工具子集，减少 token 浪费。
- **基于 Token 的预算控制器** — 跟踪累计输入/输出 token；发出 NORMAL → WARN_CONVERGE → FORCE_CONCLUDE 信号。LLM 每轮都能看到预算上下文。
- **证据评估器** — 答案最终确认前的规则质检：要求包含文件:行号引用、≥2 次工具调用、≥1 个已访问文件。预算充足时拒绝低质量答案。
- **会话追踪** — 每个会话生成 JSON 追踪文件（LLM 延迟、工具延迟、token 分布、预算信号），供离线分析使用。
- **Git 工作区管理** — 每个房间独立的裸仓库 + worktree 隔离。文件通过 `conductor://` URI 方案（FileSystemProvider）出现在 VS Code 文件管理器中。
- **实时聊天** — WebSocket 房间，支持打字指示、已读回执、断线重连和 AI 消息注入。
- **文件共享** — 多部分上传，SHA-256 去重，DuckDB 元数据存储。
- **审计与任务追踪** — DuckDB 持久化审计日志（AI 变更接受/跳过事件）和房间级 TODO 追踪器。
- **多提供商 AI** — Bedrock Converse、Anthropic Direct、OpenAI。`ProviderResolver` 在启动时对所有已配置的提供商做健康检查，自动选择最快的。三个提供商均实现 `chat_with_tools()`。
- **LangExtract 集成** — 多厂商 Bedrock 语言模型插件，支持 Google langextract 库。`BedrockCatalog` 在启动时动态发现所有可用的 Bedrock 模型（Claude、Amazon Nova、Llama、Mistral、DeepSeek、Qwen）。

### 架构

架构图见上方英文部分。

### 快速开始

```bash
# 后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload

# 扩展
cd extension
npm install
npm run compile
# 在 VS Code 中按 F5 启动扩展开发主机
```

### 运行测试

```bash
cd backend
pytest                                        # 所有测试 (900+)
pytest tests/test_code_tools.py -v            # 21 个代码工具 (98 项)
pytest tests/test_agent_loop.py -v            # agent loop + 三层提示 (39 项)
pytest tests/test_budget_controller.py -v     # token 预算控制器 (20 项)
pytest tests/test_session_trace.py -v         # 会话追踪 (15 项)
pytest tests/test_evidence.py -v              # 证据评估器 (14 项)
pytest tests/test_symbol_role.py -v           # 符号角色分类 (24 项)
pytest tests/test_output_policy.py -v         # 工具输出策略 (19 项)
pytest tests/test_query_classifier.py -v      # 查询分类器 (26 项)
pytest tests/test_compressed_tools.py -v      # 压缩视图工具 (24 项)
pytest tests/test_langextract.py -v           # LangExtract 多厂商 (57 项)
pytest tests/test_repo_graph.py -v            # 仓库图 (72 项)
pytest tests/test_config_new.py -v            # 配置 (27 项)
```

### 配置

在 `backend/config/conductor.secrets.yaml` 中配置凭证：

```yaml
aws:
  access_key_id: "AKIA..."
  secret_access_key: "..."
  region: "us-east-1"
openai:
  api_key: "sk-..."
anthropic:
  api_key: "sk-ant-..."
```

非敏感配置在 `backend/config/conductor.settings.yaml` 中。
